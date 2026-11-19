"""Plugin lifecycle contracts: real imports, owned calls and persistence failures."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "xixi-core"))

from xixi_core.platform import CoreApp, PlatformError


PLUGIN = '''from .helpers import VALUE

class Plugin:
    def start(self, context):
        self.context = context
        self.events = 0
        count = context.data_dir / 'starts.txt'
        count.write_text(str(int(count.read_text()) + 1) if count.exists() else '1')
        context.services.provide('PLUGIN_ID.service', self)
        context.http.register('GET', '/value', lambda request: {'value': VALUE})
        context.events.subscribe('ping', self.ping)

    def read(self):
        return VALUE

    def ping(self, payload):
        self.events += 1

    def status(self):
        return {'value': VALUE, 'events': self.events}

    def stop(self):
        pass
'''


class PlatformRuntime(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xixi-platform-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "platform.json"

    def plugin(self, module_id="demo", source=None, *, value="first", requires=None):
        directory = self.root / "plugins" / module_id
        directory.mkdir(parents=True)
        (directory / "plugin.json").write_text(json.dumps({
            "id": module_id, "name": module_id, "version": "0.1", "api_version": 1,
            "entry": "plugin.py:Plugin", "requires": requires or [],
        }), encoding="utf-8")
        (directory / "__init__.py").write_text("", encoding="utf-8")
        (directory / "plugin.py").write_text((source or PLUGIN).replace("PLUGIN_ID", module_id), encoding="utf-8")
        (directory / "helpers.py").write_text("VALUE = " + repr(value) + "\n", encoding="utf-8")
        return directory

    def app(self, modules, *, builtins=None, drain_timeout=0.2):
        self.config_path.write_text(json.dumps({
            "http": {"host": "127.0.0.1", "port": 0}, "plugins_dir": "plugins",
            "modules": modules,
        }), encoding="utf-8")
        app = CoreApp(self.config_path, self.root / "data", builtins=builtins or {}, drain_timeout=drain_timeout)
        self.addCleanup(app.close)
        app.start()
        return app

    @staticmethod
    def module(app, name):
        return next(item for item in app.manager.list() if item["id"] == name)

    def test_import_namespace_isolation_and_same_timestamp_reload(self):
        first = self.plugin("first", value="alpha")
        self.plugin("second", value="gamma")
        app = self.app({"first": {"enabled": True}, "second": {"enabled": True}})
        shared_json = sys.modules["json"]
        stale = app.services.get("first.service")
        helper = first / "helpers.py"
        metadata = helper.stat()
        helper.write_text(helper.read_text().replace("alpha", "bravo"))
        os.utime(helper, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        self.assertEqual(app.manager.reload("first")["state"], "running")
        self.assertEqual(app.services.get("first.service").read(), "bravo")
        self.assertEqual(app.services.get("second.service").read(), "gamma")
        self.assertIs(sys.modules["json"], shared_json)
        with self.assertRaises(PlatformError):
            stale.read()
        self.assertEqual(app.events.emit("ping", {})["delivered"], 2)

    def test_bad_manifest_and_missing_entry_cannot_restart_old_code(self):
        directory = self.plugin()
        app = self.app({"demo": {"enabled": True}})
        manifest = directory / "plugin.json"
        original = manifest.read_text()
        manifest.write_text("not-json")
        self.assertEqual(app.manager.reload("demo")["state"], "error")
        self.assertEqual(app.manager.start("demo")["state"], "error")
        with self.assertRaises(PlatformError):
            app.services.get("demo.service")
        self.assertEqual((self.root / "data/modules/demo/starts.txt").read_text(), "1")
        manifest.write_text(original)
        (directory / "plugin.py").rename(directory / "saved-plugin.py")
        self.assertEqual(app.manager.reload("demo")["state"], "error")
        self.assertEqual(app.manager.start("demo")["state"], "error")
        self.assertEqual((self.root / "data/modules/demo/starts.txt").read_text(), "1")
        self.assertEqual(app.events.emit("ping")["delivered"], 0)

    def test_stop_failure_fences_old_instance_and_never_starts_a_second(self):
        source = PLUGIN.replace("    def stop(self):\n        pass", "    def stop(self):\n        raise RuntimeError('ordinary stop failed')")
        directory = self.plugin(source=source)
        app = self.app({"demo": {"enabled": True}})
        result = app.manager.stop("demo")
        self.assertEqual(result["state"], "error")
        self.assertTrue(result["restart_required"])
        self.assertTrue(result["enabled"])
        (directory / "plugin.py").write_text(PLUGIN.replace("PLUGIN_ID", "demo"))
        for operation in (app.manager.start, app.manager.reload):
            self.assertTrue(operation("demo")["restart_required"])
        self.assertEqual((self.root / "data/modules/demo/starts.txt").read_text(), "1")
        self.assertEqual(app.events.emit("ping")["delivered"], 0)
        with self.assertRaises(PlatformError):
            app.manager.dispatch_http("demo", "GET", "/value", None)

    def test_start_failure_removes_registered_resources_and_runs_cleanup(self):
        class Broken:
            stopped = 0
            cleaned = 0

            def start(self, context):
                context.services.provide("broken.service", self)
                context.http.register("GET", "/value", lambda request: {})
                context.events.subscribe("ping", lambda value: None)
                context.add_cleanup(lambda: setattr(Broken, "cleaned", Broken.cleaned + 1))
                raise RuntimeError("start failed")

            def stop(self):
                Broken.stopped += 1

            def status(self):
                return {}

        app = self.app({"broken": {"enabled": True}}, builtins={"broken": Broken})
        result = self.module(app, "broken")
        self.assertEqual(result["state"], "error")
        self.assertFalse(result["restart_required"])
        self.assertEqual((Broken.stopped, Broken.cleaned), (1, 1))
        self.assertEqual(app.events.emit("ping")["delivered"], 0)
        with self.assertRaises(PlatformError):
            app.services.get("broken.service")

    def test_cleanup_callback_failure_also_prevents_new_instance(self):
        class CleanupFailure:
            starts = 0

            def start(self, context):
                CleanupFailure.starts += 1
                context.services.provide("broken.service", self)
                context.add_cleanup(lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed")))

            def stop(self):
                pass

            def status(self):
                return {}

        app = self.app({"broken": {"enabled": True}}, builtins={"broken": CleanupFailure})
        self.assertTrue(app.manager.stop("broken")["restart_required"])
        self.assertEqual(app.manager.start("broken")["state"], "error")
        self.assertEqual(CleanupFailure.starts, 1)

    def test_dependencies_reject_stop_but_allow_provider_reload(self):
        provider = self.plugin("provider", value="alpha")
        source = PLUGIN.replace("return VALUE", "return self.context.services.get('provider.service').read()")
        self.plugin("consumer", source=source, requires=["provider.service"])
        app = self.app({"provider": {"enabled": True}, "consumer": {"enabled": True}})
        with self.assertRaises(PlatformError) as raised:
            app.manager.stop("provider")
        self.assertEqual(raised.exception.code, "module_in_use")
        (provider / "helpers.py").write_text("VALUE='bravo'\n")
        self.assertEqual(app.manager.reload("provider")["state"], "running")
        self.assertEqual(self.module(app, "consumer")["state"], "running")
        self.assertEqual(app.services.get("consumer.service").read(), "bravo")
        self.assertEqual(app.manager.stop("consumer")["state"], "stopped")
        self.assertEqual(app.manager.stop("provider")["state"], "stopped")

    def test_shutdown_preserves_provider_for_consumer_cleanup_even_after_reload(self):
        source = PLUGIN + "\n    def flush(self):\n        (self.context.data_dir / 'flushed').write_text('done')\n"
        self.plugin("provider", source=source)
        consumer = PLUGIN.replace("    def stop(self):\n        pass", "    def stop(self):\n        self.context.services.get('provider.service').flush()")
        self.plugin("consumer", source=consumer, requires=["provider.service"])
        app = self.app({"provider": {"enabled": True}, "consumer": {"enabled": True}})
        self.assertEqual(app.manager.reload("provider")["state"], "running")
        self.assertEqual(app.close(), [])
        self.assertEqual((self.root / "data/modules/provider/flushed").read_text(), "done")

    def test_short_inflight_call_finishes_before_cleanup_and_new_calls_are_rejected(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()

        class Slow:
            def start(self, context):
                context.services.provide("slow.service", self)

            def query(self):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("test did not release call")
                return "finished"

            def stop(self):
                stopped.set()

            def status(self):
                return {}

        app = self.app({"slow": {"enabled": True}}, builtins={"slow": Slow}, drain_timeout=1)
        values, results = [], []
        old = app.services.get("slow.service")
        call = threading.Thread(target=lambda: values.append(old.query()))
        stop = threading.Thread(target=lambda: results.append(app.manager.stop("slow")))
        call.start()
        self.assertTrue(entered.wait(1))
        stop.start()
        deadline = time.monotonic() + 1
        while self.module(app, "slow")["operation"] != "stop" and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(stopped.is_set())
        with self.assertRaises(PlatformError):
            old.query()
        release.set()
        call.join(2)
        stop.join(2)
        self.assertFalse(call.is_alive() or stop.is_alive())
        self.assertEqual(values, ["finished"])
        self.assertTrue(stopped.is_set())
        self.assertEqual(results[0]["state"], "stopped")

    def test_drain_timeout_requires_external_restart_without_reusing_instance(self):
        entered, release = threading.Event(), threading.Event()

        class Slow:
            starts = 0

            def start(self, context):
                Slow.starts += 1
                context.services.provide("slow.service", self)

            def query(self):
                entered.set()
                release.wait(2)

            def stop(self):
                pass

            def status(self):
                return {}

        app = self.app({"slow": {"enabled": True}}, builtins={"slow": Slow}, drain_timeout=0.02)
        call = threading.Thread(target=app.services.get("slow.service").query)
        call.start()
        self.assertTrue(entered.wait(1))
        try:
            result = app.manager.stop("slow")
            self.assertEqual(result["state"], "error")
            self.assertTrue(result["restart_required"])
            self.assertTrue(app.manager.start("slow")["restart_required"])
            self.assertEqual(Slow.starts, 1)
        finally:
            release.set()
            call.join(2)

    def test_pending_dependent_start_prevents_provider_stop(self):
        entered, release = threading.Event(), threading.Event()

        class Provider:
            def start(self, context):
                context.services.provide("provider.service", self)

            def stop(self):
                pass

            def status(self):
                return {}

        class Consumer(Provider):
            manifest = {"requires": ["provider.service"]}

            def start(self, context):
                entered.set()
                release.wait(2)

        app = self.app({"provider": {"enabled": True}, "consumer": {"enabled": False}},
                       builtins={"provider": Provider, "consumer": Consumer})
        results = []
        start = threading.Thread(target=lambda: results.append(app.manager.start("consumer")))
        start.start()
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaises(PlatformError) as raised:
                app.manager.stop("provider")
            self.assertEqual(raised.exception.code, "module_in_use")
        finally:
            release.set()
            start.join(2)
        self.assertEqual(results[0]["state"], "running")

    def test_invalid_config_and_failed_replace_preserve_original_file(self):
        self.plugin()
        app = self.app({"demo": {"enabled": False, "config": {"text": "old"}}})
        before = self.config_path.read_bytes()
        with self.assertRaises(PlatformError):
            app.manager.update_config("demo", {"nested": {"password": "must-not-persist"}})
        self.assertEqual(self.config_path.read_bytes(), before)
        with patch("xixi_core.platform.config.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(PlatformError) as raised:
                app.manager.update_config("demo", {"text": "new"})
        self.assertEqual(raised.exception.code, "config_write_failed")
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(app.manager.configuration("demo")["config"], {"text": "old"})
        self.assertEqual(list(self.root.glob(".platform.json.*")), [])

    def test_http_runtime_override_does_not_change_persistent_config(self):
        self.plugin()
        app = self.app({"demo": {"enabled": False}})
        app.config.runtime_http({"host": "127.0.0.2", "port": 32123})
        app.manager.update_config("demo", {"text": "saved"})
        app.manager.start("demo")
        stored = json.loads(self.config_path.read_text())
        self.assertEqual(stored["http"], {"host": "127.0.0.1", "port": 0})
        self.assertEqual(app.config.snapshot()["http"]["port"], 32123)

    def test_reload_reads_saved_config_from_disk_but_keeps_disabled_plugin_stopped(self):
        self.plugin()
        app = self.app({"demo": {"enabled": False, "config": {"text": "old"}}})
        document = json.loads(self.config_path.read_text())
        document["modules"]["demo"]["config"] = {"text": "disk"}
        self.config_path.write_text(json.dumps(document))
        result = app.manager.reload("demo")
        self.assertEqual(result["state"], "stopped")
        self.assertFalse(result["enabled"])
        self.assertEqual(app.manager.configuration("demo")["config"], {"text": "disk"})
        self.assertFalse((self.root / "data/modules/demo/starts.txt").exists())

    def test_same_data_directory_has_one_host_and_is_released_at_close(self):
        app = self.app({})
        with self.assertRaises(BlockingIOError):
            CoreApp(self.config_path, self.root / "data", builtins={})
        app.close()
        next_app = CoreApp(self.config_path, self.root / "data", builtins={})
        next_app.start()
        next_app.close()


if __name__ == "__main__":
    unittest.main()
