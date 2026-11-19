"""Service and HTTP behavior of the assistant against controlled local dependencies."""
from contextlib import closing
import json
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "xixi-core"))

from xixi_core.platform.app import CoreApp
from xixi_core.platform.errors import PlatformError
from xixi_core.modules.persona import PersonaStore


def message(message_id, *, when=None, text=None, attachments=None):
    return {"id": message_id, "channel": "test", "peer": "owner", "time": time.time() if when is None else when,
            "text": message_id if text is None else text, "attachments": attachments or []}


class MessageBridge:
    def __init__(self):
        self.windows = []
        self.sent = []
        self.puller = lambda _stop, _start: []

    def pull_unread(self, stopped_at, started_at, *, cancel_event=None):
        self.cancel_event = cancel_event
        self.windows.append((stopped_at, started_at))
        return self.puller(stopped_at, started_at)

    def send(self, incoming, reply):
        self.sent.append((incoming["id"], reply))
        return {"id": f"reply-{incoming['id']}"}


class DshBridge:
    def __init__(self):
        self.sessions = []
        self.calls = []
        self.blocks = {}
        self.fail_next_open = False

    def block(self, kind):
        entered, release = threading.Event(), threading.Event()
        self.blocks[kind] = (entered, release)
        return entered, release

    def open_session(self, current=None, *, kind="daily", context=None):
        if current is None:
            if self.fail_next_open and kind == "daily":
                self.fail_next_open = False
                raise RuntimeError("新会话创建失败")
            current = f"session-{len(self.sessions) + 1}"
            self.sessions.append(current)
        if current not in self.sessions:
            raise RuntimeError("当前会话不可打开")
        return current

    def execute(self, session, messages, *, kind="chat", prompt="", context=None, cancel_event=None):
        self.calls.append((session, kind, messages, prompt, context))
        blocked = self.blocks.pop(kind, None)
        if blocked:
            entered, release = blocked
            entered.set()
            deadline = time.monotonic() + 8
            while not release.wait(0.01):
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("模拟取消执行")
                if time.monotonic() >= deadline:
                    raise RuntimeError("测试未释放执行")
        if kind == "handoff":
            return "已核对需交接的事项"
        if kind == "dream":
            source = context["sourceMessages"][0]["id"]
            return json.dumps({"memories": [{"title": "已知事实", "body": "主人说过的话", "category": "事实与经历",
                                             "sourceRefs": [source] if source else []}],
                               "diary": {"title": "今日回顾", "body": "日记正文"}}, ensure_ascii=False)
        return "回复 " + "/".join(item["id"] for item in messages)


class Library:
    def __init__(self):
        self.archived = []
        self.published = []
        self.timezone = timezone.utc
        self.diaries = {}

    def archive_chat(self, incoming, reply, *, reply_time=None):
        self.archived.append((incoming, reply))
        return {"input": {"recordId": f"archive-{incoming['id']}"},
                "reply": {"recordId": f"archive-reply-{incoming['id']}"} if reply else None}

    def dream_diary(self, day):
        existing = self.diaries.get(day)
        return {"day": day, "existingDiary": existing, "diaryExpected": existing["version"] if existing else 0}

    def dream_memories(self):
        return [{"id": "memory-1", "title": "已有记忆", "version": 2}]

    @staticmethod
    def validate_dream_output(memories, diary, *, existing_memories, source_ids):
        if not isinstance(memories, list) or not diary.get("body"):
            raise ValueError("记忆或日记格式错误")
        if any(not set(item.get("sourceRefs", [])) <= set(source_ids) for item in memories):
            raise ValueError("来源超出固定范围")
        return memories

    def publish_dream(self, memories, diary, *, request_id, source_ids, existing_memories=None):
        self.published.append((memories, diary, source_ids, request_id))


def provider(name, service):
    class Module:
        def start(self, context):
            context.services.provide(name, service)

        def stop(self):
            pass
    return Module


class AssistantAcceptance(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xixi-assistant-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(ROOT / "plugins" / "assistant", self.root / "plugins" / "assistant")
        self.cfg = self.root / "platform.json"
        self.data = self.root / "data"
        self.messages, self.dsh, self.library = MessageBridge(), DshBridge(), Library()
        self.builtins = {"message_bridge": provider("message_bridge", self.messages),
                         "dsh_bridge": provider("dsh_bridge", self.dsh), "library": provider("library", self.library)}
        self.config = {"http": {"host": "127.0.0.1", "port": 0}, "plugins_dir": "plugins", "modules": {
            name: {"enabled": True, "config": {}} for name in (*self.builtins, "assistant")}}
        self.config["modules"]["assistant"]["config"].update(automaticHandoff=False, automaticDream=False,
                                                               maxBatchMessages=1)
        self.cfg.write_text(json.dumps(self.config), encoding="utf-8")
        self.open_app()

    def open_app(self):
        self.app = CoreApp(self.cfg, self.data, builtins=self.builtins)
        self.addCleanup(self.app.close)
        self.app.start()

    def call(self, path, method="POST", request=None):
        return self.app.manager.dispatch_http("assistant", method, path, request)

    def status(self):
        return self.call("/status", "GET")

    def wait_for(self, predicate):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"助手状态未在时限内达到预期：{self.status()}")

    def idle(self):
        def is_idle():
            status = self.status()
            return not (status["activity"] or status["requested"] or status["queue_depth"])
        self.wait_for(is_idle)

    def start_receiving(self):
        self.assertEqual(self.status()["state"], "not_started")
        self.assertEqual(self.call("/start")["state"], "running")
        self.idle()

    def receive(self, incoming):
        return self.app.services.get("assistant").receive(incoming)

    def history(self):
        return [(kind, [item["id"] for item in inputs]) for _, kind, inputs, _, _ in self.dsh.calls]

    def test_stop_drains_current_and_pull_orders_old_unread_and_realtime(self):
        with self.assertRaises(PlatformError) as stopped:
            self.receive(message("before"))
        self.assertEqual(stopped.exception.code, "assistant_not_running")
        self.start_receiving()
        self.assertEqual(self.status()["startup_unread"]["state"], "first_start")
        self.assertEqual(len(self.messages.windows), 1)
        self.assertIsNone(self.messages.windows[0][0])
        self.assertEqual(self.dsh.calls, [])

        entered, release = self.dsh.block("chat")
        self.addCleanup(release.set)
        self.assertTrue(self.receive(message("in-flight"))["accepted"])
        self.assertTrue(entered.wait(3))
        self.receive(message("ordinary"))
        self.assertEqual(self.call("/stop")["state"], "stopping")
        with self.assertRaises(PlatformError):
            self.receive(message("while-stopping"))
        release.set()
        self.wait_for(lambda: self.status()["state"] == "not_started")
        self.assertEqual(self.history(), [("chat", ["in-flight"])])
        self.assertEqual(self.status()["queue_depth"], 1)
        self.assertEqual(self.messages.sent, [("in-flight", "回复 in-flight")])

        pull_entered, pull_release = threading.Event(), threading.Event()
        self.addCleanup(pull_release.set)
        original_attachment = [{"type": "image", "reference": "pulled-copy"}]

        def pull(stopped_at, started_at):
            self.assertLessEqual(stopped_at, started_at)
            pull_entered.set()
            if not pull_release.wait(4):
                raise RuntimeError("测试未释放补收")
            second = math.floor(started_at)
            return [message("late-unread", when=second + 0.8),
                    message("ordinary", when=second + 0.5),
                    message("shared", when=second + 0.2, attachments=original_attachment),
                    message("shared", when=second + 0.2, attachments=original_attachment)]

        self.messages.puller = pull
        self.call("/start")
        self.assertTrue(pull_entered.wait(3))
        self.receive(message("realtime"))
        self.receive(message("shared", attachments=[{"type": "image", "reference": "realtime-copy"}]))
        self.assertEqual(self.status()["queue_depth"], 3)
        self.assertEqual(len(self.dsh.calls), 1)
        pull_release.set()
        self.idle()
        self.assertEqual(self.history(), [("chat", ["in-flight"]), ("chat", ["ordinary"]),
                                         ("startup_unread", ["shared"]), ("startup_unread", ["late-unread"]),
                                         ("chat", ["realtime"])])
        self.assertEqual(self.dsh.sessions, ["session-1"])
        self.assertEqual(self.dsh.calls[2][2][0]["attachments"], original_attachment)
        self.assertEqual([self.dsh.calls[index][4]["batch_number"] for index in (2, 3)], [1, 2])
        self.assertFalse(self.dsh.calls[2][4]["shutdown"]["estimated"])
        self.assertIn("停机期间", self.dsh.calls[2][3])
        self.assertEqual(self.dsh.calls[4][3:], ("", {}))
        self.assertEqual(self.receive(message("shared")), {"accepted": False, "duplicate": True})

    def test_failure_unblocks_realtime_and_explicit_retry_uses_saved_window(self):
        self.start_receiving()
        self.call("/stop")
        failed, released = threading.Event(), threading.Event()
        self.addCleanup(released.set)

        def fail(_stop, _start):
            failed.set()
            released.wait(4)
            raise RuntimeError("消息页不完整")

        self.messages.puller = fail
        self.call("/start")
        self.assertTrue(failed.wait(3))
        self.receive(message("now"))
        released.set()
        self.idle()
        status = self.status()
        self.assertEqual(status["startup_unread"]["state"], "failed")
        self.assertIn("消息页不完整", status["startup_unread"]["error"])
        self.assertTrue(status["startup_unread_retry_available"])
        self.assertEqual(self.history(), [("chat", ["now"])])
        self.assertEqual(len(self.messages.windows), 2)

        self.messages.puller = lambda _stop, start: [message("late", when=start)]
        self.call("/startup-unread")
        self.idle()
        self.assertEqual(self.messages.windows[2], self.messages.windows[1])
        self.assertEqual(self.history()[-1], ("startup_unread", ["late"]))
        self.assertFalse(self.status()["startup_unread_retry_available"])

    def test_handoff_commits_only_after_new_session_works(self):
        self.start_receiving()
        self.receive(message("intro"))
        self.idle()
        entered, release = self.dsh.block("handoff")
        self.addCleanup(release.set)
        self.call("/handoff")
        self.assertTrue(entered.wait(3))
        self.receive(message("waits-for-new"))
        self.assertEqual(self.status()["activity"], "handoff")
        self.assertEqual(self.status()["session_id"], "session-1")
        self.assertEqual(len(self.dsh.calls), 2)
        release.set()
        self.idle()
        self.assertEqual(self.dsh.calls[-1][0], "session-3")
        self.assertEqual(self.dsh.calls[-1][3:], ("", {"handoff": "已核对需交接的事项"}))

        entered, release = self.dsh.block("handoff")
        self.addCleanup(release.set)
        self.dsh.fail_next_open = True
        self.call("/handoff")
        self.assertTrue(entered.wait(3))
        self.receive(message("waits-for-old"))
        release.set()
        self.idle()
        self.assertEqual(self.status()["session_id"], "session-3")
        self.assertEqual(self.dsh.calls[-1][0], "session-3")
        self.assertEqual(self.dsh.calls[-1][3:], ("", {}))
        self.assertEqual(self.status()["last_error"]["operation"], "handoff")

    def test_large_handoff_carries_selected_source_across_review_sessions(self):
        self.start_receiving()
        first = message("first")
        first["text"] = "a" * 35000
        second = {**message("second"), "text": "b" * 35000}
        for row in (first, second):
            self.receive(row)
            self.idle()
        review_batches = []
        execute = self.dsh.execute

        def summarize(session, messages, **kwargs):
            if kwargs["kind"] == "handoff":
                context = kwargs["context"]
                review_batches.append(context)
                return json.dumps({"handoff": "继续", "selectedOriginals": ["archive-first"]})
            return execute(session, messages, **kwargs)

        self.dsh.execute = summarize
        self.call("/handoff")
        self.idle()
        self.assertEqual(len(review_batches), 2)
        self.assertEqual(review_batches[1]["priorHandoff"], "继续")
        self.assertEqual([r["id"] for r in review_batches[1]["sourceMessages"]],
                         ["archive-first", "archive-second", "archive-reply-second"])
        self.receive(message("third"))
        self.idle()
        self.assertEqual(self.dsh.calls[-1][4]["selectedOriginals"][0]["text"], first["text"])

    def test_dream_publishes_memory_and_diary_and_serializes_chat(self):
        self.start_receiving()
        self.receive(message("first"))
        self.idle()
        entered, release = self.dsh.block("dream")
        self.addCleanup(release.set)
        self.call("/dream")
        self.assertTrue(entered.wait(3))
        self.receive(message("after"))
        self.assertEqual(self.status()["activity"], "dream")
        self.assertEqual(self.status()["queue_depth"], 1)
        self.assertEqual(self.messages.sent, [("first", "回复 first")])
        release.set()
        self.idle()
        memories, diary, source_ids, _ = self.library.published[0]
        self.assertEqual(memories[0]["sourceRefs"], ["archive-first"])
        self.assertEqual(diary["body"], "日记正文")
        self.assertEqual(source_ids, ["archive-first", "archive-reply-first"])
        self.assertEqual(self.dsh.calls[1][4]["existingMemories"][0]["version"], 2)
        self.assertEqual(self.dsh.calls[1][0], "session-2")
        self.assertEqual(self.dsh.calls[2][0], "session-1")
        self.assertEqual(self.dsh.calls[2][3:], ("", {}))
        self.assertEqual(self.status()["next_dream_at"] - self.status()["last_dream_at"], 12 * 3600)
        with self.assertRaises(PlatformError) as too_soon:
            self.call("/dream")
        self.assertEqual(too_soon.exception.code, "dream_interval")

    def test_failed_dream_releases_normal_queue_on_original_session(self):
        self.start_receiving()
        self.receive(message("before"))
        self.idle()
        original = self.status()["session_id"]
        entered, release = self.dsh.block("dream")
        self.addCleanup(release.set)
        self.call("/dream")
        self.assertTrue(entered.wait(3))
        self.receive(message("during"))
        self.assertEqual(self.app.manager.stop("library")["state"], "stopped")
        release.set()
        self.idle()
        self.assertEqual(self.status()["last_error"]["operation"], "dream")
        self.assertEqual(self.status()["session_id"], original)
        self.assertEqual(self.dsh.calls[-1][0], original)
        self.assertEqual(self.dsh.calls[-1][3:], ("", {}))
        self.assertEqual(self.messages.sent[-1], ("during", "回复 during"))

    def test_dream_uses_original_local_day_and_keeps_fixed_material_on_correction(self):
        self.start_receiving()
        day = datetime.now(self.library.timezone).date() - timedelta(days=2)
        stamp = datetime.combine(day, datetime.min.time(), self.library.timezone).timestamp() + 3600
        self.library.diaries[day.isoformat()] = {"id": "existing", "version": 3, "title": "该日已有记录", "body": "昨日已有日记"}
        self.receive(message("old-owner", when=stamp))
        self.idle()
        self.receive(message("today-owner"))
        self.idle()

        execute = self.dsh.execute
        attempts = []

        def make_one_validation_error(session, messages, **kwargs):
            if kwargs["kind"] == "dream":
                attempts.append((session, messages, kwargs["prompt"], kwargs["context"]))
                if len(attempts) == 1:
                    return json.dumps({"memories": [{"sourceRefs": ["outside"]}], "diary": {"body": "bad"}})
            return execute(session, messages, **kwargs)

        self.dsh.execute = make_one_validation_error
        self.call("/dream")
        self.idle()
        self.assertEqual(len(attempts), 2)
        initial, retry = attempts
        self.assertEqual(initial[0], retry[0])
        self.assertEqual(initial[1], retry[1])
        self.assertEqual(initial[3], retry[3])
        self.assertIn("来源超出固定范围", retry[2])
        self.assertEqual(initial[3]["day"], day.isoformat())
        self.assertEqual(initial[3]["existingDiary"]["version"], 3)
        self.assertEqual([source["id"] for source in initial[3]["sourceMessages"]], ["archive-old-owner"])
        self.assertEqual(self.library.published[0][1]["expectedVersion"], 3)
        self.assertEqual(len(self.library.published), 1)

        # Simulate the next permitted dream without sleeping overnight.
        self.assertEqual(self.app.close(), [])
        database = self.data / "modules" / "assistant" / "assistant.sqlite3"
        with closing(sqlite3.connect(database)) as db, db:
            progress = json.loads(db.execute("SELECT data FROM meta WHERE key='dream_coverage:UTC'").fetchone()[0])
            self.assertEqual(progress["floor"], 0, "yesterday's reply still needs today's review")
            db.execute("UPDATE meta SET data=? WHERE key='last_dream_at'", (json.dumps(time.time() - 13 * 3600),))
        self.open_app()
        self.idle()
        self.call("/dream")
        self.idle()
        self.assertEqual(len(attempts), 3)
        self.assertEqual([source["id"] for source in attempts[-1][3]["sourceMessages"]],
                         ["archive-reply-old-owner", "archive-today-owner", "archive-reply-today-owner"])
        self.assertEqual(len(self.library.published), 2)
        self.assertEqual(self.app.close(), [])
        with closing(sqlite3.connect(database)) as db:
            progress = json.loads(db.execute("SELECT data FROM meta WHERE key='dream_coverage:UTC'").fetchone()[0])
        self.assertEqual(progress["floor"], 2)
        self.assertEqual(progress["days"], {})

    def test_handoff_material_preserves_reply_time_and_channel(self):
        self.start_receiving()
        incoming = message("original")
        incoming["replyTo"] = {"messageId": "quoted", "text": "真实引用"}
        self.receive(incoming)
        self.idle()
        self.call("/handoff")
        self.idle()
        source, reply = next(call[4]["sourceMessages"] for call in self.dsh.calls if call[1] == "handoff")
        self.assertEqual(source["replyTo"], incoming["replyTo"])
        self.assertEqual(source["channel"], incoming["channel"])
        self.assertGreaterEqual(datetime.fromisoformat(reply["sentAt"]).timestamp(), incoming["time"])
        self.assertEqual(reply["replyTo"], "archive-original")

    def test_abnormal_restart_keeps_session_and_never_replays_inflight(self):
        self.start_receiving()
        self.receive(message("old"))
        self.idle()
        self.assertEqual(self.app.close(), [])
        database = self.data / "modules" / "assistant" / "assistant.sqlite3"
        heartbeat = time.time() - 8
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("UPDATE meta SET data=? WHERE key='runtime'", (json.dumps({
                "started_at": heartbeat - 60, "last_alive_at": heartbeat, "stopped_at": None}),))
            db.execute("UPDATE messages SET status='started' WHERE message_id='old'")
        self.messages.puller = lambda stop, _start: [message("old", when=stop), message("after-crash", when=stop + 1)]
        self.open_app()
        self.idle()
        self.assertEqual(self.status()["state"], "running")
        self.assertEqual(self.history(), [("chat", ["old"]), ("startup_unread", ["after-crash"])])
        self.assertEqual(self.dsh.calls[-1][0], "session-1")
        window = self.dsh.calls[-1][4]["shutdown"]
        self.assertTrue(window["estimated"])
        self.assertEqual(window["stopped_at"], heartbeat)
        self.call("/stop")
        self.assertEqual(self.app.close(), [])
        self.open_app()
        self.assertEqual(self.status()["state"], "not_started")
        self.assertEqual(len(self.messages.windows), 2)

    def test_prompt_override_persists_and_is_applied_only_to_unread(self):
        class Request:
            def get_json(self):
                return {"text": "这次只附加给停机来信"}

        self.start_receiving()
        self.call("/stop")
        expected = self.call("/startup-prompt", "PUT", Request())
        old_service = self.app.services.get("assistant")
        self.assertEqual(self.app.manager.reload("assistant")["state"], "running")
        with self.assertRaises(PlatformError):
            old_service.status()
        self.assertEqual(self.status()["state"], "not_started")
        self.assertEqual(self.call("/startup-prompt", "GET"), expected)
        self.messages.puller = lambda _stop, start: [message("unread", when=start)]
        self.call("/start")
        self.idle()
        self.receive(message("ordinary"))
        self.idle()
        self.assertEqual(self.dsh.calls[0][3], expected["text"])
        self.assertEqual(self.dsh.calls[1][3], "")
        self.assertEqual(len([t for t in threading.enumerate() if t.name == "xixi-assistant"]), 1)

    def test_reload_cancels_current_and_leaves_waiting_input_for_new_worker(self):
        self.start_receiving()
        entered, release = self.dsh.block("chat")
        self.addCleanup(release.set)
        self.receive(message("cancelled"))
        self.assertTrue(entered.wait(3))
        self.receive(message("pending"))
        self.assertEqual(self.app.manager.reload("assistant")["state"], "running")
        self.idle()
        self.assertEqual(self.history(), [("chat", ["cancelled"]), ("chat", ["pending"])])
        self.assertEqual(self.messages.sent, [("pending", "回复 pending")])
        self.assertEqual(self.status()["session_id"], "session-1")

    def test_interrupted_and_skipped_input_notice_waits_for_matching_real_owner_and_is_consumed(self):
        self.assertEqual(self.app.close(), [])
        persona = PersonaStore(self.root / "isolated-persona.sqlite3")
        self.addCleanup(persona.close)
        item = persona.injections.get("event.recovery")
        persona._set_injection({"id": item["id"], "expectedVersion": item["version"],
                               "body": "实际恢复 {restartSummary}{recoverySummary}\n{recoveryDetails}"})
        self.builtins["persona"] = provider("persona", persona)
        self.config["modules"]["persona"] = {"enabled": True, "config": {}}
        self.cfg.write_text(json.dumps(self.config), encoding="utf-8")
        self.open_app()
        self.start_receiving()
        entered, release = self.dsh.block("chat")
        self.addCleanup(release.set)
        self.receive(message("interrupted-source"))
        self.assertTrue(entered.wait(3))
        self.receive(message("explicitly-skipped"))
        assistant = self.app.services.get("assistant")
        self.assertEqual(assistant.skip_input({"channel": "test", "peer": "owner", "id": "explicitly-skipped"})["state"], "skipped")
        self.assertEqual(self.app.manager.reload("assistant")["state"], "running")
        self.idle()
        self.assertEqual(self.history(), [("chat", ["interrupted-source"])])
        self.assertEqual(self.messages.sent, [])

        self.app.services.get("assistant").publish_system_event({"kind": "notice", "channel": "test", "peer": "owner",
                                                                "id": "non-owner", "text": "此轮不是主人来信"})
        self.wait_for(lambda: len(self.dsh.calls) == 2)
        self.idle()
        self.assertNotIn("inputRecoveryText", self.dsh.calls[-1][4])
        self.receive({**message("other-owner"), "peer": "another"})
        self.idle()
        self.assertNotIn("inputRecoveryText", self.dsh.calls[-1][4])
        self.receive(message("matching-owner"))
        self.idle()
        prompt = self.dsh.calls[-1][4]["inputRecoveryText"]
        self.assertEqual(prompt.count("实际恢复"), 1)
        self.assertIn("interrupted-source", prompt)
        self.assertIn("explicitly-skipped", prompt)
        self.receive(message("following-owner"))
        self.idle()
        self.assertNotIn("inputRecoveryText", self.dsh.calls[-1][4])

    def test_reload_cancels_pull_and_releases_held_message_once(self):
        self.start_receiving()
        self.call("/stop")
        entered, cancelled = threading.Event(), threading.Event()

        def pull(_stop, _start):
            entered.set()
            if not self.messages.cancel_event.wait(7):
                raise RuntimeError("重载未取消正在运行的拉取")
            cancelled.set()
            raise RuntimeError("模拟取消拉取")

        self.messages.puller = pull
        self.call("/start")
        self.assertTrue(entered.wait(3))
        self.receive(message("held"))

        def resume(_stop, start):
            self.app.services.get("assistant")
            return [message("held", when=start)]

        self.messages.puller = resume
        self.assertEqual(self.app.manager.reload("assistant")["state"], "running")
        self.assertTrue(cancelled.is_set())
        self.idle()
        self.assertEqual(self.history(), [("chat", ["held"])])
        self.assertEqual(self.status()["startup_unread"]["state"], "empty")
        self.assertTrue(self.status()["startup_unread_retry_available"])

    def test_missing_current_session_fails_without_replacing_it(self):
        self.start_receiving()
        self.receive(message("first"))
        self.idle()
        self.dsh.sessions.clear()
        self.receive(message("same-session"))
        self.idle()
        self.assertEqual(self.status()["session_id"], "session-1")
        self.assertIn("当前会话不可打开", self.status()["last_error"]["message"])
        self.assertEqual(self.history(), [("chat", ["first"])])
        self.assertEqual(self.dsh.sessions, [])

    def test_automatic_idle_handoff_and_dream(self):
        self.app.manager.update_config("assistant", {"automaticHandoff": True, "handoffIdleSeconds": 1,
                                                      "handoffMinTurns": 1, "automaticDream": True, "dreamIdleSeconds": 1})
        self.assertEqual(self.app.manager.reload("assistant")["state"], "running")
        self.start_receiving()
        self.receive(message("first"))
        self.wait_for(lambda: len(self.dsh.calls) >= 3)
        self.idle()
        self.assertEqual(self.history(), [("chat", ["first"]), ("dream", ["first"]), ("handoff", [])])
        self.assertEqual(len(self.library.published), 1)

    def test_library_can_be_disabled_while_assistant_replies(self):
        self.start_receiving()
        self.assertEqual(self.app.manager.stop("library")["state"], "stopped")
        self.receive(message("ordinary"))
        self.idle()
        self.assertEqual(self.messages.sent, [("ordinary", "回复 ordinary")])
        self.assertEqual(self.status()["state"], "running")
        self.assertEqual(self.status()["last_archive_error"]["operation"], "archive_chat")
        self.assertEqual(self.app.manager.start("library")["state"], "running")
        self.receive(message("archived"))
        self.idle()
        self.assertEqual([entry[0]["id"] for entry in self.library.archived], ["archived"])


if __name__ == "__main__":
    unittest.main()
