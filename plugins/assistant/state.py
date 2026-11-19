"""The assistant's queue, delivery ledger, and lifecycle record."""
from datetime import datetime, timezone
import json
import math
import sqlite3
from typing import NamedTuple


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def message_record(value):
    if not isinstance(value, dict):
        raise ValueError("消息需要是对象")
    for field in ("id", "channel", "peer"):
        item = value.get(field)
        limit = 100 if field == "channel" else 300
        if not isinstance(item, str) or not item.strip() or len(item) > limit:
            raise ValueError(f"消息 {field} 需要 1—{limit} 字符的字符串")
    timestamp = value.get("time")
    if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
            or not 0 <= timestamp < 253402300800 or not math.isfinite(timestamp)):
        raise ValueError("消息 time 需要是 Unix 秒")
    text = value.get("text", "")
    attachments = value.get("attachments", [])
    if not isinstance(text, str) or not isinstance(attachments, list):
        raise ValueError("消息 text 需要是字符串，attachments 需要是数组")
    record = {key: value[key] for key in ("id", "channel", "peer", "time")}
    record.update(text=text, attachments=attachments, receivedAt=datetime.now(timezone.utc).isoformat())
    reply = value.get("replyTo")
    if not isinstance(reply, dict):
        for attachment in attachments:
            if not isinstance(attachment, dict) or attachment.get("type") != "reply":
                continue
            data = attachment.get("data")
            if isinstance(data, dict) and data.get("id") is not None:
                reply = {"messageId": str(data["id"]), **{key: data[key] for key in ("sentAt", "text") if data.get(key)}}
                break
    if isinstance(reply, dict):
        record["replyTo"] = reply
    return json.loads(encode(record).encode("utf-8"))


class Material(NamedTuple):
    order: int
    message: dict
    reply: str | None
    input_ref: str | None
    reply_ref: str | None
    reply_at: float | None
    include_input: bool = True


class Store:
    """All access is serialized by the assistant's condition lock."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY,
                    channel TEXT NOT NULL,
                    peer TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    order_no INTEGER UNIQUE,
                    status TEXT NOT NULL DEFAULT 'queued',
                    startup TEXT,
                    reply TEXT,
                    reply_at REAL,
                    input_ref TEXT,
                    reply_ref TEXT,
                    recovery_announced INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(channel, peer, message_id)
                );
                CREATE INDEX IF NOT EXISTS messages_status_order ON messages(status, order_no);
            """)
            fields = {row["name"] for row in self.db.execute("PRAGMA table_info(messages)")}
            if "reply_at" not in fields:
                self.db.execute("ALTER TABLE messages ADD COLUMN reply_at REAL")
            if "recovery_announced" not in fields:
                self.db.execute("ALTER TABLE messages ADD COLUMN recovery_announced INTEGER NOT NULL DEFAULT 0")
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT data FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["data"]) if row else default

    def _put(self, key, data):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, encode(data)))

    def update(self, **values):
        with self.db:
            for key, value in values.items():
                self._put(key, value)

    def begin(self, now):
        previous = self.get("runtime", {})
        stopped_at = previous.get("stopped_at")
        estimated = stopped_at is None and previous.get("last_alive_at") is not None
        if estimated:
            stopped_at = previous["last_alive_at"]
        window = {
            "stopped_at": stopped_at,
            "started_at": now,
            "duration_seconds": max(0, now - stopped_at) if stopped_at is not None else None,
            "estimated": estimated,
        }
        with self.db:
            interrupted = self.db.execute(
                "UPDATE messages SET status='interrupted' WHERE status='started'"
            ).rowcount
            self._release_held(self._next_order())
            self._put("desired_running", True)
            self._put("runtime", {"started_at": now, "last_alive_at": now, "stopped_at": None})
            self._put("startup_unread", {"state": "pulling", "count": 0, "window": window})
        return window, interrupted

    def _next_order(self):
        return self.db.execute("SELECT COALESCE(MAX(order_no),0) + 1 FROM messages").fetchone()[0]

    def enqueue(self, message, *, hold):
        with self.db:
            order = None if hold else self._next_order()
            inserted = self.db.execute(
                """INSERT INTO messages(channel,peer,message_id,payload,order_no) VALUES (?,?,?,?,?)
                ON CONFLICT(channel,peer,message_id) DO NOTHING""",
                (message["channel"], message["peer"], message["id"], encode(message), order),
            ).rowcount
        return bool(inserted)

    def _release_held(self, order):
        rows = self.db.execute(
            "SELECT seq FROM messages WHERE order_no IS NULL ORDER BY seq"
        ).fetchall()
        self.db.executemany("UPDATE messages SET order_no=? WHERE seq=?",
                            [(order + offset, row["seq"]) for offset, row in enumerate(rows)])

    def finish_pull(self, messages, window, *, retry=False, error=None, cancelled=False):
        with self.db:
            order = self._next_order()
            count = 0
            startup = encode(window)
            for message in messages:
                # A held realtime copy belongs to this window after verification by the pull.
                # Copies already admitted to ordinary work keep their original work type.
                count += self.db.execute(
                    """INSERT INTO messages(channel,peer,message_id,payload,order_no,startup)
                    VALUES (?,?,?,?,?,?) ON CONFLICT(channel,peer,message_id) DO UPDATE SET
                    order_no=excluded.order_no, startup=excluded.startup,
                    payload=json_set(excluded.payload,'$.receivedAt',COALESCE(json_extract(messages.payload,'$.receivedAt'),
                                                                          json_extract(excluded.payload,'$.receivedAt')))
                    WHERE messages.order_no IS NULL""",
                    (message["channel"], message["peer"], message["id"], encode(message), order + count, startup),
                ).rowcount
            self._release_held(order + count)
            state = "cancelled" if cancelled else "failed" if error else "queued" if count else "empty"
            if window["stopped_at"] is None and not error and not cancelled:
                state = "first_start"
            status = {"state": state, "count": count, "window": window}
            if error:
                status["error"] = error
                self._put("failed_window", window)
            elif retry and not cancelled:
                self._put("failed_window", None)
            self._put("startup_unread", status)
        return count

    def queued(self):
        return self.db.execute("SELECT COUNT(*) FROM messages WHERE status='queued'").fetchone()[0]

    def claim_batch(self, limits):
        rows = self.db.execute(
            "SELECT * FROM messages WHERE status='queued' AND order_no IS NOT NULL ORDER BY order_no LIMIT ?",
            (limits["maxBatchMessages"],),
        ).fetchall()
        if not rows:
            return None
        first = rows[0]
        group = (first["channel"], first["peer"], first["startup"])
        messages, orders, size = [], [], 0
        for row in rows:
            if (row["channel"], row["peer"], row["startup"]) != group:
                break
            message = json.loads(row["payload"])
            if orders and (message.get("source", "owner") != "owner" or messages[0].get("source", "owner") != "owner"):
                break
            message_chars = len(row["payload"])
            if orders and size + message_chars > limits["maxBatchChars"]:
                break
            orders.append(row["order_no"])
            messages.append(message)
            size += message_chars

        window = json.loads(first["startup"]) if first["startup"] else None
        context = {}
        with self.db:
            self.db.executemany("UPDATE messages SET status='started' WHERE order_no=?",
                                [(order,) for order in orders])
            if window is not None:
                previous = self.get("startup_batch", {})
                number = previous["number"] + 1 if previous.get("window") == window else 1
                self._put("startup_batch", {"window": window, "number": number})
                context.update(shutdown=window, batch_number=number)
        return orders, messages, context

    def finish_batch(self, orders, status, reply, references, *, executed, reply_at=None, recovery_orders=()):
        with self.db:
            self.db.executemany("UPDATE messages SET status=? WHERE order_no=?",
                                [(status, order) for order in orders])
            if reply is not None:
                self.db.execute("UPDATE messages SET reply=?,reply_at=? WHERE order_no=?", (reply, reply_at, orders[-1]))
            self.db.executemany("UPDATE messages SET input_ref=?,reply_ref=? WHERE order_no=?",
                                [(input_ref, reply_ref, order) for order, (input_ref, reply_ref) in references.items()])
            if executed:
                self.db.executemany("UPDATE messages SET recovery_announced=1 WHERE order_no=?",
                                    [(order,) for order in recovery_orders])
                self._put("handoff", None)
                self._put("selected_originals", [])
                self._put("turns_since_handoff", self.get("turns_since_handoff", 0) + 1)

    def pending_recovery(self, channel, peer, before_order):
        return self.db.execute("""
            SELECT order_no,message_id FROM messages WHERE channel=? AND peer=? AND order_no<?
            AND recovery_announced=0 AND status IN ('interrupted','skipped') ORDER BY order_no DESC
        """, (channel, peer, before_order)).fetchall()

    def skip_input(self, channel, peer, message_id):
        with self.db:
            row = self.db.execute("SELECT status,payload FROM messages WHERE channel=? AND peer=? AND message_id=?",
                                  (channel, peer, message_id)).fetchone()
            if row is None:
                return None
            if row["status"] == "skipped":
                return "skipped"
            status = row["status"]
            if status in {"queued", "failed", "interrupted"} and json.loads(row["payload"]).get("source", "owner") == "owner":
                self.db.execute("UPDATE messages SET status='skipped' WHERE channel=? AND peer=? AND message_id=?",
                                (channel, peer, message_id))
                return "skipped"
            return status

    def latest_settled(self):
        return self.db.execute(
            "SELECT COALESCE(MAX(order_no),0) FROM messages WHERE status IN ('completed','failed')"
        ).fetchone()[0]

    def material_after(self, cursor, limit=-1):
        return list(self._material_rows(cursor, limit))

    def _material_rows(self, cursor, limit=-1):
        rows = self.db.execute(
            "SELECT * FROM messages WHERE status IN ('completed','failed') AND order_no>? ORDER BY order_no LIMIT ?",
            (cursor, limit),
        )
        for row in rows:
            yield Material(row["order_no"], json.loads(row["payload"]), row["reply"], row["input_ref"], row["reply_ref"], row["reply_at"])

    def missing_reply_dates(self):
        return self.db.execute("SELECT order_no,reply_ref FROM messages WHERE reply IS NOT NULL AND reply_ref IS NOT NULL AND reply_at IS NULL").fetchall()

    def save_reply_dates(self, times):
        with self.db:
            self.db.executemany("UPDATE messages SET reply_at=? WHERE order_no=? AND reply_at IS NULL", times)

    @staticmethod
    def _source_days(sent_at, reply_at, has_reply, zone):
        input_day = datetime.fromtimestamp(sent_at, zone).date().isoformat()
        reply_day = None
        if has_reply:
            reply_day = datetime.fromtimestamp(reply_at, zone).date().isoformat() if reply_at is not None else input_day
        return input_day, reply_day

    def _dream_progress(self, zone):
        legacy = self.get("dream_cursor", 0)
        progress = self.get(f"dream_coverage:{zone}", {})
        progress.setdefault("floor", legacy)
        progress.setdefault("days", {})
        progress.setdefault("attempts", {})
        return progress

    def _source_dates(self, after):
        return self.db.execute("""
            SELECT order_no,status,json_extract(payload,'$.time') AS sent_at,reply_at,reply IS NOT NULL AS has_reply
            FROM messages WHERE order_no>? ORDER BY order_no
        """, (after,))

    def dream_material(self, zone, limit, *, automatic):
        # The old platform cursor describes work already reviewed before this upgrade.
        progress = self._dream_progress(zone)
        floor, days, attempts = progress["floor"], progress["days"], progress["attempts"]
        today = datetime.now(zone).date().isoformat()
        candidates = {}
        for row in self._source_dates(floor):
            if row["status"] not in {"completed", "failed"}:
                continue
            source_days = self._source_days(row["sent_at"], row["reply_at"], row["has_reply"], zone)
            for day in set(source_days) - {None}:
                if day > today or row["order_no"] <= days.get(day, floor):
                    continue
                size, last = candidates.get(day, (0, 0))
                if size < limit:
                    candidates[day] = size + 1, row["order_no"]
        for day, (_, last) in sorted(candidates.items()):
            if automatic and attempts.get(day) == last:
                continue
            batch = []
            for row in self._material_rows(max(floor, days.get(day, floor))):
                if row.order > last:
                    break
                input_day, reply_day = self._source_days(row.message["time"], row.reply_at, row.reply is not None, zone)
                if day in (input_day, reply_day):
                    batch.append(row._replace(include_input=day == input_day, reply=row.reply if day == reply_day else None,
                                              reply_ref=row.reply_ref if day == reply_day else None))
            return day, batch
        return None

    def start_dream(self, zone, day, last_order, now):
        progress = self._dream_progress(zone)
        progress["attempts"][day] = last_order
        self.update(last_dream_at=now, **{f"dream_coverage:{zone}": progress})

    def finish_dream(self, zone, day, last_order):
        progress = self._dream_progress(zone)
        days = progress["days"]
        days[day] = last_order
        floor = progress["floor"]
        for row in self._source_dates(floor):
            if row["status"] not in {"completed", "failed", "interrupted", "skipped"}:
                break
            if row["status"] in {"completed", "failed"}:
                phases = self._source_days(row["sent_at"], row["reply_at"], row["has_reply"], zone)
                if any(days.get(date, floor) < row["order_no"] for date in phases if date is not None):
                    break
            floor = row["order_no"]
        progress["floor"] = floor
        progress["days"] = {day: order for day, order in days.items() if order > floor}
        progress["attempts"] = {day: order for day, order in progress["attempts"].items() if order > floor}
        self.update(**{f"dream_coverage:{zone}": progress})
