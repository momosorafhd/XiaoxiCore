"""Public Library service shared by the assistant, DSH tools and owner console."""
from datetime import date, datetime, timezone
from threading import Lock
from uuid import uuid4

from xixi_core.library_v04 import CATEGORIES, SEARCH_CATEGORIES, fingerprint, integer, strings, text
from xixi_core.raw_library import canonical


class LibraryService:
    def __init__(self, backend, library_timezone):
        self._backend = backend
        self.timezone = library_timezone
        self._archive_lock = Lock()

    def dispatch(self, operation, args=None, request_id=None):
        if args is not None and not isinstance(args, dict):
            raise ValueError("图书馆操作参数须为对象")
        args = dict(args or {})
        operation = operation.removeprefix("library.")
        if operation == "search":
            nested = args.get("filter", {})
            categories = {"categories", "category"}
            if isinstance(nested, dict) and not (categories & args.keys() or categories & nested.keys()):
                args["categories"] = list(SEARCH_CATEGORIES)
        if operation == "chat_history":
            args.setdefault("timezone", self.timezone.key)
        return self._backend.dispatch(operation, args, request_id)

    def search(self, query, categories=None, **filters):
        args = dict(filters, query=query)
        if categories is not None:
            args["categories"] = categories
        return self.dispatch("search", args)["items"]

    def read(self, item_id, version=None):
        return self._backend.read(item_id, version)

    def list_memories(self, limit=20):
        return self._backend.catalog_page({"limit": limit})["items"]

    def dream_memories(self):
        return self._backend.query({"kind": "memory", "limit": 100})

    def dream_diary(self, day=None):
        day = day or datetime.now(self.timezone).date().isoformat()
        return {"day": day, **self._backend.prepare_dream({"day": day})}

    def archived_message(self, record_id):
        return self._backend.archive.resolve_message(record_id)

    def write_diary(self, body, *, day=None, title=None, request_id=None):
        entry = {"body": body}
        if title is not None:
            entry["title"] = title
        if day is not None:
            entry["day"] = day
        return self.publish_dream([], entry, request_id=request_id)["items"][0]

    @staticmethod
    def validate_dream_output(memories, diary, *, existing_memories, source_ids):
        """Check model content against the exact source and memory snapshots it saw."""
        if not isinstance(memories, list) or not isinstance(diary, dict):
            raise ValueError("做梦结果需要 memories 数组和 diary 对象")
        if set(diary) - {"day", "title", "body", "sourceRefs", "expectedVersion"}:
            raise ValueError("做梦日记含未知字段")
        text(diary.get("body"), "日记正文")
        text(diary.get("title", ""), "日记标题", empty=True, maximum=2000)
        allowed = set(source_ids)
        if not set(strings(diary.get("sourceRefs", []), "sourceRefs")) <= allowed:
            raise ValueError("日记来源超出本批固定范围")
        originals = {item["id"]: item for item in existing_memories}
        entries, revised = [], set()
        for entry in memories:
            if not isinstance(entry, dict) or set(entry) - {"id", "expectedVersion", "kind", "category", "title", "body", "summary", "tags", "aliases", "sourceRefs", "needsReview", "status"}:
                raise ValueError("做梦记忆需要已登记的条目字段")
            entry = dict(entry)
            previous = None
            if "id" in entry:
                item_id = text(entry["id"], "记忆编号", maximum=200)
                previous = originals.get(item_id)
                expected = integer(entry.get("expectedVersion"), "expectedVersion", 1, 1000000000)
                if not previous or expected != previous["version"] or item_id in revised:
                    raise ValueError("做梦修订必须引用本批已展示的编号与版本，且每个编号仅修改一次")
                revised.add(item_id)
                for field in ("category", "title", "summary", "tags", "aliases", "sourceRefs"):
                    entry.setdefault(field, previous[field])
            elif "expectedVersion" in entry:
                raise ValueError("新记忆无需填写 expectedVersion")
            category = entry.get("category")
            if not isinstance(category, str) or category not in CATEGORIES:
                raise ValueError("记忆须属于五个并列分类之一")
            text(entry.get("body"), "记忆正文")
            text(entry.get("title", ""), "记忆标题", empty=True, maximum=2000)
            text(entry.get("summary", ""), "记忆摘要", empty=category != "资料", maximum=20000)
            for field in ("tags", "aliases", "sourceRefs"):
                strings(entry.get(field, []), field)
            if "needsReview" in entry and type(entry["needsReview"]) is not bool:
                raise ValueError("needsReview 必须为布尔值")
            if entry.get("status", "active") not in ("active", "delete_marked"):
                raise ValueError("记忆状态无效")
            refs = set(entry.get("sourceRefs", []))
            if not refs or not refs <= (allowed | set(previous["sourceRefs"] if previous else [])):
                raise ValueError("记忆须引用本批材料编号或修订条目的原有来源")
            entries.append(entry)
        return entries

    def publish_dream(self, memories, diary, *, request_id=None, source_ids=(), existing_memories=None):
        if not isinstance(memories, list) or not isinstance(diary, dict):
            raise ValueError("做梦结果需要 memories 数组和 diary 对象")
        diary = dict(diary)
        if set(diary) - {"day", "title", "body", "sourceRefs", "expectedVersion"}:
            raise ValueError("做梦日记含未知字段")
        if "title" in diary:
            text(diary["title"], "日记标题", empty=True, maximum=2000)
        strings(diary.get("sourceRefs", []), "sourceRefs")
        day = diary.pop("day", datetime.now(self.timezone).date().isoformat())
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError("日记日期须为 YYYY-MM-DD")
        if not isinstance(source_ids, (list, tuple)):
            raise ValueError("source_ids 须为来源编号数组")
        if request_id is None:
            request_id = uuid4().hex
        text(request_id, "request_id", maximum=400)
        batch = {"id": "platform-" + request_id, "day": day, "sourceIds": strings(list(source_ids), "sourceIds")}
        batch.update(self._backend.prepare_dream(batch))
        if "expectedVersion" in diary:
            batch["diaryExpected"] = integer(diary["expectedVersion"], "expectedVersion", 0, 1000000000)

        if existing_memories is not None:
            entries = self.validate_dream_output(memories, diary, existing_memories=existing_memories, source_ids=source_ids)
            batch["existingMemories"] = existing_memories
            return self._backend.apply_dream(batch, {"memories": entries, "diary": diary})

        entries, snapshots = [], {}
        for entry in memories:
            if not isinstance(entry, dict):
                raise ValueError("做梦记忆须为对象")
            entry = dict(entry)
            item_id = entry.get("id")
            if item_id:
                version = integer(entry.get("expectedVersion"), "expectedVersion", 1, 1000000000)
                previous = self._backend.read(item_id, version)["item"]
                snapshots[item_id] = previous
                for field in ("category", "title", "summary", "tags", "aliases", "sourceRefs"):
                    entry.setdefault(field, previous[field])
            if not entry.get("category"):
                entry["category"] = "认识与判断"
            entries.append(entry)
        batch["existingMemories"] = list(snapshots.values())
        return self._backend.apply_dream(batch, {"memories": entries, "diary": diary}, require_memory_sources=False)

    def archive_chat(self, message, reply=None, *, reply_time=None):
        if not isinstance(message, dict):
            raise ValueError("原话消息须为对象")
        message_id = text(message.get("id"), "id", maximum=300)
        channel = text(message.get("channel"), "channel", maximum=100)
        peer = text(message.get("peer"), "peer", maximum=300)
        if reply is not None:
            text(reply, "回复正文", maximum=1000000)
        sent_at = _timestamp(message["time"])
        conversation = canonical([channel, peer])
        identity = fingerprint([channel, peer, message_id])
        common = {"channel": channel, "source": channel, "conversationId": conversation}
        incoming = dict(common, id=message_id, direction="input", sender=message.get("sender", peer), channelMessageId=message_id,
                        text=message["text"], sentAt=sent_at, receivedAt=message.get("receivedAt") or sent_at,
                        source=message.get("source", channel), attachments=message.get("attachments", []))
        if isinstance(message.get("replyTo"), dict):
            incoming["replyTo"] = message["replyTo"]
        archive = self._backend.archive
        with self._archive_lock:
            result = {"input": archive.append_message(incoming, "platform-input-" + identity)}
            if reply is not None:
                reply_id = "reply-" + identity
                # A resend check reuses the stored reply timestamp when no time was provided.
                if reply_time is None:
                    previous = archive.query_messages({"conversationId": conversation, "direction": "outbound", "messageId": reply_id})["items"]
                    replied_at = previous[0]["sentAt"] if previous else datetime.now(timezone.utc).isoformat()
                else:
                    replied_at = _timestamp(reply_time)
                outgoing = dict(common, id=reply_id, direction="outbound", sender="小希", text=reply,
                                sentAt=replied_at, receivedAt=replied_at, replyTo=result["input"]["recordId"])
                result["reply"] = archive.append_message(outgoing, "platform-reply-" + identity)
        return result


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("原话时间须为 Unix 时间戳")
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("原话时间无效") from exc
