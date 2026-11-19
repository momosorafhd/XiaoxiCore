"""Small plugin using only the public ModuleContext contract."""
import math
import threading

from . import helpers


class GreetingService:
    def __init__(self, plugin):
        self._plugin = plugin

    def describe(self):
        return self._plugin.status()


class Plugin:
    @staticmethod
    def validate_config(config):
        if not isinstance(config, dict) or set(config) - {"text", "intervalSeconds"}:
            raise ValueError("示例配置只接受 text 和 intervalSeconds")
        text = config.get("text", "示例插件已运行")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError("text 需要 1—2000 字符的非空文本")
        interval = config.get("intervalSeconds", 1)
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 0.05 <= interval <= 60:
            raise ValueError("intervalSeconds 需要 0.05—60 秒")

    def start(self, context):
        self.validate_config(context.config)
        self._text = context.config.get("text", "示例插件已运行")
        self._interval = context.config.get("intervalSeconds", 1)
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._ticks = 0
        self._events = 0
        self._thread = threading.Thread(target=self._count, name="xixi-example-counter", daemon=True)
        # Register cleanup before resources: partially failed starts can close the worker too.
        context.add_cleanup(self._stop_worker)
        context.services.provide("example.greeting", GreetingService(self))
        context.http.register("GET", "/status", self._http_status)
        context.events.subscribe("example.ping", self._on_ping)
        self._thread.start()
        context.logger.info("示例插件已启动")

    def _count(self):
        while not self._stopped.wait(self._interval):
            with self._lock:
                self._ticks += 1

    def _on_ping(self, payload):
        with self._lock:
            self._events += 1

    def _http_status(self, request):
        return self.status()

    def status(self):
        with self._lock:
            return {
                "text": self._text,
                "greeting": helpers.greeting(self._text),
                "ticks": self._ticks,
                "events": self._events,
                "worker_running": self._thread.is_alive(),
            }

    def _stop_worker(self):
        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        if self._thread.is_alive():
            raise RuntimeError("示例计数线程未退出，请重启 Core")

    def stop(self):
        self._stop_worker()
