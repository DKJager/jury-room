"""Switch agent client for communicating with the Switch 0.31.0 agent bridge.

Implements the Switch agent protocol (SSE down, HTTP up) against a real
Switch stack at a given base URL.
"""

import json
import threading
import urllib.request
import queue
import uuid
import time
import urllib.error
import os


RETRY_BUDGET_S = 15.0  # [#2475] hard wall-clock cap on 5xx retries when opening the stream
UPSTREAM_EVIDENCE = os.environ.get("JURY_UPSTREAM_EVIDENCE", "/opt/jury-room/upstream_evidence.jsonl")


class RetryTimeout(RuntimeError):
    """The event stream kept answering 5xx for longer than RETRY_BUDGET_S."""


def _record_upstream_evidence(row: dict) -> None:
    """[#2475] Append one absorbed-5xx row for the Switch maintainers; never raises, never logs secrets."""
    import sys
    sys.stderr.write(f"WARN switch_client absorbed {row.get('status')} on {row.get('route')} attempt {row.get('attempt')}: {row.get('body', '')[:200]}\n")
    try:
        with open(UPSTREAM_EVIDENCE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass

class SwitchClient:
    """Client for a custom Switch agent using the real agent protocol.

    Handles registration, operations, rooms, SSE streaming, heartbeats,
    and message posting against a live Switch bridge.
    """

    def __init__(self, base_url: str, api_key: str, agent_id: str) -> None:
        """Initialise the client.

        Args:
            base_url: Root URL of the Switch stack (e.g. http://127.0.0.1:8000).
            api_key: Agent API key for Bearer authentication.
            agent_id: Registered agent UUID.

        Sets self.connection_id to a new UUID4 string reused for every stream
        reopen.
        """
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._agent_id = agent_id
        self.connection_id = str(uuid.uuid4())
        self._event_queue: queue.Queue[dict] = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self._response: urllib.request.addinfourl | None = None
        self._cursor: int | None = None
        self._lock = threading.Lock()
        self.absorbed_retries_total = 0  # [#2475] 5xx stream opens absorbed by the retry
        self.reordered_events = 0  # [#2475] domain events whose sequence was not above the last one seen
        self._closed = False
        self._stream_closed = False

    @staticmethod
    def register(
        base_url: str, registration_token: str, name: str, description: str
    ) -> tuple[str, str]:
        """Register a custom agent and return (agent_id, api_key).

        POST {base_url}/agents with the registration token as Bearer and a
        JSON body describing the agent.  Raises RuntimeError with the HTTP
        status code on non-200.  Never logs or prints keys or tokens.
        """
        body = {
            "name": name,
            "description": description,
            "connector_type": "custom",
            "integration_profile": {
                "connection_model": "always_on",
                "message_exchange": True,
                "pre_invocation_mediation": [],
                "post_invocation_mediation": [],
                "event_reporting": [],
            },
            "overwrite": True,
        }
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/agents",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {registration_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["id"], data["api_key"]
        except urllib.error.HTTPError as e:
            raise RuntimeError(e.code) from e

    def _do_request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        extra_headers: dict | None = None,
    ) -> dict:
        """Internal helper to make an HTTP request and return parsed JSON."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(
            f"{self._base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            status = e.code
            excerpt = e.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(status, excerpt) from e

    def list_operations(self) -> dict:
        """Return the operations mapping for this agent.

        GET {base_url}/agents/{id}/ops -> dict keyed by operation name.
        """
        data = self._do_request("GET", f"/agents/{self._agent_id}/ops")
        return data.get("operations", {})

    def call_op(
        self, name: str, args: dict, with_connection: bool = False
    ) -> object:
        """Invoke a named operation and return its result.

        POST {base_url}/agents/{id}/ops/{name} with the unwrapped arguments
        as JSON body.  If *with_connection* is True the X-Switch-Connection-Id
        header is included.  Raises RuntimeError(status, body-excerpt) on
        non-200.
        """
        body = json.dumps(args).encode("utf-8")
        extra_headers: dict = {}
        if with_connection:
            extra_headers["X-Switch-Connection-Id"] = self.connection_id
        data = self._do_request(
            "POST",
            f"/agents/{self._agent_id}/ops/{name}",
            body=body,
            extra_headers=extra_headers,
        )
        return data.get("result")

    def create_room(self, name: str, description: str, agent_names: list[str]) -> str:
        """Create a room and return its id.

        Calls the ``create_room`` operation with the given name, description,
        and list of agent names.
        """
        result = self.call_op(
            "create_room",
            {"name": name, "description": description, "agent_names": agent_names},
        )
        if isinstance(result, dict):
            return result.get("id", result.get("room_id", ""))
        return str(result)

    def open_stream(self, rooms: list[str]) -> dict:
        """Open the SSE event stream for the given rooms.

        Starts a daemon thread that reads ``text/event-stream`` frames
        into an internal queue.Queue.  Blocks until the first frame (the
        ``connection_state`` control frame) is received, then returns its
        parsed data dict.  Domain events are queued as their parsed JSON
        payload (with keys ``type``, ``room_id``, ``payload``, ``sequence``);
        subsequent control frames are queued as ``{"control": <event>,
        **data}``.  Tracks the highest ``sequence`` as the cursor.

        Raises TimeoutError if no first frame arrives within 15 seconds.
        """
        if self._closed:
            raise RuntimeError("client is closed")
        if self._response is not None or self._reader_thread is not None:
            self._close_stream_only()
        self._rooms = list(rooms)  # [reopen] remembered so a dead connection can be reopened on the same rooms

        rooms_param = ",".join(rooms)
        url_params = f"?connection_id={self.connection_id}&scope=single&filter=all&rooms={rooms_param}"
        with self._lock:
            if self._cursor is not None:
                url_params += f"&start_from={self._cursor}"
            else:
                url_params += "&start_from=head"

        url = f"{self._base_url}/agents/{self._agent_id}/events{url_params}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "text/event-stream",
            },
            method="GET",
        )

        self._stream_closed = False
        # Switch 0.31.0 can answer 500 ("No incarnation drawn: a connection is opened inside a recorded move") when
        # several agents open streams at the same instant. Transient server race: retry 5xx with backoff, reusing the
        # same connection id, inside a hard 15 s wall-clock budget [#2475]; every absorbed 5xx is logged as upstream
        # evidence (WARN on stderr + one JSON line in UPSTREAM_EVIDENCE).
        _t0 = time.time()
        _attempt = 0
        while True:
            try:
                self._response = urllib.request.urlopen(req, timeout=60)
                break
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    raise RuntimeError(e.code) from e
                _body = ""
                try:
                    _body = e.read().decode("utf-8", "replace")[:600]
                except Exception:
                    pass
                _wait = 0.5 * (2 ** _attempt)
                if time.time() - _t0 + _wait > RETRY_BUDGET_S:
                    raise RetryTimeout(f"Switch event stream not healthy after {RETRY_BUDGET_S:.0f}s (last {e.code})") from e
                self.absorbed_retries_total += 1
                _record_upstream_evidence({"ts": time.time(), "route": "GET /agents/{id}/events", "status": e.code,
                                           "attempt": _attempt + 1, "body": _body})
                time.sleep(_wait)
                _attempt += 1

        self._reader_thread = threading.Thread(target=self._read_stream, daemon=True)
        self._reader_thread.start()
        # [#2475 follow-up] protocol: 'Beat every 2 seconds. The TTL is 6 seconds.' A busy room never lets
        # next_event() return None, so beating only when idle lets the connection lapse. Beat on a timer.
        if getattr(self, '_hb_thread', None) is None or not self._hb_thread.is_alive():
            self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._hb_thread.start()

        deadline = time.time() + 15.0
        first_frame: dict | None = None
        _kept: list[dict] = []  # [reopen] real events already queued before this (re)open are kept, in order
        while time.time() < deadline:
            try:
                _item = self._event_queue.get(timeout=0.5)
            except queue.Empty:
                if not self._reader_thread.is_alive():
                    raise RuntimeError("reader thread died before first frame")
                continue
            if _item.get("control") == "stream_closed":
                continue  # stale marker from the previous stream
            if "type" in _item and "connection_id" not in _item:
                _kept.append(_item)  # a domain event from the previous stream
                continue
            first_frame = _item
            break
        if _kept:
            _rest = []
            while True:
                try:
                    _rest.append(self._event_queue.get_nowait())
                except queue.Empty:
                    break
            for _item in _kept + _rest:
                self._event_queue.put(_item)

        if first_frame is None:
            self._close_stream_only()
            raise TimeoutError("no first frame within 15 seconds")

        if "control" in first_frame:
            if first_frame.get("control") != "connection_state":
                self._close_stream_only()
                raise RuntimeError(
                    f"expected connection_state, got {first_frame.get('control')}"
                )
            state_data = {k: v for k, v in first_frame.items() if k != "control"}
        else:
            state_data = first_frame

        if "cursor" in state_data:
            with self._lock:
                self._cursor = state_data["cursor"]
        return state_data

    def _close_stream_only(self) -> None:
        """Close just the SSE stream and reader thread, not the whole client."""
        if self._response:
            try:
                self._response.close()
            except Exception:
                pass
            self._response = None
        self._stream_closed = True
        self._reader_thread = None

    def _read_stream(self) -> None:
        """Background thread SSE reader."""
        self._stream_closed = False
        buffer = ""
        try:
            while (
                not self._closed
                and not self._stream_closed
                and self._response is not None
            ):
                try:
                    line_bytes = self._response.readline()
                    if not line_bytes:
                        break
                    line = line_bytes.decode("utf-8", errors="replace")
                    if line.startswith(":"):
                        continue
                    buffer += line
                except (urllib.error.HTTPError, ConnectionError, OSError):
                    break

                if buffer.endswith("\n\n"):
                    raw_frame = buffer.rstrip("\n\n")
                    buffer = ""
                    self._parse_frame(raw_frame)
        except Exception:
            if not self._closed:
                self._event_queue.put({"control": "stream_closed"})
        finally:
            self._close_stream_only()

    def _parse_frame(self, raw: str) -> None:
        """Parse a single SSE frame and enqueue it."""
        lines = raw.split("\n")
        event_type: str | None = None
        data_json: str | None = None
        seq_str: str | None = None

        for line in lines:
            line = line.rstrip()
            if line.startswith("id:"):
                seq_str = line[3:].strip()
                continue
            if line.startswith("event:"):
                event_type = line[6:].strip()
                continue
            if line.startswith("data:"):
                data_part = line[5:].strip()
                data_json = (
                    (data_json + "\n" + data_part) if data_json is not None else data_part
                )
                continue
            if line.startswith(":"):
                continue

        if data_json is None:
            return

        try:
            data = json.loads(data_json)
        except json.JSONDecodeError:
            return

        domain_events = {
            "message",
            "command",
            "room_join",
            "task_delegate",
            "task_accept",
            "task_update",
            "task_finalise",
            "task_cancel",
        }

        if seq_str is not None:
            try:
                seq = int(seq_str)
                with self._lock:
                    if self._cursor is not None and seq <= self._cursor and self._cursor > 0:
                        self.reordered_events += 1  # [#2475] sequence validation
                    if self._cursor is None or seq > self._cursor:
                        self._cursor = seq
            except ValueError:
                pass

        if event_type in domain_events:
            data.setdefault("type", event_type)
            data.setdefault("room_id", "<unknown>")
            data.setdefault("payload", {})
            if "sequence" not in data and seq_str is not None:
                try:
                    data["sequence"] = int(seq_str)
                except ValueError:
                    pass
            self._event_queue.put(data)
        else:
            if event_type == "connection_state":
                self._event_queue.put(data)
            elif event_type is not None:
                item = {"control": event_type}
                if isinstance(data, dict):
                    item.update(data)
                self._event_queue.put(item)

    def next_event(self, timeout: float = 1.0) -> dict | None:
        """Return the next queued event or None if *timeout* expires.

        Args:
            timeout: Maximum time in seconds to wait for an event.
        """
        try:
            return self._event_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def connect_to_room(self, room_id: str) -> dict:
        """Call the ``connect_to_room`` operation and return its result.

        Sends the connection id header so the server knows which SSE
        connection to associate the room with.
        """
        try:
            result = self.call_op(
                "connect_to_room",
                {"room_id": room_id, "include_general_instructions": False},
                with_connection=True,
            )
        except RuntimeError as e:
            if isinstance(e.args[0], int) and e.args[0] == 409:
                if not self._stream_closed:
                    self.open_stream([room_id])
                    result = self.call_op(
                        "connect_to_room",
                        {"room_id": room_id, "include_general_instructions": False},
                        with_connection=True,
                    )
                else:
                    raise
            else:
                raise
        if isinstance(result, dict):
            return result
        return {"room_id": room_id}

    def _heartbeat_loop(self) -> None:
        """Background heartbeat every 2 s while the stream is open; stops on close() or a dead connection."""
        while not self._closed:
            time.sleep(2.0)
            if self._closed:
                break
            try:
                self.beat()
            except Exception:
                continue  # transient (5xx, timeout) or a failed reopen: keep beating, beat() reopens on 404/409

    def beat(self) -> dict:
        """Send a heartbeat and return the parsed JSON response.

        POST {base_url}/agents/{id}/connection/beat with the current
        ``connection_id`` and, once a sequence has been seen, the ``cursor``.
        Raises RuntimeError on non-200.
        """
        payload: dict = {"connection_id": self.connection_id}
        with self._lock:
            if self._cursor is not None:
                payload["cursor"] = self._cursor
        body = json.dumps(payload).encode("utf-8")
        try:
            resp = self._do_request(
                "POST",
                f"/agents/{self._agent_id}/connection/beat",
                body=body,
            )
            if "cursor" in resp:
                with self._lock:
                    self._cursor = resp["cursor"]
            return resp
        except RuntimeError as e:
            if isinstance(e.args[0], int) and e.args[0] in (404, 409):
                # docs/upstream/agent-protocol.md "The heartbeat": both errors mean reopen, not back off.
                if self._reopen():
                    return {"ok": True, "reopened": True}
                raise
            raise

    def _reopen(self) -> bool:
        """Reopen the stream with the same connection id and cursor, then re-claim the rooms. True on success."""
        if self._closed or getattr(self, "_reopening", False) or not getattr(self, "_rooms", None):
            return False
        self._reopening = True
        try:
            self.reopens_total = getattr(self, "reopens_total", 0) + 1
            _record_upstream_evidence({"ts": time.time(), "route": "POST /agents/{id}/connection/beat", "status": "404/409",
                                       "attempt": self.reopens_total, "body": "connection not open: reopened stream"})
            self.open_stream(self._rooms)
            for room in self._rooms:
                try:
                    self.connect_to_room(room)
                except Exception:
                    pass
            return True
        except Exception:
            return False
        finally:
            self._reopening = False

    def post_message(self, room_id: str, content: str) -> str:
        """Post a message to a room and return the resulting event_id.

        POST {base_url}/agents/{id}/message with ``room_id`` and ``content``
        in the JSON body.
        """
        body = json.dumps({"room_id": room_id, "content": content}).encode("utf-8")
        data = self._do_request(
            "POST",
            f"/agents/{self._agent_id}/message",
            body=body,
        )
        return data.get("event_id", "")

    def close(self) -> None:
        """Stop the SSE reader thread and close the response.

        Safe to call multiple times and safe to call before ``open_stream``.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True

        self._close_stream_only()

        while not self._event_queue.empty():
            try:
                self._event_queue.get_nowait()
            except queue.Empty:
                break
