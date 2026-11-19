"""Worker persistence and the DSH callback contract without an external model."""
from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from threading import Event
import time
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages" / "xixi-core"), str(ROOT)]

from plugins.assistant.work_events import WorkEvents
from plugins.dsh_bridge.plugin import DshService
from xixi_core.platform.errors import PlatformError


class Services(dict):
    def get(self, service):
        if service not in self:
            raise PlatformError("service_unavailable", "Unavailable test service", 503)
        return self[service]


class SyntheticWorkBridge:
    def __init__(self):
        self.model = "old-model"
        self.calls = []
        self.entered = Event()
        self.release = Event()
        self.artifact_writer = None

    def freeze_work(self, task_id, work):
        return {"specification": {"id": "work-" + task_id, "model": self.model, "kind": "work"}, "text": work["objective"]}

    def run_background_work(self, frozen, *, cancel_event):
        self.calls.append(frozen)
        self.entered.set()
        while not self.release.wait(.01):
            if cancel_event.is_set():
                raise PlatformError("dsh_interrupted", "Canceled in test", 409)
        if self.artifact_writer:
            self.artifact_writer(frozen["specification"]["id"], {"name": "背景结果", "text": "正文"}, "artifact-write")
        return "异" * 5000


class WorkEventsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xixi-work-events-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bridge = SyntheticWorkBridge()
        self.events = []
        self.context = SimpleNamespace(data_dir=self.root, services=Services(dsh_bridge=self.bridge),
                                       logger=logging.getLogger("work-test"))
        self.worker = WorkEvents(self.context, self.events.append)
        self.worker.start()
        self.addCleanup(self.worker.stop)
        self.origin = {"sessionId": "daily-owner", "channel": "test", "peer": "synthetic-owner"}

    def wait(self, test):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if result := test():
                return result
            time.sleep(.02)
        self.fail("Background work did not reach expected state")

    def test_callback_frozen_work_artifact_and_full_paged_result(self):
        worker = self.worker
        bridge = DshService(None, "dsh", "synthetic-token-not-production", self.context, 3)
        self.context.services["assistant"] = worker
        worker.set_running(True)
        callback = {"sessionKind": "daily", "sessionId": self.origin["sessionId"], "requestId": "native-tool-1",
                    "tool": "task_create", "args": {"title": "测试工作", "objective": "独立调查"},
                    "origin": {key: self.origin[key] for key in ("channel", "peer")}}
        created = bridge.handle_tool(callback)["data"]
        self.assertTrue(self.bridge.entered.wait(2))
        self.assertTrue(worker.busy())
        self.assertEqual(created["state"], "queued")
        self.assertEqual(bridge.handle_tool(callback)["data"]["id"], created["id"])
        self.assertFalse(bridge.handle_tool({**callback, "args": {"objective": "修改后的目标"}})["ok"])
        self.bridge.model = "new-model"
        self.bridge.artifact_writer = worker.write_task_artifact
        self.bridge.release.set()
        event = self.wait(lambda: self.events[0] if self.events else None)
        self.assertEqual(self.bridge.calls[0]["specification"]["model"], "old-model")
        self.assertEqual(self.bridge.calls[0]["specification"]["kind"], "work")
        self.assertNotEqual(self.bridge.calls[0]["specification"]["id"], self.origin["sessionId"])
        self.assertEqual((event["channel"], event["peer"]), ("test", "synthetic-owner"))
        result = event["event"]
        self.assertEqual(event["kind"], "task_result")
        self.assertTrue(result["summaryTruncated"])
        first = bridge.handle_tool({**callback, "tool": "task_result_read", "args": {"taskId": created["id"], "limit": 50}})["data"]
        self.assertEqual(first["sha256"], result["sha256"])
        self.assertEqual(first["nextOffset"], 50)
        pages = []
        offset = 0
        while offset is not None:
            page = worker.read_task_result(created["id"], offset, 16000)
            pages.append(page["text"])
            offset = page["nextOffset"]
        self.assertEqual(json.loads("".join(pages))["summary"], "异" * 5000)
        artifact = worker.read_task_artifact({"artifactRef": result["artifactRefs"][0]})
        self.assertEqual(artifact["text"], "正文")
        self.assertNotEqual(artifact["sha256"], result["sha256"])
        self.assertFalse(bridge.handle_tool({**callback, "sessionKind": "work"})["ok"])

    def test_restart_replays_pending_events_and_never_restarts_running_work(self):
        now = time.time()
        future = datetime.fromtimestamp(now + .7, timezone.utc).isoformat()
        wake = {"kind": "wake", "id": "wake-1", "channel": "test", "peer": "synthetic-owner", "text": "提醒测试",
                "event": {"dueAt": future}}
        self.assertFalse(self.worker.publish_system_event(wake)["duplicate"])
        self.assertTrue(self.worker.publish_system_event(wake)["duplicate"])
        self.assertEqual(self.events, [])
        self.worker.set_running(True)
        queued = self.worker.submit_background_task({"objective": "等待重启"}, request_id="before-exit", origin=self.origin)
        self.assertTrue(self.bridge.entered.wait(2))
        self.worker.stop()
        self.assertEqual(len(self.bridge.calls), 1)
        # Simulate a hard exit after admission and before any outcome transaction.
        with closing(sqlite3.connect(self.root / "background.sqlite3")) as database, database:
            database.execute("UPDATE work SET state='running',result=NULL WHERE id=?", (queued["id"],))
            database.execute("DELETE FROM system_events WHERE id=?", ("task-result-" + queued["id"],))
        restarted = WorkEvents(self.context, self.events.append)
        restarted.start()
        self.addCleanup(restarted.stop)
        self.assertEqual(restarted.task_status(queued["id"])["state"], "stopped")
        restarted.set_running(True)
        self.wait(lambda: len(self.events) >= 2)
        self.assertEqual(len([event for event in self.events if event["kind"] == "wake"]), 1)
        self.assertEqual(len([event for event in self.events if event["kind"] == "task_result"]), 1)
        self.assertEqual(len(self.bridge.calls), 1)

    def test_due_reminder_enters_daily_queue_while_background_model_waits(self):
        self.worker.set_running(True)
        queued = self.worker.submit_background_task({"objective": "耗时工作"}, request_id="concurrent", origin=self.origin)
        self.assertTrue(self.bridge.entered.wait(2))
        due = datetime.fromtimestamp(time.time() + .15, timezone.utc).isoformat()
        self.worker.publish_system_event({"kind": "wake", "id": "wake-during-work", "channel": "test",
                                          "peer": "synthetic-owner", "text": "无需等待独立工作结束", "event": {"dueAt": due}})
        message = self.wait(lambda: self.events[0] if self.events else None)
        self.assertEqual(message["kind"], "wake")
        self.assertEqual(self.worker.task_status(queued["id"])["state"], "running")
        self.bridge.release.set()
        self.wait(lambda: len(self.events) == 2)
