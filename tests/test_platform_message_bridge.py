"""Protocol-level regressions for the platform's owner-private transport."""
import base64
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/xixi-core"), str(ROOT)]
from plugins.message_bridge.history import collect, owner_message
from plugins.message_bridge.plugin import Plugin
from xixi_core.platform.app import CoreApp
from xixi_core.platform.errors import PlatformError


def event(mid, stamp, *, sender=11, text="test", parts=None):
    return dict(post_type="message", message_type="private", self_id=22, user_id=sender,
                message_id=mid, time=stamp, message=parts if parts is not None else
                [{"type": "text", "data": {"text": text}}])


class HistoryTests(unittest.TestCase):
    def test_owner_can_be_configured_before_connecting(self):
        Plugin.validate_config({"mode": "offline", "ownerId": "33"})
        Plugin.validate_config({"mode": "offline", "ownerId": "", "botId": ""})
        for owner in ("any", "0", "011", 33):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                Plugin.validate_config({"mode": "offline", "ownerId": owner})
        with self.assertRaises(ValueError):
            Plugin.validate_config({"mode": "offline", "ownerId": "22", "botId": "22"})

    def test_unconfigured_sources_and_no_history_never_mean_empty(self):
        first = lambda *_: self.fail("first boot must not read account history")
        self.assertEqual(collect(first, "22", "11", None, 100), [])
        with self.assertRaisesRegex(PlatformError, "未能覆盖"):
            collect(lambda *_: {"messages": []}, "22", "11", 99, 100)
        with self.assertRaisesRegex(PlatformError, "无法证实"):
            collect(lambda *_: {"messages": [event(1, 99)]}, "22", "11", 99, 100)
        history = {"messages": [event(2, 91), event(3, 99, sender=22)]}
        self.assertEqual(collect(lambda *_: history, "22", "11", 98, 100), [])

    def test_anchor_overlap_keeps_order_and_includes_second_resolution_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "messages.sqlite"
            db = sqlite3.connect(index)
            db.execute("CREATE TABLE messages(message_hash, is_group, session_id, sequence_authoritative, sequence)")
            db.execute("INSERT INTO messages VALUES(77,0,11,1,30)")
            db.commit()
            db.close()
            # Identifiers are opaque. Their numerical order differs from channel order.
            pages = {0: [event(77, 104), event(12, 105, sender=22), event(-9, 106)],
                     77: [event(53, 98), event(13, 100), event(77, 104)]}
            calls = []
            def pull(action, params):
                self.assertEqual(action, "get_friend_msg_history")
                calls.append(params["message_id"])
                return {"messages": pages[params["message_id"]]}
            items = collect(pull, "22", "11", 100.8, 106.3, index, page_size=3)
            self.assertEqual(calls, [0, 77])
            self.assertEqual([x["id"] for x in items], ["13", "77", "-9"])
            pages[77] = [event(2, 98)]
            with self.assertRaisesRegex(PlatformError, "缺少请求的消息锚点"):
                collect(pull, "22", "11", 100, 106, index, page_size=3)
            pages[77] = pages[0]
            with self.assertRaisesRegex(PlatformError, "没有继续向前"):
                collect(pull, "22", "11", 100, 106, index, page_size=3)

    def test_only_bound_private_input_including_attachment_only(self):
        parts = [{"type":"image", "data":{"file":"fixture-ref"}}]
        original = event(56, 100, parts=parts)
        value = owner_message(original, "22", "11")
        self.assertEqual((value["channel"], value["peer"], value["text"], value["attachments"]),
                         ("onebot:22", "11", "", parts))
        for patch in ({"post_type":"message_sent"}, {"user_id":22}, {"self_id":44}, {"message_type":"group"}):
            self.assertIsNone(owner_message({**original, **patch}, "22", "11"))


def wait_for(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError("timed out waiting for isolated transport")


class Peer:
    """In-memory QQ account on a real localhost reverse WebSocket."""
    def __init__(self, port, token, on_action):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.send_lock = threading.Lock()
        self.errors = []
        self.on_action = on_action
        request = ("GET /xixi-onebot HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " +
                   base64.b64encode(os.urandom(16)).decode() + "\r\nAuthorization: Bearer " + token +
                   "\r\nX-Self-ID: 22\r\nX-Client-Role: Universal\r\n\r\n")
        self.sock.sendall(request.encode("ascii"))
        response = b""
        while not response.endswith(b"\r\n\r\n"):
            response += self.exact(1)
        assert b"101 Switching Protocols" in response
        self.sock.settimeout(None)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def exact(self, size):
        data = bytearray()
        while len(data) < size:
            chunk = self.sock.recv(size-len(data))
            if not chunk:
                raise EOFError()
            data.extend(chunk)
        return bytes(data)

    def send(self, frame):
        data = json.dumps(frame).encode()
        size, mask = len(data), os.urandom(4)
        length = bytes([128 | size]) if size < 126 else bytes([254]) + struct.pack("!H", size)
        with self.send_lock:
            self.sock.sendall(b"\x81" + length + mask + bytes(x ^ mask[i % 4] for i,x in enumerate(data)))

    def _loop(self):
        try:
            while True:
                _, size = self.exact(2)
                if size in (126, 127):
                    size = int.from_bytes(self.exact(2 if size==126 else 8), "big")
                request = json.loads(self.exact(size))
                data = self.on_action(request["action"], request["params"])
                self.send({"status":"ok", "retcode":0, "echo":request["echo"], "data":data})
        except (EOFError, OSError):
            pass
        except BaseException as exc:
            self.errors.append(exc)

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.thread.join(3)


class TransportTests(unittest.TestCase):
    def test_wire_and_plugin_reload_release_socket_and_revoke_handle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "test-only-" + os.urandom(16).hex()
            secret = root / "token"
            secret.write_text(token, encoding="ascii")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1",0))
                port = probe.getsockname()[1]
            config = {"mode":"onebot_reverse","ownerId":"11","botId":"22","listenHost":"127.0.0.1",
                      "listenPort":port,"allowedPeerIp":"127.0.0.1","reverseTokenFile":str(secret)}
            path = root/"platform.json"
            path.write_text(json.dumps({"plugins_dir": str(ROOT/"plugins"), "modules":{
                "message_bridge":{"enabled":True,"config":config}}}), encoding="utf-8")
            with CoreApp(path,root/"data",builtins={}) as app:
                service = app.services.get("message_bridge")
                sends = []
                def act(action, params):
                    if action == "get_login_info":
                        return {"user_id":22}
                    if action == "get_friend_msg_history":
                        return {"messages":[event(8,90),event(-11,100)]}
                    self.assertEqual(action,"send_private_msg")
                    sends.append(params)
                    return {"message_id":-12}
                client = Peer(port,token,act)
                try:
                    wait_for(lambda: service.status()["connected"])
                    messages = service.pull_unread(97.1,101.0)
                    self.assertEqual([m["id"] for m in messages],["-11"])
                    self.assertEqual(service.send(messages[0],"[CQ:image]"), {"id":"-12"})
                    self.assertEqual(sends[0]["message"],[{"type":"text","data":{"text":"[CQ:image]"}}])
                    app.manager.update_config("message_bridge", {**config, "ownerId": "33"})
                    self.assertEqual(app.manager.reload("message_bridge")["state"],"running")
                    with self.assertRaises(PlatformError):
                        service.status()
                    self.assertFalse(app.services.get("message_bridge").status()["connected"])
                    self.assertFalse(client.errors,client.errors)
                finally:
                    client.close()
                def new_owner_action(action, params):
                    if action == "get_login_info":
                        return {"user_id": 22}
                    self.assertEqual(params["user_id"], 33)
                    if action == "get_friend_msg_history":
                        return {"messages": [event(10, 90, sender=33), event(11, 100, sender=33)]}
                    self.assertEqual(action, "send_private_msg")
                    return {"message_id": 12}
                client = Peer(port, token, new_owner_action)
                try:
                    rebound = app.services.get("message_bridge")
                    wait_for(lambda: rebound.status()["connected"])
                    current, = rebound.pull_unread(97, 101)
                    self.assertEqual(current["peer"], "33")
                    self.assertIsNone(owner_message(event(13, 100, sender=11), "22", "33"))
                    self.assertEqual(rebound.send(current, "new owner"), {"id": "12"})
                    with self.assertRaisesRegex(PlatformError, "绑定不一致"):
                        rebound.send(messages[0], "old queued reply")
                    self.assertFalse(client.errors, client.errors)
                finally:
                    client.close()
