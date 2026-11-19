"""Check persona authoring and the assemblies sent to the DSH bridge."""
from pathlib import Path
import sys
import tempfile
import unittest

from flask import Flask, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "xixi-core"))

from xixi_core.modules.persona import PersonaStore, persona_http
from xixi_core.platform.errors import PlatformError


class PersonaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xixi-platform-persona-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "persona.sqlite3"
        self.persona = PersonaStore(self.path)
        self.addCleanup(self.persona.close)
        web = Flask(__name__)
        web.add_url_rule("/<resource>", view_func=lambda resource: persona_http(self.persona, resource, request),
                         methods=["GET", "POST"])
        web.register_error_handler(PlatformError, lambda exc: (exc.as_dict(), exc.status))
        self.client = web.test_client()

    @staticmethod
    def sections(bundle):
        return {section["id"]: section["text"] for section in bundle}

    def edit(self, name, **changes):
        item = self.persona.injections.get(name)
        response = self.client.post("/injections", json={"id": name, "expectedVersion": item["version"], **changes})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_persona_and_mode_additions_freeze_when_creating_a_session(self):
        before = self.persona.session_sections()
        self.edit("daily.role", body="测试身份 {原样保留}")
        self.edit("mode.standard", body="标准附加 {{model}}")
        self.edit("mode.minimal", body="极简附加")
        environment = self.client.post("/environment", json={"environmentDescription": "独立隔离环境", "expectedVersion": 0})
        self.assertEqual(environment.status_code, 200)
        self.assertNotIn("测试身份", self.sections(before)["daily.role"])
        standard = self.sections(self.persona.session_sections(context={"handoff": "前一轮的交接"}, runtime="isolated"))
        self.assertEqual(standard["daily.role"], "测试身份 {原样保留}")
        self.assertEqual(standard["mode.standard"], "标准附加 {{model}}")
        self.assertNotIn("mode.minimal", standard)
        self.assertIn("独立隔离环境", standard["initial.environment"])
        self.assertIn("isolated", standard["initial.environment"])
        self.assertIn("前一轮的交接", standard["handoff.summary"])
        minimal = self.sections(self.persona.session_sections(agent_preset="minimal"))
        self.assertEqual(minimal["mode.minimal"], "极简附加")
        self.assertNotIn("mode.standard", minimal)

        reopened = PersonaStore(self.path)
        try:
            self.assertEqual(self.sections(reopened.session_sections())["daily.role"], "测试身份 {原样保留}")
        finally:
            reopened.close()

    def test_per_message_templates_change_next_turn_keep_raw_and_metadata(self):
        message = {"id": "q-1", "channel": "local", "peer": "owner", "time": 1795000000, "text": "需要保留的原文",
                   "attachments": [{"name": "照片", "id": "img-ref"}],
                   "replyTo": {"messageId": "q-0", "sentAt": "2026-11-18T12:00:00+00:00", "text": "前文"}}
        original = self.persona.run_text("daily", [message])
        self.assertIn("来源：local", original)
        self.assertIn("img-ref", original)
        self.assertIn("前文", original)
        self.edit("message.owner", body="[{messageId}] 本轮：{text}")
        self.assertIn("[q-1] 本轮：需要保留的原文", self.persona.run_text("daily", [message]))
        self.edit("message.owner", body="[{messageId}] 本轮", enabled=False)
        self.assertIn("需要保留的原文", self.persona.run_text("daily", [message]))
        self.assertNotIn("本轮", original)
        wire_message = {key: value for key, value in message.items() if key != "replyTo"}
        wire_message["attachments"] = [{"type": "image", "data": {"file_id": "onebot-image"}},
                                       {"type": "reply", "data": {"id": "quoted-onebot"}}]
        presented = self.persona.run_text("daily", [wire_message])
        self.assertIn("onebot-image", presented)
        self.assertIn("quoted-onebot", presented)
        self.assertIn("原文未随消息提供", presented)
        fixed = self.client.post("/injections", json={"id": "message.owner", "expectedVersion": 3, "placement": "start"})
        self.assertEqual(fixed.status_code, 400)

    def test_maintenance_sections_and_two_summaries_follow_their_real_scope(self):
        current = {"selfUnderstanding": "当前认识", "ownerUnderstanding": "主人偏好", "expectedVersion": 0,
                   "requestId": "manual-summary"}
        response = self.client.post("/summaries", json=current)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json, self.client.post("/summaries", json=current).json)
        old = self.persona.stage_summaries()
        daily = self.sections(self.persona.session_sections())
        self.assertIn("当前认识", daily["stage.summaries"])
        self.assertNotIn("dream.stage_summaries", daily)
        dream = self.sections(self.persona.session_sections("dream", context={"day": "2026-11-18", "sourceMessages": ["固定原话"],
                         "existingMemories": [{"id": "m-1", "excerpt": "目录摘录"}]}))
        self.assertIn("固定原话", dream["maintenance.materials"])
        self.assertIn("目录摘录", dream["maintenance.materials"])
        self.assertIn("主人偏好", dream["dream.stage_summaries"])
        self.assertIn("2026-11-18", dream["dream.instructions"])

        publish_calls = []
        candidate = self.persona.prepare_dream_summaries({"stageSummaries": {
            "selfUnderstanding": "做梦更新自我", "ownerUnderstanding": "做梦更新主人"}}, old, "dream-fixture")
        def reject_library():
            raise RuntimeError("synthetic library failure")

        with self.assertRaisesRegex(RuntimeError, "synthetic library failure"):
            self.persona.publish_dream_summaries(candidate, publish_library=reject_library)
        self.assertEqual(self.persona.stage_summaries(), old)
        self.persona.publish_dream_summaries(candidate, publish_library=lambda: publish_calls.append("published"))
        self.persona.publish_dream_summaries(candidate, publish_library=lambda: publish_calls.append("duplicate"))
        self.assertEqual(publish_calls, ["published"])
        self.assertEqual([entry["version"] for entry in self.client.get("/summaries").json["history"]], [2, 1])
        self.assertIn("做梦更新自我", self.sections(self.persona.session_sections())["stage.summaries"])
        self.assertIn("当前认识", daily["stage.summaries"])

    def test_reinstated_templates_report_real_timing_and_render_their_context(self):
        items = {row["id"]: row for row in self.client.get("/injections").json["items"]}
        self.assertIn("不可用", items["initial.library_deferred"]["runtimeHint"])
        self.assertIn("纠错一次", items["maintenance.retry"]["runtimeHint"])
        self.assertEqual(self.persona.library_unavailable_text("dream"), "")
        self.assertIn("图书馆暂缓", self.persona.library_unavailable_text("daily"))
        self.assertNotIn("initial.library_deferred", self.sections(self.persona.session_sections("daily")))
        self.edit("maintenance.retry", body="纠错正文 {previousError}")
        self.assertEqual(self.persona.maintenance_feedback("idle", "校验信息"), "纠错正文 校验信息")
        work = self.sections(self.persona.session_sections("work", context={"objective": "任务目标", "contextRefs": ["引用一"]}))
        self.assertIn("任务目标", work["work.instructions"])
        self.assertIn("引用一", work["work.instructions"])
        event = {"id": "task-event", "source": "task_result", "sender": "system", "channel": "local",
                 "peer": "owner", "time": 1795000000, "text": "受限预览",
                 "event": {"eventType": "task_result", "taskId": "known-task", "title": "独立任务", "status": "completed",
                           "summary": "受限预览", "resultRef": "full_result", "artifactRefs": ["artifact-ref"],
                           "summaryTruncated": True}}
        view = self.persona.run_text("daily", [event])
        self.assertEqual(view.count("受限预览"), 1)
        self.assertIn("本轮仅提供简要结果", view)
        self.assertIn("artifact-ref", view)
        self.edit("format.original", body="{channel} | {sentAt} | {replyText} | {attachments} | {text}")
        material = self.sections(self.persona.session_sections("idle", context={"sourceMessages": [{
            "id": "source-ref", "text": "原正文", "channel": "原路线", "sentAt": "原发送时间",
            "replyTo": {"messageId": "回复号", "text": "被引用正文"}, "attachments": [{"ref": "附件引用"}],
        }]}))["maintenance.materials"]
        for marker in ("原路线", "原发送时间", "被引用正文", "附件引用", "原正文"):
            self.assertIn(marker, material)

    def test_stale_dream_never_publishes_library_and_disabled_templates_cannot_be_saved(self):
        old = self.persona.stage_summaries()
        result = {"stageSummaries": {"selfUnderstanding": "过时的自我认识", "ownerUnderstanding": "过时的主人认识"}}
        prepared = self.persona.prepare_dream_summaries(result, old, "stale-dream")
        self.persona.update_stage_summaries(dict(result["stageSummaries"], expectedVersion=0), "owner-edit")
        with self.assertRaisesRegex(ValueError, "版本冲突"):
            self.persona.publish_dream_summaries(prepared, publish_library=lambda: self.fail("published a stale dream"))
        self.assertEqual(self.persona.stage_summaries()["version"], 1)
        self.assertNotIn("native.compaction.instructions", {row["id"] for row in self.client.get("/injections").json["items"]})
        self.assertEqual(self.client.post("/injections", json={"id": "native.compaction.instructions", "expectedVersion": 1,
                                                              "body": "不要覆盖独立原生策略"}).status_code, 400)
        self.assertEqual(self.client.post("/environment", json={"expectedVersion": 1, "environmentDescription": "过时"}).status_code, 409)
