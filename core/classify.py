"""OpenRouter enrichment with strict local validation and content-based caching."""

import hashlib
import json
import math
from pathlib import Path

import requests

from config import (MARKUP_DIR, OPENROUTER_API_KEY, OPENROUTER_BASE_URL, CLASSIFIER_MODEL,
                    CLASSIFIER_TIMEOUT_SECONDS, CLASSIFIER_MAX_OUTPUT_TOKENS,
                    CLASSIFIER_MAX_INPUT_CHARS, TELEGRAM_CHANNELS_JSON)


class EnrichmentError(RuntimeError):
    """An unavailable provider or an invalid model response; safe to retry."""


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

    def close(self):
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
        # Bound the whole model input, not just the main description. Raw SQLite
        # data is retained intact. Keep both ends of long text (requirements are
        # often at the end), explicitly telling the model about the cut.
        full_input = json.dumps(data, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256((CLASSIFIER_MODEL + self.version + full_input).encode()).hexdigest()
        if len(full_input) > CLASSIFIER_MAX_INPUT_CHARS:
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

    def payload(self, data: dict) -> dict:
        return {"model": CLASSIFIER_MODEL,
                "messages": [{"role": "system", "content": self.prompt},
                             {"role": "user", "content": json.dumps({"jobs": [data]}, ensure_ascii=False)}],
                "max_tokens": CLASSIFIER_MAX_OUTPUT_TOKENS,
                "provider": {"require_parameters": True},
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "rtjobs_enrichment", "strict": True, "schema": self.schema}}}

    def request_bound(self, data: dict) -> float:
        """Conservative per-request reservation, using advertised model prices.

        UTF-8 bytes bound input tokens; doubling the estimate leaves room for
        framing/cache pricing. Failed requests retain their reservation because
        a timeout does not prove the provider did not perform billable work.
        """
        if self.pricing is None:
            response = self.session.get(OPENROUTER_BASE_URL + "/models", timeout=CLASSIFIER_TIMEOUT_SECONDS)
            response.raise_for_status()
            model = next((m for m in response.json()["data"] if m["id"] == CLASSIFIER_MODEL), None)
            if not model:
                raise EnrichmentError(f"Unknown OpenRouter model: {CLASSIFIER_MODEL}")
            self.pricing = {k: float(model["pricing"].get(k) or 0) for k in ("prompt", "completion", "request")}
            if any(not math.isfinite(v) or v < 0 for v in self.pricing.values()):
                raise EnrichmentError("Invalid provider pricing")
        input_bytes = len(json.dumps(self.payload(data), ensure_ascii=False).encode()) + 1024
        return 2 * (input_bytes * self.pricing["prompt"] +
                    CLASSIFIER_MAX_OUTPUT_TOKENS * self.pricing["completion"] + self.pricing["request"])

    def classify(self, data: dict) -> tuple[dict, dict]:
        if not OPENROUTER_API_KEY:
            raise EnrichmentError("OpenRouter API key is missing")
        payload = self.payload(data)
        if self.pricing:
            # Refuse routes charging above the prices used for the reservation.
            payload["provider"]["max_price"] = {"prompt": self.pricing["prompt"] * 1_000_000,
                                                  "completion": self.pricing["completion"] * 1_000_000}
        response = self.session.post(OPENROUTER_BASE_URL + "/chat/completions",
            headers={"Authorization": "Bearer " + OPENROUTER_API_KEY},
            json=payload, timeout=CLASSIFIER_TIMEOUT_SECONDS)
        if not response.ok:
            raise EnrichmentError(f"OpenRouter HTTP {response.status_code}")
        try:
            envelope = response.json()
            choice = envelope["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise EnrichmentError("Model output did not finish normally")
            batch = json.loads(choice["message"]["content"])
            results = self.validate_batch(batch, [data["id"]])
            return results[0], envelope.get("usage") or {}
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise EnrichmentError(f"Invalid enrichment: {exc}") from exc

    def validate(self, result: dict, job_id: int):
        """Reject invalid output without rewriting evidence or inferred seniority."""
        _validate(result, self.result_schema)
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
        _validate(batch, self.schema)
        results = batch["jobs"]
        ids = [row["job_id"] for row in results]
        if len(set(job_ids)) != len(job_ids) or len(ids) != len(job_ids) or set(ids) != set(job_ids):
            raise ValueError("Missing, duplicate or unexpected job IDs in batch")
        by_id = {row["job_id"]: row for row in results}
        for job_id in job_ids:
            self.validate(by_id[job_id], job_id)
        return [by_id[job_id] for job_id in job_ids]

    def fallback(self, job_id: int) -> dict:
        def empty(schema):
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


def _validate(value, schema: dict, path: str = "result"):
    """Validate the restricted JSON Schema vocabulary used by our schema."""
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
        for key, child in value.items():
            _validate(child, schema["properties"][key], path + "." + key)
    elif actual == "array":
        for item in value:
            _validate(item, schema["items"], path + "[]")
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
