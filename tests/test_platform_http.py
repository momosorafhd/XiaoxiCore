"""Owner HTTP and the real platform manager, with temporary synthetic plugins."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "xixi-core"))

from xixi_core.platform.auth import AuthError, AuthStore
from xixi_core.platform.http import create_app
from xixi_core.platform.app import CoreApp


PASSWORD = "temporary-platform-test-password"
ORIGIN = "http://127.0.0.1:18910"
PLUGIN = '''from .helpers import VALUE
class Plugin:
    @staticmethod
    def validate_config(config):
        if not isinstance(config.get("text", ""), str):
            raise ValueError("text必须为字符串")
    def start(self, context):
        self.config = context.config
        if self.config.get("fail_start"):
            raise RuntimeError("synthetic start failure")
        context.http.register("GET", "/status", self.status_request)
        context.http.register("GET", "/explode", self.explode)
    def stop(self):
        if self.config.get("fail_stop"):
            raise RuntimeError("synthetic stop failure")
    def status(self):
        return {"value": VALUE}
    def status_request(self, request):
        return {"value": VALUE, "text": self.config.get("text", ""), "entry": "v1"}
    def explode(self, request):
        raise ArithmeticError("synthetic plugin exception")
'''


class PlatformHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xixi-platform-http-")
        self.root = Path(self.temp.name)
        self.plugin = self.root / "plugins" / "sample"
        self.plugin.mkdir(parents=True)
        (self.plugin / "plugin.json").write_text(json.dumps({"id": "sample", "name": "样例", "version": "0.1.0", "api_version": 1, "entry": "plugin.py:Plugin", "requires": []}), encoding="utf-8")
        (self.plugin / "__init__.py").write_text("", encoding="utf-8")
        (self.plugin / "plugin.py").write_text(PLUGIN, encoding="utf-8")
        (self.plugin / "helpers.py").write_text('VALUE = "v1"\n', encoding="utf-8")
        self.config_path = self.root / "platform.json"
        self.configuration = {"http": {"host": "127.0.0.1", "port": 18910, "origin": ORIGIN}, "plugins_dir": "plugins", "modules": {
            "console": {"enabled": True, "config": {}}, "system": {"enabled": False, "config": {}},
            "sample": {"enabled": False, "config": {"text": "original"}}
        }}
        self.config_path.write_text(json.dumps(self.configuration), encoding="utf-8")
        self.data_dir = self.root / "data"
        AuthStore.initialize(self.data_dir / "owner.auth.json", password=PASSWORD)
        self.core = CoreApp(self.config_path, self.data_dir)
        self.core.start()
        self.web = create_app(self.core)
        self.client = self.web.test_client()
        self.csrf = None

    def tearDown(self):
        self.core.close()
        self.temp.cleanup()

    def call(self, path, method="GET", data=None, headers=None):
        actual = {"Origin": ORIGIN}
        if self.csrf:
            actual["X-CSRF-Token"] = self.csrf
        actual.update(headers or {})
        return self.client.open(path, method=method, json=data, headers=actual, base_url=ORIGIN)

    def login(self):
        response = self.call("/api/auth/session")
        self.assertEqual(response.status_code, 200)
        self.csrf = response.json["csrf"]
        response = self.call("/api/auth/login", "POST", {"password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.csrf = response.json["csrf"]

    def action(self, action, module="sample"):
        return self.call(f"/api/platform/modules/{module}/{action}", "POST")

    def test_login_boundary_and_secret_not_returned(self):
        self.assertEqual(self.call("/").status_code, 303)
        self.assertEqual(self.call("/api/platform/modules").status_code, 401)
        self.assertEqual(self.call("/api/platform/config/sample").status_code, 401)
        self.assertEqual(self.call("/login").status_code, 200)
        self.login()
        response = self.call("/api/platform/modules")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(PASSWORD, response.get_data(as_text=True))
        self.assertNotIn("password_hash", response.get_data(as_text=True))
        response = self.call("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("常用功能", response.get_data(as_text=True))
        self.assertIn("模块状态", response.get_data(as_text=True))
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_external_links_are_opt_in_and_shared_by_overview_and_sidebar(self):
        self.login()
        self.assertNotIn("外部服务", self.call("/").get_data(as_text=True))
        path = "/api/platform/config/console"
        link = {"label": "本地服务", "url": "http://127.0.0.1:3080/", "description": "自管入口"}
        self.assertEqual(self.call(path, "PUT", {"config": {"serviceLinks": [link]}}).status_code, 200)
        self.action("stop", "console")
        self.action("start", "console")
        self.assertEqual(self.call("/").get_data(as_text=True).count(link["url"]), 2)
        self.assertEqual(self.call("/modules").get_data(as_text=True).count(link["url"]), 1)
        for url in ("javascript:alert(1)", "https://user:pw@example.invalid/", "//example.invalid/"):
            invalid = {"serviceLinks": [{**link, "url": url}]}
            self.assertEqual(self.call(path, "PUT", {"config": invalid}).status_code, 400)
        self.assertEqual(self.call(path).json["config"], {"serviceLinks": [link]})

    def test_wrong_origin_csrf_and_host_cannot_mutate(self):
        self.login()
        path = "/api/platform/modules/sample/start"
        self.assertEqual(self.call(path, "POST", headers={"Origin": "https://elsewhere.invalid"}).status_code, 403)
        self.assertEqual(self.call(path, "POST", headers={"X-CSRF-Token": "wrong"}).status_code, 403)
        self.assertEqual(self.call(path, "POST", headers={"Host": "elsewhere.invalid"}).status_code, 403)
        self.assertEqual(self.core.manager.configuration("sample")["enabled"], False)

    def test_configuration_saved_without_start_and_reload_uses_it(self):
        self.login()
        response = self.call("/api/platform/config/sample", "PUT", {"config": {"text": "saved"}})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.call("/api/platform/config/sample").json["enabled"])
        self.assertEqual(self.action("reload").json["item"]["state"], "stopped")
        self.assertEqual(self.call("/api/modules/sample/status").status_code, 503)
        self.assertEqual(self.action("start").status_code, 200)
        self.assertEqual(self.call("/api/modules/sample/status").json["text"], "saved")
        self.call("/api/platform/config/sample", "PUT", {"config": {"text": "new value"}})
        self.assertTrue(self.call("/api/platform/config/sample").json["config_pending"])
        self.assertEqual(self.call("/api/modules/sample/status").json["text"], "saved")
        self.assertEqual(self.action("reload").status_code, 200)
        self.assertEqual(self.call("/api/modules/sample/status").json["text"], "new value")

    def test_invalid_configuration_keeps_original_bytes(self):
        self.login()
        original = self.config_path.read_bytes()
        self.assertEqual(self.call("/api/platform/config/sample", "PUT", {"config": {"text": 42}}).status_code, 400)
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertEqual(self.call("/api/platform/config/sample", "PUT", {"config": {"token": "synthetic-secret"}}).status_code, 400)
        self.assertEqual(self.config_path.read_bytes(), original)

    def test_reload_reads_entry_and_internal_file_without_stopping_console(self):
        self.login()
        self.assertEqual(self.action("start").status_code, 200)
        entry = self.plugin / "plugin.py"
        helper = self.plugin / "helpers.py"
        entry_stat, helper_stat = entry.stat(), helper.stat()
        entry.write_text(PLUGIN.replace('"entry": "v1"', '"entry": "v2"'), encoding="utf-8")
        helper.write_text('VALUE = "v2"\n', encoding="utf-8")
        os.utime(entry, ns=(entry_stat.st_atime_ns, entry_stat.st_mtime_ns))
        os.utime(helper, ns=(helper_stat.st_atime_ns, helper_stat.st_mtime_ns))
        self.assertEqual(self.action("reload").status_code, 200)
        response = self.call("/api/modules/sample/status")
        self.assertEqual(response.json["value"], "v2")
        self.assertEqual(response.json["entry"], "v2")
        self.assertEqual(self.call("/").status_code, 200)

    def test_console_stop_keeps_auth_and_control_available(self):
        self.login()
        self.assertEqual(self.action("stop", "console").status_code, 200)
        self.assertEqual(self.call("/").status_code, 503)
        self.assertEqual(self.call("/static/common.js").status_code, 503)
        self.assertEqual(self.call("/api/platform/modules").status_code, 200)
        response = self.call("/api/auth/logout", "POST")
        self.csrf = response.json["csrf"]
        self.assertEqual(self.call("/api/platform/modules").status_code, 401)
        self.login()
        self.assertEqual(self.action("start", "console").status_code, 200)
        self.assertEqual(self.call("/").status_code, 200)

    def test_original_resource_pages_and_assets_are_reused(self):
        for path in ("/modules", "/logs", "/status", "/status/history"):
            self.assertEqual(self.call(path).status_code, 303, path)
        self.login()
        status = self.call("/status")
        self.assertEqual(status.status_code, 200)
        for identifier in ("core-list", "memory-chart", "cpu-hardware", "gpu-list", "fan-list", "temperature-list", "toggle-monitor"):
            self.assertIn('id="' + identifier + '"', status.get_data(as_text=True))
        history = self.call("/status/history")
        self.assertEqual(history.status_code, 200)
        for identifier in ("history-cpu-chart", "history-memory-chart", "history-temperature-chart", "history-interval", "history-next"):
            self.assertIn('id="' + identifier + '"', history.get_data(as_text=True))
        source = Path(__file__).resolve().parents[1] / "packages/xixi-core/xixi_core/modules/console"
        for name in ("library.css", "shell.css", "status.css", "style.css", "status.js", "history.js", "shell.js"):
            response = self.call("/static/" + name)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, (source / "static" / name).read_bytes(), name)
        self.assertNotIn("platform.css", self.call("/").get_data(as_text=True))
        self.assertIn('href="/library"', self.call("/").get_data(as_text=True))
        self.assertEqual(self.call("/static/platform.css").status_code, 404)
        for path in ("/modules", "/logs"):
            self.assertEqual(self.call(path).status_code, 200)

    def test_original_login_and_resource_routes_use_new_host_services(self):
        response = self.call("/v1/session")
        self.csrf = response.json["csrf"]
        response = self.call("/v1/login", "POST", {"password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["authenticated"])
        self.csrf = response.json["csrf"]
        self.assertEqual(self.call("/v1/system/status").status_code, 503)
        self.assertEqual(self.call("/v1/system/history").status_code, 503)
        self.assertEqual(self.action("start", "system").status_code, 200)
        response = self.call("/v1/system/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("cpu", response.json)
        self.assertIn("memory", response.json)
        for interval in (60, 600, 1800):
            response = self.call("/v1/system/history?interval=" + str(interval))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["interval_seconds"], interval)
            self.assertIn("points", response.json)
            self.assertIn("disks", response.json)
            self.assertIn("monitor", response.json)
        for suffix in ("?interval=1", "?interval=60&interval=600", "?unrelated=true"):
            self.assertEqual(self.call("/v1/system/history" + suffix).status_code, 400)
        self.assertEqual(self.action("stop", "system").status_code, 200)
        self.assertEqual(self.call("/v1/system/history").status_code, 503)
        response = self.call("/v1/logout", "POST", {})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["authenticated"])
        self.assertEqual(self.call("/v1/system/history").status_code, 401)

    def test_failed_start_and_reload_are_not_success_responses(self):
        self.login()
        self.call("/api/platform/config/sample", "PUT", {"config": {"text": "x", "fail_start": True}})
        response = self.action("start")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json["success"])
        self.assertEqual(response.json["item"]["state"], "error")
        self.assertEqual(self.call("/api/platform/status").status_code, 200)
        (self.plugin / "plugin.py").write_text("invalid Python !\n", encoding="utf-8")
        response = self.action("reload")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json["success"])

    def test_failed_stop_requires_restart_and_blocks_second_instance(self):
        self.login()
        self.call("/api/platform/config/sample", "PUT", {"config": {"text": "x", "fail_stop": True}})
        self.assertEqual(self.action("start").status_code, 200)
        response = self.action("stop")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json["item"]["restart_required"])
        self.assertNotEqual(self.action("start").status_code, 200)
        self.assertNotEqual(self.action("reload").status_code, 200)
        self.assertEqual(self.call("/api/modules/sample/status").status_code, 503)

    def test_unhandled_plugin_error_is_visible_in_platform_log(self):
        self.login()
        self.assertEqual(self.action("start").status_code, 200)
        response = self.call("/api/modules/sample/explode")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json["error"]["code"], "internal_error")
        logs = self.call("/api/platform/logs").json["items"]
        self.assertTrue(any(item["module"] == "http" and "ArithmeticError" in item["message"] for item in logs))
        self.assertEqual(self.call("/api/platform/logs?limit=501").status_code, 400)
        self.assertEqual(self.call("/api/platform/resources").status_code, 503)

    def test_https_proxy_requires_expected_peer_and_single_allowed_client(self):
        self.core.close()
        self.configuration["http"].update(origin="https://10.50.0.200:18911", trusted_proxy="127.0.0.1", allowed_networks=["10.50.0.0/24"])
        self.config_path.write_text(json.dumps(self.configuration), encoding="utf-8")
        self.core = CoreApp(self.config_path, self.data_dir)
        self.core.start()
        self.web = create_app(self.core)
        client = self.web.test_client()
        headers = {"X-Forwarded-For": "10.50.0.100", "X-Forwarded-Proto": "https"}
        url = "http://10.50.0.200:18911"
        response = client.get("/api/auth/session", base_url=url, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Secure", response.headers["Set-Cookie"])
        self.assertIn("__Host-xixi_platform", response.headers["Set-Cookie"])
        self.assertEqual(client.get("/health", base_url=url, headers={**headers, "X-Forwarded-For": "8.8.8.8"}).status_code, 403)
        self.assertEqual(client.get("/health", base_url=url, headers={**headers, "X-Forwarded-For": "10.50.0.100, 127.0.0.1"}).status_code, 403)
        self.assertEqual(client.get("/health", base_url=url, headers=headers, environ_overrides={"REMOTE_ADDR": "10.50.0.101"}).status_code, 403)


class PlatformAuthTests(unittest.TestCase):
    def test_imported_verifier_and_existing_file_not_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="xixi-auth-test-") as directory:
            original = Path(directory) / "first.json"
            imported = Path(directory) / "imported.json"
            AuthStore.initialize(original, password=PASSWORD)
            payload = json.loads(original.read_text(encoding="utf-8"))
            AuthStore.initialize(imported, password_hash=payload["password_hash"])
            auth = AuthStore(imported)
            token, _ = auth.new_session()
            _, result = auth.authenticate(token, PASSWORD, "127.0.0.1")
            self.assertTrue(result["authenticated"])
            prior = imported.read_bytes()
            with self.assertRaises(AuthError):
                AuthStore.initialize(imported, password="must-not-replace-existing")
            self.assertEqual(imported.read_bytes(), prior)

    def test_session_rotation_expiry_and_restart(self):
        with tempfile.TemporaryDirectory(prefix="xixi-auth-test-") as directory:
            path = Path(directory) / "owner.auth.json"
            AuthStore.initialize(path, password=PASSWORD)
            now = [100]
            auth = AuthStore(path, clock=lambda: now[0])
            old_token, _ = auth.new_session()
            token, item = auth.authenticate(old_token, PASSWORD, "127.0.0.1")
            self.assertIsNone(auth.session(old_token))
            self.assertTrue(auth.session(token)["authenticated"])
            self.assertIsNone(AuthStore(path, clock=lambda: now[0]).session(token))
            now[0] = item["expires"] + 1
            self.assertIsNone(auth.session(token))


if __name__ == "__main__":
    unittest.main()
