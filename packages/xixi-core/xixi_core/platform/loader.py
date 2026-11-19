"""Load trusted local plugin packages under independent Python namespaces.

The scoped source loader always compiles plugin source, not timestamp-based pyc.
That makes an immediate same-size edit reload correctly without deleting shared
import caches or changing Python's global bytecode policy.
"""
from dataclasses import dataclass
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import re
import sys
import threading
import types
import uuid

from .config import ID
from .errors import PlatformError


@dataclass
class PluginDefinition:
    manifest: dict
    directory: Path | None = None
    cls: object = None
    error: str | None = None


def read_manifest(directory):
    directory = Path(directory).resolve()
    try:
        path = directory / "plugin.json"
        if path.stat().st_size > 65536:
            raise ValueError("plugin.json 超过 64 KiB")
        item = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(item, dict):
            raise ValueError("清单必须为对象")
        module_id = item.get("id")
        if not isinstance(module_id, str) or not ID.fullmatch(module_id):
            raise ValueError("id 需要稳定的小写标识")
        for field in ("name", "version"):
            if not isinstance(item.get(field), str) or not item[field] or len(item[field]) > 160:
                raise ValueError(field + " 必须为非空文本")
        if type(item.get("api_version")) is not int or item["api_version"] != 1:
            raise ValueError("初版只支持 api_version=1")
        entry = item.get("entry")
        if not isinstance(entry, str) or entry.count(":") != 1:
            raise ValueError("entry 格式为 plugin.py:Plugin")
        file_name, class_name = entry.split(":")
        relative = Path(file_name)
        if (relative.is_absolute() or relative.suffix != ".py" or ".." in relative.parts
                or not class_name.isidentifier()):
            raise ValueError("entry 必须为插件目录内 Python 文件和类")
        target = (directory / relative).resolve()
        if not target.is_relative_to(directory) or not target.is_file():
            raise ValueError("入口文件不存在或越出插件目录")
        module_parts = relative.with_suffix("").parts
        if any(not part.isidentifier() for part in module_parts):
            raise ValueError("入口文件及包路径需要有效的 Python 标识")
        requires = item.get("requires", [])
        if (not isinstance(requires, list) or len(requires) > 64
                or any(not isinstance(value, str) or not value or len(value) > 160 for value in requires)
                or len(set(requires)) != len(requires)):
            raise ValueError("requires 需要不重复的服务名称数组")
        return {key: item[key] for key in ("id", "name", "version", "api_version", "entry")} | {"requires": requires}
    except (OSError, ValueError, TypeError) as exc:
        raise PlatformError("invalid_manifest", "插件清单无效：" + str(exc)[:500]) from exc


class _Source(importlib.abc.Loader):
    def __init__(self, file_path):
        self.path = file_path

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        if self.path is not None:
            module.__file__ = str(self.path)
            code = compile(self.path.read_bytes(), str(self.path), "exec", dont_inherit=True)
            exec(code, module.__dict__)


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, prefix):
        self.prefix = prefix
        self.packages = {}
        self.lock = threading.RLock()

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(self.prefix + "."):
            return None
        with self.lock:
            root_name = ".".join(fullname.split(".")[:2])
            directory = self.packages.get(root_name)
            if directory is None:
                return None
            relative = fullname[len(root_name):].lstrip(".")
            location = directory.joinpath(*relative.split(".")) if relative else directory
            if not location.resolve().is_relative_to(directory):
                raise ImportError("插件内部导入越出代码目录")
            if location.is_dir():
                source = location / "__init__.py"
                source = source if source.is_file() else None
                is_package = True
            else:
                source = location.with_suffix(".py")
                if not source.is_file():
                    return None
                is_package = False
            if source is not None and not source.resolve().is_relative_to(directory):
                raise ImportError("插件源码越出代码目录")
            spec = importlib.machinery.ModuleSpec(fullname, _Source(source),
                                                origin=str(source or location), is_package=is_package)
            if is_package:
                spec.submodule_search_locations = [str(location)]
            return spec


class PluginLoader:
    def __init__(self, plugins_dir):
        self.plugins_dir = Path(plugins_dir).resolve()
        self.prefix = "_xixi_plugins_" + uuid.uuid4().hex
        self._finder = _Finder(self.prefix)
        self._lock = threading.RLock()
        package = types.ModuleType(self.prefix)
        package.__path__ = []
        package.__package__ = self.prefix
        package.__spec__ = importlib.machinery.ModuleSpec(self.prefix, loader=None, is_package=True)
        sys.modules[self.prefix] = package
        sys.meta_path.insert(0, self._finder)

    def namespace(self, module_id):
        name = module_id.replace("-", "_")
        suffix = hashlib.sha256(module_id.encode()).hexdigest()[:8]
        return self.prefix + "." + name + "_" + suffix

    def discover(self):
        if not self.plugins_dir.exists():
            return []
        definitions = []
        for directory in sorted(self.plugins_dir.iterdir()):
            if not directory.is_dir() or not (directory / "plugin.json").exists():
                continue
            try:
                if not directory.resolve().is_relative_to(self.plugins_dir):
                    raise PlatformError("invalid_manifest", "插件目录不能指向插件根目录外")
                manifest = read_manifest(directory)
                definitions.append(PluginDefinition(manifest, directory.resolve()))
            except Exception as exc:
                fallback = "invalid_" + hashlib.sha256(str(directory).encode()).hexdigest()[:10]
                manifest = {"id": fallback, "name": directory.name, "version": "", "api_version": 1,
                            "entry": "", "requires": []}
                definitions.append(PluginDefinition(manifest, directory.resolve(), error=str(exc)[:500]))
        return definitions

    def load(self, definition):
        with self._lock:
            module_id = definition.manifest["id"]
            namespace = self.namespace(module_id)
            # Even a broken manifest/entry must not leave an old executable
            # class behind: a later start is not an implicit code rollback.
            definition.cls = None
            self.clear(module_id)
            try:
                manifest = read_manifest(definition.directory)
                if manifest["id"] != module_id:
                    raise PlatformError("plugin_id_changed", "插件重载不能更换 id；修改身份后需要重启 Core")
                with self._finder.lock:
                    self._finder.packages[namespace] = definition.directory
                file_name, class_name = manifest["entry"].split(":")
                parts = list(Path(file_name).with_suffix("").parts)
                if parts[-1] == "__init__":
                    parts.pop()
                module = importlib.import_module(namespace + ("." + ".".join(parts) if parts else ""))
                cls = getattr(module, class_name)
                if not isinstance(cls, type) or any(not callable(getattr(cls, name, None)) for name in ("start", "stop", "status")):
                    raise PlatformError("invalid_plugin", "入口类必须提供 start、stop 和 status")
                definition.manifest = manifest
                definition.cls = cls
                definition.error = None
                return cls
            except BaseException:
                self.clear(module_id)
                definition.cls = None
                raise

    def clear(self, module_id):
        namespace = self.namespace(module_id)
        for name in list(sys.modules):
            if name == namespace or name.startswith(namespace + "."):
                sys.modules.pop(name, None)
        importlib.invalidate_caches()

    def close(self):
        with self._lock:
            if self._finder in sys.meta_path:
                sys.meta_path.remove(self._finder)
            for name in list(sys.modules):
                if name == self.prefix or name.startswith(self.prefix + "."):
                    sys.modules.pop(name, None)
            with self._finder.lock:
                self._finder.packages.clear()
