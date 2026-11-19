"""Store individual DSH prompt overrides for newly created sessions."""
import hashlib
import json
import os
import tempfile
import threading

from xixi_core.platform.errors import PlatformError
from xixi_core.prompt_policy import normalize


EFFECT = "new_sessions_and_new_defined_tasks"


class PromptPolicy:
    def __init__(self, path, capabilities):
        self.path = path
        self.capabilities = capabilities
        self.lock = threading.Lock()

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"revision": 1, "policy": normalize({"version": 1}), "compaction": {}}

    def _write(self, document):
        fd, name = tempfile.mkstemp(prefix=".prompt-policy-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(document, stream, ensure_ascii=False, allow_nan=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def session_fields(self):
        with self.lock:
            saved = self._read()
        result = {"promptPolicy": saved["policy"]}
        if saved["compaction"]:
            result["nativeCompaction"] = {
                **saved["compaction"],
                "versions": dict.fromkeys(saved["compaction"], saved["revision"]),
            }
        return result

    @staticmethod
    def _response(saved):
        return {key: saved[key] for key in ("revision", "policy", "compaction")} | {"effect": EFFECT}

    def get(self, _request):
        with self.lock:
            saved = self._read()
        return {**self._response(saved), "capabilities": self.capabilities(),
                "existingSessions": "当前会话保留创建时的提示"}

    def save(self, request):
        body = request.get_json()
        try:
            if (not isinstance(body, dict) or set(body) - {"policy", "compaction", "expectedRevision", "requestId"}
                    or not {"expectedRevision", "requestId"} <= set(body)
                    or not {"policy", "compaction"}.intersection(body)):
                raise ValueError("请提供提示正文和读取时的版本")
            expected, request_id = body["expectedRevision"], body["requestId"]
            if type(expected) is not int or expected < 1 or not isinstance(request_id, str) or not 1 <= len(request_id) <= 300:
                raise ValueError("提示配置版本或请求编号无效")
            policy = normalize(body["policy"]) if "policy" in body else None
            compaction = body.get("compaction", {})
            if (not isinstance(compaction, dict) or set(compaction) - {"instruction", "checkpoint"}
                    or any(not isinstance(value, str) or len(value) > 100000 for value in compaction.values())
                    or "instruction" in compaction and not compaction["instruction"].strip()):
                raise ValueError("压缩指令须为非空文本，检查点说明可为空")
            digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        except ValueError as exc:
            raise PlatformError("invalid_prompt_policy", str(exc)) from exc

        with self.lock:
            saved = self._read()
            last = saved.get("last", {})
            if last.get("id") == request_id and last.get("digest") == digest:
                return self._response(saved)
            if last.get("id") == request_id or expected != saved["revision"]:
                raise PlatformError("prompt_policy_conflict", "提示配置版本或请求编号冲突，请重新读取", 409)
            if policy is None:
                policy = saved["policy"]
            if "compaction" not in body:
                compaction = saved["compaction"]
            required = []
            for name, row in policy["runtime"].items():
                if row["mode"] != "template":
                    continue
                previous = saved["policy"]["runtime"].get(name, {})
                if name == "time":
                    changed = row != previous
                else:
                    before = previous.get("templates", {})
                    changed = any(before.get(part) != value for part, value in row["templates"].items())
                if changed:
                    required.append(name)
            if any(saved["compaction"].get(part) != value for part, value in compaction.items()):
                required.append("compaction")
            if required:
                caps = self.capabilities()["hostCapabilities"]
                if any(caps.get(name) != 1 for name in required):
                    raise PlatformError("native_prompt_hook_unavailable", "DSH 未提供所选原生提示的编辑接口", 409)
            saved = {"revision": expected + 1, "policy": policy, "compaction": compaction,
                     "last": {"id": request_id, "digest": digest}}
            self._write(saved)
            return self._response(saved)
