"""Boot a private DSH 0.2.2-rc.2 copy against a loopback Messages fixture."""
from argparse import ArgumentParser
from collections import deque
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
from subprocess import Popen, TimeoutExpired
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


REPO = Path(__file__).resolve().parents[1]
VERSION = "0.2.2-rc.2"
FIXTURE_REPLY = "隔离 DSH 合成模型回复"
from native_prompts import configure

def install_patches(runtime):
    return configure(runtime, "apply")


class ModelFixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        if self.path != "/v1/messages" or self.headers.get("x-api-key") != "fixture-synthetic-model-token":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append(body)
        tools = {row.get("name") for row in body.get("tools", [])}
        newest = next((m["content"] for m in reversed(body.get("messages", [])) if m["role"] == "user"), "")
        blocks = [{"text": newest}] if isinstance(newest, str) else newest
        tool_result = any(row.get("type") == "tool_result" for row in blocks)
        latest_text = "\n".join(row.get("text", "") for row in blocks)
        if self.server.slow_turn is not None and "FIXTURE_CANCEL_TEST" in latest_text:
            arrived, release = self.server.slow_turn
            arrived.set()
            release.wait(10)
        kind = None
        for row in blocks:
            for line in row.get("text", "").splitlines():
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and ("messages" in value or set(value) == {"kind"}):
                    kind = value.get("kind")
                    break
        title = any("Create a concise title" in text or "FIXTURE_TITLE" in text
                    for text in self.system_text(body))
        response = FIXTURE_REPLY
        if title:
            response = "隔离测试标题"
        elif kind == "dream":
            response = json.dumps({"memories": [], "diary": {"title": "隔离测试日记", "body": FIXTURE_REPLY}}, ensure_ascii=False)
        elif kind == "handoff":
            response = "隔离测试会话交接摘要"
        extension = getattr(self.server, "response_override", None)
        if extension is not None:
            override = extension(body, kind)
            if override is not None:
                response = override
        if isinstance(response, dict):
            block = response
            stop = "tool_use"
        else:
            block = {"type": "text", "text": response}
            stop = "end_turn"
        if "library_search" in tools and "FIXTURE_TOOL_TEST" in latest_text and not tool_result:
            block = {"type": "tool_use", "id": "toolu_fixture_search", "name": "library_search",
                     "input": {"query": "fixture", "categories": ["认识与判断"]}}
            stop = "tool_use"
        if "library_record" in tools and "FIXTURE_TOOL_WRITE" in latest_text and not tool_result:
            block = {"type": "tool_use", "id": "toolu_fixture_record", "name": "library_record",
                     "input": {"body": "FIXTURE_NATIVE_WRITE", "category": "认识与判断", "title": "fixture native write"}}
            stop = "tool_use"
        events = [
            {"type": "message_start", "message": {"id": "fixture-synthetic", "type": "message", "role": "assistant",
                                                 "model": body["model"], "content": [], "usage": {"input_tokens": 10, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": block},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 3}},
            {"type": "message_stop"},
        ]
        if stop == "tool_use":
            payload = block.pop("input")
            block["input"] = {}
            events.insert(2, {"type": "content_block_delta", "index": 0,
                              "delta": {"type": "input_json_delta", "partial_json": json.dumps(payload)}})
        wire = "".join(f"event: {data['type']}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n" for data in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(wire)))
        self.end_headers()
        try:
            self.wfile.write(wire)
        except (BrokenPipeError, ConnectionResetError):
            return

    @staticmethod
    def system_text(body):
        system = body.get("system", [])
        return [system] if isinstance(system, str) else [row.get("text", "") for row in system]


def bridge_request(origin, token, path, body=None):
    request = Request(origin + path, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      data=json.dumps(body).encode() if body is not None else None)
    with urlopen(request, timeout=3) as response:
        return json.load(response)


def wait_ready(origin, token, child):
    for _ in range(160):
        if child.poll() is not None:
            raise RuntimeError(f"Isolated DSH stopped during startup ({child.returncode})")
        try:
            return bridge_request(origin, token, "/health")
        except (OSError, URLError):
            time.sleep(0.1)
    raise RuntimeError("Isolated DSH bridge did not open")


def verify(root, origin, token, fixture, overlay, child, core_port):
    sys.path.insert(0, str(REPO / "packages" / "xixi-core"))
    sys.path.insert(0, str(REPO))
    from plugins.dsh_bridge.plugin import DshService
    from xixi_core.platform.errors import PlatformError
    from xixi_core.runtime import BridgeRuntime

    class RequestBody:
        def __init__(self, body):
            self.body = body

        def get_json(self):
            return self.body

    class Context:
        class UnavailableServices:
            def get(self, _service):
                raise PlatformError("service_unavailable", "验证模式未连接图书馆", 503)

        data_dir = root / "core-verification"
        services = UnavailableServices()

    context = Context()
    context.data_dir.mkdir(exist_ok=True)
    service = DshService(BridgeRuntime(origin, token), "dsh", token, context, 35)
    capabilities = service.capabilities()
    assert all(capabilities["hostCapabilities"].get(part) == 1 for part in (
        "time", "agentInstructions", "planMode", "runtimeContext", "modelSelection", "workingDirectory", "sessionTitle", "compaction"))
    old = service.open_session()
    saved = service.policy.get(None)
    policy = deepcopy(saved["policy"])
    originals = saved["capabilities"]["catalog"]
    time_starter = originals["time"]["parts"]["time"]
    roles = originals["system"]["originalSections"]
    assert roles["plan:policy"].startswith("You are in plan mode.")
    assert "structured checkpoint" in originals["compaction"]["parts"]["instruction"]["originalBody"]
    assert "automatically generated checkpoint" in originals["compaction"]["parts"]["checkpoint"]["originalBody"]
    prefix = originals["system"]["originalPresets"]["standard"]["prefix"]
    policy["standard"]["overrides"] = [
        {"id": "harness:identity", "text": "FIXTURE_SYSTEM", "enabled": True},
        {"id": "deployment:persona-prefix", "text": prefix + "\nFIXTURE_PREFIX", "enabled": True, "templateSyntax": "dsh"},
        {"id": "deployment:persona-suffix", "text": "FIXTURE_SUFFIX", "enabled": True},
        {"id": "plan:policy", "text": roles["plan:policy"] + "FIXTURE_PLAN", "enabled": True},
    ]
    policy["runtime"]["time"] = {"mode": "template", "text": time_starter["editorText"] + "\nFIXTURE_TIME {{turn}}",
                                    "templateSyntax": time_starter["editorSyntax"]}
    policy["runtime"]["agentInstructions"] = {"mode": "template", "templates": {"intro": {"text": "FIXTURE_INSTRUCTIONS"}}}
    policy["runtime"]["runtimeContext"] = {"mode": "template", "templates": {"initialIntro": {"text": "FIXTURE_RUNTIME"}}}
    policy["runtime"]["sessionTitle"] = {"mode": "template", "templates": {"system": {"text": "FIXTURE_TITLE"},
                                            "inputIntro": {"text": "FIXTURE_TITLE_INPUT"}}}
    policy["minimal"] = {"mode": "override", "text": "FIXTURE_MINIMAL"}
    revision = service.policy.save(RequestBody({"policy": policy, "compaction": {"checkpoint": "FIXTURE_CHECKPOINT"},
                                               "expectedRevision": saved["revision"], "requestId": secrets.token_hex(12)}))["revision"]
    new = service.open_session()
    assert old != new and service.open_session(new) == new
    row = {"id": "verify-msg", "channel": "fixture", "peer": "owner", "time": 1795000000,
           "text": "ISOLATED_VERSION_CHECK", "attachments": [{"ref": "synthetic-attachment"}]}
    start = len(fixture.calls)
    assert service.execute(old, [row]) == FIXTURE_REPLY
    baseline_calls = fixture.calls[start:]
    assert not any("FIXTURE_SYSTEM" in json.dumps(call) for call in baseline_calls)
    start = len(fixture.calls)
    assert service.execute(new, [row], kind="startup_unread", prompt="FIXTURE_STARTUP",
                           context={"handoff": "FIXTURE_HANDOFF", "shutdown": {"estimated": True}}) == FIXTURE_REPLY
    for _ in range(100):
        if any("FIXTURE_TITLE" in json.dumps(call) for call in fixture.calls[start:]):
            break
        time.sleep(0.05)
    captures = [json.dumps(call, ensure_ascii=False) for call in fixture.calls[start:]]
    primary = next(call for call in captures if "FIXTURE_STARTUP" in call and "FIXTURE_SYSTEM" in call)
    for marker in ("FIXTURE_TIME 1", "FIXTURE_INSTRUCTIONS", "FIXTURE_RUNTIME", "FIXTURE_HANDOFF", "synthetic-attachment", "1795000000"):
        assert marker in primary, marker
    auxiliary = next(call for call in captures if "FIXTURE_TITLE" in call)
    assert "FIXTURE_TITLE_INPUT" in auxiliary
    assert "Time sampled while preparing turn 1, step 1:" in primary and "{{now}}" not in primary
    assert "FIXTURE_PREFIX" in primary and "FIXTURE_SUFFIX" in primary and "{{model}}" not in primary
    assert "FIXTURE_PLAN" not in primary
    raw_context = bridge_request(origin, token, f"/sessions/{new}/context")["promptEvidence"]
    assert raw_context["nativeSessionAgentPreset"] == "standard"
    assert raw_context["configuredNativeCompactionVersions"] == {"checkpoint": revision}
    minimal = DshService(BridgeRuntime(origin, token), "dsh", token, context, 35, "minimal")
    minimal_id = minimal.open_session()
    start = len(fixture.calls)
    assert minimal.execute(minimal_id, [row]) == FIXTURE_REPLY
    assert any("FIXTURE_MINIMAL" in ModelFixture.system_text(call) for call in fixture.calls[start:])
    verify_cancel(service, new, row, fixture, PlatformError)
    verify_tools(root, origin, token, fixture, core_port)
    reload_bridge(overlay, origin, token, child)
    assert service.open_session(new) == new
    assert service.execute(new, [row]) == FIXTURE_REPLY
    reset = service.policy.save(RequestBody({"policy": saved["policy"], "compaction": {}, "expectedRevision": revision,
                                            "requestId": secrets.token_hex(12)}))
    inherited = service.open_session()
    start = len(fixture.calls)
    assert service.execute(inherited, [row]) == FIXTURE_REPLY
    assert not any("FIXTURE_SYSTEM" in json.dumps(call) or "FIXTURE_TIME" in json.dumps(call) for call in fixture.calls[start:])
    print(json.dumps({"dsh_version": VERSION, "mode": "real isolated DSH with loopback synthetic model",
                      "prompt_revision": revision, "markers": ["system", "minimal system", "time", "workspace instructions", "runtime context", "title system", "title introduction"],
                      "old_session_preserved": True, "native_final_reply": True, "library_tool_read_write": True,
                      "library_stop_removes_next_turn_tools": True, "dsh_profile_plugin_remount_same_process": True,
                      "cancel_then_next_turn": True,
                      "catalog_originals_and_time_starter": True, "restored_inheritance_revision": reset["revision"],
                      "capture_count": len(captures)}, ensure_ascii=False), flush=True)


def verify_cancel(service, session, row, fixture, expected_error):
    arrived, release, cancel = Event(), Event(), Event()
    result = []
    fixture.slow_turn = (arrived, release)

    def execute():
        try:
            result.append(service.execute(session, [{**row, "text": "FIXTURE_CANCEL_TEST"}], cancel_event=cancel))
        except expected_error as exc:
            result.append(exc.code)

    worker = Thread(target=execute)
    worker.start()
    try:
        assert arrived.wait(5), "DSH cancellation input did not reach the local model"
        cancel.set()
        worker.join(4)
        assert result == ["dsh_interrupted"], result
    finally:
        release.set()
        worker.join(10)
        fixture.slow_turn = None
    assert service.execute(session, [{**row, "text": "FIXTURE_AFTER_CANCEL"}]) == FIXTURE_REPLY


def reload_bridge(overlay, origin, token, child):
    full_profile = overlay.read_text(encoding="utf-8")
    standalone = full_profile.split("- insert:\n", 1)[0]
    overlay.write_text(standalone, encoding="utf-8")
    try:
        for _ in range(120):
            assert child.poll() is None
            try:
                bridge_request(origin, token, "/health")
            except URLError as exc:
                if isinstance(exc.reason, ConnectionRefusedError):
                    break
            except ConnectionResetError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("DSH profile unload did not release Bridge port")
    finally:
        overlay.write_text(full_profile, encoding="utf-8")
    assert wait_ready(origin, token, child)["dshVersion"] == VERSION


def verify_tools(root, bridge_origin, token, fixture, port):
    from werkzeug.serving import make_server
    from xixi_core.platform.app import CoreApp
    from xixi_core.platform.auth import AuthStore
    from xixi_core.platform.http import create_app

    from verify_owner_settings import VerificationMessageBridge, verify_owner_settings

    os.environ["XIXI_DSH_BRIDGE_TOKEN"] = token
    local_origin = f"http://127.0.0.1:{port}"
    with TemporaryDirectory(prefix="core-tools-", dir=root) as directory:
        isolated_core = Path(directory)
        config = isolated_core / "platform.json"
        config.write_text(json.dumps({"http": {"port": port, "origin": local_origin}, "plugins_dir": str(REPO / "plugins"),
                                      "modules": {"library": {"enabled": True, "config": {}},
                                                  "model_settings": {"enabled": True, "config": {}},
                                                  "persona": {"enabled": True, "config": {}},
                                                  "message_bridge": {"enabled": True, "config": {"mode": "offline"}},
                                                  "dsh_bridge": {"enabled": True, "config": {"mode": "dsh", "url": bridge_origin}},
                                                  "assistant": {"enabled": True, "config": {"automaticDream": False, "automaticHandoff": False}}}}), encoding="utf-8")
        password = "isolated-fixture-test-owner-password"
        AuthStore.initialize(isolated_core / "data" / "owner.auth.json", password=password)
        with CoreApp(config, isolated_core / "data", builtins={"message_bridge": VerificationMessageBridge}) as core:
            web = create_app(core)
            server = make_server("127.0.0.1", port, web, threaded=True)
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                client = web.test_client()
                csrf = client.get("/api/auth/session", base_url=local_origin).json["csrf"]
                auth = client.post("/api/auth/login", base_url=local_origin, json={"password": password},
                                   headers={"Origin": local_origin, "X-CSRF-Token": csrf})
                assert auth.status_code == 200
                policy_path = "/api/modules/dsh_bridge/prompt-policy"
                current = client.get(policy_path, base_url=local_origin).json
                policy = current["policy"]
                policy["runtime"]["time"] = {"mode": "template", "text": "FIXTURE_HTTP_POLICY"}
                saved = client.post(policy_path, base_url=local_origin, headers={"Origin": local_origin, "X-CSRF-Token": auth.json["csrf"]},
                                    json={"policy": policy, "expectedRevision": current["revision"], "requestId": "fixture-verify-http"})
                assert saved.status_code == 200 and saved.json["revision"] == 2, saved.get_data(as_text=True)
                library = core.services.get("library")
                library.dispatch("record", {"category": "认识与判断", "title": "fixture known memory", "body": "FIXTURE_LIBRARY_FOUND"},
                                 "fixture-seed-library")
                service = core.services.get("dsh_bridge")
                session = service.open_session()
                row = {"id": "verify-tool", "channel": "fixture", "peer": "owner", "time": 1795000000, "attachments": []}
                start = len(fixture.calls)
                assert service.execute(session, [{**row, "text": "FIXTURE_TOOL_TEST"}]) == FIXTURE_REPLY
                searched = [json.dumps(call) for call in fixture.calls[start:]]
                assert any("tool_result" in call and "fixture known memory" in call for call in searched)
                assert any("FIXTURE_HTTP_POLICY" in call for call in searched)
                assert service.execute(session, [{**row, "id": "verify-write", "text": "FIXTURE_TOOL_WRITE"}]) == FIXTURE_REPLY
                assert any(item["title"] == "fixture native write" for item in library.search("fixture native write"))
                verify_owner_settings(core, client, local_origin, {"Origin": local_origin, "X-CSRF-Token": auth.json["csrf"]}, fixture)
                assert core.manager.stop("library")["state"] == "stopped"
                start = len(fixture.calls)
                assert service.execute(session, [{**row, "id": "verify-disabled", "text": "FIXTURE_TOOL_TEST"}]) == FIXTURE_REPLY
                assert not any(any(tool["name"].startswith("library_") for tool in call.get("tools", [])) for call in fixture.calls[start:])
            finally:
                server.shutdown()
                server.server_close()


def main(verify_instance=verify):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--installed-dsh", required=True, type=Path)
    parser.add_argument("--node", required=True, type=Path)
    parser.add_argument("--bridge-port", type=int, required=True)
    parser.add_argument("--core-port", type=int, default=18910)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    if not root.name.startswith("dsh-fixture-") or root.stat().st_mode & 0o077:
        parser.error("root 须为 fixture 专属的 0700 目录")
    runtime = root / "dsh"
    if any(root.iterdir()):
        parser.error("Please use a new, empty private QA directory")
    shutil.copytree(args.installed_dsh, runtime, symlinks=True)
    if runtime.resolve() != runtime or runtime.samefile(args.installed_dsh):
        parser.error("需使用独立 DSH 副本")
    patches = install_patches(runtime)
    assert len(patches) == 8, patches
    home, work, ledger = (root / name for name in ("home", "work", "bridge-ledger"))
    for directory in (home, work, ledger):
        directory.mkdir(exist_ok=True, mode=0o700)
    (work / "AGENTS.md").write_text("Fixture synthetic workspace instructions.\n", encoding="utf-8")
    token_path = root / "fixture-dsh.env"
    if not token_path.exists():
        with os.fdopen(os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as target:
            target.write("XIXI_DSH_BRIDGE_TOKEN=" + secrets.token_urlsafe(36) + "\n")
    token = token_path.read_text(encoding="ascii").strip().split("=", 1)[1]
    config = root / "bridge.json"
    descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as target:
        json.dump({"token": token, "root": str(ledger), "cwd": str(work), "port": args.bridge_port,
                   "coreUrl": f"http://127.0.0.1:{args.core_port}",
                   "validationModel": "loopback-fixture"}, target)
    profile = home / "profiles" / "web"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "package.json").write_text(json.dumps({"name": "dsh-profile-web", "private": True, "dependencies": {},
                                                     "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"]}}}), encoding="utf-8")
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
    fixture.calls = []
    fixture.slow_turn = None
    worker = Thread(target=fixture.serve_forever, daemon=True)
    worker.start()
    model_url = f"http://127.0.0.1:{fixture.server_port}"
    overlay = profile / "cordis.patch.yml"
    plugin = REPO / "packages" / "xixi-bridge" / "src" / "existing-dsh-plugin.mjs"
    overlay.write_text(f"- id: llm-deepseek\n  config:\n    baseURL: {model_url}\n- id: llm-deepseek-account\n  disabled: true\n"
                       f"- insert:\n    - id: xixi-platform-dsh-bridge\n      name: {plugin.as_uri()}\n"
                       f"      config:\n        configFile: {config}\n", encoding="utf-8")
    environment = {"PATH": str(args.node.parent) + ":/usr/bin:/bin", "LANG": "C.UTF-8", "DSH_HOME": str(home),
                   "DEEPSEEK_BASE_URL": model_url, "DEEPSEEK_API_KEY": "fixture-synthetic-model-token",
                   "NO_PROXY": "127.0.0.1,localhost", "XDG_CONFIG_HOME": str(home / "config"),
                   "XDG_CACHE_HOME": str(home / "cache"), "XDG_DATA_HOME": str(home / "data")}
    stopping = Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    log_fd = os.open(root / "dsh-fixture.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(log_fd, "wb") as log:
        child = Popen([str(args.node), str(runtime / "node_modules" / ".bin" / "dsh"), "web", "--host", "127.0.0.1",
                       "--port", "0", "--no-open"], cwd=work, env=environment, stdout=log, stderr=log)
        origin = f"http://127.0.0.1:{args.bridge_port}"
        try:
            health = wait_ready(origin, token, child)
            assert health["dshVersion"] == VERSION
            if args.serve:
                fixture.calls = deque(maxlen=0)
                print(json.dumps({"state": "ready", "dsh_version": VERSION, "bridge_port": args.bridge_port,
                                  "core_callback_port": args.core_port, "model": "loopback synthetic"}), flush=True)
                while not stopping.wait(1) and child.poll() is None:
                    pass
            else:
                with TemporaryDirectory(prefix="isolation-check-", dir=root) as directory:
                    verify_instance(Path(directory), origin, token, fixture, overlay, child, args.core_port)
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except TimeoutExpired:
                    child.kill()
                    child.wait()
            fixture.shutdown()
            fixture.server_close()


if __name__ == "__main__":
    main()
