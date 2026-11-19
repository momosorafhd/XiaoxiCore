"""Persist roster-validated model bindings and expose DSH session fields."""
import hashlib
import json
import os
import tempfile
import threading

from xixi_core.model_settings import binding_options, bounded_id, normalize_binding, validate_catalog_choice
from xixi_core.platform.errors import PlatformError


PURPOSES = ("daily", "work", "consolidation", "dream", "leisure")
SESSION_PURPOSES = {"daily": "daily", "work": "work", "idle": "consolidation", "maintenance": "consolidation",
                    "dream": "dream", "leisure": "leisure"}


class Settings:
    def __init__(self, path, catalog):
        self.path = path
        self._catalog = catalog
        self._lock = threading.Lock()

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"revision": 1, "bindings": dict.fromkeys(PURPOSES)}

    def _write(self, state):
        fd, name = tempfile.mkstemp(prefix=".model-settings-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, allow_nan=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _response(state):
        return {"revision": state["revision"], "bindings": state["bindings"]}

    def settings(self):
        with self._lock:
            return self._response(self._read())

    def session_fields(self, kind):
        try:
            purpose = SESSION_PURPOSES[kind]
        except (KeyError, TypeError) as exc:
            raise ValueError("未知模型用途") from exc
        with self._lock:
            binding = self._read()["bindings"][purpose]
        if binding is None:
            return {}
        return {"model": binding["model"], "agentPreset": binding["agentPreset"], "modelOptions": binding_options(binding)}

    @staticmethod
    def _validate_request(body):
        if not isinstance(body, dict) or set(body) != {"expectedRevision", "requestId", "bindings"}:
            raise ValueError("请提供模型设置、读取时的版本和请求编号")
        revision, request_id, bindings = body["expectedRevision"], body["requestId"], body["bindings"]
        if type(revision) is not int or revision < 1:
            raise ValueError("模型设置版本无效")
        bounded_id(request_id, "requestId", 300).encode("utf-8")
        if not isinstance(bindings, dict) or not bindings or set(bindings) - set(PURPOSES):
            raise ValueError("请指定至少一个有效模型用途")
        bindings = {purpose: normalize_binding(value) if value is not None else None for purpose, value in bindings.items()}
        digest = hashlib.sha256(json.dumps([revision, bindings], sort_keys=True, allow_nan=False).encode()).hexdigest()
        return revision, request_id, bindings, digest

    @staticmethod
    def _check_revision(saved, revision, request_id, digest):
        last = saved.get("last", {})
        if last.get("id") == request_id and last.get("digest") == digest:
            return True
        if last.get("id") == request_id or saved["revision"] != revision:
            raise PlatformError("model_settings_conflict", "模型设置版本或请求编号冲突，请重新读取", 409)
        return False

    def save(self, body):
        try:
            revision, request_id, supplied, digest = self._validate_request(body)
        except (TypeError, ValueError) as exc:
            raise PlatformError("invalid_model_settings", str(exc)) from exc
        with self._lock:
            saved = self._read()
            if self._check_revision(saved, revision, request_id, digest):
                return self._response(saved)
            changes = {purpose: value for purpose, value in supplied.items() if value != saved["bindings"][purpose]}
        choices = [value for value in changes.values() if value is not None]
        if choices:
            catalog = self._catalog()
            try:
                for value in choices:
                    validate_catalog_choice(catalog, value)
            except ValueError as exc:
                raise PlatformError("invalid_model_settings", str(exc)) from exc

        with self._lock:
            saved = self._read()
            if self._check_revision(saved, revision, request_id, digest):
                return self._response(saved)
            if changes:
                saved["bindings"].update(changes)
                saved["revision"] += 1
            saved["last"] = {"id": request_id, "digest": digest}
            self._write(saved)
            return self._response(saved)


class Plugin:
    @staticmethod
    def validate_config(config):
        if config:
            raise ValueError("模型绑定请在管理台模型设置页保存")

    def start(self, context):
        self._context = context
        self._settings = Settings(context.data_dir / "model-settings.json", self._catalog)
        context.services.provide("model_settings", self._settings)
        context.http.register("GET", "/settings", lambda _request: self._settings.settings())
        context.http.register("POST", "/settings", lambda request: self._settings.save(request.get_json()))
        context.http.register("GET", "/catalog", lambda _request: self._catalog())

    def _catalog(self):
        try:
            catalog = self._context.services.get("dsh_bridge").model_catalog()
            if not isinstance(catalog, dict) or not isinstance(catalog.get("groups"), list):
                raise ValueError("DSH 模型目录无效")
            return catalog
        except (PlatformError, ValueError) as exc:
            raise PlatformError("dsh_catalog_not_ready", "DSH 模型目录不可用，已保存的设置保持有效", 503) from exc

    def stop(self):
        pass

    def status(self):
        settings = self._settings.settings()
        return {"revision": settings["revision"], "configured": [key for key, value in settings["bindings"].items() if value is not None]}
