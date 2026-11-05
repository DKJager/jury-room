"""Juror agent for jury room: review case via model, post verdict.

This module provides the logic for a juror agent that reads a case,
calls a model, parses the response, and posts a structured verdict.
"""
import os
import json
import time
from typing import Callable, Dict, Any
import urllib.request
import urllib.error

from agents import foreperson

def build_prompt(case_text: str) -> str:
    """Build the review prompt for the model.

    Args:
        case_text: The case text as included in the room message.

    Returns:
        Prompt string instructing reviewer to output three lines:
        VERDICT, REASON, RISK.
    """
    return (
        "You are a code reviewer. Judge the patch described in the case below.\n"
        "Output exactly three lines:\n"
        "VERDICT: APPROVE or REJECT\n"
        "REASON: <one sentence>\n"
        "RISK: low, medium, or high\n\n"
        "Case:\n"
        f"{case_text}\n"
    )

def format_verdict(v: Dict[str, str]) -> str:
    """Format a parsed verdict dict into the required three‑line string.

    Args:
        v: Dict from ``foreperson.parse_verdict`` (keys ``verdict``, ``reason``,
           ``risk``).

    Returns:
        A string of the form ``VERDICT: …\\nREASON: …\\nRISK: …``.
    """
    verdict = v.get("verdict", "ABSTAIN")
    reason = v.get("reason", "")
    risk = v.get("risk", "")
    return f"VERDICT: {verdict}\nREASON: {reason}\nRISK: {risk}"

def run_juror(
    client: Any,
    room_id: str,
    model_call: Callable[[str], str],
    timeout: float = 45.0,
) -> str:
    """Run juror logic in a Switch room.

    The function opens a stream, connects to the room, reads room history via
    read_context, waits for a ``CASE:`` message that comes from any other agent,
    builds a prompt, calls the supplied ``model_call`` once, parses the model
    reply, posts a formatted verdict (or ``ABSTAIN`` when the reply cannot be
    parsed), and returns the posted text.

    Args:
        client: An instance of ``SwitchClient``.
        room_id: Identifier of the Switch room.
        model_call: Callable that accepts a prompt string and returns a reply.
        timeout: Seconds to wait for the ``CASE:`` message before raising.

    Returns:
        The exact text that was posted to the room.

    Raises:
        TimeoutError: If no ``CASE:`` message arrives within ``timeout``.
    """
    client.open_stream([room_id])
    connect_result = client.connect_to_room(room_id)

    # Read room history to catch up with any CASE already posted.
    try:
        history = client.call_op("read_context", {"limit": 50}, with_connection=True)
    except Exception:
        history = {"threads": []}

    case_body = None
    seen_messages = set()

    # Scan history for CASE messages.
    for thread in history.get("threads", []):
        root = thread.get("root", {})
        if root.get("kind") == "message":
            body = str(root.get("body", ""))
            sender = str(root.get("sender_name", ""))
            if body.startswith("CASE:"):
                case_body = body
                break
        replies = thread.get("replies", [])
        for reply in replies:
            if reply.get("kind") == "message":
                body = str(reply.get("body", ""))
                sender = str(reply.get("sender_name", ""))
                if body.startswith("CASE:"):
                    case_body = body
                    break
        if case_body:
            break

    deadline = time.time() + timeout
    while case_body is None and time.time() < deadline:
        ev = client.next_event(timeout=2.0)
        if ev is None:
            client.beat()
            continue

        if ev.get("type") != "message":
            continue

        payload = ev.get("payload", {})
        body = str(payload.get("body", ""))
        sender_name = str(payload.get("sender_name", ""))

        if body.startswith("CASE:"):
            # Do NOT filter out own_name; accept CASE from any sender.
            case_body = body
            break

    if case_body is None:
        raise TimeoutError("No CASE message received within timeout")

    prompt = build_prompt(case_body)
    reply = model_call(prompt)

    parsed = foreperson.parse_verdict(reply)
    verdict_text = format_verdict(parsed)

    client.post_message(room_id, verdict_text)

    return verdict_text

def nebius_caller(model: str, timeout: float = 60.0) -> Callable[[str], str]:
    """Create a callable that invokes the Nebius OpenAI‑compatible API.

    The returned function takes a prompt string, performs a POST request to
    ``https://api.studio.nebius.com/v1/chat/completions`` using the provided
    model name, and returns the model's reply text.

    Args:
        model: Name of the model to invoke.
        timeout: HTTP request timeout in seconds.

    Returns:
        Callable that accepts a prompt and returns the model reply.

    Raises:
        RuntimeError: If the ``NEBIUS_API_KEY`` environment variable is not set
            at the time the callable is invoked.
    """
    def caller(prompt: str) -> str:
        api_key = os.environ.get("NEBIUS_API_KEY")
        if api_key is None:
            raise RuntimeError("NEBIUS_API_KEY environment variable not set")

        request_body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 200,
        }

        req = urllib.request.Request(
            "https://api.studio.nebius.com/v1/chat/completions",
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Nebius API error: {e.code}") from e

    return caller
