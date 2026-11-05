"""Slice-1 acceptance test for Jury Room (Committee #2467). MANAGER-WRITTEN from docs/upstream/agent-protocol.md
and checked against the live Switch 0.31.0 stack on 127.0.0.1:8000. READ-ONLY: never edit this file to make it pass.

It drives agents/switch_client.py against the REAL server (no fake server anywhere):
register two agents -> create a room with both -> agent B opens its event stream (reads connection_state first)
-> B claims the room with connect_to_room -> B beats 3 times -> agent A posts a message -> B must read that exact
message back as an SSE `message` event for that room. Prints PASS as the last line only if every step held.

Environment: SWITCH_BASE_URL (default http://127.0.0.1:8000); the registration token is read from the file named by
SWITCH_ENV_FILE (default /opt/jury-room/stack/.env, key AGENT_REGISTRATION_TOKEN) and is never printed.
"""
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.switch_client import SwitchClient  # noqa: E402

BASE = os.environ.get("SWITCH_BASE_URL", "http://127.0.0.1:8000")
ENV_FILE = os.environ.get("SWITCH_ENV_FILE", "/opt/jury-room/stack/.env")


def _token():
    for line in open(ENV_FILE, encoding="utf-8"):
        if line.startswith("AGENT_REGISTRATION_TOKEN="):
            tok = line.split("=", 1)[1].strip()
            assert tok, "AGENT_REGISTRATION_TOKEN is empty"
            return tok
    raise AssertionError("AGENT_REGISTRATION_TOKEN not found in " + ENV_FILE)


def test_live_roundtrip():
    tok = _token()
    run = uuid.uuid4().hex[:6]
    # 1. register two custom always_on agents (POST /agents with an integration profile)
    a_id, a_key = SwitchClient.register(BASE, tok, f"jr-accept-a-{run}", "slice-1 acceptance sender")
    b_id, b_key = SwitchClient.register(BASE, tok, f"jr-accept-b-{run}", "slice-1 acceptance reader")
    assert a_id and a_key and b_id and b_key, "register must return (agent_id, api_key)"
    assert a_id != b_id
    a = SwitchClient(BASE, a_key, a_id)
    b = SwitchClient(BASE, b_key, b_id)
    try:
        # 2. ops discovery is live (GET /agents/{id}/ops)
        ops = a.list_operations()
        for name in ("create_room", "connect_to_room", "post_message"):
            assert name in ops, f"operation {name} missing from GET /ops"
        # 3. room with both agents (op create_room)
        room = a.create_room(f"jr-accept-room-{run}", "slice-1 acceptance room", [f"jr-accept-a-{run}", f"jr-accept-b-{run}"])
        assert isinstance(room, str) and len(room) >= 8, f"create_room must return the room id, got {room!r}"
        # 4. B opens its stream; the first frame must be connection_state naming this room
        state = b.open_stream([room])
        assert state.get("connection_id") == b.connection_id, "connection_state must echo the client's connection id"
        assert room in state.get("rooms", []), f"connection_state rooms {state.get('rooms')} lacks {room}"
        # 5. claim the room (op connect_to_room with X-Switch-Connection-Id)
        ctx = b.connect_to_room(room)
        assert ctx.get("room_id") == room, f"connect_to_room returned {ctx!r}"
        # 6. heartbeat x3 (POST /agents/{id}/connection/beat), every one ok and naming the room
        for _ in range(3):
            beat = b.beat()
            assert beat.get("ok") is True, f"beat refused: {beat!r}"
            assert room in beat.get("rooms", []), f"beat rooms {beat.get('rooms')} lacks {room}"
            time.sleep(2)
        # 7. A posts; B must read that exact text back on its stream
        text = f"jury-room acceptance {uuid.uuid4().hex}"
        event_id = a.post_message(room, text)
        assert isinstance(event_id, str) and event_id, f"post_message must return the event id, got {event_id!r}"
        deadline = time.time() + 20
        seen = None
        while time.time() < deadline and seen is None:
            ev = b.next_event(timeout=2.0)
            if ev is None:
                b.beat()
                continue
            if ev.get("type") == "message" and ev.get("room_id") == room and text in str(ev.get("payload", "")):
                seen = ev
        assert seen is not None, "B never received A's message on its event stream"
        assert isinstance(seen.get("sequence"), int), "a domain event must carry an integer sequence"
    finally:
        b.close()
        a.close()


if __name__ == "__main__":
    test_live_roundtrip()
    print("PASS")
