"""Passive Codex wait detection. Never returns an approval decision."""
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

QUESTION_TOOLS = {"request_user_input", "request_user_input_async"}


def update_rollout(event, cache):
    payload = event.get("payload", {})
    kind = payload.get("type")
    questions = cache.setdefault("questions", {})
    if event.get("type") == "event_msg":
        if kind in ("task_started", "user_message"):
            questions.clear()
            cache["state"] = "yellow"
            cache["boundary_ts"] = event.get("timestamp", "")
        elif kind in ("task_complete", "turn_aborted"):
            if kind == "turn_aborted":
                questions.clear()
            cache["state"] = "red" if questions else "green"
            cache["boundary_ts"] = event.get("timestamp", "")
        elif kind == "item_completed":
            item = payload.get("item", {})
            if item.get("type") == "UserMessage":
                questions.clear()
                cache["state"] = "yellow"
            elif item.get("questions") and item.get("delivery") == "async":
                questions[item.get("id")] = "async"
    elif event.get("type") == "response_item":
        if kind == "message" and payload.get("role") == "user":
            questions.clear()
            cache["state"] = "yellow"
        elif kind == "function_call" and payload.get("name", "").split(".")[-1] in QUESTION_TOOLS:
            questions[payload.get("call_id")] = payload["name"].split(".")[-1]
        elif kind == "function_call_output" and payload.get("call_id") in questions:
            output = payload.get("output", "")
            try:
                result = json.loads(output) if isinstance(output, str) else output
            except (ValueError, TypeError):
                result = None
            # Async {accepted:true} only acknowledges presentation, not an answer.
            accepted = isinstance(result, dict) and result.get("accepted") is True
            if not accepted:
                questions.pop(payload["call_id"], None)
                cache["state"] = "yellow"
    if questions:
        cache["state"] = "red"


def hook_directory(data_dir):
    return Path(data_dir) / "codex-permissions"


def record_hook(payload, data_dir, now=None):
    sid = payload.get("session_id", "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", sid):
        return
    kind = payload.get("hook_event_name")
    if kind not in {"PermissionRequest", "PostToolUse", "UserPromptSubmit", "Stop", "Interrupt", "SessionEnd"}:
        return
    signature = hashlib.sha256(json.dumps(
        [payload.get("tool_name"), payload.get("tool_input")],
        sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    record = {"ts": time.time() if now is None else now, "kind": kind,
              "turn": payload.get("turn_id"), "signature": signature}
    directory = hook_directory(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    # A single append avoids read/modify/write races between parallel tools.
    with (directory / (sid + ".jsonl")).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")


def _approval_responses(database, sid, since):
    """Read actual human approval submissions, including long-running commands."""
    try:
        uri = Path(database).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.1) as connection:
            rows = connection.execute(
                "SELECT ts, ts_nanos, feedback_log_body FROM logs "
                "WHERE thread_id=? AND ts>=? AND target='codex_core::session::handlers' "
                "AND (feedback_log_body LIKE '%operation=\"exec_approval\"%' "
                "OR feedback_log_body LIKE '%operation=\"patch_approval\"%' "
                "OR feedback_log_body LIKE '%operation=\"permissions_approval\"%') "
                "ORDER BY ts, ts_nanos, id", (sid, int(since))).fetchall()
        return [{"ts": ts + nanos / 1e9, "kind": "answered",
                 "turn": (re.search(r'turn_id: Some\("([^"]+)"\)', body) or [None, None])[1]}
                for ts, nanos, body in rows]
    except (OSError, ValueError, sqlite3.Error):
        return []


def permission_state(sid, cache, data_dir, database):
    path = hook_directory(data_dir) / (sid + ".jsonl")
    journal = cache.setdefault("permission_journal", {"offset": 0, "events": []})
    try:
        if path.stat().st_size < journal["offset"]:
            journal.update(offset=0, events=[])
        with path.open("rb") as stream:
            stream.seek(journal["offset"])
            while True:
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    break
                journal["offset"] = stream.tell()
                try:
                    record = json.loads(line)
                    if record["kind"] in ("UserPromptSubmit", "Stop", "Interrupt", "SessionEnd"):
                        journal["events"] = []
                    else:
                        journal["events"].append(record)
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        return cache.get("state", "green")
    events = journal["events"]
    requests = [e for e in events if e["kind"] == "PermissionRequest"]
    if not requests:
        return cache.get("state", "green")
    from datetime import datetime
    try:
        boundary = datetime.fromisoformat(cache.get("boundary_ts", "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        boundary = 0
    # A completed/aborted/new turn supersedes old permission markers.
    events = [e for e in events if e["ts"] > boundary]
    if not events:
        return cache.get("state", "green")
    responses = _approval_responses(database, sid, min(e["ts"] for e in events))
    pending = []
    for record in sorted(events + responses, key=lambda e: e["ts"]):
        if record["kind"] == "PermissionRequest":
            pending.append(dict(record, answered=False))
        elif record["kind"] == "answered":
            for request in pending:
                if not request["answered"] and (not record["turn"] or request["turn"] == record["turn"]):
                    request["answered"] = True
                    break
        elif record["kind"] == "PostToolUse":
            for request in pending:
                if request["signature"] == record["signature"]:
                    pending.remove(request)
                    break
    return "red" if any(not r["answered"] for r in pending) else cache.get("state", "green")


if __name__ == "__main__":
    try:
        record_hook(json.loads(sys.stdin.buffer.read().decode("utf-8-sig")), os.path.join(
            os.environ.get("APPDATA") or os.path.dirname(__file__), "cc-light"))
    except Exception:
        pass  # Monitoring must never interfere with the approval flow.
