'''Foreperson agent for jury room: parse verdicts, tally results, run room coordination.

This module provides the logic for the foreperson agent in a jury room simulation.
It includes parsing individual verdicts, tallying multiple verdicts, formatting
results, and orchestrating the deliberation room.
'''
import json
import time
from typing import List, Dict, Any

def parse_verdict(text: str) -> Dict[str, str]:
    """Parse a verdict text into a structured dict.

    The function looks for lines starting with ``VERDICT:``, ``REASON:``,
    and ``RISK:`` (case‑insensitive).  The verdict word is upper‑cased and must
    be either ``APPROVE`` or ``REJECT``; otherwise the result is ``ABSTAIN``.
    Missing or malformed input also results in ``ABSTAIN`` with empty reason
    and risk.  The function never raises.

    Args:
        text: The raw verdict message.

    Returns:
        A mapping with keys ``verdict``, ``reason`` and ``risk``.
    """
    if not isinstance(text, str):
        return {"verdict": "ABSTAIN", "reason": "", "risk": ""}

    verdict = None
    reason = ""
    risk = ""

    for line in text.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("VERDICT:"):
            val = stripped.split(":", 1)[1].strip().upper()
            if val in ("APPROVE", "REJECT"):
                verdict = val
            else:
                verdict = "ABSTAIN"
        elif upper.startswith("REASON:"):
            reason = stripped.split(":", 1)[1].strip()
        elif upper.startswith("RISK:"):
            risk = stripped.split(":", 1)[1].strip()

    if verdict is None:
        return {"verdict": "ABSTAIN", "reason": "", "risk": ""}

    return {"verdict": verdict, "reason": reason, "risk": risk}

def tally(verdicts: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    """Combine the individual verdicts into a final decision.

    The algorithm follows the specification exactly:

    * Count ``APPROVE``, ``REJECT`` and ``ABSTAIN`` votes.
    * If two or more jurors abstain → outcome ``HUNG``.
    * If the numbers of approves and rejects are equal → outcome ``HUNG``.
    * If everybody approves (no abstentions) → ``UNANIMOUS_APPROVE``.
    * If everybody rejects (no abstentions) → ``UNANIMOUS_REJECT``.
    * Otherwise outcome is ``SPLIT``; the majority side is the larger of
      approve/reject and every juror not on that side (including abstainers)
      appears in ``dissent``.
    * All abstainers are listed in ``abstained`` in the order they appear in
      the input mapping.

    Args:
        verdicts: Mapping of juror name → parsed verdict dict.

    Returns:
        A dict matching the contract in the specification.
    """
    approve = reject = abstain = 0
    approve_jurors: List[str] = []
    reject_jurors: List[str] = []
    abstain_jurors: List[str] = []

    for name, v in verdicts.items():
        if v["verdict"] == "APPROVE":
            approve += 1
            approve_jurors.append(name)
        elif v["verdict"] == "REJECT":
            reject += 1
            reject_jurors.append(name)
        else:
            abstain += 1
            abstain_jurors.append(name)

    outcome = ""
    majority: str | None = None

    if abstain >= 2:
        outcome = "HUNG"
    elif approve == reject:
        outcome = "HUNG"
    elif approve > 0 and reject == 0 and abstain == 0:
        outcome = "UNANIMOUS_APPROVE"
        majority = "APPROVE"
    elif reject > 0 and approve == 0 and abstain == 0:
        outcome = "UNANIMOUS_REJECT"
        majority = "REJECT"
    else:
        outcome = "SPLIT"
        majority = "APPROVE" if approve > reject else "REJECT"

    dissent: List[Dict[str, str]] = []
    if outcome == "SPLIT":
        for name, v in verdicts.items():
            if v["verdict"] != majority:
                dissent.append({"juror": name, "verdict": v["verdict"], "reason": v["reason"]})
    elif outcome == "HUNG":
        dissent = []

    abstained = [name for name, v in verdicts.items() if v["verdict"] == "ABSTAIN"]

    return {
        "outcome": outcome,
        "approve": approve,
        "reject": reject,
        "abstain": abstain,
        "majority": majority,
        "dissent": dissent,
        "abstained": abstained,
    }

def format_tally(t: Dict[str, Any]) -> str:
    """Render a tally dict as a message suitable for posting in a room.

    The layout follows the exact specification:

    1. ``TALLY: <outcome>``
    2. ``APPROVE <n> / REJECT <n> / ABSTAIN <n>``
    3. One ``DISSENT <juror>: <verdict> - <reason>`` line per dissenter.
    4. Optional ``ABSTAINED: <comma‑separated names>`` line when any abstain.
    5. ``JSON: <machine‑readable dict>`` line – the dict contains the same data.
    6. ``HUMAN DECIDES: reply "merge" or "reject"`` line.

    Args:
        t: The tally dict returned by :func:`tally`.

    Returns:
        A single string ready to be posted.
    """
    lines = [
        f"TALLY: {t['outcome']}",
        f"APPROVE {t['approve']} / REJECT {t['reject']} / ABSTAIN {t['abstain']}",
    ]

    for d in t["dissent"]:
        lines.append(f"DISSENT {d['juror']}: {d['verdict']} - {d['reason']}")

    if t["abstained"]:
        lines.append(f"ABSTAINED: {', '.join(t['abstained'])}")

    json_payload = {
        "outcome": t["outcome"],
        "counts": {"approve": t["approve"], "reject": t["reject"], "abstain": t["abstain"]},
        "majority": t["majority"],
        "dissent": [
            {"juror": d["juror"], "verdict": d["verdict"], "reason": d["reason"]}
            for d in t["dissent"]
        ],
        "abstained": t["abstained"],
    }
    lines.append(f"JSON: {json.dumps(json_payload)}")
    lines.append('HUMAN DECIDES: reply "merge" or "reject"')
    return "\n".join(lines)

def run_foreperson(
    client: Any,
    room_id: str,
    juror_names: List[str],
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """Coordinate a deliberation round.

    The function:

    1. Opens an SSE stream for *room_id*.
    2. Claims the room via ``connect_to_room``.
    3. Reads room history via ``read_context`` to discover any VERDICT messages
       already posted.
    4. Waits for the first message starting with ``VERDICT`` from each
       expected juror (identified by *sender_name* in the event payload).
       Missing jurors at the timeout are treated as ``ABSTAIN``.
    5. Parses the verdicts, tallies them, posts the formatted tally,
       and returns the tally dict.

    Heartbeats are sent whenever the event poll times out.

    Args:
        client: An instance of :class:`agents.switch_client.SwitchClient`.
        room_id: The room to join.
        juror_names: List of expected juror participant names.
        timeout: Overall deadline in seconds.

    Returns:
        The tally dictionary produced by :func:`tally`.
    """
    client.open_stream([room_id])
    client.connect_to_room(room_id)

    # Read room history to catch up on VERDICTs posted before we connected
    context = client.call_op("read_context", {"limit": 50}, with_connection=True)
    seen_keys = set()
    verdicts: Dict[str, Dict[str, str]] = {}

    if "threads" in context:
        for thread in context["threads"]:
            # Check root message
            root = thread.get("root", {})
            if root.get("kind") == "message":
                body = str(root.get("body", ""))
                sender = root.get("sender_name", "")
                key = (sender, body)
                if sender in juror_names and body.upper().startswith("VERDICT") and key not in seen_keys:
                    seen_keys.add(key)
                    if sender not in verdicts:
                        verdicts[sender] = parse_verdict(body)
            # Check replies
            for reply in thread.get("replies", []):
                if reply.get("kind") == "message":
                    body = str(reply.get("body", ""))
                    sender = reply.get("sender_name", "")
                    key = (sender, body)
                    if sender in juror_names and body.upper().startswith("VERDICT") and key not in seen_keys:
                        seen_keys.add(key)
                        if sender not in verdicts:
                            verdicts[sender] = parse_verdict(body)

    pending = {name for name in juror_names if name not in verdicts}
    deadline = time.time() + timeout

    while pending and time.time() < deadline:
        event = client.next_event(timeout=1.0)
        if event is None:
            client.beat()
            continue

        if event.get("type") != "message":
            continue

        payload = event.get("payload", {})
        body = str(payload.get("body", ""))
        sender = payload.get("sender_name", "")

        if sender not in pending:
            continue

        key = (sender, body)
        if body.upper().startswith("VERDICT") and key not in seen_keys:
            seen_keys.add(key)
            verdicts[sender] = parse_verdict(body)
            pending.remove(sender)

    # Treat missing jurors as ABSTAIN
    for name in juror_names:
        if name not in verdicts:
            verdicts[name] = parse_verdict("")

    tally_dict = tally(verdicts)
    client.post_message(room_id, format_tally(tally_dict))
    return tally_dict
