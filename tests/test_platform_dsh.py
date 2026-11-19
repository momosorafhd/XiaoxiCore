"""DSH prompt policy contracts through the Core plugin and owner HTTP routes."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "xixi-core"))

from xixi_core.platform.app import CoreApp
from xixi_core.platform.auth import AuthStore
from xixi_core.platform.errors import PlatformError
from xixi_core.platform.http import create_app
from xixi_core.runtime import BridgeRuntime


ORIGIN = "http://127.0.0.1:18910"
POLICY = "/api/modules/dsh_bridge/prompt-policy"
PASSWORD = "temporary-dsh-policy-test-password"


class TestPlatformDsh(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xixi-platform-dsh-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = root / "platform.json"
        settings = {"mode": "dsh", "url": "http://127.0.0.1:18866"}
        config.write_text(json.dumps({
            "http": {"origin": ORIGIN},
            "plugins_dir": str(ROOT / "plugins"),
            "modules": {"dsh_bridge": {"enabled": True, "config": settings}},
        }), encoding="utf-8")
        self.caps = {"time": 1, "compaction": 1}
        self.session_specs = {}
        environment = patch.dict(os.environ, {"XIXI_DSH_BRIDGE_TOKEN": "isolated-dsh-policy-test-token"})
        environment.start()
        self.addCleanup(environment.stop)
        transport = patch.object(BridgeRuntime, "request", autospec=True, side_effect=self.bridge_request)
        transport.start()
        self.addCleanup(transport.stop)
        data = root / "data"
        AuthStore.initialize(data / "owner.auth.json", password=PASSWORD)
        self.platform = CoreApp(config, data, builtins={})
        self.addCleanup(self.platform.close)
        self.platform.start()
        self.client = create_app(self.platform).test_client()
        csrf = self.client.get("/api/auth/session", base_url=ORIGIN).json["csrf"]
        response = self.client.post("/api/auth/login", base_url=ORIGIN, json={"password": PASSWORD},
                                    headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 200)
        self.headers = {"Origin": ORIGIN, "X-CSRF-Token": response.json["csrf"]}

    def bridge_request(self, _transport, path, body=None, timeout=None):
        if path == "/health":
            return {"connectionMode": "existing-dsh", "dshVersion": "0.2.2-rc.2"}
        if path == "/prompt-policy/capabilities":
            return {"version": 1, "hostCapabilities": self.caps, "catalog": {}}
        if path == "/sessions":
            self.session_specs[body["id"]] = deepcopy(body)
            return {"id": body["id"], "status": "idle"}
        if path.startswith("/sessions/") and path.endswith("/state"):
            identifier = path.split("/")[2]
            return {"id": self.session_specs[identifier]["id"], "status": "idle"}
        self.fail(f"Unexpected Bridge request: {path}")

    def read_policy(self):
        response = self.client.get(POLICY, base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        return response.json

    def save_policy(self, payload):
        return self.client.post(POLICY, base_url=ORIGIN, headers=self.headers, json=payload)

    def test_large_artifact_reaches_authenticated_work_service(self):
        class WorkReceiver:
            def write_task_artifact(self, session_id, args, request_id):
                return {"totalChars": len(args["text"])}

        payload = {"tool": "task_artifact_write", "sessionKind": "work", "sessionId": "platform-test",
                   "requestId": "large-work-artifact", "args": {"name": "fixture.txt", "text": "字" * 100000}}
        headers = {"Authorization": "Bearer " + os.environ["XIXI_DSH_BRIDGE_TOKEN"]}
        get_service = self.platform.services.get

        def service(name):
            return WorkReceiver() if name == "assistant" else get_service(name)
        with patch.object(self.platform.services, "get", side_effect=service):
            result = self.client.post("/api/bridge/tool", json=payload, base_url=ORIGIN, headers=headers)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json["data"]["totalChars"], 100000)

    def test_save_reload_restore_and_session_freeze(self):
        initial = self.read_policy()
        self.assertEqual(initial["revision"], 1)
        self.assertEqual(initial["compaction"], {})
        bridge = self.platform.services.get("dsh_bridge")
        old_session = bridge.open_session()
        policy = initial["policy"]
        override = {"mode": "template", "text": "时间标记 {{turn}}", "templateSyntax": "core"}
        policy["runtime"]["time"] = override
        request = {"expectedRevision": 1, "requestId": "edit", "policy": policy, "compaction": {"checkpoint": "压缩检查点"}}
        saved = self.save_policy(request)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json["revision"], 2)
        self.assertEqual(self.save_policy(request).json, saved.json)
        self.assertEqual(self.platform.manager.reload("dsh_bridge")["state"], "running")
        with self.assertRaises(PlatformError):
            bridge.open_session()
        self.assertEqual(self.read_policy()["revision"], 2)
        bridge = self.platform.services.get("dsh_bridge")
        self.assertEqual(bridge.open_session(old_session), old_session)
        self.assertEqual(self.session_specs[old_session]["promptPolicy"]["runtime"]["time"], {"mode": "inherit"})
        new_session = bridge.open_session()
        self.assertEqual(self.session_specs[new_session]["promptPolicy"]["runtime"]["time"], override)
        self.assertEqual(self.session_specs[new_session]["nativeCompaction"], {"checkpoint": "压缩检查点", "versions": {"checkpoint": 2}})

        policy["runtime"]["time"] = {"mode": "inherit"}
        restored = self.save_policy({"expectedRevision": 2, "requestId": "restore-policy", "policy": policy})
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json["compaction"], {"checkpoint": "压缩检查点"})
        restored = self.save_policy({"expectedRevision": 3, "requestId": "restore-compaction", "compaction": {}})
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json["revision"], 4)
        restored_spec = self.session_specs[bridge.open_session()]
        self.assertEqual(restored_spec["promptPolicy"]["runtime"]["time"], {"mode": "inherit"})
        self.assertNotIn("nativeCompaction", restored_spec)

    def test_stale_version_and_missing_native_hook_leave_policy_unchanged(self):
        initial = self.read_policy()
        saved = self.save_policy({"expectedRevision": 1, "requestId": "checkpoint", "compaction": {"checkpoint": "已有"}})
        self.assertEqual(saved.status_code, 200)
        current = self.read_policy()
        policy = deepcopy(current["policy"])
        policy["runtime"]["time"] = {"mode": "template", "text": "修改"}
        stale = self.save_policy({"expectedRevision": initial["revision"], "requestId": "stale", "policy": policy})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json["error"]["code"], "prompt_policy_conflict")
        self.caps["time"] = 0
        missing = self.save_policy({"expectedRevision": 2, "requestId": "missing", "policy": policy})
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.json["error"]["code"], "native_prompt_hook_unavailable")
        final = self.read_policy()
        for key in ("revision", "policy", "compaction"):
            self.assertEqual(final[key], current[key])

    def test_unavailable_hook_does_not_block_editing_other_items_or_restoring_inheritance(self):
        self.caps["sessionTitle"] = 1
        policy = self.read_policy()["policy"]
        policy["runtime"]["sessionTitle"] = {"mode": "template", "templates": {
            "system": {"text": "标题模板"}, "inputIntro": {"text": "请使用此条消息"},
        }}
        saved = self.save_policy({"expectedRevision": 1, "requestId": "title", "policy": policy,
                                  "compaction": {"instruction": "整理上下文", "checkpoint": "已有检查点"}})
        self.assertEqual(saved.status_code, 200)
        policy = saved.json["policy"]
        self.caps.update(sessionTitle=0, compaction=0)
        policy["runtime"]["time"] = {"mode": "template", "text": "时间覆盖"}
        saved = self.save_policy({"expectedRevision": 2, "requestId": "time", "policy": policy})
        self.assertEqual(saved.status_code, 200)
        self.assertIn("inputIntro", saved.json["policy"]["runtime"]["sessionTitle"]["templates"])
        policy = saved.json["policy"]
        policy["runtime"]["sessionTitle"]["templates"].pop("inputIntro")
        restored = self.save_policy({"expectedRevision": 3, "requestId": "restore", "policy": policy,
                                     "compaction": {"checkpoint": "已有检查点"}})
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json["compaction"], {"checkpoint": "已有检查点"})
        self.assertEqual(list(restored.json["policy"]["runtime"]["sessionTitle"]["templates"]), ["system"])
