"""Core-facing adapter for the Bridge plugin loaded by the DSH host."""
from hmac import compare_digest
import json
import math
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from uuid import uuid4

from xixi_core.platform.errors import PlatformError
from xixi_core.runtime import BridgeRuntime, MockRuntime

from .policy import PromptPolicy


OPERATIONS = {"library_search": "search", "library_read": "get", "library_record": "record"}
TASK_READ_TOOLS = ("task_status", "task_result_read", "task_artifact_read")
TASK_DAILY_TOOLS = ("task_create", "task_cancel", *TASK_READ_TOOLS)
TASK_WORK_TOOLS = (*TASK_READ_TOOLS, "task_artifact_write")
KINDS = {"chat", "startup_unread", "handoff", "dream"}
SESSION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,180}$")


class DshService:
    def __init__(self, runtime, mode, token, context, timeout, agent_preset="standard"):
        self.runtime = runtime
        self.mode = mode
        self._token = token
        self.context = context
        self.timeout = timeout
        self.agent_preset = agent_preset
        self.poll_interval = 0.2
        self.policy = PromptPolicy(context.data_dir / "prompt-policy.json", self.capabilities)

    def _request(self, path, body=None):
        try:
            return self.runtime.request(path, body, timeout=2)
        except HTTPError as exc:
            with exc:
                try:
                    error = json.load(exc).get("error", {})
                except ValueError:
                    error = {}
            if isinstance(error, dict):
                code = error.get("code")
                detail = error.get("message", "")
                if code == "BRIDGE_ERROR" and re.fullmatch(r"[A-Z0-9_]+", str(detail)):
                    code = detail
            else:
                code = error
            raise PlatformError("dsh_request_failed", f"DSH 桥拒绝请求（{code or exc.code}）", 502) from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise PlatformError("dsh_unreachable", "DSH 桥连接失败或未返回有效结果", 503) from exc

    def health(self):
        return self.runtime.health() if self.mode == "mock" else self._request("/health")

    def capabilities(self):
        if self.mode == "mock":
            return {"version": 1, "hostCapabilities": {}, "catalog": {}, "mock": True}
        try:
            return self._request("/prompt-policy/capabilities")
        except PlatformError:
            return {"version": 1, "hostCapabilities": {}, "catalog": {}, "unavailable": True}

    def model_catalog(self):
        return self.runtime.model_catalog() if self.mode == "mock" else self._request("/models")

    def _optional_service(self, name):
        try:
            return self.context.services.get(name)
        except PlatformError as exc:
            if exc.code != "service_unavailable":
                raise
            return None

    def _session_state(self, session):
        if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
            raise ValueError("DSH 会话编号无效")
        return self.runtime.state(session) if self.mode == "mock" else self._request(f"/sessions/{session}/state")

    def open_session(self, current=None, *, kind="daily", context=None):
        if current is not None:
            self._session_state(current)
            return current
        if kind not in {"daily", "idle", "dream", "work"}:
            raise ValueError("不支持的 DSH 会话用途")
        session = "platform-" + uuid4().hex
        self._create_session(self._session_specification(session, kind, context))
        return session

    def _session_specification(self, session, kind, context):
        models = self._optional_service("model_settings")
        model_fields = models.session_fields(kind) if models else {}
        preset = model_fields.get("agentPreset", self.agent_preset)
        specification = {"id": session, "kind": kind, "agentPreset": preset, "toolScopes": [],
                         "platformLibraryTools": True, **model_fields, **self.policy.session_fields()}
        persona = self._optional_service("persona")
        if persona:
            sections = persona.session_sections(kind, agent_preset=preset, context=context, runtime=self.mode)
            minimal = specification["promptPolicy"]["minimal"]
            if preset == "minimal" and minimal["mode"] == "inherit" and any(s["placement"] == "system" for s in sections):
                raise PlatformError("minimal_persona_conflict", "角色卡包含系统提示。请在原生提示页将精简模式设为覆盖，或在模型设置选择标准模式。", 409)
            specification["promptSections"] = sections
        return specification

    def _create_session(self, specification):
        if self.mode == "mock":
            self.runtime.create(specification)
        else:
            self._request("/sessions", specification)

    def freeze_work(self, task_id, work):
        specification = self._session_specification("work-" + task_id, "work", work)
        persona = self._optional_service("persona")
        run_text = persona.run_text("work", [], agent_preset=specification["agentPreset"], context=work,
                                    runtime=self.mode) if persona else ""
        already_rendered = "\n".join([run_text, *(section["text"] for section in specification.get("promptSections", []))])
        original = {"title": work["title"]}
        if work["objective"] not in already_rendered:
            original["objective"] = work["objective"]
        refs = [ref for ref in work["contextRefs"] if ref not in already_rendered]
        if refs:
            original["contextRefs"] = refs
        return {"specification": specification, "text": "\n\n".join(part for part in (
            json.dumps({"kind": "work", **original}, ensure_ascii=False), run_text,
        ) if part)}

    def run_background_work(self, frozen, *, cancel_event):
        self._check_cancel(cancel_event)
        specification = frozen["specification"]
        self._create_session(specification)
        self._check_cancel(cancel_event)
        presentation = "\n\n".join(part for part in (frozen["text"], self._library_notice("work")) if part)
        return self._deliver_turn(specification["id"], {
            "inputId": "input-" + specification["id"], "text": frozen["text"], "presentationText": presentation,
            "libraryTools": self._library_tools("work"), "assistantTools": self._assistant_tools("work"),
        }, cancel_event)

    def _library_notice(self, kind):
        persona = self._optional_service("persona")
        if persona and self._optional_service("library") is None:
            return persona.library_unavailable_text(kind)
        return ""

    def _assistant_tools(self, kind, owner=None):
        if self.mode == "mock" or self._optional_service("assistant") is None:
            return []
        if kind == "work":
            return list(TASK_WORK_TOOLS)
        if kind in {"chat", "startup_unread"}:
            return list(TASK_DAILY_TOOLS if owner is not None else TASK_READ_TOOLS)
        return []

    @staticmethod
    def _check_cancel(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise PlatformError("dsh_interrupted", "DSH 执行已中断", 409)

    def _library_tools(self, kind):
        if self.mode == "mock":
            return []
        try:
            self.context.services.get("library")
        except PlatformError:
            return []
        return [tool for tool in OPERATIONS if kind in {"chat", "startup_unread"} or tool != "library_record"]

    def execute(self, session, messages, *, kind="chat", prompt="", context=None, cancel_event=None):
        if session is None:
            raise ValueError("DSH 执行需要已打开的会话")
        state = self._session_state(session)
        if (not isinstance(kind, str) or kind not in KINDS or not isinstance(messages, (list, tuple))
                or not isinstance(prompt, str) or context is not None and not isinstance(context, dict)):
            raise ValueError("DSH 执行参数无效")
        rows = [{key: message[key] for key in ("id", "channel", "peer", "time", "text", "attachments", "receivedAt", "replyTo", "source", "event", "sender")
                 if key in message} for message in messages]
        self._check_cancel(cancel_event)
        raw_text = "\n".join(row["text"] for row in rows)
        delivery_context = dict(context or {})
        recovery = delivery_context.pop("inputRecoveryText", "")
        persona = self._optional_service("persona")
        session_kind = state.get("kind", {"handoff": "idle", "dream": "dream"}.get(kind, "daily"))
        preset = state.get("agentPreset", self.agent_preset)
        if persona:
            maintenance = kind in {"handoff", "dream"}
            # Persona templates own these values across system, opening and turn placements.
            # Only operational metadata outside those templates accompanies the rendered text.
            template_inputs = ("handoff", "selectedOriginals", "sourceMessages", "existingMemories", "existingDiary",
                               "fixedRange", "priorHandoff", "day", "currentStageSummaries")
            for key in template_inputs:
                delivery_context.pop(key, None)
            rendered = persona.run_text(session_kind, [] if maintenance else rows, agent_preset=preset,
                                        context=context, runtime=self.mode)
            parts = [json.dumps({"kind": kind}) if maintenance else "", prompt, json.dumps(delivery_context, ensure_ascii=False, allow_nan=False) if delivery_context else "", rendered]
            presentation = "\n\n".join(part for part in parts if part)
        else:
            presentation = json.dumps({"kind": kind, "prompt": prompt, "context": delivery_context, "messages": rows},
                                      ensure_ascii=False, allow_nan=False)
        if kind in {"chat", "startup_unread"}:
            presentation = "\n\n".join(part for part in (presentation, recovery, self._library_notice("daily")) if part)
        owner = next((row for row in reversed(rows) if row.get("source", "owner") == "owner"), None)
        envelope = {"inputId": "input-" + uuid4().hex, "text": raw_text, "presentationText": presentation,
                    "libraryTools": self._library_tools(kind)}
        assistant_tools = self._assistant_tools(kind, owner)
        if assistant_tools:
            envelope["assistantTools"] = assistant_tools
            if owner is not None:
                envelope["assistantOrigin"] = {"channel": owner["channel"], "peer": owner["peer"]}
        result = self._deliver_turn(session, envelope, cancel_event)
        if self.mode == "mock" and kind == "handoff":
            return "[mock 演示] 会话交接摘要"
        if self.mode == "mock" and kind == "dream":
            return json.dumps({"memories": [], "diary": {"title": "mock 日记", "body": result}}, ensure_ascii=False)
        return result

    def _deliver_turn(self, session, envelope, cancel_event):
        if self.mode == "mock":
            return self.runtime.deliver(session, envelope)["text"]
        input_id = envelope["inputId"]
        base = f"/sessions/{session}"
        status_path = f"{base}/inputs/{input_id}"
        deadline = time.monotonic() + self.timeout
        try:
            result = self._request(base + "/deliver", envelope)
        except PlatformError as exc:
            if exc.code != "dsh_unreachable":
                raise
            # Admission can complete after the response times out. Never submit a second request.
            result = self._request(status_path)
        while True:
            status = result.get("status")
            if status == "completed" and isinstance(result.get("text"), str):
                return result["text"]
            if status not in {"preparing", "accepted", "running", "completed"}:
                raise PlatformError("dsh_turn_failed", f"DSH 本轮未完成（{status}）", 502)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._request(base + "/cancel", {"inputId": input_id, "keepInbox": False})
                raise PlatformError("dsh_turn_timeout", "等待 DSH 回复超时，已请求中断本轮", 504)
            delay = min(remaining, self.poll_interval)
            if cancel_event is None:
                time.sleep(delay)
            elif cancel_event.wait(delay):
                self._request(base + "/cancel", {"inputId": input_id, "keepInbox": False})
                raise PlatformError("dsh_interrupted", "已请求中断 DSH 本轮", 409)
            result = self._request(status_path)

    def authenticate_tool(self, authorization):
        if (self.mode != "dsh" or not isinstance(authorization, str)
                or not compare_digest(authorization.encode(), ("Bearer " + self._token).encode())):
            raise PlatformError("dsh_tool_unauthorized", "DSH 工具调用认证失败", 401)

    def handle_tool(self, request):
        if not isinstance(request, dict) or request.get("sessionKind") not in {"daily", "idle", "dream", "work"}:
            raise PlatformError("dsh_tool_invalid", "DSH 工具请求格式无效")
        tool, args, request_id = (request.get(key) for key in ("tool", "args", "requestId"))
        if (not isinstance(tool, str) or tool not in OPERATIONS and tool not in TASK_DAILY_TOOLS and tool not in TASK_WORK_TOOLS
                or not isinstance(args, dict)
                or not isinstance(request_id, str) or not 1 <= len(request_id) <= 300):
            raise PlatformError("dsh_tool_invalid", "DSH 工具名称或参数无效")
        if tool == "library_record" and request["sessionKind"] != "daily":
            raise PlatformError("dsh_tool_invalid", "整理会话只能查询图书馆")
        try:
            if tool in OPERATIONS:
                library = self.context.services.get("library")
                arguments = dict(args)
                if tool == "library_read":
                    arguments["id"] = arguments.pop("itemId")
                if tool == "library_record":
                    arguments.pop("requestId", None)
                value = library.dispatch(OPERATIONS[tool], arguments, request_id)
            else:
                value = self._handle_task_tool(tool, args, request, request_id)
        except (PlatformError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        except (KeyError, TypeError):
            return {"ok": False, "error": "工具参数缺失或类型无效"}
        return {"ok": True, "data": value}

    def _handle_task_tool(self, tool, args, request, request_id):
        scopes = {"daily": TASK_DAILY_TOOLS, "work": TASK_WORK_TOOLS}.get(request["sessionKind"], ())
        if tool not in scopes:
            raise PlatformError("dsh_tool_invalid", "当前会话不能调用此工具", 403)
        assistant = self.context.services.get("assistant")
        if tool == "task_create":
            origin = request.get("origin")
            if not isinstance(origin, dict):
                raise ValueError("独立工作需要原日常消息的接收方")
            return assistant.submit_background_task(args, request_id=request_id, origin={**origin, "sessionId": request["sessionId"]})
        if tool == "task_cancel":
            return assistant.cancel_task(args["taskId"])
        if tool == "task_status":
            return assistant.task_status(args["taskId"])
        if tool == "task_result_read":
            paging = {key: args[key] for key in ("offset", "limit") if key in args}
            return assistant.read_task_result(args["taskId"], **paging)
        if tool == "task_artifact_write":
            return assistant.write_task_artifact(request["sessionId"], args, request_id)
        return assistant.read_task_artifact(args)


class Plugin:
    @staticmethod
    def validate_config(config):
        if not isinstance(config, dict) or set(config) - {"mode", "url", "tokenEnv", "timeoutSeconds", "agentPreset"}:
            raise ValueError("DSH 桥配置包含未知字段")
        if config.get("mode", "dsh") not in ("mock", "dsh"):
            raise ValueError("DSH 桥 mode 只支持 dsh 或 mock")
        if config.get("agentPreset", "standard") not in ("standard", "minimal"):
            raise ValueError("DSH 桥 agentPreset 只支持 standard 或 minimal")
        timeout = config.get("timeoutSeconds", 300)
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("DSH 桥 timeoutSeconds 须为正数")
        if config.get("mode", "dsh") == "dsh":
            url = config.get("url", "http://127.0.0.1:18766")
            if not isinstance(url, str):
                raise ValueError("DSH 桥 url 须为文本")
            parsed = urlsplit(url)
            if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"} or not parsed.port
                    or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
                raise ValueError("DSH 桥 url 须为明确的本机 HTTP 地址和端口")
            token_env = config.get("tokenEnv", "XIXI_DSH_BRIDGE_TOKEN")
            if not isinstance(token_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", token_env):
                raise ValueError("DSH 桥 tokenEnv 须为大写环境变量名")

    def start(self, context):
        settings = context.config
        self.validate_config(settings)
        mode = settings.get("mode", "dsh")
        token = ""
        if mode == "dsh":
            token = os.environ.get(settings.get("tokenEnv", "XIXI_DSH_BRIDGE_TOKEN"), "")
            if len(token) < 24 or not token.isascii() or not token.isprintable():
                raise ValueError("DSH 桥令牌环境变量须提供至少 24 个可打印 ASCII 字符")
            runtime = BridgeRuntime(settings.get("url", "http://127.0.0.1:18766"), token)
        else:
            runtime = MockRuntime(context.data_dir / "mock-dsh.sqlite3")
            context.add_cleanup(runtime.db.close)
        service = DshService(runtime, mode, token, context, settings.get("timeoutSeconds", 300),
                             settings.get("agentPreset", "standard"))
        health = service.health()
        if mode == "dsh" and health.get("connectionMode") != "existing-dsh":
            raise ValueError("指定地址未提供现有 DSH 桥")
        self._status = {"mode": mode, "agentPreset": service.agent_preset, "dshVersion": health.get("dshVersion"),
                        "model": health.get("modelConnection", {}).get("validationModel")}
        if mode == "dsh":
            self._status["url"] = runtime.url
        context.services.provide("dsh_bridge", service)
        context.http.register("GET", "/status", lambda _request: service.health())
        context.http.register("GET", "/prompt-policy", service.policy.get)
        context.http.register("POST", "/prompt-policy", service.policy.save)

    def status(self):
        return dict(self._status)

    def stop(self):
        pass
