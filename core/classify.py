"""OpenRouter enrichment with strict local validation and content-based caching."""

import hashlib
import json
import math
from pathlib import Path

import requests

from config import (MARKUP_DIR, OPENROUTER_API_KEY, OPENROUTER_BASE_URL, CLASSIFIER_MODEL,
                    CLASSIFIER_TIMEOUT_SECONDS, CLASSIFIER_MAX_OUTPUT_TOKENS,
                    CLASSIFIER_MAX_INPUT_CHARS, TELEGRAM_CHANNELS_JSON)


# Persisted error prefix for cycles held back by Enricher.capacity_problem.
CAPACITY_PREFIX = "Cycle too large for one request: "


class EnrichmentError(RuntimeError):
    """An unavailable provider or an invalid model response; safe to retry."""

    def __init__(self, message: str, status_code: int | None = None, usage: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.usage = usage or {}


class OutputValidationError(EnrichmentError):
    """A completed response needs correction, rather than provider backoff."""


class Enricher:
    def __init__(self):
        folder = Path(MARKUP_DIR) / "enrichment"
        self.taxonomy = json.loads((folder / "taxonomy.json").read_text())
        self.schema = json.loads((folder / "schema.json").read_text())
        self.result_schema = self.schema["properties"]["jobs"]["items"]
        self.prompt = (folder / "prompt.txt").read_text() + "\nCanonical taxonomy:\n" + json.dumps(self.taxonomy, ensure_ascii=False)
        self.version = hashlib.sha256((self.prompt + json.dumps(self.schema, sort_keys=True)).encode()).hexdigest()[:16]
        self.session = requests.Session()
        self.pricing = None
        self.max_completion_tokens = None
        self.context_length = None
        self._minimum_result_tokens = None

    def close(self):
        self.session.close()

    def _discard_pooled_connections(self):
        """Drop keep-alive sockets after a connection-level failure.

        Host suspend/resume (laptop travel) strands dead sockets in the pool;
        the next request would reuse one and fail again two minutes later.
        Closing the session empties the adapters' pools; new connections are
        created lazily, so the same session stays usable afterwards.
        """
        self.session.close()

    def prepare(self, job: dict) -> tuple[dict, str]:
        extra = job.get("extra") or {}
        if isinstance(extra, str):
            extra = json.loads(extra)
        # Recruiter biographies, tracking metadata, URLs and scraped timestamps
        # do not establish requirements and need not be sent to the model.
        if not isinstance(extra, dict):
            raise EnrichmentError("Source extra must be a JSON object")
        if type(job.get("id")) is not int or job["id"] < 1:
            raise EnrichmentError("Job id must be a positive integer")
        fields = ("location", "detail_location", "workplace", "job_type", "job_types",
                  "work_types", "career_level", "experience_years", "education_level",
                  "salary", "salary_details", "requirements", "keywords", "work_roles",
                  "company_industry", "company_description", "description_truncated",
                  "benefits", "shift_and_schedule", "snippet", "detail_status")
        data = {"source": job.get("source"), "title": job.get("title"), "company": job.get("company"),
                "description": job.get("description") or "",
                "extra": {k: extra[k] for k in fields if k in extra}}
        # Preserve full relevant input by default. An explicit operator cap
        # keeps both ends and marks the cut; raw SQLite data is never altered.
        full_input = json.dumps(data, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256((CLASSIFIER_MODEL + self.version + full_input).encode()).hexdigest()
        if CLASSIFIER_MAX_INPUT_CHARS and len(full_input) > CLASSIFIER_MAX_INPUT_CHARS:
            allowance = max(0, CLASSIFIER_MAX_INPUT_CHARS - len(json.dumps(data["extra"], ensure_ascii=False)) - 2000)
            text = data["description"]
            head = allowance * 2 // 3
            tail = allowance - head
            data["description"] = text[:head] + "\n[Input truncated]\n" + (text[-tail:] if tail else "")
            data["input_truncated"] = True
            if len(json.dumps(data, ensure_ascii=False)) > CLASSIFIER_MAX_INPUT_CHARS:
                raise EnrichmentError("Source metadata exceeds classifier input limit")
        data["id"] = job["id"]
        return data, digest

    def payload(self, data: dict | list[dict], feedback: str = "") -> dict:
        jobs = [data] if isinstance(data, dict) else data
        if not jobs:
            raise EnrichmentError("An enrichment request must contain jobs")
        output_tokens = CLASSIFIER_MAX_OUTPUT_TOKENS * len(jobs)
        if self.max_completion_tokens:
            output_tokens = min(output_tokens, self.max_completion_tokens)
        messages = [{"role": "system", "content": self.prompt},
                    {"role": "user", "content": json.dumps({"jobs": jobs}, ensure_ascii=False)}]
        if feedback:
            messages.append({"role": "system", "content":
                "The previous whole-batch response failed local validation: " + feedback +
                "\nCorrect this issue and recheck every result against the schema and taxonomy. "
                "Return all input job IDs exactly once. Do not omit jobs or invent missing evidence."})
        if self.context_length:
            # Providers reject prompt + max_tokens beyond the context window.
            # The optimistic input estimate never lowers a request that the
            # provider would otherwise have accepted.
            output_tokens = max(1, min(output_tokens, self.context_length - _optimistic_tokens(messages)))
        return {"model": CLASSIFIER_MODEL,
                "messages": messages,
                "max_tokens": output_tokens,
                "provider": {"require_parameters": True},
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "rtjobs_enrichment", "strict": True, "schema": self.schema}}}

    def request_bound(self, data: dict | list[dict], feedback: str = "") -> float:
        """Conservative per-request reservation, using advertised model prices.

        UTF-8 bytes bound input tokens; doubling the estimate leaves room for
        framing/cache pricing. Failed requests retain their reservation because
        a timeout does not prove the provider did not perform billable work.
        """
        if self.pricing is None:
            try:
                response = self.session.get(OPENROUTER_BASE_URL + "/models", timeout=CLASSIFIER_TIMEOUT_SECONDS)
            except requests.ConnectionError:
                self._discard_pooled_connections()
                raise
            response.raise_for_status()
            model = next((m for m in response.json()["data"] if m["id"] == CLASSIFIER_MODEL), None)
            if not model:
                raise EnrichmentError(f"Unknown OpenRouter model: {CLASSIFIER_MODEL}")
            self.max_completion_tokens = (model.get("top_provider") or {}).get("max_completion_tokens")
            self.context_length = model.get("context_length")
            self.pricing = {k: float(model["pricing"].get(k) or 0) for k in ("prompt", "completion", "request")}
            # Long-context tiers can cost more. Reserve against every advertised
            # tier rather than rejecting a whole cycle at the base-price ceiling.
            for tier in model['pricing'].get('overrides', []):
                for key in self.pricing:
                    if tier.get(key) is not None:
                        self.pricing[key] = max(self.pricing[key], float(tier[key]))
            if any(not math.isfinite(v) or v < 0 for v in self.pricing.values()):
                raise EnrichmentError("Invalid provider pricing")
        payload = self.payload(data, feedback)
        input_bytes = len(json.dumps(payload, ensure_ascii=False).encode()) + 1024
        return 2 * (input_bytes * self.pricing["prompt"] +
                    payload["max_tokens"] * self.pricing["completion"] + self.pricing["request"])

    def capacity_problem(self, data: list[dict], bound: float, budget: float) -> str:
        """Why this whole batch can never be sent as one request, or "".

        Estimates are deliberately optimistic, so only a cycle that cannot fit
        even then is held back; a borderline cycle is still attempted. Holding
        back costs nothing, while sending it would pay for a guaranteed failure
        on every retry. Call after request_bound (it loads the model limits).
        """
        prefix = CAPACITY_PREFIX
        if bound > budget:
            return prefix + f"reserved cost ${bound:.2f} exceeds the ${budget:.2f} daily budget"
        minimum_output = len(data) * self.minimum_result_tokens()
        if self.max_completion_tokens and minimum_output > self.max_completion_tokens:
            return prefix + (f"{len(data)} jobs need at least ~{minimum_output:,} output tokens; "
                             f"the model allows {self.max_completion_tokens:,}")
        if self.context_length:
            input_tokens = _optimistic_tokens(self.payload(data)["messages"])
            if input_tokens + minimum_output > self.context_length:
                return prefix + (f"~{input_tokens:,} input + ~{minimum_output:,} output tokens exceed "
                                 f"the model's {self.context_length:,}-token context")
        return ""

    def minimum_result_tokens(self) -> int:
        """Lower bound for one valid result: the all-empty schema object, compact."""
        if self._minimum_result_tokens is None:
            self._minimum_result_tokens = _optimistic_tokens(self.fallback(1))
        return self._minimum_result_tokens

    def classify(self, data: dict) -> tuple[dict, dict]:
        """Compatibility helper for an explicitly requested single-job preview."""
        results, usage = self.classify_batch([data])
        return results[0], usage

    def classify_batch(self, data: list[dict], feedback: str = "") -> tuple[list[dict], dict]:
        """One completion request for the complete supplied scrape batch."""
        if not OPENROUTER_API_KEY:
            raise EnrichmentError("OpenRouter API key is missing")
        payload = self.payload(data, feedback)
        if self.pricing:
            # Refuse routes charging above the prices used for the reservation.
            payload["provider"]["max_price"] = {"prompt": self.pricing["prompt"] * 1_000_000,
                                                  "completion": self.pricing["completion"] * 1_000_000}
        try:
            response = self.session.post(OPENROUTER_BASE_URL + "/chat/completions",
                headers={"Authorization": "Bearer " + OPENROUTER_API_KEY},
                json=payload, timeout=CLASSIFIER_TIMEOUT_SECONDS)
        except requests.ConnectionError:
            self._discard_pooled_connections()
            raise
        if not response.ok:
            raise EnrichmentError(f"OpenRouter HTTP {response.status_code}", response.status_code)
        usage = {}
        try:
            envelope = response.json()
            usage = envelope.get("usage") or {}
            choice = envelope["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise OutputValidationError("Invalid enrichment: whole-batch output did not finish normally; no results accepted", usage=usage)
            batch = json.loads(choice["message"]["content"])
            results = self.validate_batch(batch, [job["id"] for job in data])
            return results, usage
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            # JSON decoder errors describe positions; validation errors contain
            # only schema paths, trusted input IDs and canonical enum values.
            raise OutputValidationError(f"Invalid enrichment: {exc}", usage=usage) from exc

    def validate(self, result: dict, job_id: int):
        """Reject invalid output without rewriting evidence or inferred seniority."""
        # Diagnose the exact pair before the schema's anyOf rejects it. Never
        # echo arbitrary model strings into logs or subsequent system messages.
        classification = result.get("classification") if isinstance(result, dict) else None
        if isinstance(classification, dict):
            family, specialization = classification.get("job_family"), classification.get("specialization")
            if isinstance(family, str) and family in self.taxonomy["specializations"]:
                allowed = self.taxonomy["specializations"][family]
                if specialization not in allowed:
                    known = {s for values in self.taxonomy["specializations"].values() for s in values}
                    received = specialization if isinstance(specialization, str) and specialization in known else "<invalid category>"
                    raise ValueError(f"job_id={job_id}: classification.specialization={received} "
                                     f"does not belong to {family}; allowed={','.join(allowed)}")
        _validate(result, self.result_schema, path=f"job_id={job_id}", root=self.schema)
        if result["job_id"] != job_id:
            raise ValueError("Mismatched job_id")
        classification = result["classification"]
        if classification["specialization"] not in self.taxonomy["specializations"][classification["job_family"]]:
            raise ValueError("Specialization does not belong to job family")
        if classification["job_family"] == "other" and classification["routing_confidence"] != "Low":
            raise ValueError("Other requires Low routing confidence")
        if classification["routing_confidence"] == "Low" and not classification["needs_review"]:
            raise ValueError("Low routing confidence requires review")
        for group, prefix in (("requirements", "experience_years"), ("compensation", "salary"),
                              ("explicit_candidate_constraints", "age")):
            lower, upper = (result[group][prefix + suffix] for suffix in ("_min", "_max"))
            if lower is not None and upper is not None and lower > upper:
                raise ValueError(f"Invalid {prefix} range")
        generic = {"communication", "leadership", "negotiation", "teamwork", "customer service",
                   "sales", "reporting", "problem solving", "problem-solving", "business development", "management"}
        if any(tool.strip().casefold() in generic for tool in result["requirements"]["tools_and_technologies"]):
            raise ValueError("Generic competencies are not tools or technologies")

    def validate_batch(self, batch: dict, job_ids: list[int]) -> list[dict]:
        """Check cardinality and identity before accepting any member of a batch."""
        if not isinstance(batch, dict) or set(batch) != {"jobs"} or not isinstance(batch["jobs"], list):
            raise ValueError("Batch must contain exactly a jobs array")
        results = batch["jobs"]
        if any(not isinstance(row, dict) or type(row.get("job_id")) is not int for row in results):
            raise ValueError("Every result must contain an integer job_id")
        ids = [row["job_id"] for row in results]
        if len(set(job_ids)) != len(job_ids) or len(ids) != len(job_ids) or set(ids) != set(job_ids):
            raise ValueError("Missing, duplicate or unexpected job IDs in batch")
        by_id = {row["job_id"]: row for row in results}
        for job_id in job_ids:
            self.validate(by_id[job_id], job_id)
        return [by_id[job_id] for job_id in job_ids]

    def fallback(self, job_id: int) -> dict:
        def empty(schema):
            if "$ref" in schema:
                return empty(self.schema["$defs"][schema["$ref"].removeprefix("#/$defs/")])
            if "anyOf" in schema:
                return empty(schema["anyOf"][0])
            kind = schema["type"]
            if kind == "object":
                return {key: empty(value) for key, value in schema["properties"].items()}
            if kind == "array":
                return []
            if kind == "boolean":
                return False
            return None
        result = empty(self.result_schema)
        result["job_id"] = job_id
        result["classification"].update(job_family="other", specialization="general", routing_confidence="Low",
            needs_review=True, employer_sector=None, posting_entity_type="unknown")
        self.validate(result, job_id)
        return result


def _optimistic_tokens(value) -> int:
    """Low token estimate (~4 UTF-8 bytes per token) for can-never-fit checks.

    Real tokenizers produce more tokens for JSON punctuation and Arabic text,
    so this only rejects batches that are too large under any tokenizer.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(text.encode()) // 4


def _validate(value, schema: dict, path: str = "result", root: dict | None = None):
    """Validate the restricted JSON Schema vocabulary used by our schema."""
    root = schema if root is None else root
    if "$ref" in schema:
        return _validate(value, root["$defs"][schema["$ref"].removeprefix("#/$defs/")], path, root)
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                _validate(value, option, path, root)
                return
            except ValueError:
                pass
        raise ValueError(f"{path}: no allowed schema variant")
    kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    actual = ("null" if value is None else "boolean" if isinstance(value, bool) else
              "integer" if isinstance(value, int) else "number" if isinstance(value, float) else
              "string" if isinstance(value, str) else "array" if isinstance(value, list) else
              "object" if isinstance(value, dict) else "invalid")
    if actual not in kinds and not (actual == "integer" and "number" in kinds):
        raise ValueError(f"{path}: unexpected {actual}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: unknown category")
    if actual == "object":
        if set(value) != set(schema["properties"]):
            raise ValueError(f"{path}: missing or extra fields")
        for key, child_schema in schema["properties"].items():
            _validate(value[key], child_schema, path + "." + key, root)
    elif actual == "array":
        for item in value:
            _validate(item, schema["items"], path + "[]", root)
    elif actual in ("number", "integer"):
        if not math.isfinite(value) or value < schema.get("minimum", -math.inf):
            raise ValueError(f"{path}: invalid number")
    elif actual == "string" and value not in schema.get("enum", []) and value.strip().lower() in ("", "unknown", "n/a", "not specified", "none"):
        raise ValueError(f"{path}: missing value must be null")


def load_channels(require_complete: bool = False) -> dict[str, str]:
    folder = Path(MARKUP_DIR) / "enrichment"
    # Deployment-specific IDs live only in the environment, never in markup.
    channels = json.loads(TELEGRAM_CHANNELS_JSON) if TELEGRAM_CHANNELS_JSON else {}
    families = json.loads((folder / "taxonomy.json").read_text())["job_families"]
    if not isinstance(channels, dict):
        raise ValueError("Telegram channel map must be an object")
    for family, chat in channels.items():
        if family not in families:
            raise ValueError(f"Invalid job-family channel: {family}")
        if not str(chat).startswith("-100") or not str(chat)[1:].isdigit():
            raise ValueError(f"Invalid channel ID for {family}")
    if require_complete and set(channels) != set(families):
        raise ValueError("Missing job-family channels: " + ", ".join(sorted(set(families) - set(channels))))
    if require_complete and len({str(chat) for chat in channels.values()}) != len(channels):
        raise ValueError("Duplicate job-family channel IDs")
    return {key: str(value) for key, value in channels.items()}
