"""Bounded recent logs and a small rotating local log, without request bodies."""
from collections import deque
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
import uuid


class _Formatter(logging.Formatter):
    @staticmethod
    def item(record):
        return {
            "at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "module": getattr(record, "module_id", "platform"),
            "message": record.getMessage()[:2000],
        }

    def format(self, record):
        return json.dumps(self.item(record), ensure_ascii=False)


class _Recent(logging.Handler):
    def __init__(self, target):
        super().__init__()
        self.target = target

    def emit(self, record):
        with self.target._lock:
            self.target._recent.append(_Formatter.item(record))


class LogStore:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._recent = deque(maxlen=1000)
        self._loggers = {}
        self._name = "xixi.platform." + uuid.uuid4().hex
        self._file = RotatingFileHandler(directory / "platform.log", maxBytes=2 * 1024 * 1024,
                                         backupCount=2, encoding="utf-8")
        self._file.setFormatter(_Formatter())
        self._memory = _Recent(self)
        self._closed = False

    def logger(self, module_id="platform"):
        with self._lock:
            if module_id not in self._loggers:
                logger = logging.Logger(self._name + "." + module_id, logging.INFO)
                logger.propagate = False
                logger.addHandler(self._file)
                logger.addHandler(self._memory)
                self._loggers[module_id] = logging.LoggerAdapter(logger, {"module_id": module_id})
            return self._loggers[module_id]

    def list(self, limit=100):
        limit = max(1, min(int(limit), 500))
        with self._lock:
            return [dict(item) for item in list(self._recent)[-limit:]]

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for adapter in self._loggers.values():
                adapter.logger.removeHandler(self._file)
                adapter.logger.removeHandler(self._memory)
            self._file.close()
            self._memory.close()
