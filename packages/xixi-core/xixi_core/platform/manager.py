"""Explicit module assembly, ordinary cleanup and manual local-code reload."""
import copy
from dataclasses import dataclass, field
import threading

from ..privacy import private_directory
from .config import ID, json_copy, validate_module_config
from .errors import PlatformError, unavailable
from .loader import PluginDefinition, PluginLoader
from .registry import EventRegistry, HttpRegistry, ModuleContext, OwnerGate, ServiceRegistry


@dataclass
class _Record:
    definition: PluginDefinition
    builtin: bool = False
    state: str = "stopped"
    error: str | None = None
    operation: str | None = None
    restart_required: bool = False
    instance: object = None
    context: ModuleContext | None = None
    gate: OwnerGate | None = None
    applied_config: dict | None = None
    lock: object = field(default_factory=threading.RLock)

    @property
    def id(self):
        return self.definition.manifest["id"]


class ModuleManager:
    def __init__(self, config, data_dir, logs, *, builtins=None, drain_timeout=3.0):
        self.config = config
        self.data_dir = data_dir
        self.logs = logs
        self.drain_timeout = max(0.0, float(drain_timeout))
        self.services = ServiceRegistry()
        self.http = HttpRegistry()
        self.events = EventRegistry(logs)
        self.loader = PluginLoader(config.plugins_dir)
        self._records = {}
        self._started = []
        self._assembly_lock = threading.RLock()
        self._closing = False
        for module_id, cls in (builtins or {}).items():
            if not ID.fullmatch(module_id):
                raise PlatformError("invalid_module", "内置模块 id 无效")
            manifest = {"id": module_id, "name": module_id, "version": "0.1.0", "api_version": 1,
                        "entry": "builtin", "requires": []}
            metadata = getattr(cls, "manifest", {})
            manifest.update({key: metadata[key] for key in ("name", "version", "requires") if key in metadata})
            self._records[module_id] = _Record(PluginDefinition(manifest, cls=cls), builtin=True)
        for definition in self.loader.discover():
            module_id = definition.manifest["id"]
            if module_id in self._records:
                # A duplicate must not replace either the built-in or another plugin.
                self.logs.logger("platform").error("忽略重复插件 id：%s", module_id)
                continue
            record = _Record(definition)
            self._records[module_id] = record
            if definition.error:
                self._error(record, definition.error)
            else:
                try:
                    self.loader.load(definition)
                except Exception as exc:
                    self._error(record, exc)
        for module_id in config.snapshot()["modules"]:
            if module_id not in self._records:
                manifest = {"id": module_id, "name": module_id, "version": "", "api_version": 1,
                            "entry": "", "requires": []}
                record = _Record(PluginDefinition(manifest, error="配置中的模块未找到代码或清单"))
                self._records[module_id] = record
                self._error(record, record.definition.error)
        configured = list(config.snapshot()["modules"])
        self._order = ([name for name in (builtins or {}) if name != "console"]
                       + [name for name in configured if name not in (builtins or {})]
                       + [name for name in self._records if name not in configured and name not in (builtins or {})]
                       + (["console"] if "console" in (builtins or {}) else []))

    def _record(self, module_id):
        try:
            return self._records[module_id]
        except (KeyError, TypeError) as exc:
            raise PlatformError("module_not_found", "模块不存在", 404) from exc

    def _error(self, record, error, *, restart_required=False):
        record.state = "error"
        record.error = (str(error) if isinstance(error, str) else type(error).__name__ + ": " + str(error))[:1000]
        record.restart_required = restart_required
        if restart_required:
            record.error += "；旧资源未确认释放，请重启 Core"
        self.logs.logger(record.id).error("%s 失败：%s", record.operation or "加载", record.error)

    def _validate(self, record, config):
        config = validate_module_config(config)
        cls = record.definition.cls
        if cls is None:
            raise PlatformError("plugin_unloaded", "插件代码尚未成功加载，请修复并重载后保存配置", 409)
        validator = getattr(cls, "validate_config", None)
        if callable(validator):
            try:
                validator(copy.deepcopy(config))
            except PlatformError:
                raise
            except Exception as exc:
                raise PlatformError("invalid_config", str(exc)[:500] or "模块配置无效") from exc
        return config

    def _snapshot(self, record):
        settings = self.config.module(record.id)
        details = {}
        if record.instance is not None and record.gate is not None and record.gate.available():
            try:
                with record.gate.call():
                    details = json_copy(record.instance.status())
                if not isinstance(details, dict):
                    details = {"status_error": "模块 status 必须返回对象"}
            except PlatformError as exc:
                if exc.code != "service_unavailable":
                    details = {"status_error": exc.message}
            except Exception as exc:
                details = {"status_error": type(exc).__name__ + ": " + str(exc)[:500]}
        manifest = record.definition.manifest
        return {**copy.deepcopy(manifest), "builtin": record.builtin,
                "reloadable": not record.builtin and record.definition.directory is not None,
                "enabled": settings["enabled"], "state": record.state, "operation": record.operation,
                "error": record.error, "restart_required": record.restart_required,
                "config_pending": record.instance is not None and record.applied_config != settings["config"],
                "details": details}

    def list(self):
        return [self._snapshot(self._records[name]) for name in self._order]

    def configuration(self, module_id):
        record = self._record(module_id)
        settings = self.config.module(module_id)
        return {"id": module_id, **settings,
                "config_pending": record.instance is not None and record.applied_config != settings["config"]}

    def update_config(self, module_id, config):
        record = self._record(module_id)
        with record.lock:
            if self._closing:
                raise unavailable("平台正在退出")
            value = self._validate(record, config)
            self.config.update_module(module_id, config=value)
            self.logs.logger(module_id).info("配置已保存，下次启动或重载时应用")
            return self.configuration(module_id)

    def _load_if_needed(self, record):
        if record.definition.cls is None:
            if record.builtin or record.definition.directory is None:
                raise PlatformError("plugin_unloaded", record.definition.error or "模块入口不可用", 503)
            self.loader.load(record.definition)

    def _activate(self, record):
        self._load_if_needed(record)
        config = self._validate(record, self.config.module(record.id)["config"])
        for service in record.definition.manifest["requires"]:
            try:
                self.services.get(service)
            except PlatformError as exc:
                raise PlatformError("dependency_unavailable", "启动所需服务不可用：" + service, 503) from exc
        gate = OwnerGate(record.id)
        context = ModuleContext(config=copy.deepcopy(config),
                                data_dir=private_directory(self.data_dir / "modules" / record.id),
                                logger=self.logs.logger(record.id), services=self.services,
                                http=self.http, events=self.events, gate=gate)
        record.gate, record.context = gate, context
        record.instance = record.definition.cls()
        record.instance.start(context)
        record.applied_config = copy.deepcopy(config)
        record.state, record.error, record.restart_required = "running", None, False
        gate.open()
        with self._assembly_lock:
            if record.id in self._started:
                self._started.remove(record.id)
            self._started.append(record.id)
        self.logs.logger(record.id).info("模块已运行")

    def _remove_registrations(self, record):
        if record.gate is not None:
            record.gate.seal()
            self.services.remove(record.gate)
            self.http.remove(record.gate)
            self.events.remove(record.gate)

    def _deactivate(self, record):
        self._remove_registrations(record)
        if record.gate is not None and not record.gate.wait_empty(self.drain_timeout):
            self._error(record, "仍有在途调用，超过正常停止等待时间", restart_required=True)
            return False
        errors = []
        if record.instance is not None:
            try:
                record.instance.stop()
            except Exception as exc:
                errors.append(type(exc).__name__ + ": " + str(exc)[:500])
        if record.context is not None:
            errors.extend(record.context._cleanup())
        if errors:
            self._error(record, "；".join(errors), restart_required=True)
            return False
        record.instance, record.context, record.gate = None, None, None
        record.state, record.error, record.restart_required = "stopped", None, False
        record.applied_config = None
        with self._assembly_lock:
            if record.id in self._started:
                self._started.remove(record.id)
        return True

    def start(self, module_id, *, persist=True):
        record = self._record(module_id)
        with record.lock:
            if self._closing:
                raise unavailable("平台正在退出")
            if record.restart_required:
                return self._snapshot(record)
            with self._assembly_lock:
                record.operation = "start"
            try:
                if record.instance is None:
                    self._activate(record)
                if persist and not self.config.module(module_id)["enabled"]:
                    self.config.update_module(module_id, enabled=True)
            except Exception as exc:
                if self._deactivate(record):
                    self._error(record, exc)
            finally:
                record.operation = None
        return self._snapshot(record)

    def _dependents(self, module_id):
        names = self.services.owner_names(module_id)
        return [record.id for record in self._records.values()
                if record.id != module_id and (record.state == "running" or record.operation in {"start", "reload"})
                and names.intersection(record.definition.manifest["requires"])]

    def stop(self, module_id, *, persist=True):
        record = self._record(module_id)
        with record.lock:
            if self._closing:
                raise unavailable("平台正在退出")
            if record.restart_required:
                return self._snapshot(record)
            with self._assembly_lock:
                dependents = self._dependents(module_id)
                if dependents:
                    raise PlatformError("module_in_use", "服务仍被使用，请先停用：" + ", ".join(dependents), 409)
                record.operation = "stop"
                # Coordinate only admission with concurrent dependent starts;
                # plugin callbacks and normal service calls never hold this lock.
                if record.gate is not None:
                    record.gate.seal()
            try:
                if self._deactivate(record):
                    if persist and self.config.module(module_id)["enabled"]:
                        self.config.update_module(module_id, enabled=False)
                    self.logs.logger(module_id).info("模块已停止")
            except Exception as exc:
                self._error(record, exc)
            finally:
                record.operation = None
        return self._snapshot(record)

    def reload(self, module_id):
        record = self._record(module_id)
        with record.lock:
            if self._closing:
                raise unavailable("平台正在退出")
            if record.builtin or record.definition.directory is None:
                raise PlatformError("reload_unsupported", "该模块代码属于宿主；修改后请重启 Core", 409)
            if record.restart_required:
                return self._snapshot(record)
            record.operation = "reload"
            with self._assembly_lock:
                previous_position = self._started.index(module_id) if module_id in self._started else None
            try:
                if self._deactivate(record):
                    self.loader.load(record.definition)
                    self.config.refresh()
                    self._validate(record, self.config.module(module_id)["config"])
                    if self.config.module(module_id)["enabled"]:
                        self._activate(record)
                        # Reload replaces an instance, not its dependency order.
                        # A provider still needs to outlive its consumers at exit.
                        if previous_position is not None:
                            with self._assembly_lock:
                                self._started.remove(module_id)
                                self._started.insert(previous_position, module_id)
                    self.logs.logger(module_id).info("插件源码已重新加载")
            except Exception as exc:
                if self._deactivate(record):
                    self._error(record, exc)
            finally:
                record.operation = None
        return self._snapshot(record)

    def dispatch_http(self, module_id, method, path, request):
        self._record(module_id)
        return self.http.dispatch(module_id, method, path, request)

    def start_all(self):
        for module_id in self._order:
            if self.config.module(module_id)["enabled"]:
                self.start(module_id, persist=False)

    def close(self):
        self._closing = True
        with self._assembly_lock:
            order = list(reversed(self._started))
        order.extend(module_id for module_id in reversed(self._order) if module_id not in order)
        errors = []
        # The HTTP owner has stopped external admission. Keep providers available
        # until their turn, so a consumer's ordinary stop can flush through them.
        for module_id in order:
            record = self._records[module_id]
            with record.lock:
                record.operation = "shutdown"
                try:
                    if not self._deactivate(record):
                        errors.append({"id": module_id, "error": record.error})
                finally:
                    record.operation = None
        self.loader.close()
        return errors
