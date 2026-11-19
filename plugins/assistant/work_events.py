"""Persist independent work and deliver its events through the daily input queue."""
from datetime import datetime
from hashlib import sha256
import json
import sqlite3
from threading import Condition, Event, Thread
import time
from uuid import uuid4

from xixi_core.limits import DEFAULT_RESULT_PAGE_CHARS, MAX_RESULT_PAGE_CHARS, canonical_json, result_notice
from xixi_core.platform.errors import PlatformError
from xixi_core.privacy import private_sqlite


EVENT_KINDS = {"restart", "fault", "recovery", "wake", "notice"}


def _text(value, field, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f"{field} 需要 {'至多' if empty else '非空且至多'} {maximum} 字符的文本")
    value.encode("utf-8")
    return value


def _page(value, offset, limit):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_RESULT_PAGE_CHARS:
        raise ValueError(f"offset 需为非负整数，limit 需为 1—{MAX_RESULT_PAGE_CHARS} 的整数")
    page = value[offset:offset + limit]
    end = offset + len(page)
    return {"offset": offset, "nextOffset": end if end < len(value) else None, "totalChars": len(value), "text": page,
            "sha256": sha256(value.encode("utf-8")).hexdigest()}


class WorkEvents:
    def __init__(self, context, enqueue_event, on_change=None):
        self.context = context
        self.enqueue_event = enqueue_event
        self.on_change = on_change
        self._condition = Condition()
        self._running = self._closing = False
        self._active = self._cancel = self._db = None
        self._threads = ()

    def start(self):
        path = self.context.data_dir / "background.sqlite3"
        private_sqlite(path)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS work (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, origin TEXT NOT NULL, input TEXT NOT NULL,
                    session_id TEXT NOT NULL UNIQUE, frozen TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_artifacts (
                    ref TEXT PRIMARY KEY, task_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL, media_type TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS system_events (
                    id TEXT PRIMARY KEY, due REAL NOT NULL, payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS system_events_pending ON system_events(delivered,due);
            """)
            with self._db:
                for row in self._db.execute("SELECT * FROM work WHERE state='running'").fetchall():
                    self._finish(row, "stopped", "Core 进程退出时尚未取得任务结果，原任务不会自动续跑。")
            self._threads = (Thread(target=self._run_work, name="xiaoxi-background-work", daemon=True),
                             Thread(target=self._run_events, name="xiaoxi-background-events", daemon=True))
            for thread in self._threads:
                thread.start()
        except BaseException:
            with self._condition:
                self._closing = True
                self._condition.notify_all()
            for thread in self._threads:
                if thread.is_alive():
                    thread.join()
            self._db.close()
            self._db = None
            raise

    def set_running(self, value):
        with self._condition:
            self._running = bool(value)
            self._condition.notify_all()

    def busy(self):
        with self._condition:
            return self._active is not None

    def stop(self):
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        for thread in self._threads:
            if thread.ident is not None:
                thread.join(2)
            if thread.is_alive():
                with self._condition:
                    if self._cancel is not None:
                        self._cancel.set()
                thread.join(10)
            if thread.is_alive():
                raise RuntimeError("独立工作或事件线程尚未结束，请重启 Core")
        with self._condition:
            if self._db is not None:
                self._db.close()
                self._db = None

    def _task(self, task_id):
        row = self._db.execute("SELECT * FROM work WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise PlatformError("task_not_found", "独立工作不存在", 404)
        return row

    def _view(self, row):
        payload = json.loads(row["input"])
        data = {"id": row["id"], "title": payload["title"], "state": row["state"], "createdAt": row["created"],
                "updatedAt": row["updated"], "sessionId": row["session_id"], "origin": json.loads(row["origin"])}
        if row["result"] is not None:
            data["result"] = self._notice(row["id"], row["result"])
        return data

    def submit_background_task(self, data, *, request_id, origin):
        if not isinstance(data, dict) or set(data) - {"title", "objective", "contextRefs", "executor"}:
            raise ValueError("独立工作参数无效")
        objective = _text(data.get("objective"), "objective", 100000)
        title = _text(data.get("title", objective.strip()[:80]), "title", 500)
        refs = data.get("contextRefs", [])
        if not isinstance(refs, list) or len(refs) > 100:
            raise ValueError("contextRefs 须为至多 100 个引用")
        refs = [_text(ref, "contextRef", 1024) for ref in refs]
        if data.get("executor", "runtime") != "runtime":
            raise ValueError("独立工作执行器使用 runtime")
        payload = {"objective": objective, "title": title, "contextRefs": refs, "executor": "runtime"}
        destination = self._origin(origin, session=True)
        request_id = _text(request_id, "requestId", 300)

        def replay():
            row = self._db.execute("SELECT * FROM work WHERE request_id=?", (request_id,)).fetchone()
            if row is not None:
                if json.loads(row["input"]) != payload or json.loads(row["origin"]) != destination:
                    raise PlatformError("task_request_conflict", "同一工作请求编号的内容不同", 409)
                return self._view(row)
            if not self._running or self._closing:
                raise PlatformError("assistant_not_running", "助手当前未开始接收独立工作", 409)

        with self._condition:
            previous = replay()
        if previous is not None:
            return previous
        task_id = "task-" + uuid4().hex
        frozen = self.context.services.get("dsh_bridge").freeze_work(task_id, payload)
        with self._condition, self._db:
            previous = replay()
            if previous is not None:
                return previous
            now = time.time()
            self._db.execute("INSERT INTO work VALUES (?,?,?,?,?,?,'queued',NULL,?,?)", (
                task_id, request_id, canonical_json(destination), canonical_json(payload), frozen["specification"]["id"],
                canonical_json(frozen), now, now,
            ))
            self._condition.notify_all()
            return self._view(self._task(task_id))

    @staticmethod
    def _origin(origin, *, session=False):
        if not isinstance(origin, dict):
            raise ValueError("需要原日常会话的消息渠道与收件人")
        result = {"channel": _text(origin.get("channel"), "channel", 100), "peer": _text(origin.get("peer"), "peer", 300)}
        if session:
            result["sessionId"] = _text(origin.get("sessionId"), "sessionId", 180)
        return result

    def task_status(self, task_id):
        with self._condition:
            return self._view(self._task(task_id))

    def list_tasks(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit 须为 1—100 的整数")
        with self._condition:
            return [self._view(row) for row in self._db.execute("SELECT * FROM work ORDER BY created DESC LIMIT ?", (limit,))]

    def cancel_task(self, task_id):
        with self._condition, self._db:
            row = self._task(task_id)
            if row["state"] == "queued":
                self._finish(row, "stopped", "任务在开始执行前已取消。")
            elif row["state"] == "running" and self._active == task_id:
                self._cancel.set()
                return {**self._view(row), "cancelRequested": True}
            self._condition.notify_all()
            return self._view(self._task(task_id))

    @staticmethod
    def _notice(task_id, full):
        return json.loads(result_notice(task_id, json.loads(full), "task-result:" + task_id,
                                        sha256(full.encode("utf-8")).hexdigest(), len(full)))

    def read_task_result(self, task_id, offset=0, limit=DEFAULT_RESULT_PAGE_CHARS):
        with self._condition:
            row = self._task(task_id)
        if row["result"] is None:
            raise PlatformError("task_result_not_ready", "独立工作尚无持久结果", 409)
        return {"taskId": task_id, "resultRef": "task-result:" + task_id, **_page(row["result"], offset, limit)}

    def write_task_artifact(self, session_id, args, request_id):
        name = _text(args.get("name"), "name", 250)
        media_type = _text(args.get("mediaType", "text/plain"), "mediaType", 100)
        value = _text(args.get("text"), "text", 1000000, empty=True)
        with self._condition, self._db:
            task = self._db.execute("SELECT * FROM work WHERE session_id=?", (session_id,)).fetchone()
            if task is None:
                raise PlatformError("task_not_found", "找不到产物对应的独立工作", 404)
            artifact = self._db.execute("SELECT * FROM work_artifacts WHERE request_id=?", (request_id,)).fetchone()
            if artifact is None:
                if task["id"] != self._active or task["state"] != "running" or self._cancel.is_set():
                    raise PlatformError("task_inactive", "该工作当前不能写入产物", 409)
                ref = "task-artifact:" + task["id"] + ":" + uuid4().hex
                self._db.execute("INSERT INTO work_artifacts VALUES(?,?,?,?,?,?,?)", (
                    ref, task["id"], request_id, name, media_type, value, time.time(),
                ))
            else:
                if (artifact["task_id"], artifact["name"], artifact["media_type"], artifact["body"]) != (task["id"], name, media_type, value):
                    raise PlatformError("artifact_request_conflict", "同一产物请求编号的内容不同", 409)
                ref = artifact["ref"]
        return {"artifactRef": ref, "name": name, "mediaType": media_type, "totalChars": len(value),
                "sha256": sha256(value.encode("utf-8")).hexdigest()}

    def read_task_artifact(self, args):
        with self._condition:
            row = self._db.execute("SELECT * FROM work_artifacts WHERE ref=?", (args.get("artifactRef"),)).fetchone()
        if row is None:
            raise PlatformError("artifact_not_found", "独立工作产物不存在", 404)
        return {"artifactRef": row["ref"], "taskId": row["task_id"], "name": row["name"], "mediaType": row["media_type"],
                **_page(row["body"], args.get("offset", 0), args.get("limit", DEFAULT_RESULT_PAGE_CHARS))}

    def publish_system_event(self, data):
        if not isinstance(data, dict) or not isinstance(data.get("kind"), str) or data["kind"] not in EVENT_KINDS:
            raise ValueError("系统事件需要 restart、fault、recovery、wake 或 notice 类型")
        kind = data["kind"]
        event_id = _text(data.get("id"), "id", 240)
        origin = self._origin(data)
        message = _text(data.get("text"), "text", 8000)
        event = data.get("event", {})
        if not isinstance(event, dict):
            raise ValueError("event 需要 JSON 对象")
        event = {**event, "type": kind}
        raw_due = event.get("dueAt") if kind == "wake" else None
        now = time.time()
        if raw_due is None:
            due = now
        elif isinstance(raw_due, str):
            stamp = datetime.fromisoformat(raw_due)
            if stamp.utcoffset() is None:
                raise ValueError("提醒计划时间需要时区")
            due = stamp.timestamp()
        else:
            raise ValueError("提醒计划时间使用带时区的 ISO 字符串")
        msg = {"id": "system-" + event_id, "kind": kind, **origin, "time": due, "text": message, "event": event}
        if len(canonical_json(msg)) > 12000 or not 0 <= due < 253402300800:
            raise ValueError("系统事件的内容或计划时间超出范围")
        with self._condition, self._db:
            prior = self._db.execute("SELECT payload,delivered FROM system_events WHERE id=?", (msg["id"],)).fetchone()
            if prior is not None:
                frozen = json.loads(prior["payload"])
                if raw_due is None:
                    msg["time"] = frozen["time"]
                if msg != frozen:
                    raise PlatformError("system_event_conflict", "同一系统事件编号的内容不同", 409)
                return {"id": msg["id"], "accepted": True, "duplicate": True}
            self._put_event(msg)
            self._condition.notify_all()
        return {"id": msg["id"], "accepted": True, "duplicate": False}

    def _put_event(self, message):
        self._db.execute("INSERT INTO system_events(id,due,payload) VALUES(?,?,?)", (
            message["id"], message["time"], canonical_json(message),
        ))

    def _finish(self, task, status, text):
        task_id = task["id"]
        refs = [row[0] for row in self._db.execute("SELECT ref FROM work_artifacts WHERE task_id=? ORDER BY created", (task_id,))]
        full = canonical_json({"status": status, "summary": text, "artifactRefs": refs})
        now = time.time()
        self._db.execute("UPDATE work SET state=?,result=?,updated=? WHERE id=?", (status, full, now, task_id))
        payload = json.loads(task["input"])
        origin = json.loads(task["origin"])
        event = {**self._notice(task_id, full), "type": "task_result", "title": payload["title"]}
        self._put_event({"id": "task-result-" + task_id, "kind": "task_result", "time": now,
                         "channel": origin["channel"], "peer": origin["peer"], "text": event["summary"], "event": event})

    def _run_events(self):
        while True:
            with self._condition:
                if self._closing:
                    return
                if not self._running:
                    self._condition.wait()
                    continue
                event = self._db.execute("SELECT * FROM system_events WHERE delivered=0 ORDER BY due,id LIMIT 1").fetchone()
                if event is None:
                    self._condition.wait()
                    continue
                remaining = event["due"] - time.time()
                if remaining > 0:
                    self._condition.wait(min(60, remaining))
                    continue
            try:
                self.enqueue_event(json.loads(event["payload"]))
                with self._condition, self._db:
                    self._db.execute("UPDATE system_events SET delivered=1 WHERE id=?", (event["id"],))
            except Exception:
                with self._condition, self._db:
                    self._db.execute("UPDATE system_events SET due=? WHERE id=?", (time.time() + 2, event["id"]))
                self.context.logger.warning("系统事件 %s 等待回交", event["id"])

    def _run_work(self):
        while True:
            with self._condition:
                if self._closing:
                    return
                task = self._db.execute("SELECT * FROM work WHERE state='queued' ORDER BY created,id LIMIT 1").fetchone() if self._running else None
                if task is None:
                    self._condition.wait()
                    continue
                with self._db:
                    self._db.execute("UPDATE work SET state='running',updated=? WHERE id=?", (time.time(), task["id"]))
                self._active, self._cancel = task["id"], Event()
            try:
                reply = self.context.services.get("dsh_bridge").run_background_work(json.loads(task["frozen"]), cancel_event=self._cancel)
                status = "completed"
            except Exception as error:
                status = "stopped" if self._cancel.is_set() else "failed"
                reply = "独立工作已中断。" if status == "stopped" else f"独立工作执行失败：{str(error)[:500]}"
                self.context.logger.warning("%s 执行结果：%s", task["id"], status)
            finally:
                with self._condition, self._db:
                    self._finish(task, status, reply)
                    self._active = self._cancel = None
                    self._condition.notify_all()
            if self.on_change is not None:
                self.on_change()
