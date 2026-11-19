"""Exercise the reusable Library v0.4 database through the platform entry points."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "xixi-core"))

from xixi_core.platform.app import CoreApp
from xixi_core.platform.auth import AuthStore
from xixi_core.platform.errors import PlatformError
from xixi_core.platform.http import create_app


ORIGIN = "http://127.0.0.1:18910"
PASSWORD = "isolated-library-platform-password"
LIBRARY_API = "/api/modules/library/v04/"


class PlatformLibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xixi-platform-library-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.plugin = root / "plugins" / "library"
        shutil.copytree(ROOT / "plugins" / "library", self.plugin, ignore=shutil.ignore_patterns("__pycache__"))
        self.config = root / "platform.json"
        self.config.write_text(json.dumps({"http": {"origin": ORIGIN}, "modules": {
            "console": {"enabled": True}, "library": {"enabled": True}, "system": {"enabled": False},
        }}), encoding="utf-8")
        self.data = root / "data"
        AuthStore.initialize(self.data / "owner.auth.json", password=PASSWORD)
        self.core = CoreApp(self.config, self.data)
        self.addCleanup(self.core.close)
        self.core.start()
        self.client = create_app(self.core).test_client()
        self.csrf = None

    def call(self, path, method="GET", body=None):
        headers = {"Origin": ORIGIN}
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        return self.client.open(path, method=method, json=body, headers=headers, base_url=ORIGIN)

    def login(self):
        self.csrf = self.call("/v1/session").json["csrf"]
        response = self.call("/v1/login", "POST", {"password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.json)
        self.csrf = response.json["csrf"]

    def library(self):
        return self.core.services.get("library")

    def operation(self, name, body, status=200):
        response = self.call(LIBRARY_API + name, "POST", body)
        self.assertEqual(response.status_code, status, response.json)
        return response.json

    def test_original_page_reads_and_edits_memory_and_diary(self):
        self.assertEqual(self.call("/library").status_code, 303)
        self.assertEqual(self.call(LIBRARY_API + "status").status_code, 401)
        self.login()
        page = self.call("/library")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        for text in ("分类与资料", "原始对话", "版本历史", "data-view=\"diary\""):
            self.assertIn(text, html)
        self.assertNotIn("/library/history-old", html)
        for asset in ("markdown.js", "library-v04.css", "library-v04.js"):
            self.assertEqual(self.call("/static/" + asset).status_code, 200)

        payload = {"requestId": "owner-create", "category": "认识与判断", "title": "收到的偏好", "body": "记得用红茶"}
        saved = self.operation("record", payload)
        self.assertEqual(saved, self.operation("record", payload))
        self.assertEqual(self.operation("list", {})["items"][0]["id"], saved["id"])
        self.assertEqual(self.operation("search", {"query": "红茶"})["items"][0]["id"], saved["id"])
        revised = self.operation("revise", {"id": saved["id"], "expectedVersion": 1,
                                            "changes": {"body": "记得用绿茶"}, "requestId": "owner-revise"})
        self.assertEqual(revised["version"], 2)
        old = self.operation("get", {"id": saved["id"], "version": 1})
        self.assertEqual(old["item"]["body"], "记得用红茶")

        diary = self.library().write_diary("日记专用标记 diaryreaderunique", day="2026-11-18")
        self.assertEqual(self.operation("diary", {"day": diary["day"]})["items"][0]["version"], 1)
        revised = self.operation("write_diary", {"day": diary["day"], "expectedVersion": 1,
                                                  "body": "主人补记", "requestId": "owner-diary"})
        self.assertEqual(revised["version"], 2)
        old = self.operation("diary", {"day": diary["day"], "version": 1, "includeVersions": True})
        self.assertEqual(old["items"][0]["body"], diary["body"])
        self.assertEqual([entry["version"] for entry in old["versions"]], [2, 1])
        self.assertEqual(self.library().search("diaryreaderunique"), [])
        self.operation("record", {"category": "兴趣", "body": "缺少请求编号"}, 400)

    def test_owner_editor_preserves_original_long_content_limit(self):
        self.login()
        body = "一个段落。\n" * 30000
        saved = self.operation("record", {"requestId": "large-owner-entry", "category": "兴趣", "body": body})
        self.assertEqual(saved["body"], body)

    def test_dream_commit_rolls_back_and_valid_revision_is_idempotent(self):
        service = self.library()
        day = "2026-11-18"
        diary = {"day": day, "body": "今夜的记录"}
        memories = [{"title": "偏好", "body": "喜欢红茶"}]
        with self.assertRaises(ValueError):
            service.publish_dream(memories + [{"category": "资料", "body": "缺少摘要"}], diary, request_id="failed")
        self.assertEqual(service.list_memories(), [])
        self.assertEqual(service.dispatch("diary", {"day": day})["items"], [])

        published = service.publish_dream(memories, diary, request_id="original")
        self.assertEqual(published, service.publish_dream(memories, diary, request_id="original"))
        original = published["items"][0]
        revision = [{"id": original["id"], "expectedVersion": original["version"], "body": "偏好改为绿茶"}]
        second = service.publish_dream(revision, {**diary, "body": "补记"}, request_id="revision")
        self.assertEqual(second, service.publish_dream(revision, {**diary, "body": "补记"}, request_id="revision"))
        self.assertEqual([entry["version"] for entry in second["items"]], [2, 2])
        service.dispatch("revise", {"id": original["id"], "expectedVersion": 2,
                                    "changes": {"title": "主人修改标题"}}, "later-owner-revision")
        self.assertEqual(second, service.publish_dream(revision, {**diary, "body": "补记"}, request_id="revision"))
        with self.assertRaises(ValueError):
            service.publish_dream(revision, diary, request_id="stale")
        self.assertEqual(service.dispatch("diary", {"day": day})["items"][0]["version"], 2)

    def test_dream_writes_only_revisions_from_its_frozen_full_memory_snapshot(self):
        service = self.library()
        issue = service.dispatch("record", {"category": "认识与判断", "body": "A" * 600 + "完整尾部",
                                            "sourceRefs": ["original-a"]}, "frozen-source")
        outside = service.dispatch("record", {"category": "兴趣", "body": "未展示条目",
                                              "sourceRefs": ["original-b"]}, "frozen-other")
        snapshot = [item for item in service.dream_memories() if item["id"] == issue["id"]]
        self.assertEqual(len(snapshot), 1)
        self.assertTrue(snapshot[0]["body"].endswith("完整尾部"))
        self.assertEqual(snapshot[0]["sourceRefs"], ["original-a"])
        day = "2026-11-13"
        diary = {"day": day, "body": "固定日期回顾", "expectedVersion": 0, "sourceRefs": ["fixed-original"]}
        outside_edit = {"id": outside["id"], "expectedVersion": outside["version"], "body": "超范围"}
        with self.assertRaisesRegex(ValueError, "本批已展示"):
            service.publish_dream([outside_edit], diary, existing_memories=snapshot,
                                  source_ids=["fixed-original"], request_id="reject-other")
        self.assertIsNone(service.dream_diary(day)["existingDiary"])
        within_scope = {"id": issue["id"], "expectedVersion": issue["version"], "body": "新认识"}
        published = service.publish_dream([within_scope], diary, existing_memories=snapshot,
                                          source_ids=["fixed-original"], request_id="good-frozen")
        self.assertEqual(published["items"][0]["version"], 2)
        self.assertEqual(service.dream_diary(day)["diaryExpected"], 1)

    def test_original_chat_text_time_and_attachment_references_are_preserved(self):
        service = self.library()
        sent = datetime(2026, 11, 17, 19, tzinfo=timezone.utc)
        message = {"id": "private-37", "channel": "isolation", "peer": "owner", "time": sent.timestamp(),
                   "receivedAt": "2026-11-17T19:00:00.750000+00:00",
                   "replyTo": {"messageId": "private-21", "text": "被引用的原话"},
                   "text": "这是原话\n原句", "attachments": [{"id": "image-1", "type": "image"}]}
        archived = service.archive_chat(message, "这是回复", reply_time=sent.timestamp() + 1)
        duplicate = service.archive_chat(message, "这是回复")
        self.assertTrue(duplicate["input"]["existing"])
        self.assertTrue(duplicate["reply"]["existing"])
        self.assertEqual(archived["reply"]["recordId"], duplicate["reply"]["recordId"])
        sources = service.dispatch("sources", {"sourceRefs": [archived["input"]["recordId"]]})
        raw = sources["items"][0]["data"]
        self.assertEqual(raw["text"], message["text"])
        self.assertEqual(raw["sentAt"], sent.isoformat())
        self.assertEqual(raw["receivedAt"], message["receivedAt"])
        self.assertEqual(raw["replyTo"], message["replyTo"])
        self.assertEqual(raw["attachments"], message["attachments"])
        history = service.dispatch("chat_history", {"date": "2026-11-18"})
        self.assertEqual(history["range"]["timezone"], "Asia/Shanghai")
        self.assertEqual(len(history["items"]), 2)
        self.assertIn(message["text"], history["text"])
        self.assertIn("这是回复", history["text"])

    def test_independent_stop_start_and_code_reload_keep_database(self):
        self.login()
        original = self.library()
        entry = original.write_diary("重载后保留", day="2026-11-18")
        self.assertEqual(self.core.manager.stop("console")["state"], "stopped")
        self.assertEqual(self.call("/library").status_code, 503)
        self.assertEqual(self.call(LIBRARY_API + "status").status_code, 200)
        self.assertEqual(self.core.manager.start("console")["state"], "running")

        service_source = self.plugin / "service.py"
        old_source = service_source.read_text(encoding="utf-8")
        service_source.write_text(old_source.replace('entry["category"] = "认识与判断"', 'entry["category"] = "兴趣"'), encoding="utf-8")
        self.assertEqual(self.core.manager.reload("library")["state"], "running")
        with self.assertRaises(PlatformError):
            original.list_memories()
        service = self.library()
        published = service.publish_dream([{"body": "重载采用新代码"}], {"body": "整理"})
        self.assertEqual(published["items"][0]["category"], "兴趣")
        self.assertEqual(service.dispatch("diary", {"day": entry["day"], "version": 1})["items"][0]["body"], entry["body"])

        self.assertEqual(self.core.manager.stop("library")["state"], "stopped")
        self.assertEqual(self.call(LIBRARY_API + "status").status_code, 503)
        self.assertEqual(self.call("/library").status_code, 200)
        self.assertEqual(self.core.manager.reload("library")["state"], "stopped")
        self.assertEqual(self.core.manager.start("library")["state"], "running")
        self.assertEqual(self.library().list_memories()[0]["id"], published["items"][0]["id"])
        self.assertTrue((self.data / "modules" / "library" / "library.sqlite3").is_file())
