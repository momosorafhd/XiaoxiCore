"""Assistant input, daily session, and maintenance on the Core plugin SDK."""
from datetime import datetime, timezone
import json
import math
import threading
import time
import uuid

from xixi_core.config import DEFAULT as CORE_DEFAULTS
from xixi_core.platform.errors import PlatformError

from .state import Store, message_record
from .work_events import EVENT_KINDS, WorkEvents


MIN_DREAM_INTERVAL = 12 * 60 * 60
DEFAULTS = {
    "startOnEnable": False,
    "startupUnreadPrompt": "以下消息在你停机期间收到。请结合附带的停机信息、消息原时间和当前会话，按正常对话流程处理并回复。",
    "handoffPrompt": "请回顾当前会话，整理正在讨论的主题、已确定事项、未完成工作及继续交流所需的原话。返回 JSON，handoff 为最多 12000 字的交接摘要，selectedOriginals 最多列出 12 个需要保留的材料编号。",
    "dreamPrompt": (
        "请回顾附带的对话与现有记忆，形成或修订值得保留的记忆并写一篇日记。区分原话事实与理解。"
        "只返回 JSON 对象，含 memories 数组与 diary 对象。日记含 title 和 body。记忆含 title、body、category，"
        "category 使用事实与经历、认识与判断、兴趣、黑话与名词解释、资料其中之一；资料须有 summary。"
        "修订应引用已有项的 id 和 expectedVersion（所给 version）。无需新增或修订记忆时返回空数组。"
        "记忆的 sourceRefs 须填可核对的归档编号；没有可核对来源时不生成该记忆。"
    ),
    "maxBatchMessages": 12,
    "maxBatchChars": 16000,
    "heartbeatSeconds": 15,
    "automaticHandoff": True,
    "handoffIdleSeconds": 900,
    "handoffMinTurns": 8,
    "automaticDream": True,
    "dreamIdleSeconds": 14400,
    "dreamIntervalSeconds": 86400,
    "dreamMaxMessages": 50,
}

NUMERIC_LIMITS = {
    "maxBatchMessages": (1, 100),
    "maxBatchChars": (1, 1000000),
    "heartbeatSeconds": (1, 300),
    "handoffIdleSeconds": (1, 86400),
    "handoffMinTurns": (1, 10000),
    "dreamIdleSeconds": (1, 604800),
    "dreamIntervalSeconds": (MIN_DREAM_INTERVAL, 604800),
    "dreamMaxMessages": (1, 500),
}


class Plugin:
    def __init__(self):
        self._condition = threading.Condition()
        self._state = "not_started"
        self._activity = None
        self._requested = None
        self._pull = None
        self._pull_in_progress = False
        self._resume_on_start = False
        self._closing = False
        self._cancel = threading.Event()
        self._thread = None
        self._store = None
        self._work = None
        self._last_error = None
        self._last_archive_error = None
        self._no_dream_material = None

    @staticmethod
    def validate_config(config):
        if not isinstance(config, dict) or set(config) - DEFAULTS.keys():
            raise ValueError("助手配置包含未知字段")
        for name, value in config.items():
            default = DEFAULTS[name]
            if isinstance(default, bool) and not isinstance(value, bool):
                raise ValueError(f"{name} 需要布尔值")
            if isinstance(default, str):
                Plugin._validate_prompt(name, value)
            if name in NUMERIC_LIMITS:
                low, high = NUMERIC_LIMITS[name]
                if type(value) is not int or not low <= value <= high:
                    raise ValueError(f"{name} 需要 {low}—{high} 之间的整数")

    @staticmethod
    def _validate_prompt(name, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise ValueError(f"{name} 需要 1—10000 字符的非空文本")
        text.encode("utf-8")

    def start(self, context):
        self.validate_config(context.config)
        self._context = context
        self._config = {**DEFAULTS, **context.config}
        self._store = Store(context.data_dir / "assistant.sqlite3")
        self._work = WorkEvents(context, self.enqueue_event, self._work_changed)
        self._work.start()
        context.services.provide("assistant", self)
        context.http.register("GET", "/status", lambda _request: self.status())
        context.http.register("POST", "/start", lambda _request: self.start_receiving())
        context.http.register("POST", "/stop", lambda _request: self.stop_receiving())
        context.http.register("POST", "/handoff", lambda _request: self._request_maintenance("handoff"))
        context.http.register("POST", "/dream", lambda _request: self._request_maintenance("dream"))
        context.http.register("POST", "/startup-unread", lambda _request: self._retry_pull())
        context.http.register("GET", "/startup-prompt", lambda _request: self._read_startup_prompt())
        context.http.register("PUT", "/startup-prompt", self._write_startup_prompt)
        context.http.register("GET", "/tasks", lambda _request: {"items": self.list_tasks()})
        context.http.register("POST", "/system-events", lambda request: self.publish_system_event(request.get_json()))
        context.http.register("POST", "/skip-input", lambda request: self.skip_input(request.get_json()))
        self._resume_on_start = self._store.get("desired_running", self._config["startOnEnable"])
        self._thread = threading.Thread(target=self._run, name="xixi-assistant", daemon=True)
        self._thread.start()

    def _service(self, name):
        return self._context.services.get(name)

    def _read_startup_prompt(self):
        with self._condition:
            return {"text": self._store.get("startup_prompt", self._config["startupUnreadPrompt"])}

    def _write_startup_prompt(self, request):
        body = request.get_json()
        if not isinstance(body, dict) or set(body) != {"text"}:
            raise PlatformError("invalid_startup_prompt", "补收提示需要一个 text 字段")
        try:
            self._validate_prompt("补收提示", body["text"])
        except ValueError as exc:
            raise PlatformError("invalid_startup_prompt", str(exc)) from exc
        with self._condition:
            self._store.update(startup_prompt=body["text"])
        return body

    def status(self):
        with self._condition:
            return self._status()

    def _status(self):
        last_dream_at = self._store.get("last_dream_at")
        return {
            "state": self._state,
            "activity": self._activity,
            "requested": ("startup_unread" if self._resume_on_start or (self._pull and not self._pull_in_progress)
                          else self._requested),
            "queue_depth": self._store.queued(),
            "background_work_running": self._work.busy(),
            "session_id": self._store.get("session_id"),
            "startup_unread": self._store.get("startup_unread"),
            "startup_unread_retry_available": self._store.get("failed_window") is not None,
            "last_error": self._last_error,
            "last_archive_error": self._last_archive_error,
            "last_dream_at": last_dream_at,
            "next_dream_at": last_dream_at + MIN_DREAM_INTERVAL if last_dream_at is not None else None,
        }

    def start_receiving(self):
        with self._condition:
            if self._state == "stopping" or self._closing:
                raise PlatformError("assistant_stopping", "助手正在停机", 409)
            if self._state == "not_started":
                now = time.time()
                window, interrupted = self._store.begin(now)
                self._resume_on_start = False
                self._runtime = {"started_at": now, "last_alive_at": now, "stopped_at": None}
                self._last_activity_at = now
                self._pull = (window, False)
                self._state = "running"
                if interrupted:
                    self._context.logger.warning("标记 %s 条中断输入，已接纳且未开始的输入继续排队", interrupted)
                self._condition.notify_all()
            return self._status()

    def _begin_stop(self, *, save_preference):
        self._resume_on_start = False
        if self._state == "running":
            runtime = dict(self._runtime, stopped_at=time.time())
            values = {"runtime": runtime}
            if save_preference:
                values["desired_running"] = False
            self._store.update(**values)
            self._runtime = runtime
            self._work.set_running(False)
            self._state = "stopping" if self._activity is not None or self._work.busy() else "not_started"
            if self._pull is not None and not self._pull_in_progress:
                window, retry = self._pull
                self._store.finish_pull([], window, retry=retry, cancelled=True)
                self._pull = None
            self._requested = None
            self._condition.notify_all()
        elif save_preference:
            self._store.update(desired_running=False)

    def stop_receiving(self):
        with self._condition:
            self._begin_stop(save_preference=True)
            return self._status()

    def stop(self):
        with self._condition:
            if self._store is None:
                return
            self._closing = True
            self._begin_stop(save_preference=False)
            self._condition.notify_all()
        if self._work is not None:
            self._work.stop()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                self._cancel.set()
                self._thread.join(timeout=10)
            if self._thread.is_alive():
                raise RuntimeError("助手当前工作未结束，请等待操作结束或重启 Core")
        with self._condition:
            self._store.close()
            self._store = None

    def _work_changed(self):
        with self._condition:
            if self._state == "stopping" and self._activity is None and not self._work.busy():
                self._state = "not_started"

    def submit_background_task(self, data, *, request_id, origin):
        with self._condition:
            self._require_running()
        return self._work.submit_background_task(data, request_id=request_id, origin=origin)

    def task_status(self, task_id):
        return self._work.task_status(task_id)

    def list_tasks(self, limit=50):
        return self._work.list_tasks(limit)

    def read_task_result(self, task_id, offset=0, limit=4096):
        return self._work.read_task_result(task_id, offset, limit)

    def cancel_task(self, task_id):
        return self._work.cancel_task(task_id)

    def write_task_artifact(self, session_id, args, request_id):
        return self._work.write_task_artifact(session_id, args, request_id)

    def read_task_artifact(self, args):
        return self._work.read_task_artifact(args)

    def publish_system_event(self, data):
        with self._condition:
            self._require_running()
        try:
            return self._work.publish_system_event(data)
        except ValueError as exc:
            raise PlatformError("invalid_system_event", str(exc)) from exc

    def enqueue_event(self, data):
        kind = data.get("kind")
        if kind not in EVENT_KINDS | {"task_result"} or not isinstance(data.get("event"), dict):
            raise ValueError("未知的助手系统事件")
        message = message_record(data)
        message.update(source="task_result" if kind == "task_result" else "system", sender="system",
                       event={**data["event"], "eventType": kind})
        with self._condition:
            if self._store is None:
                raise PlatformError("assistant_not_running", "助手状态库尚未打开", 409)
            accepted = self._store.enqueue(message, hold=self._pull is not None)
            self._condition.notify_all()
        return {"accepted": accepted, "duplicate": not accepted}

    def skip_input(self, data):
        if not isinstance(data, dict) or set(data) != {"channel", "peer", "id"}:
            raise PlatformError("invalid_input", "跳过需要渠道、会话对象和原消息编号")
        try:
            normalized = message_record(dict(data, time=0))
        except (ValueError, TypeError) as exc:
            raise PlatformError("invalid_input", str(exc)) from exc
        with self._condition:
            state = self._store.skip_input(normalized["channel"], normalized["peer"], normalized["id"])
            if state is None:
                raise PlatformError("input_not_found", "消息不存在", 404)
            if state != "skipped":
                raise PlatformError("input_cannot_skip", "仅能跳过已排队、失败或中断的主人消息", 409)
            self._condition.notify_all()
        return {"id": normalized["id"], "state": state}

    def receive(self, message):
        with self._condition:
            if self._resume_on_start:
                self.start_receiving()
            self._require_running()
            try:
                message = message_record(message)
            except (ValueError, TypeError, OverflowError) as exc:
                raise PlatformError("invalid_message", str(exc)) from exc
            accepted = self._store.enqueue(message, hold=self._pull is not None)
            if accepted:
                self._last_activity_at = time.time()
                self._heartbeat(self._last_activity_at)
                self._condition.notify_all()
            return {"accepted": accepted, "duplicate": not accepted}

    def _require_running(self):
        if self._state != "running" or self._closing:
            raise PlatformError("assistant_not_running", "助手未接收渠道消息或启动新工作", 409)

    def _request_maintenance(self, kind):
        with self._condition:
            self._require_running()
            if self._pull or self._pull_in_progress or self._requested or self._activity in {"dream", "handoff"}:
                raise PlatformError("assistant_busy", "助手已有整理或补收工作", 409)
            if kind == "handoff" and self._store.get("session_id") is None:
                raise PlatformError("assistant_no_session", "当前还没有会话可整理", 409)
            library = self._maintenance_library()
            if kind == "dream":
                last = self._store.get("last_dream_at")
                if last is not None and time.time() - last < MIN_DREAM_INTERVAL:
                    raise PlatformError("dream_interval", "两次做梦至少间隔 12 小时", 409)
                if not self._dream_material(library, automatic=False):
                    raise PlatformError("dream_no_dialogue", "当前没有待整理的已处理对话", 409)
            self._requested = kind
            self._condition.notify_all()
            return self._status()

    def _retry_pull(self):
        with self._condition:
            self._require_running()
            if self._pull or self._pull_in_progress or self._requested or self._activity in {"dream", "handoff"}:
                raise PlatformError("assistant_busy", "助手已有整理或补收工作", 409)
            window = self._store.get("failed_window")
            if window is None:
                raise PlatformError("no_failed_unread", "当前没有可重试的启动补收", 409)
            self._store.update(startup_unread={"state": "pulling", "count": 0, "window": window})
            self._pull = (window, True)
            self._work.set_running(False)
            self._condition.notify_all()
            return self._status()

    def _heartbeat(self, now):
        if now - self._runtime["last_alive_at"] >= self._config["heartbeatSeconds"]:
            self._runtime = dict(self._runtime, last_alive_at=now)
            self._store.update(runtime=self._runtime)

    def _maintenance_library(self):
        try:
            return self._service("library")
        except PlatformError as exc:
            if exc.code == "service_unavailable":
                raise PlatformError("maintenance_library_unavailable", "图书馆暂缓，换聊与做梦暂缓，普通消息可继续处理", 409) from exc
            raise

    def _restore_reply_dates(self, library):
        recovered = []
        for row in self._store.missing_reply_dates():
            try:
                record = library.archived_message(row["reply_ref"])
                sent_at = datetime.fromisoformat(record["sentAt"]).timestamp()
            except (ValueError, KeyError):
                continue
            recovered.append((sent_at, row["order_no"]))
        if recovered:
            self._store.save_reply_dates(recovered)

    def _dream_material(self, library, *, automatic):
        zone = library.timezone
        today = datetime.now(zone).date()
        signature = (str(zone), today, self._store.latest_settled())
        if automatic and self._no_dream_material == signature:
            return None
        self._restore_reply_dates(library)
        candidate = self._store.dream_material(zone, self._config["dreamMaxMessages"], automatic=automatic)
        if candidate is None:
            if automatic:
                self._no_dream_material = signature
            return None
        self._no_dream_material = None
        return (zone, *candidate)

    def _automatic_work(self, now):
        idle = now - self._last_activity_at
        latest = self._store.latest_settled()
        last_dream = self._store.get("last_dream_at")
        try:
            library = self._maintenance_library()
        except PlatformError:
            return None, None
        if (self._config["automaticDream"] and idle >= self._config["dreamIdleSeconds"]
                and (last_dream is None or now - last_dream >= self._config["dreamIntervalSeconds"])):
            material = self._dream_material(library, automatic=True)
            if material:
                return "dream", material
        if (self._config["automaticHandoff"] and idle >= self._config["handoffIdleSeconds"]
                and latest > self._store.get("handoff_attempt_order", 0)
                and self._store.get("turns_since_handoff", 0) >= self._config["handoffMinTurns"]):
            return "handoff", None
        return None, None

    def _next_work(self):
        if self._state != "running":
            return None, None
        now = time.time()
        self._heartbeat(now)
        if self._pull:
            return "startup_unread", self._pull
        kind = self._requested
        self._requested = None
        material = None
        if kind is None:
            if self._store.queued():
                batch = self._store.claim_batch(self._config)
                if batch is None:
                    return None, None
                return ("chat" if not batch[2] else "startup_unread"), batch
            kind, material = self._automatic_work(now)
        if kind == "handoff":
            self._store.update(handoff_attempt_order=self._store.latest_settled())
        if kind == "dream":
            if material is None:
                try:
                    material = self._dream_material(self._maintenance_library(), automatic=False)
                except PlatformError as exc:
                    self._report_error("dream", exc)
                    return None, None
            if not material:
                return None, None
            zone, day, rows = material
            self._store.start_dream(zone, day, rows[-1].order, now)
            return kind, material
        return kind, None

    def _run(self):
        while True:
            with self._condition:
                if self._closing:
                    return
                if self._resume_on_start:
                    # The host opens this plugin's service gate only after start() returns.
                    try:
                        self._service("assistant")
                    except PlatformError:
                        self._condition.wait(timeout=0.01)
                        continue
                    self.start_receiving()
                try:
                    kind, data = self._next_work()
                except Exception as exc:
                    self._report_error("schedule", exc)
                    self._condition.wait(timeout=1)
                    continue
                if kind is None:
                    self._condition.wait(timeout=1 if self._state == "running" else None)
                    continue
                self._activity = kind
                pulling = self._pull is not None
                self._pull_in_progress = pulling
            try:
                if pulling:
                    self._pull_unread(*data)
                elif kind == "handoff":
                    self._handoff()
                elif kind == "dream":
                    self._dream(*data)
                else:
                    self._chat(kind, *data)
            except Exception as exc:
                self._report_error(kind, exc)
            finally:
                with self._condition:
                    if pulling and self._pull is data:
                        self._pull = None
                    self._pull_in_progress = False
                    if kind == "chat" or (kind == "startup_unread" and not pulling):
                        self._last_activity_at = time.time()
                    self._activity = None
                    self._work_changed()

    def _report_error(self, operation, error, *, archive=False):
        info = {"operation": operation, "message": f"{type(error).__name__}: {str(error)[:500]}", "at": time.time()}
        with self._condition:
            if archive:
                self._last_archive_error = info
            else:
                self._last_error = info
        self._context.logger.warning("%s 失败：%s", operation, info["message"])
        return info["message"]

    def _pull_unread(self, window, retry):
        messages, error = [], None
        try:
            result = self._service("message_bridge").pull_unread(
                window["stopped_at"], window["started_at"], cancel_event=self._cancel,
            )
            if not isinstance(result, list):
                raise ValueError("消息桥没有返回完整的未读消息列表")
            if window["stopped_at"] is None and result:
                raise ValueError("首次启动缺少停机边界，消息桥不能返回历史来信")
            recovered = [message_record(item) for item in result]
            if recovered:
                first, last = math.floor(window["stopped_at"]), math.floor(window["started_at"])
                if any(not first <= math.floor(message["time"]) <= last for message in recovered):
                    raise ValueError("消息桥返回了停机窗口之外的来信")
            messages = sorted(recovered, key=lambda message: message["time"])
        except Exception as exc:
            error = self._report_error("startup_unread", exc)
        with self._condition:
            cancelled = self._cancel.is_set()
            if cancelled:
                error = error or "助手工作因插件停止而中断"
            self._store.finish_pull([] if cancelled else messages, window, retry=retry, error=error, cancelled=cancelled)
            # Release and admit realtime under the same lock as queue promotion.
            self._pull = None
            if self._state == "running" and not self._closing:
                self._work.set_running(True)

    def _guard_cancel(self):
        if self._cancel.is_set():
            raise RuntimeError("助手工作因插件停止而中断")

    def _open_session(self, existing=None, *, kind="daily", context=None):
        self._guard_cancel()
        bridge = self._service("dsh_bridge")
        session = bridge.open_session(kind=kind, context=context) if existing is None else bridge.open_session(existing)
        if not isinstance(session, str) or not session.strip():
            raise RuntimeError("DSH 未返回有效会话编号")
        if existing is not None and existing != session:
            raise RuntimeError("DSH 未能打开当前会话")
        return session

    def _current_session(self):
        with self._condition:
            current = self._store.get("session_id")
        session = self._open_session(current)
        if current is None:
            with self._condition:
                self._store.update(session_id=session)
        return session

    def _execute(self, session, messages, *, kind, prompt, context):
        self._guard_cancel()
        result = self._service("dsh_bridge").execute(
            session, messages, kind=kind, prompt=prompt, context=context, cancel_event=self._cancel,
        )
        if not isinstance(result, str) or not result.strip():
            raise RuntimeError("DSH 未返回最终文本")
        return result

    def _chat(self, kind, orders, messages, context):
        response = None
        executed = False
        recovery_orders = []
        reply_time = None
        status = "failed"
        try:
            session = self._current_session()
            with self._condition:
                handoff = self._store.get("handoff")
                originals = self._store.get("selected_originals")
            if originals:
                context["selectedOriginals"] = originals
            if handoff:
                context["handoff"] = handoff
            prompt = self._read_startup_prompt()["text"] if kind == "startup_unread" else ""
            recovery, recovery_orders = self._input_recovery(orders[0], messages[0])
            if recovery:
                context["inputRecoveryText"] = recovery
            response = self._execute(session, messages, kind=kind, prompt=prompt, context=context)
            executed = True
            self._guard_cancel()
            receipt = self._service("message_bridge").send(messages[-1], response)
            if isinstance(receipt, dict) and receipt.get("accepted") is False:
                raise RuntimeError("消息桥未接受回复")
            reply_time = time.time()
            status = "completed"
        except Exception:
            if self._cancel.is_set():
                status = "interrupted"
            raise
        finally:
            reply = response if reply_time is not None else None
            references = {} if status == "interrupted" else self._archive(orders, messages, reply, reply_time)
            with self._condition:
                self._store.finish_batch(orders, status, reply, references, executed=executed, reply_at=reply_time,
                                         recovery_orders=recovery_orders)

    def _input_recovery(self, order, message):
        if message.get("source", "owner") != "owner":
            return "", []
        with self._condition:
            rows = self._store.pending_recovery(message["channel"], message["peer"], order)
        if not rows:
            return "", []
        try:
            persona = self._service("persona")
        except PlatformError as exc:
            if exc.code == "service_unavailable":
                return "", []
            raise
        latest = [row["message_id"] for row in rows[:10]]
        text = persona.input_recovery_text(message, latest, len(rows) - len(latest))
        return text, [row["order_no"] for row in rows]

    def _archive(self, orders, messages, response, reply_time):
        references = {}
        try:
            library = self._service("library")
            for order, message in zip(orders, messages):
                try:
                    if order == orders[-1] and response is not None:
                        result = library.archive_chat(message, response, reply_time=reply_time)
                    else:
                        result = library.archive_chat(message, None)
                    if isinstance(result, dict):
                        references[order] = tuple((result.get(kind) or {}).get("recordId") for kind in ("input", "reply"))
                except Exception as exc:
                    self._report_error("archive_chat", exc, archive=True)
        except Exception as exc:
            self._report_error("archive_chat", exc, archive=True)
        return references

    @staticmethod
    def _material_context(material, prior, *, archived_ids_only=False):
        sources = []
        for row in material:
            message = row.message
            sent_at = datetime.fromtimestamp(message["time"], timezone.utc).isoformat()
            if row.include_input:
                source = message.get("source", "owner")
                sources.append({"id": row.input_ref or ("" if archived_ids_only else f"input-{row.order}"),
                                "source": source, "sender": message.get("sender", source), "sentAt": sent_at,
                                "receivedAt": message.get("receivedAt"), "channel": message["channel"],
                                "channelMessageId": message["id"], "replyTo": message.get("replyTo"),
                                "attachments": message["attachments"], "text": message["text"]})
            if row.reply is not None:
                replied = datetime.fromtimestamp(row.reply_at, timezone.utc).isoformat() if row.reply_at is not None else "未记录"
                sources.append({"id": row.reply_ref or ("" if archived_ids_only else f"reply-{row.order}"),
                                "source": "assistant", "sender": "小希", "sentAt": replied, "receivedAt": replied,
                                "channel": message["channel"], "replyTo": row.input_ref, "text": row.reply})
        return {"sourceMessages": sources, "priorHandoff": prior or "",
                "fixedRange": {"fromSeq": material[0].order if material else 0, "toSeq": material[-1].order if material else 0,
                               "cutoffAt": datetime.now(timezone.utc).isoformat()}}

    def _maintenance_output(self, session, messages, *, kind, context, parser):
        prompt = self._config["handoffPrompt" if kind == "handoff" else "dreamPrompt"]
        result = self._execute(session, messages, kind=kind, prompt=prompt, context=context)
        try:
            return parser(result)
        except ValueError as exc:
            problem = str(exc)[:1500]
            review_kind = "idle" if kind == "handoff" else "dream"
            try:
                persona = self._service("persona")
            except PlatformError as error:
                if error.code != "service_unavailable":
                    raise
                persona = None
            feedback = persona.maintenance_feedback(review_kind, problem, context=context) if persona else (
                f"上次输出校验未通过：\n{problem}\n请在同一固定范围内修正，不扩大来源。"
            )
            self._context.logger.info("%s 校验失败，在同一分析会话修正一次", kind)
            result = self._execute(session, messages, kind=kind, prompt=feedback, context=context)
            return parser(result)

    def _summarize(self, material, prior, previous_originals):
        context = self._material_context(material, prior)
        if previous_originals:
            context["sourceMessages"] = list(previous_originals) + context["sourceMessages"]
        review = self._open_session(kind="idle", context=context)
        return self._maintenance_output(review, [], kind="handoff", context=context,
                                        parser=lambda result: self._parse_handoff(result, context["sourceMessages"]))

    @staticmethod
    def _parse_handoff(summary, sources):
        if not summary.lstrip().startswith(("{", "[")):
            if len(summary) > CORE_DEFAULTS["handoffMaxChars"]:
                raise ValueError("交接摘要超出字数限制，请缩短后再换聊")
            return summary, []
        result = json.loads(summary)
        if not isinstance(result, dict):
            raise ValueError("换聊结果需要 JSON 对象")
        summary = result.get("handoff")
        ids = result.get("selectedOriginals", [])
        available = {row["id"]: row for row in sources}
        if (not isinstance(summary, str) or not summary.strip() or len(summary) > CORE_DEFAULTS["handoffMaxChars"]
                or not isinstance(ids, list) or len(ids) > CORE_DEFAULTS["selectedOriginalLimit"]
                or any(not isinstance(key, str) or key not in available for key in ids)):
            raise ValueError("换聊需要限长的交接摘要，保留原话须在数量限制内引用本批材料编号")
        return summary, [available[key] for key in dict.fromkeys(ids)]

    def _handoff(self):
        current = self._current_session()
        library = self._maintenance_library()
        with self._condition:
            self._restore_reply_dates(library)
            cursor = self._store.get("handoff_cursor", 0)
            end = self._store.latest_settled()
            summary = self._store.get("last_handoff") or self._store.get("handoff")
            originals = self._store.get("last_originals", [])
        # Freeze the ledger boundary. Long conversations carry the summary through
        # multiple review sessions before replacing the active daily session.
        while True:
            with self._condition:
                rows = self._store.material_after(cursor, 100)
            material, size = [], 0
            for row in rows:
                length = len(row.message["text"]) + len(row.reply or "") + 250
                if row.order > end or (material and size + length > 60000):
                    break
                material.append(row)
                size += length
            summary, originals = self._summarize(material, summary, originals)
            if not material or material[-1].order >= end:
                break
            cursor = material[-1].order
        candidate = self._open_session(context={"handoff": summary, "selectedOriginals": originals})
        if candidate == current:
            raise RuntimeError("DSH 未建立新的日常会话，继续使用当前会话")
        self._guard_cancel()
        with self._condition:
            self._store.update(session_id=candidate, handoff=summary, last_handoff=summary, selected_originals=originals,
                               last_originals=originals, handoff_cursor=end, turns_since_handoff=0)

    def _dream(self, zone, day, material):
        library = self._maintenance_library()
        try:
            persona = self._service("persona")
        except PlatformError as exc:
            if exc.code != "service_unavailable":
                raise
            persona = None
        with self._condition:
            current = self._store.get("session_id")
            prior = self._store.get("last_handoff") or self._store.get("handoff")
        context = self._material_context(material, prior, archived_ids_only=True)
        diary = library.dream_diary(day)
        memories = library.dream_memories()
        context.update(day=day, timezone=str(zone), existingDiary=diary["existingDiary"], existingMemories=memories)
        summaries = persona.stage_summaries() if persona else None
        if summaries is not None:
            context["currentStageSummaries"] = summaries
        session = self._open_session(kind="dream", context=context)
        if session == current:
            raise RuntimeError("DSH 未建立独立的做梦会话")
        messages = [row.message for row in material if row.include_input]
        source_ids = [row["id"] for row in context["sourceMessages"] if row["id"]]
        request_id = f"dream-{uuid.uuid4().hex}"

        def parse(raw):
            result = json.loads(raw)
            if not isinstance(result, dict) or not isinstance(result.get("diary"), dict):
                raise ValueError("做梦结果需要 memories 数组和 diary 对象")
            entry = {**result["diary"], "day": day, "expectedVersion": diary["diaryExpected"]}
            entries = library.validate_dream_output(result.get("memories"), entry, existing_memories=memories, source_ids=source_ids)
            prepared = persona.prepare_dream_summaries(result, summaries, request_id) if persona else None
            return entries, entry, prepared

        entries, entry, prepared = self._maintenance_output(session, messages, kind="dream", context=context, parser=parse)
        self._guard_cancel()

        def publish():
            return library.publish_dream(entries, entry, request_id=request_id, source_ids=source_ids, existing_memories=memories)

        if persona:
            persona.publish_dream_summaries(prepared, publish_library=publish)
        else:
            publish()
        with self._condition:
            self._store.finish_dream(zone, day, material[-1].order)
