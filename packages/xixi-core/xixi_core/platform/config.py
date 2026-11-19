"""One ordinary JSON configuration file; module data lives elsewhere."""
import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit

from .errors import PlatformError


ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SECRET_KEYS = frozenset({
    "password", "passwd", "token", "secret", "api_key", "apikey",
    "access_token", "refresh_token", "private_key", "client_secret",
    "password_hash", "authorization",
})


def json_copy(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise ValueError("配置超过 1 MiB")
        return json.loads(encoded)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PlatformError("invalid_config", "配置必须是有限大小的标准 JSON") from exc


def validate_module_config(value):
    if not isinstance(value, dict):
        raise PlatformError("invalid_config", "模块 config 必须为 JSON 对象")
    value = json_copy(value)

    def visit(item):
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower().replace("-", "_")
                if normalized in _SECRET_KEYS:
                    raise PlatformError(
                        "secret_in_config", "普通配置不能保存凭据；请使用专用凭据文件或环境引用"
                    )
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return value


def validate_document(document):
    if not isinstance(document, dict):
        raise PlatformError("invalid_config", "平台配置必须为对象")
    document = json_copy(document)
    unknown = set(document) - {"http", "plugins_dir", "modules"}
    if unknown:
        raise PlatformError("invalid_config", "未知平台配置字段：" + ", ".join(sorted(unknown)))
    http = document.setdefault("http", {})
    if not isinstance(http, dict):
        raise PlatformError("invalid_config", "http 必须为对象")
    host = http.setdefault("host", "127.0.0.1")
    if not isinstance(host, str) or not host or len(host) > 255:
        raise PlatformError("invalid_config", "http.host 无效")
    port = http.setdefault("port", 18910)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise PlatformError("invalid_config", "http.port 必须为 0–65535 的整数")
    origin = http.get("origin")
    if origin is not None:
        try:
            parsed = urlsplit(origin)
            valid = (isinstance(origin, str) and parsed.scheme in {"http", "https"}
                     and parsed.hostname and not parsed.username and not parsed.password
                     and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
            parsed.port
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise PlatformError("invalid_config", "http.origin 需要完整的 HTTP(S) 来源地址")
        http["origin"] = origin.rstrip("/")
    proxy = http.get("trusted_proxy")
    if proxy is not None:
        try:
            ipaddress.ip_address(proxy)
        except ValueError as exc:
            raise PlatformError("invalid_config", "http.trusted_proxy 需要单个 IP 地址") from exc
    networks = http.get("allowed_networks")
    if networks is not None:
        if not isinstance(networks, list) or not networks:
            raise PlatformError("invalid_config", "http.allowed_networks 需要非空网段数组")
        try:
            for item in networks:
                if not isinstance(item, str):
                    raise ValueError()
                ipaddress.ip_network(item)
        except ValueError as exc:
            raise PlatformError("invalid_config", "http.allowed_networks 含无效网段") from exc
    plugins_dir = document.setdefault("plugins_dir", "plugins")
    if not isinstance(plugins_dir, str) or not plugins_dir or len(plugins_dir) > 4096:
        raise PlatformError("invalid_config", "plugins_dir 需要目录路径")
    modules = document.setdefault("modules", {})
    if not isinstance(modules, dict):
        raise PlatformError("invalid_config", "modules 必须为对象")
    for module_id, settings in modules.items():
        if not ID.fullmatch(module_id) or not isinstance(settings, dict):
            raise PlatformError("invalid_config", "模块标识或配置无效")
        if set(settings) - {"enabled", "config"}:
            raise PlatformError("invalid_config", "模块配置只保存 enabled 和 config，身份与入口属于清单")
        enabled = settings.setdefault("enabled", False)
        if not isinstance(enabled, bool):
            raise PlatformError("invalid_config", "enabled 必须为布尔值")
        settings["config"] = validate_module_config(settings.get("config", {}))
    return document


class ConfigStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self._lock = threading.RLock()
        self._document = self._read()
        self._runtime_http = None

    def _read(self):
        try:
            if self.path.stat().st_size > 1024 * 1024:
                raise PlatformError("invalid_config", "配置超过 1 MiB")
            document = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise PlatformError("config_read_failed", "无法读取平台 JSON 配置：" + type(exc).__name__) from exc
        return validate_document(document)

    def snapshot(self):
        with self._lock:
            document = copy.deepcopy(self._document)
            if self._runtime_http is not None:
                document["http"] = copy.deepcopy(self._runtime_http)
            return document

    def runtime_http(self, http):
        """CLI binding overrides are visible to HTTP but never written to disk."""
        with self._lock:
            document = copy.deepcopy(self._document)
            document["http"] = http
            self._runtime_http = validate_document(document)["http"]

    def refresh(self):
        with self._lock:
            self._document = self._read()

    def module(self, module_id):
        with self._lock:
            return copy.deepcopy(self._document["modules"].get(module_id, {"enabled": False, "config": {}}))

    @property
    def plugins_dir(self):
        with self._lock:
            return (self.path.parent / self._document["plugins_dir"]).resolve()

    def update_module(self, module_id, *, enabled=None, config=None):
        with self._lock:
            document = copy.deepcopy(self._document)
            settings = document["modules"].setdefault(module_id, {"enabled": False, "config": {}})
            if enabled is not None:
                settings["enabled"] = enabled
            if config is not None:
                settings["config"] = config
            document = validate_document(document)
            self._write(document)
            self._document = document
            return copy.deepcopy(document["modules"][module_id])

    def _write(self, document):
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix="." + self.path.name + ".", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(document, stream, ensure_ascii=False, allow_nan=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError as exc:
            raise PlatformError("config_write_failed", "配置保存失败，原文件未替换：" + type(exc).__name__, 500) from exc
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink()
                except OSError:
                    pass
