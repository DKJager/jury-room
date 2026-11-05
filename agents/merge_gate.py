import os
import subprocess
import time
import hashlib
from typing import Any


def _extract_diff(case_body: str) -> str:
    """Return the diff text that lies between a line containing exactly ``DIFF:``
    and the first line that starts with ``TESTS:``.  If the delimiters are not
    found an empty string is returned.
    """
    lines = case_body.splitlines(keepends=True)
    diff_start = None
    for i, line in enumerate(lines):
        if line.rstrip() == "DIFF:":
            diff_start = i + 1
            break
    if diff_start is None:
        return ""
    diff_lines = []
    for line in lines[diff_start:]:
        if line.startswith("TESTS:"):
            break
        diff_lines.append(line)
    return "".join(diff_lines)


def _first_line_of_case(case_body: str) -> str:
    """Return the text after ``CASE:`` on the first line of the message."""
    first = case_body.splitlines()[0] if case_body else ""
    if first.startswith("CASE:"):
        return first[5:].strip()
    return first.strip()


def run_merge_gate(
    client: Any,
    room_id: str,
    repo_dir: str,
    human_names: list[str],
    timeout: float = 180.0,
) -> dict[str, Any]:
    """
    Gate diff application on a human merge/reject decision.

    Reads room history for the latest CASE message's diff (text between
    ``DIFF:`` and the line starting ``TESTS:``). Waits for a TALLY message.
    After tally, the first message whose stripped+lowercased body is exactly
    ``"merge"`` or ``"reject"`` **and** whose ``sender_name`` is in
    ``JURY_MERGE_AUTHORIZED`` (comma‑separated env var, intersected with
    ``human_names``) decides.

    On ``"merge"``: creates branch ``jury/<8 hex>`` from ``main``, applies the
    diff, commits with author ``Jury Room <jury@3dogs.ai>`` and a message that
    contains the first line of the CASE, switches back to ``main`` and posts
    ``MERGED <hash> on jury/<8 hex> by <human>``.

    On ``"reject"``: posts ``REJECTED by <human>; nothing applied``.

    On timeout: posts ``NO DECISION; nothing applied``.

    On a failed apply: posts ``MERGE FAILED: <reason>; nothing applied`` and
    leaves the repository untouched.
    """
    try:
        client.open_stream([room_id])
    except Exception as exc:
        try:
            exc_type = type(exc).__name__
            client.post_message(room_id, f"MERGE FAILED: {exc_type}: {str(exc)[:200]}; nothing applied")
        except Exception:
            pass
        return {"decision": None, "decided_by": None, "commit": None, "branch": None}

    try:
        result: dict[str, Any] = {
            "decision": None,
            "decided_by": None,
            "commit": None,
            "branch": None,
        }

        client.connect_to_room(room_id)

        env_val = os.environ.get("JURY_MERGE_AUTHORIZED", "")
        authorized_env = {n.strip() for n in env_val.split(",") if n.strip()}
        authorized = {name for name in human_names if name in authorized_env}

        case_diff: str | None = None
        case_message_body: str | None = None
        tally_seen = False
        seen_messages: set[tuple[str, str]] = set()
        decision_made = False

        def handle_message(body: str, sender: str) -> None:
            nonlocal case_diff, case_message_body, tally_seen
            key = (sender, body.strip().lower())
            if key in seen_messages:
                return
            seen_messages.add(key)

            if body.startswith("CASE:"):
                case_message_body = body
                extracted = _extract_diff(body)
                case_diff = extracted if extracted else None
            elif body.startswith("TALLY:"):
                tally_seen = True

        def try_decision(body: str, sender: str) -> bool:
            nonlocal decision_made
            if decision_made:
                return True
            if not tally_seen or case_diff is None:
                return False
            stripped = body.strip().lower()
            if stripped not in ("merge", "reject"):
                return False
            if sender not in authorized:
                return False

            success = False
            try:
                if stripped == "reject":
                    result.update({
                        "decision": "reject",
                        "decided_by": sender,
                        "commit": None,
                        "branch": None,
                    })
                    client.post_message(room_id, f"REJECTED by {sender}; nothing applied")
                    success = True
                else:  # merge
                    branch_hex = hashlib.sha256(os.urandom(16) + str(time.time()).encode()).hexdigest()[:8]
                    branch_name = f"jury/{branch_hex}"
                    try:
                        subprocess.run(
                            ["git", "-C", repo_dir, "checkout", "main"],
                            capture_output=True, text=True, check=True
                        )
                        subprocess.run(
                            ["git", "-C", repo_dir, "branch", "-D", branch_name],
                            capture_output=True, text=True
                        )
                        subprocess.run(
                            ["git", "-C", repo_dir, "checkout", "-b", branch_name, "main"],
                            capture_output=True, text=True, check=True
                        )
                        # Validate the patch first
                        check_res = subprocess.run(
                            ["git", "-C", repo_dir, "apply", "--check", "--whitespace=nowarn"],
                            input=case_diff,
                            capture_output=True, text=True
                        )
                        if check_res.returncode != 0:
                            raise RuntimeError(check_res.stderr.strip() or "git apply --check failed")
                        # Apply the patch
                        apply_res = subprocess.run(
                            ["git", "-C", repo_dir, "apply", "--whitespace=nowarn"],
                            input=case_diff,
                            capture_output=True, text=True
                        )
                        if apply_res.returncode != 0:
                            raise RuntimeError(apply_res.stderr.strip() or "git apply failed")
                        # Commit
                        task_line = _first_line_of_case(case_message_body or "") if case_message_body else "fix"
                        commit_msg = f"jury-room: {task_line} (approved by {sender})"
                        subprocess.run(
                            ["git", "-C", repo_dir, "add", "-A"],
                            capture_output=True, text=True, check=True
                        )
                        subprocess.run(
                            [
                                "git", "-C", repo_dir,
                                "-c", "user.email=jury@3dogs.ai",
                                "-c", "user.name=Jury Room",
                                "commit", "-m", commit_msg
                            ],
                            capture_output=True, text=True, check=True
                        )
                        # Get commit hash
                        hash_res = subprocess.run(
                            ["git", "-C", repo_dir, "rev-parse", f"refs/heads/{branch_name}"],
                            capture_output=True, text=True
                        )
                        commit_hash = hash_res.stdout.strip() if hash_res.returncode == 0 else None
                        # Return to main
                        subprocess.run(
                            ["git", "-C", repo_dir, "checkout", "main"],
                            capture_output=True, text=True, check=True
                        )
                        result.update({
                            "decision": "merge",
                            "decided_by": sender,
                            "commit": commit_hash,
                            "branch": branch_name,
                        })
                        client.post_message(room_id, f"MERGED {commit_hash} on {branch_name} by {sender}")
                        success = True
                    except Exception as e:
                        # Cleanup on failure
                        subprocess.run(
                            ["git", "-C", repo_dir, "checkout", "main"],
                            capture_output=True, text=True
                        )
                        subprocess.run(
                            ["git", "-C", repo_dir, "branch", "-D", branch_name],
                            capture_output=True, text=True
                        )
                        exc_type = type(e).__name__
                        msg = str(e)[:200]
                        client.post_message(room_id, f"MERGE FAILED: {exc_type}: {msg}; nothing applied")
                        success = True
            except Exception as e:
                exc_type = type(e).__name__
                msg = str(e)[:200]
                try:
                    client.post_message(room_id, f"MERGE FAILED: {exc_type}: {msg}; nothing applied")
                except Exception:
                    pass
                success = True
            finally:
                if success:
                    decision_made = True
            return success

        # Scan existing history
        try:
            raw_history = client.call_op("read_context", {"limit": 50}, with_connection=True)
        except Exception:
            raw_history = {"threads": []}

        for thread in raw_history.get("threads", []):
            root = thread.get("root", {})
            if root.get("kind") == "message":
                handle_message(str(root.get("body", "")), str(root.get("sender_name", "")))
            for reply in thread.get("replies", []):
                if reply.get("kind") == "message":
                    handle_message(str(reply.get("body", "")), str(reply.get("sender_name", "")))

        # Check if decision already possible from history
        for thread in raw_history.get("threads", []):
            root = thread.get("root", {})
            if root.get("kind") == "message":
                if try_decision(str(root.get("body", "")), str(root.get("sender_name", ""))):
                    return result
            for reply in thread.get("replies", []):
                if reply.get("kind") == "message":
                    if try_decision(str(reply.get("body", "")), str(reply.get("sender_name", ""))):
                        return result

        deadline = time.time() + timeout

        # Wait until we have both CASE and TALLY
        while time.time() < deadline and not (case_diff and tally_seen):
            ev = client.next_event(timeout=2.0)
            if ev is None:
                continue
            if ev.get("type") != "message":
                continue
            payload = ev.get("payload", {})
            body = str(payload.get("body", ""))
            sender = str(payload.get("sender_name", ""))
            handle_message(body, sender)

        # If we never got the necessary info, fall through to NO DECISION
        if not (case_diff and tally_seen):
            client.post_message(room_id, "NO DECISION; nothing applied")
            return result

        # Wait for a decision from an authorized human
        while time.time() < deadline and not decision_made:
            ev = client.next_event(timeout=2.0)
            if ev is None:
                continue
            if ev.get("type") != "message":
                continue
            payload = ev.get("payload", {})
            body = str(payload.get("body", ""))
            sender = str(payload.get("sender_name", ""))
            if not body or not sender:
                continue
            try_decision(body, sender)

        if not decision_made:
            client.post_message(room_id, "NO DECISION; nothing applied")
        return result
    except Exception as exc:
        try:
            exc_type = type(exc).__name__
            msg = str(exc)[:200]
            client.post_message(room_id, f"MERGE FAILED: {exc_type}: {msg}; nothing applied")
        except Exception:
            pass
        return {"decision": None, "decided_by": None, "commit": None, "branch": None}
    finally:
        try:
            client.close()
        except Exception:
            pass
