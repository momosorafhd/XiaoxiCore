import time
from pathlib import Path

from xixi_core.platform.errors import PlatformError
from xixi_core.qq_onebot_reverse import private_ipv4

from .history import collect, identifier, owner_message
from .transport import ReverseTransport, disconnected


class Plugin:
    @staticmethod
    def validate_config(config):
        mode = config.get("mode", "offline")
        if mode not in {"offline", "onebot_reverse"}:
            raise ValueError("mode 需要 offline 或 onebot_reverse")
        allowed = {"mode", "ownerId", "botId", "listenHost", "listenPort", "allowedPeerIp",
                   "reverseTokenFile", "historyIndex"}
        if set(config) - allowed:
            raise ValueError("消息桥配置含未知字段")
        for key, label in (("ownerId", "主人 QQ"), ("botId", "机器人 QQ")):
            value = config.get(key)
            if mode == "offline" and value in (None, ""):
                continue
            if not isinstance(value, str) or not value.isascii() or not value.isdigit() or value.startswith("0"):
                raise ValueError(f"{label}需要填写不带前导零的正整数账号")
        if config.get("ownerId") and config["ownerId"] == config.get("botId"):
            raise ValueError("主人账号与机器人账号须分别配置")
        if mode == "onebot_reverse":
            for key in ("listenHost", "allowedPeerIp"):
                private_ipv4(config.get(key))
            if type(config.get("listenPort")) is not int or not 1024 <= config["listenPort"] <= 65535:
                raise ValueError("请指定非特权监听端口")
            if not isinstance(config.get("reverseTokenFile"), str) or not Path(config["reverseTokenFile"]).is_absolute():
                raise ValueError("reverseTokenFile 需要专用凭据文件的绝对路径")
            index = config.get("historyIndex")
            if index is not None and (not isinstance(index, str) or not Path(index).is_absolute()):
                raise ValueError("historyIndex 需要 SnowLuma 消息索引的绝对路径")

    def start(self, context):
        self.validate_config(context.config)
        self.ctx = context
        self.config = context.config
        self.transport = None
        self.last_input_error = None
        self.received = self.sent = 0
        if self.config.get("mode") == "onebot_reverse":
            token_path = Path(self.config["reverseTokenFile"])
            with token_path.open(encoding="ascii") as stream:
                token = stream.read(4097).strip()
            self.transport = ReverseTransport(dict(self.config, reverseToken=token),
                                              self._receive, context.logger)
            self.transport.start()
        context.services.provide("message_bridge", self)
        context.http.register("GET", "/status", lambda _: self.status())

    def _receive(self, event):
        try:
            message = owner_message(event, self.config["botId"], self.config["ownerId"])
            if message is not None:
                result = self.ctx.services.get("assistant").receive(message)
                if result["accepted"]:
                    self.received += 1
                self.last_input_error = None
        except PlatformError as exc:
            if exc.code not in ("assistant_not_running", "service_unavailable"):
                self.last_input_error = exc.code
                self.ctx.logger.warning("私聊输入未接纳（%s）", exc.code)
        except Exception as exc:
            self.last_input_error = type(exc).__name__
            self.ctx.logger.error("私聊输入未接纳（%s）", self.last_input_error)

    def pull_unread(self, stopped_at, started_at, *, cancel_event=None):
        if stopped_at is None:
            return []
        if not self.transport:
            raise disconnected()
        deadline = time.monotonic() + 5
        while not self.transport.connected.wait(.1):
            if cancel_event is not None and cancel_event.is_set():
                raise PlatformError("onebot_cancelled", "助手已中断启动补收", 409)
            if time.monotonic() >= deadline:
                raise disconnected()
        def call(action, params):
            return self.transport.call(action, params, cancel_event=cancel_event)
        if self.transport.stopping.is_set():
            raise disconnected()
        return collect(call, self.config["botId"], self.config["ownerId"],
                       stopped_at, started_at, self.config.get("historyIndex"))

    def send(self, message, text):
        if not self.transport:
            raise disconnected()
        owner, bot = self.config["ownerId"], self.config["botId"]
        if message.get("peer") != owner or message.get("channel") != "onebot:" + bot:
            raise PlatformError("onebot_wrong_recipient", "回复消息所属会话与绑定不一致", 400)
        if not isinstance(text, str) or not text.strip():
            raise PlatformError("empty_reply", "渠道回复缺少正文")
        data = self.transport.call("send_private_msg", {"user_id": int(owner),
                                   "message": [{"type": "text", "data": {"text": text}}], "auto_escape": True})
        receipt = identifier(data.get("message_id") if isinstance(data, dict) else None)
        self.sent += 1
        return {"id": receipt}

    def status(self):
        return {"mode": self.config.get("mode", "offline"), "connected": bool(self.transport and self.transport.connected.is_set()),
                "received": self.received, "sent": self.sent, "last_input_error": self.last_input_error,
                "connection_error": self.transport.last_error if self.transport else None}

    def stop(self):
        if self.transport:
            self.transport.close()
