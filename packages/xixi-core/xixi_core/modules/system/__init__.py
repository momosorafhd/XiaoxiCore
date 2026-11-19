"""Machine resource snapshots exposed through the platform service registry."""
from __future__ import annotations

import math

from .sampler import SnapshotBusy, SystemSampler
from .monitoring import ResourceMonitor


class ResourceSampler(SystemSampler):
    """Translate collector contention at the public service boundary."""

    def collect(self):
        try:
            return super().collect()
        except SnapshotBusy:
            from ...platform.errors import PlatformError
            raise PlatformError("collection_in_progress", "资源采集正在进行，请稍后刷新。", 503) from None


class Module:
    """Live snapshots and minute history, using only module-owned data."""

    manifest = {"name": "资源监控", "version": "0.1.0", "requires": []}

    def __init__(self):
        self.sampler = None
        self.monitor = None

    @staticmethod
    def validate_config(config):
        if not isinstance(config, dict) or set(config) - {"refreshSeconds"}:
            raise ValueError("资源模块只支持 refreshSeconds 配置")
        value = config.get("refreshSeconds", 1)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("refreshSeconds 必须是数字")
        if not math.isfinite(value) or not 0.5 <= value <= 300:
            raise ValueError("refreshSeconds 必须在 0.5 到 300 秒之间")

    def start(self, context):
        self.validate_config(context.config)
        self.sampler = ResourceSampler(cache_seconds=context.config.get("refreshSeconds", 1))
        self.monitor = ResourceMonitor(self.sampler, context.data_dir, logger=context.logger)
        context.add_cleanup(self.monitor.stop)
        self.service = ResourceService(self.sampler, self.monitor)
        context.services.provide("system.resources", self.service)
        context.http.register("GET", "/history", self._http_history)
        self.monitor.start()
        context.logger.info("资源采集模块已启动，历史每分钟保存到本模块数据目录")

    def _http_history(self, request):
        from ...platform.errors import PlatformError
        if set(request.args) - {"interval"} or len(request.args.getlist("interval")) > 1:
            raise PlatformError("invalid_query", "历史查询只接受一个 interval 参数")
        try:
            interval = int(request.args.get("interval", "60"))
            return self.service.history(interval)
        except ValueError:
            raise PlatformError("invalid_interval", "历史聚合间隔须为 60、600 或 1800 秒") from None

    def stop(self):
        if self.monitor is not None:
            self.monitor.stop()
        self.monitor = None
        self.sampler = None

    def status(self):
        if self.sampler is None:
            return {"message": "未启动"}
        if not self.sampler.enabled:
            return {"message": "当前系统不支持 Linux 资源采集", "available": False}
        return {
            "message": "实时资源快照与分钟历史",
            "available": True,
            "refreshSeconds": self.sampler.cache_seconds,
            "monitor": self.monitor.status(),
        }


class ResourceService:
    """Public data contract used by the original status and history pages."""

    def __init__(self, sampler, monitor):
        self._sampler, self._monitor = sampler, monitor

    @property
    def cache_seconds(self):
        return self._sampler.cache_seconds

    def collect(self):
        result = dict(self._sampler.collect())
        result["history"] = "server_minute" if self._sampler.enabled else "none"
        return result

    def history(self, interval=60):
        return {**self._monitor.history.query(interval), "monitor": self._monitor.status()}
