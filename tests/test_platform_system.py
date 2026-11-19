"""Resource snapshots and the platform module boundary, without external services."""
from pathlib import Path
import json
import logging
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from werkzeug.datastructures import MultiDict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/xixi-core"))

from xixi_core.modules.system import Module, ResourceSampler
from xixi_core.modules.system.monitoring import History, ResourceMonitor
from xixi_core.modules.system.sampler import SystemSampler, cpu_counters, cpu_delta, memory_info


class FakeClock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        return self.value


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.proc = self.root / "proc"
        self.sys = self.root / "sys"
        self.proc.mkdir()
        self.sys.mkdir()
        self.clock = FakeClock()
        self.write(self.proc / "stat", "cpu 10 0 10 80 0 0 0 0\ncpu0 10 0 10 80 0 0 0 0\n")
        self.write(self.proc / "meminfo", "MemTotal: 1024 kB\nMemAvailable: 256 kB\n")
        self.write(self.proc / "cpuinfo", "model name: Synthetic CPU\nphysical id: 0\ncore id: 0\n")
        self.write(self.proc / "uptime", "123.8 0\n")

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def sampler(self, **kwargs):
        def sample_window(seconds):
            self.clock.value += seconds
            self.write(self.proc / "stat", "cpu 20 0 20 100 0 0 0 0\ncpu0 20 0 20 100 0 0 0 0\n")

        return SystemSampler(
            self.proc, self.sys, enabled=True, monotonic=self.clock,
            sleep=sample_window, epoch=lambda: 1700000000,
            mode_file=self.root / "absent-mode", **kwargs,
        )

    def test_cpu_and_memory_counter_meaning(self):
        before = cpu_counters("cpu 10 0 10 80 0\ncpu0 10 0 10 80 0\nignored garbage\n")
        after = cpu_counters("cpu 20 0 20 100 0\ncpu0 20 0 20 100 0\n")
        self.assertEqual(cpu_delta(before, after)["percent"], 50.0)
        self.assertIsNone(cpu_delta(after, before)["percent"])
        self.assertEqual(memory_info("MemTotal: 1024 kB\nMemAvailable: 256 kB\n")["percent"], 75.0)
        self.assertEqual(memory_info("")["status"], "unavailable")

    def test_snapshot_reads_cpu_memory_disk_and_timestamp(self):
        self.write(self.proc / "self/mountinfo", "36 25 8:1 / / rw - ext4 /dev/sda1 rw\n")
        usage = SimpleNamespace(f_blocks=100, f_frsize=4096, f_bfree=25, f_bavail=20)
        with patch("os.statvfs", return_value=usage, create=True):
            snapshot = self.sampler().collect()
        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["cpu"]["percent"], 50.0)
        self.assertEqual(snapshot["cpu"]["model"], "Synthetic CPU")
        self.assertEqual(snapshot["memory"]["used_bytes"], 768 * 1024)
        self.assertEqual(snapshot["disks"][0]["percent"], 75.0)
        self.assertEqual(snapshot["uptime_seconds"], 123)
        self.assertEqual(snapshot["collected_at"], "2023-11-14T22:13:20+00:00")
        self.assertEqual(snapshot["history"], "none")

    def test_configured_cache_prevents_repeated_collection(self):
        sampler = self.sampler(cache_seconds=5)
        first = sampler.collect()
        self.write(self.proc / "meminfo", "MemTotal: 1024 kB\nMemAvailable: 512 kB\n")
        self.clock.value += 2
        self.assertEqual(sampler.collect()["memory"]["percent"], 75.0)
        self.clock.value += 5
        self.assertEqual(sampler.collect()["memory"]["percent"], 50.0)
        self.assertEqual(first["memory"]["percent"], 75.0)

    def test_suspended_gpu_is_not_probed_for_dynamic_counters(self):
        # Portable fixture name: Windows cannot create PCI names containing ':'.
        device = self.sys / "bus/pci/devices/fixture-gpu"
        for name, value in {"class": "0x030000", "vendor": "0x10de", "device": "0x1234", "power/runtime_status": "suspended"}.items():
            self.write(device / name, value)
        sampler = self.sampler()
        original = sampler._read
        forbidden = {"gpu_busy_percent", "mem_info_vram_total", "mem_info_vram_used", "power1_average", "power1_input", "power1_cap"}

        def guarded_read(path, limit=65536):
            self.assertNotIn(Path(path).name, forbidden)
            return original(path, limit)

        sampler._read = guarded_read
        snapshot = sampler.collect()
        self.assertEqual(len(snapshot["gpus"]), 1)
        self.assertEqual(snapshot["gpus"][0]["reason"], "suspended")
        self.assertIsNone(snapshot["gpus"][0]["percent"])
        self.assertIsNone(snapshot["gpus"][0]["vram_used_bytes"])

    def test_non_linux_reports_unavailable_consistently(self):
        snapshot = SystemSampler(enabled=False).collect()
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertEqual(snapshot["reason"], "linux_required")
        for key in ("disks", "gpus", "temperatures", "fans"):
            self.assertEqual(snapshot[key], [])
        self.assertIsNone(snapshot["collected_at"])
        self.assertEqual(snapshot["history"], "none")

    def test_module_uses_only_public_registration(self):
        services = {}
        cleanups = []
        handlers = {}
        context = SimpleNamespace(config={"refreshSeconds": 2}, logger=logging.getLogger("test.system"),
                                  data_dir=self.root,
                                  services=SimpleNamespace(provide=lambda name, value: services.__setitem__(name, value)),
                                  http=SimpleNamespace(register=lambda method, path, handler: handlers.__setitem__((method, path), handler)),
                                  add_cleanup=cleanups.append)
        module = Module()
        try:
            module.start(context)
            self.assertIn("system.resources", services)
            self.assertEqual(services["system.resources"].cache_seconds, 2)
            self.assertIn("message", module.status())
            self.assertIn(("GET", "/history"), handlers)
            history = handlers[("GET", "/history")](SimpleNamespace(args=MultiDict({"interval": "600"})))
            self.assertEqual(history["interval_seconds"], 600)
            self.assertIn("last_sample", history["monitor"])
        finally:
            module.stop()
            for cleanup in reversed(cleanups):
                cleanup()
        self.assertEqual(module.status()["message"], "未启动")

    def test_invalid_refresh_config_is_rejected(self):
        Module.validate_config({})
        for value in (True, "5", 0, 301, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Module.validate_config({"refreshSeconds": value})
        with self.assertRaises(ValueError):
            Module.validate_config({"other": 1})

    def test_busy_collector_returns_public_service_error(self):
        from xixi_core.platform.errors import PlatformError
        sampler = ResourceSampler(self.proc, self.sys, enabled=True, lock_timeout=0.03)
        sampler.lock.acquire()
        try:
            with self.assertRaises(PlatformError) as raised:
                sampler.collect()
            self.assertEqual(raised.exception.code, "collection_in_progress")
            self.assertEqual(raised.exception.status, 503)
        finally:
            sampler.lock.release()

    def test_live_request_shares_inflight_cold_minute_sample(self):
        entered, release, live_done = threading.Event(), threading.Event(), threading.Event()
        results, errors, windows = {}, {}, []

        def sample_window(seconds):
            windows.append(seconds)
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test sampling gate timed out")
            self.clock.value += seconds
            self.write(self.proc / "stat", "cpu 20 0 20 100 0 0 0 0\ncpu0 20 0 20 100 0 0 0 0\n")

        sampler = ResourceSampler(self.proc, self.sys, enabled=True, monotonic=self.clock,
                                  sleep=sample_window, cache_seconds=1, lock_timeout=1,
                                  mode_file=self.root / "absent-mode")
        monitor = ResourceMonitor(sampler, self.root, now=lambda: 120)

        def minute_sample():
            try:
                results["minute"] = monitor.sample_once()
            except Exception as exc:
                errors["minute"] = exc

        def live_request():
            try:
                results["live"] = sampler.collect()
            except Exception as exc:
                errors["live"] = exc
            finally:
                live_done.set()

        history_thread = threading.Thread(target=minute_sample)
        live_thread = threading.Thread(target=live_request)
        history_thread.start()
        try:
            self.assertTrue(entered.wait(1), "minute worker did not begin the cold CPU sample")
            live_thread.start()
            self.assertFalse(live_done.wait(0.1), "live request failed instead of waiting for the in-flight sample")
        finally:
            release.set()
            history_thread.join(timeout=2)
            if live_thread.ident is not None:
                live_thread.join(timeout=2)
        self.assertFalse(history_thread.is_alive())
        self.assertFalse(live_thread.is_alive())
        self.assertEqual(errors, {})
        self.assertEqual(results["live"]["cpu"]["percent"], 50.0)
        self.assertIs(results["minute"], results["live"])
        self.assertEqual(windows, [0.15], "only one sample should run for both callers")
        with monitor.history.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 1)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Real /proc metrics require Linux")
    def test_real_linux_snapshot(self):
        snapshot = SystemSampler().collect()
        self.assertEqual(snapshot["status"], "ok")
        self.assertGreater(snapshot["cpu"]["logical_cores"], 0)
        self.assertGreater(snapshot["memory"]["total_bytes"], 0)
        self.assertIsInstance(snapshot["uptime_seconds"], int)


def historical_snapshot(cpu=20, memory=50, temperature=60, disk=70):
    return {
        "status": "ok",
        "cpu": {"percent": cpu},
        "memory": {"percent": memory},
        "temperatures": [{"component": "coretemp", "status": "ok", "celsius": temperature}],
        "disks": [{"id": "8:1", "mount": "/", "status": "ok", "percent": disk}],
    }


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_history_survives_reopen_and_keeps_original_aggregation(self):
        history = History(self.root, now=lambda: 600)
        history.record(historical_snapshot(cpu=10, memory=50, temperature=60, disk=40), 0)
        history.record(historical_snapshot(cpu=30, memory=70, temperature=80, disk=80), 60)
        reopened = History(self.root, now=lambda: 600)
        result = reopened.query(600)
        first = next(point for point in result["points"] if point["at"] == 0)
        self.assertEqual(first, {"at": 0, "cpu": 20, "memory": 60, "temperature": 80, "count": 2})
        self.assertEqual(result["disks"][0]["percent"], 60)
        self.assertEqual(result["disks"][0]["peak"], 80)
        self.assertIsNone(next(point for point in result["points"] if point["at"] == 600)["cpu"])
        self.assertEqual(result["retention_days"], 30)
        for interval in (60, 600, 1800):
            self.assertEqual(reopened.query(interval)["interval_seconds"], interval)
        for interval in (True, "60", 0, 300):
            with self.assertRaises(ValueError):
                reopened.query(interval)

    def test_history_retains_30_days_without_alert_tables(self):
        history = History(self.root)
        history.record(historical_snapshot(), 0)
        history.record(historical_snapshot(), 31 * 86400)
        with history.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM disks").fetchone()[0], 1)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"samples", "disks"})

    def test_minute_worker_stops_without_waiting_for_next_minute(self):
        sampler = SimpleNamespace(enabled=True, collect=lambda: historical_snapshot())
        monitor = ResourceMonitor(sampler, self.root, now=lambda: 120)
        recorded = threading.Event()
        original_record = monitor.history.record

        def record(data, at):
            original_record(data, at)
            recorded.set()

        monitor.history.record = record
        try:
            monitor.start()
            first = monitor.thread
            monitor.start()
            self.assertIs(monitor.thread, first)
            self.assertTrue(recorded.wait(2), "initial sample was not persisted")
        finally:
            monitor.stop()
        self.assertFalse(first.is_alive())
        self.assertFalse(monitor.status()["running"])
        reopened = ResourceMonitor(sampler, self.root)
        self.assertEqual(reopened.status()["last_sample"], 120)
        with reopened.history.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 1)

    def test_unavailable_system_does_not_write_fabricated_history(self):
        sampler = SimpleNamespace(enabled=False, collect=lambda: {"status": "unavailable", "reason": "linux_required"})
        monitor = ResourceMonitor(sampler, self.root)
        monitor.start()
        self.assertIsNone(monitor.thread)
        monitor.sample_once()
        with monitor.history.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 0)
        self.assertEqual(monitor.status()["last_error"], "linux_required")
        self.assertIsNone(monitor.status()["last_sample"])
        monitor.stop()

    def test_busy_minute_sample_records_failure_without_outer_retries(self):
        from xixi_core.platform.errors import PlatformError
        calls = []
        logged = threading.Event()

        def busy_collect():
            calls.append(1)
            raise PlatformError("collection_in_progress", "busy", 503)

        sampler = SimpleNamespace(enabled=True, collect=busy_collect)
        logger = SimpleNamespace(error=lambda *args: logged.set())
        monitor = ResourceMonitor(sampler, self.root, logger=logger)
        try:
            monitor.start()
            self.assertTrue(logged.wait(2), "failed sample was not recorded")
        finally:
            monitor.stop()
        self.assertEqual(calls, [1])
        self.assertEqual(monitor.status()["last_error"], "sample_failed")
        self.assertFalse(monitor.thread.is_alive())
        with monitor.history.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 0)

    def test_system_stop_joins_history_worker_but_keeps_console_service(self):
        from xixi_core.platform.app import CoreApp
        from xixi_core.platform.errors import PlatformError

        class Console:
            def start(self, context):
                context.services.provide("console.fixture", lambda: "still running")
            def stop(self):
                pass
            def status(self):
                return {}

        config = self.root / "platform.json"
        config.write_text(json.dumps({"plugins_dir": "plugins", "modules": {
            "system": {"enabled": True, "config": {}}, "console": {"enabled": True, "config": {}}
        }}), encoding="utf-8")
        sampler = SimpleNamespace(enabled=True, cache_seconds=1, collect=lambda: historical_snapshot())
        with patch("xixi_core.modules.system.ResourceSampler", return_value=sampler):
            with CoreApp(config, self.root / "runtime", builtins={"system": Module, "console": Console}) as app:
                snapshot = app.services.get("system.resources").collect()
                self.assertEqual(snapshot["history"], "server_minute")
                result = app.manager.dispatch_http("system", "GET", "/history", SimpleNamespace(args=MultiDict({"interval": "1800"})))
                self.assertEqual(result["interval_seconds"], 1800)
                self.assertTrue(result["monitor"]["running"])
                stopped = app.manager.stop("system")
                self.assertEqual(stopped["state"], "stopped")
                self.assertFalse(any(thread.name == "resource-history" and thread.is_alive() for thread in threading.enumerate()))
                self.assertEqual(app.services.get("console.fixture")(), "still running")
                with self.assertRaises(PlatformError):
                    app.manager.dispatch_http("system", "GET", "/history", SimpleNamespace(args=MultiDict()))


if __name__ == "__main__":
    unittest.main()
