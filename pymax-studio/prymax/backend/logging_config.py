"""
PryMax Studio — structured logging
====================================
JSON-lines logging to stdout (and optionally a file), so log output is
directly parseable by any log aggregator (CloudWatch, Datadog, ELK, or
even just `jq` on the command line) instead of needing regex scraping of
free-text log lines.

Configuration via environment variables:
    PRYMAX_LOG_LEVEL   - DEBUG/INFO/WARNING/ERROR (default INFO)
    PRYMAX_LOG_FILE    - optional path; if set, logs also go to this file
                         via a rotating file handler (5MB x 3 backups)

What gets logged (see app.py's call sites):
    - Every request's method/path/status/duration/client-ip (via an
      after_request hook)
    - Business events worth an audit trail: room joins, host claims,
      breakout start/end, raffle draws — anything a real operator would
      want to search for after the fact
    - Every rejected request (400/403/404/409/413/429) at WARNING level,
      since a spike in these is usually the first sign of a bug or abuse
    - Uncaught exceptions at ERROR level with the traceback

What this is NOT: a metrics/observability platform. There's no log
shipping, no dashboards, no alerting configured here — just structured
output that a real one could ingest. See ../README.md for the honest
limits.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Anything passed via logger.info("msg", extra={"fields": {...}})
        # gets merged in as top-level structured fields.
        fields = getattr(record, "fields", None)
        if fields:
            payload.update(fields)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(logger_name: str = "prymax") -> logging.Logger:
    level_name = os.environ.get("PRYMAX_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    logger.propagate = False

    # Avoid duplicate handlers if setup_logging() is called more than once
    # (e.g. once directly, once via the Flask reloader).
    if logger.handlers:
        return logger

    formatter = JsonFormatter()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    log_file = os.environ.get("PRYMAX_LOG_FILE")
    if log_file:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def log_event(logger: logging.Logger, message: str, **fields) -> None:
    """Convenience wrapper so call sites read as
    log_event(logger, "room joined", room_id=room_id, peer_id=peer_id)
    instead of the more awkward logger.info(msg, extra={"fields": {...}})."""
    logger.info(message, extra={"fields": fields})


def log_warning(logger: logging.Logger, message: str, **fields) -> None:
    logger.warning(message, extra={"fields": fields})
