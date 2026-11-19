"""Platform host for the independently managed console and assistant plugins."""
from datetime import datetime, timezone
import os
from pathlib import Path
import time

from ..privacy import private_directory
from ..single_instance import SingleInstanceLock
from .config import ConfigStore
from .errors import PlatformError
from .logs import LogStore
from .manager import ModuleManager


VERSION = "0.4.0"


class CoreApp:
    def __init__(self, config_path, data_dir, *, builtins=None, drain_timeout=3.0):
        self.config = ConfigStore(config_path)
        if builtins is None:
            from ..modules.system import Module as SystemModule
            from ..modules.console import Module as ConsoleModule
            builtins = {"system": SystemModule, "console": ConsoleModule}
        self.data_dir = private_directory(Path(data_dir).resolve())
        self._lock = SingleInstanceLock(self.data_dir / "platform.lock")
        self._closed, self._started = False, False
        self._shutdown_errors = []
        self.started_at = None
        self._monotonic_start = None
        self.logs = None
        try:
            self._lock.acquire()
            self.logs = LogStore(self.data_dir / "logs")
            self.manager = ModuleManager(self.config, self.data_dir, self.logs,
                                         builtins=builtins, drain_timeout=drain_timeout)
            self.services = self.manager.services
            self.events = self.manager.events
        except BaseException:
            if self.logs is not None:
                self.logs.close()
            self._lock.close()
            raise

    def start(self):
        if self._closed:
            raise PlatformError("platform_closed", "已关闭的宿主不能再次启动；请创建新的 CoreApp", 409)
        if not self._started:
            self.started_at = datetime.now(timezone.utc).isoformat()
            self._monotonic_start = time.monotonic()
            self.manager.start_all()
            self._started = True
            self.logs.logger().info("Core 平台已启动")
        return self.snapshot()

    def snapshot(self):
        modules = self.manager.list()
        return {
            "name": "XiaoxiCore", "version": VERSION, "mode": "platform",
            "pid": os.getpid(),
            "release": os.environ.get("XIXI_RELEASE", "platform-local"),
            "state": "stopped" if self._closed or not self._started else "running",
            "started_at": self.started_at,
            "uptime_seconds": round(time.monotonic() - self._monotonic_start, 3) if self._monotonic_start else 0,
            "modules": {"total": len(modules),
                        "running": sum(item["state"] == "running" for item in modules),
                        "error": sum(item["state"] == "error" for item in modules)},
            "legacy_business_started": False,
        }

    def close(self):
        if self._closed:
            return self._shutdown_errors
        self._closed = True
        try:
            self._shutdown_errors = self.manager.close()
            self.logs.logger().info("Core 平台已退出%s", "，存在未清理资源" if self._shutdown_errors else "")
            return self._shutdown_errors
        finally:
            self.logs.close()
            self._lock.close()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.close()
