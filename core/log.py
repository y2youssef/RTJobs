"""Central logging setup — one format, two destinations (stdout + file).

Call :func:`setup_logging` once at startup (``main.py`` does it). All other
modules then just do ``logger = logging.getLogger(__name__)`` and use
``logger.info / warning / error`` instead of ``print``.

Env:
    LOG_LEVEL — debug/info/warning/error (default: info)
    LOG_FILE  — path to log file (default: <DATA_DIR>/logs/rtjobs.log,
                empty to disable file logging)
    DATA_DIR  — base data dir (default: .)
"""

import logging
import logging.handlers
import os
import sys

_configured = False


def setup_logging() -> None:
    """Configure root logging once. Safe to call multiple times (idempotent)."""
    global _configured
    if _configured:
        return
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Handler 1 → stdout (so `docker logs` / `docker logs ofelia` capture it)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    handlers: list[logging.Handler] = [stream_handler]

    # Handler 2 → rotating file under DATA_DIR/logs/rtjobs.log (persisted via
    # scraper_data volume in Docker). Keeps last ~25 MB (5 × 5 MB).
    data_dir = os.environ.get("DATA_DIR", ".")
    default_log = os.path.join(data_dir, "logs", "rtjobs.log")
    log_file = os.environ.get("LOG_FILE", default_log).strip()
    if log_file:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)
        except OSError:
            # If file can't be created (e.g. read-only FS), fall back to stdout only
            pass

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)
    # Silence overly verbose libraries unless explicitly requested
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _configured = True


def get_log_file() -> str | None:
    """Return the active log file path, or None if file logging is disabled."""
    data_dir = os.environ.get("DATA_DIR", ".")
    default_log = os.path.join(data_dir, "logs", "rtjobs.log")
    path = os.environ.get("LOG_FILE", default_log).strip()
    return path or None
