"""One reader owns the reverse WebSocket, including ping/pong and RPC writes."""
from concurrent.futures import Future
import queue
import threading
import time
import uuid

from xixi_core.qq_onebot_reverse import IdleTimeout, ReverseListener
from xixi_core.platform.errors import PlatformError


def disconnected():
    return PlatformError("onebot_disconnected", "消息桥尚未连接已核验身份的 OneBot 客户端", 503)


class ReverseTransport:
    def __init__(self, config, receive, log):
        self.listener = ReverseListener(config)
        self.bot = config["botId"]
        self.receive = receive
        self.log = log
        self.stopping = threading.Event()
        self.connected = threading.Event()
        self.lock = threading.Lock()
        self.commands = queue.Queue(maxsize=128)
        self.socket = None
        self.last_error = None
        self.thread = threading.Thread(target=self._run, name="xixi-onebot-private", daemon=True)

    def start(self):
        self.listener.open()
        self.thread.start()

    def call(self, action, params, *, cancel_event=None):
        future = Future()
        deadline = time.monotonic() + 10
        with self.lock:
            if not self.connected.is_set():
                raise disconnected()
            try:
                self.commands.put_nowait((future, action, params, deadline))
            except queue.Full:
                raise PlatformError("onebot_busy", "渠道请求过多", 503) from None
        try:
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    future.cancel()
                    raise PlatformError("onebot_cancelled", "助手已中断启动补收", 409)
                try:
                    return future.result(timeout=.1)
                except TimeoutError:
                    if future.done():
                        raise
            raise TimeoutError()
        except TimeoutError:
            future.cancel()
            raise PlatformError("onebot_timeout", "渠道回执超时，操作结果不确定", 504) from None

    def _serve(self, ws):
        login = uuid.uuid4().hex
        ws.send({"action": "get_login_info", "params": {}, "echo": login})
        pending = {}
        login_deadline = time.monotonic() + 5
        try:
            while not self.stopping.is_set():
                now = time.monotonic()
                if not self.connected.is_set() and now > login_deadline:
                    raise disconnected()
                for echo, (future, end) in list(pending.items()):
                    if end < now:
                        del pending[echo]
                        if not future.done():
                            future.set_exception(TimeoutError())
                for _ in range(16):
                    try:
                        future, action, params, end = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    if time.monotonic() >= end or not future.set_running_or_notify_cancel():
                        continue
                    echo = uuid.uuid4().hex
                    pending[echo] = future, end
                    ws.send({"action": action, "params": params, "echo": echo})
                try:
                    event = ws.recv(timeout=.05)
                except IdleTimeout:
                    continue
                if not isinstance(event, dict):
                    continue
                echo = event.get("echo")
                if echo == login and not self.connected.is_set():
                    if (event.get("status") != "ok" or event.get("retcode") != 0
                            or str((event.get("data") or {}).get("user_id")) != self.bot):
                        raise disconnected()
                    self.last_error = None
                    self.connected.set()
                elif isinstance(echo, str) and echo in pending:
                    future, _ = pending.pop(echo)
                    if event.get("status") == "ok" and event.get("retcode") == 0:
                        future.set_result(event.get("data"))
                    else:
                        future.set_exception(PlatformError("onebot_rejected", "OneBot 未接受渠道请求", 502))
                elif self.connected.is_set() and "post_type" in event:
                    self.receive(event)
        finally:
            with self.lock:
                self.connected.clear()
                for future, _ in pending.values():
                    if not future.done():
                        future.set_exception(disconnected())
                while not self.commands.empty():
                    future = self.commands.get_nowait()[0]
                    if future.set_running_or_notify_cancel():
                        future.set_exception(disconnected())

    def _run(self):
        while not self.stopping.is_set():
            try:
                self.socket = self.listener.accept()
                self._serve(self.socket)
            except IdleTimeout:
                pass
            except Exception as exc:
                if not self.stopping.is_set():
                    self.last_error = type(exc).__name__
                    self.log.warning("OneBot 连接已结束（%s）", self.last_error)
            finally:
                if self.socket:
                    self.socket.close()
                    self.socket = None

    def close(self):
        self.stopping.set()
        active = self.socket
        if active:
            active.close()
        self.listener.close()
        if self.thread.ident is not None:
            self.thread.join(5)
            if self.thread.is_alive():
                raise RuntimeError("消息桥接收线程仍在退出")
