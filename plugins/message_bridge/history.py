"""Owner-private event normalization and one bounded startup history walk."""
import math
from pathlib import Path
import sqlite3

from xixi_core.platform.errors import PlatformError


def incomplete(reason):
    return PlatformError("history_incomplete", reason, 503)


def identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise incomplete("渠道消息缺少有效编号")
    try:
        number = int(value)
    except ValueError:
        raise incomplete("渠道消息编号格式无效") from None
    if not number or str(number) != str(value):
        raise incomplete("渠道消息编号无效")
    return str(number)


def timestamp(event):
    stamp = event.get("time")
    if type(stamp) not in (float, int) or not math.isfinite(stamp) or stamp <= 0:
        raise incomplete("渠道消息缺少有效时间")
    return stamp


def owner_message(event, bot, owner, *, history=False):
    if not isinstance(event, dict):
        raise incomplete("渠道消息格式无效")
    if not history and (event.get("post_type") != "message" or str(event.get("self_id")) != bot):
        return None
    if event.get("message_type") != "private" or event.get("group_id"):
        if history:
            raise incomplete("好友历史含未识别的会话类型")
        return None
    sender = str((event.get("sender") or {}).get("user_id", event.get("user_id")))
    if sender != owner:
        if history and sender != bot:
            raise incomplete("历史消息的发送人与绑定会话不一致")
        return None
    target = event.get("target_id")
    if target not in (None, 0, "0") and str(target) != bot:
        if history:
            raise incomplete("历史消息接收方与绑定账号不一致")
        return None
    parts = event.get("message")
    if not isinstance(parts, list):
        raise incomplete("渠道消息缺少原始消息段")
    body, attachments = [], []
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get("data"), dict) or not part.get("type"):
            raise incomplete("无法识别渠道消息段")
        data = part["data"]
        if part["type"] == "text":
            if not isinstance(data.get("text"), str):
                raise incomplete("文本消息段缺少正文")
            body.append(data["text"])
        else:
            attachments.append({"type": part["type"], "data": data})
    return {"channel": "onebot:" + bot, "peer": owner, "id": identifier(event.get("message_id")),
            "time": timestamp(event), "text": "".join(body), "attachments": attachments}


def authoritative_anchor(index_path, anchor, owner):
    # Mirrors this SnowLuma release's findMeta/hasAuthoritativeSequence check.
    # A cached page and a server page otherwise have the same OneBot response.
    if not index_path:
        return False
    uri = Path(index_path).resolve().as_uri() + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            row = db.execute("SELECT sequence FROM messages WHERE message_hash=? AND is_group=0 "
                             "AND session_id=? AND sequence_authoritative=1 AND sequence>0",
                             (int(anchor), int(owner))).fetchone()
            return row is not None
        finally:
            db.close()
    except sqlite3.Error:
        raise incomplete("无法只读核对 SnowLuma 分页锚点的来源") from None


def collect(call, bot, owner, stopped_at, started_at, index_path=None, page_size=200):
    if stopped_at is None:
        return []
    lower, upper = math.floor(stopped_at), math.floor(started_at)
    if lower > upper:
        raise incomplete("停机时间晚于启动时间")
    anchor = 0
    seen_anchors = set()
    pages = []
    while True:
        if anchor and not authoritative_anchor(index_path, anchor, owner):
            raise incomplete("首批历史未覆盖停机时间，分页锚点无法证实可取服务器记录")
        data = call("get_friend_msg_history", {"user_id": int(owner), "message_id": anchor,
                                               "count": page_size, "reverse_order": True})
        rows = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not rows:
            raise incomplete("历史查询未能覆盖停止接收时间")
        if not all(isinstance(row, dict) for row in rows):
            raise incomplete("历史查询含无效消息")
        ids = [identifier(row.get("message_id")) for row in rows]
        if anchor and str(anchor) not in ids:
            raise incomplete("历史分页缺少请求的消息锚点")
        stamps = [timestamp(row) for row in rows]
        items = []
        for row, stamp in zip(rows, stamps):
            if lower <= math.floor(stamp) <= upper:
                normalized = owner_message(row, bot, owner, history=True)
                if normalized is not None:
                    items.append(normalized)
        pages.append(items)
        if min(stamps) < lower:
            break
        # SnowLuma returns a page in ascending sequence order, anchor included.
        anchor = int(ids[0])
        if anchor in seen_anchors:
            raise incomplete("历史分页没有继续向前")
        seen_anchors.add(anchor)
    result = {}
    for page in reversed(pages):
        for message in page:
            result.setdefault(message["id"], message)
    return sorted(result.values(), key=lambda item: item["time"])
