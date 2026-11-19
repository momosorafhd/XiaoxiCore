"""Read-only test catalog, owner HTTP, durable bindings, and session snapshots."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "xixi-core"))

from xixi_core.platform.app import CoreApp
from xixi_core.platform.auth import AuthStore
from xixi_core.platform.http import create_app
from xixi_core.runtime import MockRuntime


ORIGIN = "http://127.0.0.1:18910"
PASSWORD = "isolated-test-owner-password"
API = "/api/modules/model_settings/"
CATALOG = {
    "metadataOnly": True,
    "source": "synthetic-test",
    "default": {"provider": "fixture-a", "model": "basic"},
    "routableProviders": ["fixture-a", "fixture-b"],
    "groups": [
        {"id": "fixture-a", "name": "Fixture A", "models": [
            {"id": "basic", "reasoning": {"efforts": [{"id": "low", "name": "低"}]}}]},
        {"id": "fixture-b", "name": "Fixture B", "models": [
            {"id": "reasoning", "reasoning": {"efforts": ["high"]}}]},
    ],
    "agentPresets": [{"id": "standard", "available": True}, {"id": "minimal", "available": True}],
    "failures": [],
}


def selection(**overrides):
    return {"provider": "fixture-b", "model": "reasoning", "reasoningEffort": "high", "agentPreset": "minimal",
            "maxTokens": 2400, **overrides}


class PlatformModelSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xixi-platform-model-settings-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for plugin in ("model_settings", "dsh_bridge"):
            shutil.copytree(ROOT / "plugins" / plugin, root / "plugins" / plugin, ignore=shutil.ignore_patterns("__pycache__"))
        self.config = root / "platform.json"
        self.config.write_text(json.dumps({"http": {"origin": ORIGIN}, "modules": {
            "console": {"enabled": True}, "system": {"enabled": False}, "model_settings": {"enabled": True},
            "dsh_bridge": {"enabled": True, "config": {"mode": "mock"}},
        }}), encoding="utf-8")
        self.data = root / "data"
        AuthStore.initialize(self.data / "owner.auth.json", password=PASSWORD)
        self.catalog = copy.deepcopy(CATALOG)
        self.catalog_reads = 0
        self.specs = []
        catalog_patch = patch.object(MockRuntime, "model_catalog", lambda _runtime: self.read_catalog())
        catalog_patch.start()
        self.addCleanup(catalog_patch.stop)
        original_create = MockRuntime.create

        def track_create(runtime, spec):
            self.specs.append(copy.deepcopy(spec))
            return original_create(runtime, spec)

        create_patch = patch.object(MockRuntime, "create", track_create)
        create_patch.start()
        self.addCleanup(create_patch.stop)
        self.open()

    def open(self):
        self.core = CoreApp(self.config, self.data)
        self.addCleanup(self.core.close)
        self.core.start()
        self.client = create_app(self.core).test_client()
        self.csrf = None

    def read_catalog(self):
        self.catalog_reads += 1
        return copy.deepcopy(self.catalog)

    def call(self, path, method="GET", body=None):
        headers = {"Origin": ORIGIN}
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        return self.client.open(path, method=method, json=body, headers=headers, base_url=ORIGIN)

    def login(self):
        self.csrf = self.call("/v1/session").json["csrf"]
        login = self.call("/v1/login", "POST", {"password": PASSWORD})
        self.assertEqual(login.status_code, 200)
        self.csrf = login.json["csrf"]

    def save(self, bindings, *, revision=1, request_id="config-change", status=200):
        response = self.call(API + "settings", "POST", {"expectedRevision": revision, "requestId": request_id, "bindings": bindings})
        self.assertEqual(response.status_code, status, response.json)
        return response.json

    def test_reused_settings_page_and_owner_boundary(self):
        self.assertEqual(self.call("/models").status_code, 303)
        self.assertEqual(self.call(API + "settings").status_code, 401)
        self.login()
        page = self.call("/models")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        for text in ("模型设置", "用途配置", 'id="model-settings-form"', '/static/models.css', '/static/models.js'):
            self.assertIn(text, html)
        js = self.call("/static/models.js").get_data(as_text=True)
        for text in ("/api/modules/model_settings/", "闲时换聊", "做梦", "最大生成 Token 数"):
            self.assertIn(text, js)
        css = self.call("/static/models.css").get_data()
        self.assertEqual(css, (ROOT / "packages/xixi-core/xixi_core/modules/console/static" / "models.css").read_bytes())
        self.assertEqual(self.call(API + "catalog").json, CATALOG)

    def test_validate_restore_reset_and_persist_model_settings(self):
        self.login()
        initial = self.call(API + "settings").json
        self.assertEqual(initial, {"revision": 1, "bindings": {purpose: None for purpose in
                         ("daily", "consolidation", "dream", "work", "leisure")}})
        self.assertEqual(self.catalog_reads, 0)
        desired = {"consolidation": selection(), "dream": selection(provider=None, model="basic", reasoningEffort="low")}
        saved = self.save(desired)
        self.assertEqual(saved["revision"], 2)
        self.assertEqual({purpose: saved["bindings"][purpose] for purpose in desired}, desired)
        self.assertEqual(self.save(desired), saved)
        self.assertEqual(self.catalog_reads, 1)
        self.save({"daily": selection()}, request_id="stale", status=409)
        self.save({"daily": selection(reasoningEffort="invalid")}, revision=2, request_id="invalid-effort", status=400)
        self.save({"work": selection(provider="fixture-a")}, revision=2, request_id="wrong-channel", status=400)
        self.save({"work": selection(apiKey="ordinary-settings-not-for-credentials")}, revision=2, request_id="credential-field", status=400)
        self.assertEqual(self.call(API + "settings").json, saved)
        self.assertEqual(self.core.manager.reload("model_settings")["state"], "running")
        self.assertEqual(self.call(API + "settings").json, saved)
        self.core.close()
        self.open()
        self.login()
        self.assertEqual(self.call(API + "settings").json, saved)
        self.core.manager.stop("dsh_bridge")
        self.assertEqual(self.call(API + "catalog").status_code, 503)
        self.save({"daily": selection()}, revision=2, request_id="offline-choice", status=503)
        removed = self.save({"consolidation": None}, revision=2, request_id="reset")
        self.assertIsNone(removed["bindings"]["consolidation"])
        self.assertEqual(removed["bindings"]["dream"], desired["dream"])

    def test_session_creation_uses_bindings_by_purpose_and_preserves_existing(self):
        self.login()
        bridge = self.core.services.get("dsh_bridge")
        previous = bridge.open_session()
        self.assertNotIn("model", self.specs[0])
        settings = {
            "daily": selection(),
            "consolidation": selection(provider=None, model="basic", reasoningEffort="low", agentPreset="standard"),
            "dream": selection(model="reasoning", maxTokens=800),
        }
        self.save(settings)
        self.assertEqual(bridge.open_session(previous), previous)
        self.assertEqual(len(self.specs), 1)
        for kind, purpose in (("daily", "daily"), ("idle", "consolidation"), ("dream", "dream")):
            with self.subTest(kind=kind):
                new = bridge.open_session(kind=kind)
                spec = self.specs[-1]
                binding = settings[purpose]
                self.assertNotEqual(new, previous)
                self.assertEqual(spec["kind"], kind)
                self.assertEqual(spec["agentPreset"], binding["agentPreset"])
                self.assertEqual(spec["model"], binding["model"])
                self.assertEqual(spec["modelOptions"], {field: value for field, value in binding.items()
                                 if field != "model" and value is not None})


if __name__ == "__main__":
    unittest.main()
