from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        event = getattr(record, "cyber_event", None)
        if event:
            payload["event"] = str(event)
        fields = getattr(record, "cyber_fields", None)
        if isinstance(fields, dict):
            payload.update({str(key): value for key, value in fields.items()})
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def configure_json_logging(path: Path, *, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("camera_agent.cyber")
    logger.setLevel(level)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not any(isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == path.resolve() for handler in logger.handlers):
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    logger.info(event, extra={"cyber_event": event, "cyber_fields": fields})


__all__ = ["JsonFormatter", "configure_json_logging", "log_event"]
