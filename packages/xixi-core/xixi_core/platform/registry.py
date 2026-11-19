"""Owned registrations and a bounded drain for ordinary short calls.

This is not an isolation boundary. Trusted plugin-created workers must still be
joined by their owner. The gates cover calls made through these public services,
HTTP handlers and event subscriptions, including a previously obtained handle.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import threading
import time

from .errors import PlatformError, unavailable


class OwnerGate:
    def __init__(self, owner):
        self.owner = owner
        self._condition = threading.Condition()
        self._accepting = False
        self._registration_open = True
        self._inflight = 0

    def open(self):
        with self._condition:
            if not self._registration_open:
                raise unavailable("模块已开始停止")
            self._accepting = True

    def seal(self):
        with self._condition:
            self._accepting = False
            self._registration_open = False

    def can_register(self):
        with self._condition:
            if not self._registration_open:
                raise unavailable("已结束的模块不能登记新资源")

    def available(self):
        with self._condition:
            return self._accepting

    def acquire(self):
        with self._condition:
            if not self._accepting:
                raise unavailable("模块未运行或正在启停／重载")
            self._inflight += 1

    def release(self):
        with self._condition:
            self._inflight -= 1
            self._condition.notify_all()

    @contextmanager
    def call(self):
        self.acquire()
        try:
            yield
        finally:
            self.release()

    def wait_empty(self, timeout):
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


@dataclass(frozen=True)
class _Entry:
    gate: OwnerGate
    value: object


class _ServiceProxy:
    def __init__(self, registry, name, entry):
        self._registry, self._name, self._entry = registry, name, entry

    def __getattr__(self, attribute):
        if attribute.startswith("_"):
            raise AttributeError(attribute)
        result = self._registry._invoke(self._name, self._entry,
                                       lambda provider: getattr(provider, attribute))
        if not callable(result):
            return result

        def invoke(*args, **kwargs):
            return self._registry._invoke(
                self._name, self._entry,
                lambda provider: getattr(provider, attribute)(*args, **kwargs),
            )

        return invoke

    def __call__(self, *args, **kwargs):
        return self._registry._invoke(self._name, self._entry, lambda provider: provider(*args, **kwargs))


def _name(value):
    if not isinstance(value, str) or not value or len(value) > 160:
        raise PlatformError("invalid_registration", "服务或事件名称无效")
    return value


class ServiceRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._entries = {}

    def provide(self, gate, name, provider):
        name = _name(name)
        with self._lock:
            gate.can_register()
            if name in self._entries:
                raise PlatformError("duplicate_service", "服务名称已登记：" + name, 409)
            self._entries[name] = _Entry(gate, provider)

    def get(self, name):
        with self._lock:
            entry = self._entries.get(name)
            if entry is None or not entry.gate.available():
                raise unavailable("服务不可用：" + str(name))
            return _ServiceProxy(self, name, entry)

    def _invoke(self, name, entry, action):
        with self._lock:
            if self._entries.get(name) is not entry:
                raise unavailable("服务已撤销或重载，请重新获取：" + name)
            entry.gate.acquire()
        try:
            return action(entry.value)
        finally:
            entry.gate.release()

    def owner_names(self, owner):
        with self._lock:
            return {name for name, entry in self._entries.items() if entry.gate.owner == owner}

    def remove(self, gate):
        with self._lock:
            self._entries = {name: entry for name, entry in self._entries.items() if entry.gate is not gate}


class HttpRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._entries = {}

    @staticmethod
    def path(path):
        if (not isinstance(path, str) or not path.startswith("/") or "?" in path
                or "#" in path or "\\" in path or any(part == ".." for part in path.split("/"))):
            raise PlatformError("invalid_registration", "HTTP 路径必须是模块内的绝对路径")
        return path.rstrip("/") or "/"

    def register(self, gate, method, path, handler):
        if not isinstance(method, str) or method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            raise PlatformError("invalid_registration", "HTTP method 无效")
        if not callable(handler):
            raise PlatformError("invalid_registration", "HTTP handler 必须可调用")
        key = (gate.owner, method.upper(), self.path(path))
        with self._lock:
            gate.can_register()
            if key in self._entries:
                raise PlatformError("duplicate_handler", "HTTP 处理器已登记", 409)
            self._entries[key] = _Entry(gate, handler)

    def dispatch(self, owner, method, path, request):
        key = (owner, method.upper(), self.path(path))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                raise PlatformError("handler_unavailable", "模块接口未登记或已停用", 503)
            entry.gate.acquire()
        try:
            return entry.value(request)
        finally:
            entry.gate.release()

    def remove(self, gate):
        with self._lock:
            self._entries = {key: entry for key, entry in self._entries.items() if entry.gate is not gate}


class EventRegistry:
    def __init__(self, logs):
        self._lock = threading.RLock()
        self._entries = {}
        self._logs = logs

    def subscribe(self, gate, name, callback):
        name = _name(name)
        if not callable(callback):
            raise PlatformError("invalid_registration", "事件 callback 必须可调用")
        with self._lock:
            gate.can_register()
            self._entries.setdefault(name, []).append(_Entry(gate, callback))

    def emit(self, name, payload=None):
        name = _name(name)
        with self._lock:
            entries = list(self._entries.get(name, []))
        result = {"delivered": 0, "errors": []}
        for entry in entries:
            try:
                entry.gate.acquire()
            except PlatformError:
                continue
            try:
                entry.value(payload)
                result["delivered"] += 1
            except Exception as exc:
                message = type(exc).__name__ + ": " + str(exc)[:500]
                self._logs.logger(entry.gate.owner).error("事件 %s 处理失败：%s", name, message)
                result["errors"].append({"module": entry.gate.owner, "error": message})
            finally:
                entry.gate.release()
        return result

    def remove(self, gate):
        with self._lock:
            self._entries = {
                name: remaining for name, entries in self._entries.items()
                if (remaining := [entry for entry in entries if entry.gate is not gate])
            }


class _Services:
    def __init__(self, registry, gate):
        self._registry, self._gate = registry, gate

    def get(self, name):
        return self._registry.get(name)

    def provide(self, name, provider):
        return self._registry.provide(self._gate, name, provider)


class _Http:
    def __init__(self, registry, gate):
        self._registry, self._gate = registry, gate

    def register(self, method, path, handler):
        return self._registry.register(self._gate, method, path, handler)


class _Events:
    def __init__(self, registry, gate):
        self._registry, self._gate = registry, gate

    def subscribe(self, name, callback):
        return self._registry.subscribe(self._gate, name, callback)

    def emit(self, name, payload=None):
        self._gate.can_register()
        return self._registry.emit(name, payload)


class ModuleContext:
    """Public module facilities only; no CoreApp or manager reference."""
    def __init__(self, *, config, data_dir, logger, services, http, events, gate):
        self.config = config
        self.data_dir = data_dir
        self.logger = logger
        self.services = _Services(services, gate)
        self.http = _Http(http, gate)
        self.events = _Events(events, gate)
        self._gate = gate
        self._cleanups = []
        self._cleanup_lock = threading.Lock()

    def add_cleanup(self, callback):
        if not callable(callback):
            raise PlatformError("invalid_cleanup", "cleanup 必须可调用")
        with self._cleanup_lock:
            self._gate.can_register()
            self._cleanups.append(callback)

    def _cleanup(self):
        with self._cleanup_lock:
            callbacks, self._cleanups = self._cleanups, []
        errors, failed = [], []
        for callback in reversed(callbacks):
            try:
                callback()
            except Exception as exc:
                errors.append(type(exc).__name__ + ": " + str(exc)[:500])
                failed.append(callback)
        # A later external shutdown can try the remaining ordinary cleanup once.
        with self._cleanup_lock:
            self._cleanups.extend(reversed(failed))
        return errors
