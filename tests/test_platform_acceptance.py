"""Independent acceptance of the supplied example against public platform APIs."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "xixi-core"))

from xixi_core.platform.app import CoreApp


class PlatformAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xiaoxi-platform-acceptance-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.plugin = self.root / "plugins" / "example"
        shutil.copytree(ROOT / "plugins" / "example", self.plugin)
        self.path = self.root / "platform.json"
        self.path.write_text(json.dumps({
            "http": {"host": "127.0.0.1", "port": 0},
            "plugins_dir": "plugins",
            "modules": {
                "console": {"enabled": False, "config": {}},
                "system": {"enabled": False, "config": {}},
                "example": {"enabled": False, "config": {"text": "首次配置", "intervalSeconds": 0.05}},
            },
        }), encoding="utf-8")
        self.app = CoreApp(self.path, self.root / "data")
        self.addCleanup(self.app.close)
        self.app.start()

    def module(self):
        return next(item for item in self.app.manager.list() if item["id"] == "example")

    def assert_running(self, result):
        self.assertEqual(result["state"], "running", result)

    def test_entry_and_submodule_reload_without_duplicate_callbacks(self):
        self.assert_running(self.app.manager.start("example"))
        previous = self.app.services.get("example.greeting")
        self.assertEqual(previous.describe()["events"], 0)
        self.assertEqual(self.app.events.emit("example.ping", {})["delivered"], 1)
        self.assertEqual(previous.describe()["events"], 1)
        # A second plugin-local file must refresh even if size and mtime match the old file.
        helper = self.plugin / "helpers.py"
        before = helper.stat()
        original = helper.read_bytes()
        replacement = original.replace("示例插件".encode(), "更新插件".encode())
        self.assertEqual(len(original), len(replacement))
        self.assertNotEqual(original, replacement)
        helper.write_bytes(replacement)
        os.utime(helper, ns=(before.st_atime_ns, before.st_mtime_ns))
        entry = self.plugin / "plugin.py"
        code = entry.read_text(encoding="utf-8")
        self.assertIn("self._events = 0", code)
        entry.write_text(code.replace("self._events = 0", "self._events = 7"), encoding="utf-8")
        sentinel = self.root / "data" / "modules" / "example" / "preserve.txt"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("插件业务数据", encoding="utf-8")
        self.assert_running(self.app.manager.reload("example"))
        current = self.app.services.get("example.greeting")
        self.assertTrue(current.describe()["greeting"].startswith("更新插件："))
        self.assertEqual(current.describe()["events"], 7)
        self.assertEqual(self.app.events.emit("example.ping", {})["delivered"], 1)
        self.assertEqual(current.describe()["events"], 8)
        with self.assertRaises(Exception):
            previous.describe()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "插件业务数据")
        self.assertEqual(len([t for t in threading.enumerate() if t.name == "xixi-example-counter"]), 1)
        self.assertEqual(self.app.manager.stop("example")["state"], "stopped")
        self.assertEqual(self.app.events.emit("example.ping", {})["delivered"], 0)
        self.assertFalse(any(t.name == "xixi-example-counter" for t in threading.enumerate()))

    def test_config_validation_disabled_reload_and_restart(self):
        before = self.path.read_bytes()
        with self.assertRaises(Exception):
            self.app.manager.update_config("example", {"text": "", "intervalSeconds": 0})
        self.assertEqual(self.path.read_bytes(), before)
        self.app.manager.update_config("example", {"text": "保存后的值", "intervalSeconds": 0.05})
        self.assertEqual(self.app.manager.reload("example")["state"], "stopped")
        self.assertFalse(self.module()["enabled"])
        self.assertEqual(self.app.events.emit("example.ping", {})["delivered"], 0)
        self.assert_running(self.app.manager.start("example"))
        self.assertEqual(self.app.services.get("example.greeting").describe()["text"], "保存后的值")
        self.app.close()
        restarted = CoreApp(self.path, self.root / "data")
        self.addCleanup(restarted.close)
        restarted.start()
        self.assertEqual(restarted.services.get("example.greeting").describe()["text"], "保存后的值")
        self.assertEqual(restarted.events.emit("example.ping", {})["delivered"], 1)

    def test_import_failure_leaves_no_active_old_plugin(self):
        self.assert_running(self.app.manager.start("example"))
        helper = self.plugin / "helpers.py"
        helper.write_text("raise RuntimeError('acceptance import failure')\n", encoding="utf-8")
        result = self.app.manager.reload("example")
        self.assertEqual(result["state"], "error", result)
        self.assertIn("acceptance import failure", str(result.get("error")))
        self.assertTrue(self.module()["enabled"], "reload must not rewrite the enabled preference")
        self.assertEqual(self.app.events.emit("example.ping", {})["delivered"], 0)
        with self.assertRaises(Exception):
            self.app.services.get("example.greeting")
        self.assertFalse(any(t.name == "xixi-example-counter" for t in threading.enumerate()))


if __name__ == "__main__":
    unittest.main()
