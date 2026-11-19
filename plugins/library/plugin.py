"""Own the Library backend and adapt the owner console's v0.4 requests."""
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xixi_core.library_v04 import LibraryV04, LocalOnnxEncoder
from xixi_core.platform.errors import PlatformError

from .service import LibraryService


DEFAULT_TIMEZONE = "Asia/Shanghai"
READ_OPERATIONS = ("list", "search", "get", "diary", "sources", "chat_history", "chat_search", "schema", "sql")
WRITE_OPERATIONS = ("record", "revise", "write_diary", "mark_delete", "cancel_delete", "retry_index")


class Plugin:
    @staticmethod
    def validate_config(config):
        if not isinstance(config, dict) or set(config) - {"timezone", "encoderPython", "modelDir", "encoderId"}:
            raise ValueError("图书馆配置只接受 timezone、encoderPython、modelDir 和 encoderId")
        if any(not isinstance(value, str) or not value.strip() for value in config.values()):
            raise ValueError("图书馆配置字段须为非空字符串")
        try:
            ZoneInfo(config.get("timezone", DEFAULT_TIMEZONE))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone 需要有效的 IANA 时区，例如 Asia/Shanghai") from exc
        if (set(config) & {"encoderPython", "modelDir", "encoderId"}) and not {"encoderPython", "modelDir"} <= set(config):
            raise ValueError("本地向量编码器需要同时设置 encoderPython 和 modelDir")
        if "encoderPython" in config and (not Path(config["encoderPython"]).is_file()
                                          or not (Path(config["modelDir"]) / "manifest.json").is_file()):
            raise ValueError("本地向量编码器的 Python 或模型目录不存在")

    def start(self, context):
        config = context.config
        self.validate_config(config)
        timezone = ZoneInfo(config.get("timezone", DEFAULT_TIMEZONE))
        encoder = None
        if "encoderPython" in config:
            encoder = LocalOnnxEncoder(config["encoderPython"], config["modelDir"], config.get("encoderId"))
        self._backend = LibraryV04(context.data_dir / "library.sqlite3", encoder=encoder)
        self._backend.start_vector_worker()

        self._service = LibraryService(self._backend, timezone)
        context.services.provide("library", self._service)
        context.http.register("GET", "/v04/status", self._http_status)
        for operation in READ_OPERATIONS + WRITE_OPERATIONS:
            context.http.register("POST", "/v04/" + operation, partial(self._http_operation, operation))

    def stop(self):
        backend = getattr(self, "_backend", None)
        if backend is not None:
            if not backend.close():
                raise RuntimeError("图书馆索引线程未正常退出，请重启 Core")
            self._backend = None

    def status(self):
        index = self._backend.index_status()
        return {
            "entry": "/library",
            "backend": self._backend.mode,
            "timezone": self._service.timezone.key,
            "keyword": index["keyword"],
            "encoderConfigured": index["encoderConfigured"],
            "workerRunning": index["workerRunning"],
        }

    def _http_status(self, request):
        return self._backend.index_status()

    def _http_operation(self, operation, request):
        if operation in {"record", "revise", "write_diary"}:
            request.max_content_length = 1024 * 1024
        body = request.get_json()
        if not isinstance(body, dict):
            raise PlatformError("invalid_library_request", "图书馆操作参数须为对象")
        request_id = body.pop("requestId", None)
        if operation in WRITE_OPERATIONS and (not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 500):
            raise PlatformError("invalid_library_request", "图书馆写入须提供稳定的 requestId")
        try:
            return self._service.dispatch(operation, body, request_id)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise PlatformError("invalid_library_request", str(exc)) from exc
        except (KeyError, TypeError) as exc:
            raise PlatformError("invalid_library_request", "图书馆操作参数缺失或类型无效") from exc
