"""Core persona storage and model-facing assembly for platform sessions."""
from datetime import datetime, timezone
import sqlite3
import threading
import time

from xixi_core.config import DEFAULT
from xixi_core.dreams import DreamsMixin
from xixi_core.injections import BY_ID, Injections
from xixi_core.platform.errors import PlatformError
from xixi_core.privacy import private_sqlite
from xixi_core.store import Store


_MESSAGE_CONDITIONS = {"owner", "input", "reply", "attachments"}
_DYNAMIC_TEMPLATES = {"initial.library_deferred", "maintenance.retry"}
_RUNTIME_HINTS = {
    "initial.library_deferred": "图书馆不可用时，附在日常或独立工作的当次递交。恢复后不再附加。",
    "maintenance.retry": "换聊或做梦输出校验失败时，在原分析会话附带错误与固定范围，最多纠错一次。",
    "work.instructions": "创建独立工作时保存在工作会话的提示快照中。",
}


def _active(identifier):
    return not identifier.startswith("native.")


class PersonaDatabase(Store):
    def __init__(self, path):
        private_sqlite(path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        except BaseException:
            self.close()
            raise


class PlatformInjections(Injections):
    def _values(self, kind, input, context, snapshot=None, trace=None):
        values, case, raw = super()._values(kind, input, context, snapshot, trace)
        values["runtime"] = context.get("runtime", "")
        values["timezone"] = context.get("timezone", values["timezone"])
        return values, case, raw

    def _originals(self, records, snapshot=None, trace=None):
        rendered = []
        for record in records if isinstance(records, list) else []:
            if isinstance(record, str):
                rendered.append(record)
                continue
            if not isinstance(record, dict):
                continue
            source = record.get("source", "")
            sender = record.get("sender") or source
            if sender == "owner" and source == "owner":
                sender = self._format("format.sender.owner", {}, "owner", snapshot, trace)
            reply = record.get("replyTo") or {}
            if isinstance(reply, str):
                reply = {"messageId": reply}
            elif not isinstance(reply, dict):
                reply = {}
            values = {
                "sender": sender, "source": source, "messageId": record.get("id", record.get("messageId", "")),
                "sentAt": record.get("sentAt", record.get("sent_at", "")), "receivedAt": record.get("receivedAt", ""),
                "channel": record.get("channel", ""), "text": record.get("text", ""),
                "attachments": record.get("attachments", []), "replyId": reply.get("messageId", ""),
                "replyText": reply.get("text", ""), "replySentAt": reply.get("sentAt", ""),
            }
            rendered.append(self._format("format.original", values, values["text"], snapshot, trace))
        return "\n\n".join(rendered)


class PersonaStore(DreamsMixin):
    def __init__(self, path, timezone="Asia/Shanghai", *, clock=time.time, logger=None):
        self.store = PersonaDatabase(path)
        self.clock = clock
        self.logger = logger
        self.config = {key: DEFAULT[key] for key in ("roleCard", "rules", "stageSummaryMaxChars")}
        self.config["timezone"] = timezone
        try:
            self.injections = PlatformInjections(self)
        except BaseException:
            self.close()
            raise

    def close(self):
        self.store.close()

    def log(self, event, metadata):
        if self.logger is not None:
            self.logger.info("%s（版本 %s）", event, metadata["version"])

    def environment(self):
        return self.store.get("environment", {"environmentDescription": "", "version": 0})

    def _set_environment(self, data):
        if not isinstance(data, dict) or set(data) != {"environmentDescription", "expectedVersion"}:
            raise ValueError("运行环境需要正文及读取时的版本")
        description = data["environmentDescription"]
        if not isinstance(description, str) or len(description) > 100000:
            raise ValueError("运行环境正文最多 100000 字符")
        description.encode("utf-8")
        with self.store.tx():
            current = self.environment()
            expected = data["expectedVersion"]
            if type(expected) is not int or expected != current["version"]:
                raise PlatformError("persona_version_conflict", "运行环境版本冲突，请重新读取", 409)
            if description == current["environmentDescription"]:
                return current
            saved = {"environmentDescription": description, "version": current["version"] + 1}
            self.store.put("environment", saved)
            return saved

    def injection_items(self):
        return [_describe(item) for item in self.injections.list() if _active(item["id"])]

    def _set_injection(self, data):
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not _active(data["id"]):
            raise ValueError("提示项没有接入平台或已在 DSH 原生提示页管理")
        if type(data.get("expectedVersion")) is not int:
            raise ValueError("请提供读取时的提示项版本")
        item = self.injections.get(data["id"])
        condition = BY_ID.get(item["id"], {}).get("condition")
        if condition == "format_only" and any(data.get(key, item[key]) != item[key] for key in ("placement", "sessionKinds", "order")):
            raise ValueError("表述格式随引用它的提示生效")
        if condition in _MESSAGE_CONDITIONS and any(data.get(key, item[key]) != item[key] for key in ("placement", "sessionKinds")):
            raise ValueError("消息提示仅在日常对话或启动补收的本轮递交时装配")
        if (item["id"] in _DYNAMIC_TEMPLATES or item["id"].startswith("event.")) and any(
                data.get(key, item[key]) != item[key] for key in ("placement", "sessionKinds")):
            raise ValueError("运行状态和纠错提示随实际状态递交")
        saved = self.injections.set(data)
        saved.pop("configPatch", None)
        return _describe(saved)

    def prepare_dream_summaries(self, result, current, request_id):
        if "stageSummaries" not in result:
            return None
        update = self._dream_summary_update({"currentStageSummaries": current}, result)
        with self.store.lock:
            self._prepare_stage_summary_update(update, request_id, source_batch=request_id)
        return update, request_id

    def publish_dream_summaries(self, prepared, *, publish_library=None):
        if prepared is None:
            return publish_library() if publish_library is not None else None
        update, batch = prepared
        with self.store.tx():
            _, _, _, previous = self._prepare_stage_summary_update(update, batch, source_batch=batch)
            if previous is not None:
                return previous
            if publish_library is not None:
                publish_library()
            return self._commit_stage_summaries(update, batch, source_batch=batch)

    def _context(self, context, runtime):
        values = dict(context or {})
        values.setdefault("environment", self.environment()["environmentDescription"])
        values.setdefault("currentStageSummaries", self.stage_summaries())
        if "existingMemories" in values and isinstance(values["existingMemories"], list):
            values["existingMemories"] = [
                dict(item, body=item.get("body", item.get("excerpt", ""))) if isinstance(item, dict) else item
                for item in values["existingMemories"]
            ]
        values["runtime"] = runtime
        return values

    def session_sections(self, kind="daily", *, agent_preset="standard", context=None, runtime="existing"):
        with self.store.lock:
            bundle = self.injections.render_bundle(kind, context=self._context(context, runtime), agentPreset=agent_preset)
            return [section for section in bundle["promptSections"]
                    if section["placement"] in {"system", "start"} and _active(section["id"]) and section["id"] not in _DYNAMIC_TEMPLATES]

    def library_unavailable_text(self, kind):
        template = self.injections.get("initial.library_deferred")
        if template["enabled"] and kind in template["sessionKinds"]:
            return self.injections._format(template["id"], {})
        return ""

    def maintenance_feedback(self, kind, error, *, context=None):
        with self.store.lock:
            bundle = self.injections.render_bundle(kind, context=self._context(dict(context or {}, previousError=error), ""))
            return next((section["text"] for section in bundle["promptSections"] if section["id"] == "maintenance.retry"), error)

    def input_recovery_text(self, owner_message, identifiers, omitted=0):
        summary = "、".join(identifiers)
        if omitted:
            summary += f" 等 {len(identifiers) + omitted} 条"
        event = {"type": "input_recovery", "summary": summary,
                 "details": "原执行结果与副作用未确认。相关输入已因中断或人工跳过停止处理。"}
        item = dict(owner_message, source="system", sender="system", text="", event=event, attachments=[], replyTo=None)
        with self.store.lock:
            bundle = self.injections.render_bundle("daily", input=_input(item), context=self._context({}, ""))
            return next((section["text"] for section in bundle["promptSections"] if section["id"] == "event.recovery"), "")

    def run_text(self, kind, messages, *, agent_preset="standard", context=None, runtime="existing"):
        with self.store.lock:
            values = self._context(context, runtime)
            sections = []
            rendered_once = set()
            for message in messages or (None,):
                bundle = self.injections.render_bundle(kind, input=_input(message), context=values, agentPreset=agent_preset)
                for section in bundle["promptSections"]:
                    identifier = section["id"]
                    if section["placement"] != "run" or not _active(identifier) or identifier in _DYNAMIC_TEMPLATES:
                        continue
                    if identifier not in rendered_once:
                        sections.append(section["text"])
                    condition = BY_ID.get(identifier, {}).get("condition")
                    if condition not in _MESSAGE_CONDITIONS and not identifier.startswith("event.") and identifier != "runtime.data":
                        rendered_once.add(identifier)
            return "\n\n".join(sections)


def _describe(item):
    if item.get("configKey"):
        item = dict(item, originalBody=DEFAULT[item["configKey"]])
    condition = BY_ID.get(item["id"], {}).get("condition")
    if condition == "format_only":
        return dict(item, formatOnly=True, effectScope="referenced")
    if condition in _MESSAGE_CONDITIONS:
        return dict(item, inputOnly=True)
    if item["id"] in _DYNAMIC_TEMPLATES:
        return dict(item, effectScope="next_delivery", inputOnly=True, runtimeHint=_RUNTIME_HINTS[item["id"]])
    if item["id"].startswith("event."):
        hint = "中断或跳过后，随同一渠道和主人的下一条真实输入递交，也可由外部提交恢复事件。" if item["id"] == "event.recovery" else "对应系统事件进入当前日常会话时使用。"
        return dict(item, inputOnly=True, runtimeHint=hint)
    if item["id"] in _RUNTIME_HINTS:
        return dict(item, runtimeHint=_RUNTIME_HINTS[item["id"]])
    return item


def _input(message):
    if message is None:
        return None
    sent_at = datetime.fromtimestamp(message["time"], timezone.utc).isoformat()
    received = message.get("receivedAt") or "未记录"
    source = message.get("source", "owner")
    item = {"id": message["id"], "source": source, "sender": message.get("sender", source), "sentAt": sent_at,
            "receivedAt": received, "channel": message["channel"], "text": message["text"],
            "attachments": []}
    if isinstance(message.get("event"), dict):
        item["event"] = message["event"]
    reply = message.get("replyTo")
    for attachment in message.get("attachments", []):
        data = attachment.get("data") if isinstance(attachment, dict) else None
        if not isinstance(data, dict):
            item["attachments"].append(attachment)
            continue
        kind = attachment.get("type", "")
        if kind == "reply" and data.get("id"):
            if not reply:
                reply = {"messageId": str(data["id"]), "sentAt": data.get("sentAt") or "时间未提供",
                         "text": data.get("text") or "原文未随消息提供"}
            continue
        name = data.get("name") or data.get("filename") or data.get("summary", "")
        ref = data.get("file_id") or data.get("file") or data.get("id") or data.get("url") or data.get("qq", "")
        item["attachments"].append({"type": kind, "name": name, "ref": ref})
    if reply:
        item["replyTo"] = {**reply, "sentAt": reply.get("sentAt") or "时间未提供",
                           "text": reply.get("text") or "原文未随消息提供"}
    return item


def persona_http(persona, resource, request):
    try:
        if request.method == "GET":
            if resource == "injections":
                return {"items": persona.injection_items()}
            if resource == "environment":
                return persona.environment()
            if resource == "summaries":
                return dict(persona.stage_summaries(), history=persona.stage_summary_history())
        elif request.method == "POST":
            request.max_content_length = 2 * 1024 * 1024
            data = request.get_json()
            if resource == "injections":
                return persona._set_injection(data)
            if resource == "environment":
                return persona._set_environment(data)
            if resource == "summaries":
                if not isinstance(data, dict) or not isinstance(data.get("requestId"), str):
                    raise ValueError("阶段认识保存需要稳定的请求编号")
                return persona.update_stage_summaries({key: value for key, value in data.items() if key != "requestId"}, data["requestId"])
    except ValueError as exc:
        conflict = "版本冲突" in str(exc)
        raise PlatformError("persona_version_conflict" if conflict else "invalid_persona", str(exc), 409 if conflict else 400) from exc
    raise PlatformError("persona_resource_unknown", "角色卡入口不存在", 404)
