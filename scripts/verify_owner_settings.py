"""Check owner settings against the isolated DSH and loopback model fixture."""
import json
import time


MODELS = "/api/modules/model_settings/"
PERSONA = "/api/modules/persona/injections"
ASSISTANT = "/api/modules/assistant/"


class VerificationMessageBridge:
    """Process synthetic input and record reply receipts without connecting QQ."""

    def start(self, context):
        self.sent = []
        context.services.provide("message_bridge", self)

    def pull_unread(self, stopped_at, started_at, *, cancel_event=None):
        return []

    def send(self, message, text):
        self.sent.append((message["id"], text))
        return {"id": f"synthetic-reply-{len(self.sent)}"}

    def status(self):
        return {"sent": len(self.sent)}

    def stop(self):
        pass


def _text(blocks):
    if isinstance(blocks, str):
        return blocks
    return "\n".join(block.get("text", "") for block in (blocks or []))


def _latest(body):
    message = next((message for message in reversed(body.get("messages", [])) if message.get("role") == "user"), {})
    return _text(message.get("content"))


def _request_text(body):
    return "\n".join([_text(body.get("system")), *(_text(message.get("content")) for message in body.get("messages", []))])


def _turn(calls, marker, *, system=None):
    for call in calls:
        system_text = _text(call.get("system"))
        if "Create a concise title" in system_text or "FIXTURE_TITLE" in system_text:
            continue
        if marker in _latest(call) and (system is None or system in system_text):
            return call
    raise AssertionError(f"No primary model request contained {marker!r}")


def _wait_idle(assistant):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        state = assistant.status()
        if not (state["activity"] or state["requested"] or state["queue_depth"]):
            assert state["last_error"] is None, state["last_error"]
            assert state["last_archive_error"] is None, state["last_archive_error"]
            return state
        time.sleep(0.05)
    raise AssertionError(f"Assistant did not drain its work: {assistant.status()}")


def verify_owner_settings(core, client, local_origin, headers, fixture):
    def request(path, body=None):
        response = client.open(path, method="GET" if body is None else "POST", json=body,
                               headers=headers, base_url=local_origin)
        assert response.status_code == 200, (path, response.get_data(as_text=True))
        return response.json

    def save_model(bindings, request_id):
        current = request(MODELS + "settings")
        result = request(MODELS + "settings", {"bindings": bindings, "expectedRevision": current["revision"],
                                               "requestId": request_id})
        assert {kind: result["bindings"][kind] for kind in bindings} == bindings

    def save_prompt(prompt_id, body):
        item = next(item for item in request(PERSONA)["items"] if item["id"] == prompt_id)
        saved = request(PERSONA, {"id": prompt_id, "body": body, "enabled": True, "expectedVersion": item["version"]})
        assert saved["body"] == body

    def receive(message_id, text):
        start = len(fixture.calls)
        result = assistant.receive({"id": message_id, "channel": "owner-settings-verification", "peer": "fixture-owner",
                                    "time": time.time(), "text": text, "attachments": []})
        assert result["accepted"], result
        state = _wait_idle(assistant)
        return state["session_id"], fixture.calls[start:]

    catalog = request(MODELS + "catalog")
    provider = catalog["default"]["provider"]
    group = next(group for group in catalog["groups"] if group["id"] == provider)
    assert provider in catalog["routableProviders"]
    flash, pro = "deepseek-flash", "deepseek-v4-pro"
    assert {flash, pro}.issubset({model["id"] for model in group["models"]})

    def binding(model):
        return {"provider": provider, "model": model, "agentPreset": "standard", "reasoningEffort": None}

    save_model({"daily": binding(flash), "consolidation": binding(pro), "dream": binding(flash)}, "owner-settings-initial-models")
    role_initial, role_next = "OWNER_SETTINGS_ROLE_INITIAL", "OWNER_SETTINGS_ROLE_NEXT"
    current_turn, updated_turn = "OWNER_SETTINGS_RUN_INITIAL", "OWNER_SETTINGS_RUN_NEXT"
    first_text, second_text, third_text = (f"OWNER_SETTINGS_MESSAGE_{index}" for index in (1, 2, 3))
    handoff_marker, dream_marker = "OWNER_SETTINGS_HANDOFF_MATERIAL", "OWNER_SETTINGS_DREAM_MATERIAL"
    save_prompt("daily.role", role_initial)
    save_prompt("message.owner", current_turn + " {text}")
    save_prompt("maintenance.instructions", handoff_marker)
    save_prompt("dream.instructions", dream_marker)

    assistant = core.services.get("assistant")
    request(ASSISTANT + "start", {})
    _wait_idle(assistant)
    old_session, first_calls = receive("owner-settings-one", first_text)
    first = _turn(first_calls, current_turn, system=role_initial)
    assert first["model"] == flash and _latest(first).count(first_text) == 1
    assert _text(first.get("system")).count(role_initial) == 1

    save_model({"daily": binding(pro)}, "owner-settings-next-daily-model")
    save_prompt("daily.role", role_next)
    save_prompt("message.owner", updated_turn + " {text}")
    same_session, second_calls = receive("owner-settings-two", second_text)
    second = _turn(second_calls, updated_turn, system=role_initial)
    second_latest = _latest(second)
    assert same_session == old_session and second["model"] == flash
    assert _text(second.get("system")).count(role_initial) == 1 and role_next not in _text(second.get("system"))
    assert second_latest.count(second_text) == 1 and first_text not in second_latest and current_turn not in second_latest

    start = len(fixture.calls)
    request(ASSISTANT + "handoff", {})
    new_session = _wait_idle(assistant)["session_id"]
    assert new_session != old_session
    handoff = _turn(fixture.calls[start:], handoff_marker)
    handoff_text = _request_text(handoff)
    assert handoff["model"] == pro and handoff_text.count(first_text) == handoff_text.count(second_text) == 1
    assert role_initial not in handoff_text and role_next not in handoff_text

    active_session, third_calls = receive("owner-settings-three", third_text)
    third = _turn(third_calls, updated_turn, system=role_next)
    assert active_session == new_session and third["model"] == pro
    assert _text(third.get("system")).count(role_next) == 1 and _latest(third).count(third_text) == 1

    start = len(fixture.calls)
    request(ASSISTANT + "dream", {})
    assert _wait_idle(assistant)["session_id"] == new_session
    dream = _turn(fixture.calls[start:], dream_marker)
    dream_text = _request_text(dream)
    assert dream["model"] == flash and all(dream_text.count(text) == 1 for text in (first_text, second_text, third_text))
    assert role_initial not in dream_text and role_next not in dream_text
    diary = core.services.get("library").dream_diary()["existingDiary"]
    assert diary and diary["version"] == 1 and diary["body"], diary
    request(ASSISTANT + "stop", {})
    result = {"provider": provider, "daily_first": first["model"], "daily_existing_after_edit": second["model"],
              "daily_after_handoff": third["model"], "handoff": handoff["model"], "dream": dream["model"],
              "frozen_session_system": True, "live_turn_injection": True, "materials_present_once": True,
              "dream_diary_version": diary["version"]}
    print(json.dumps({"owner_settings_verified": result}, ensure_ascii=False), flush=True)
    return result
