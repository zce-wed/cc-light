import importlib.util
import json
import sqlite3
import subprocess
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cc_light as light
import codex_wait as wait


def event(kind, **fields):
    return {"type": "event_msg", "payload": {"type": kind, **fields}}


def response(kind, **fields):
    return {"type": "response_item", "payload": {"type": kind, **fields}}


def test_async_question_remains_red_after_acknowledgement_and_completion():
    cache = {}
    for e in [event("task_started"), response("function_call", name="request_user_input_async", call_id="q"),
              response("function_call_output", call_id="q", output='{"accepted":true}'),
              response("function_call_output", call_id="unrelated", output="done"), event("task_complete")]:
        wait.update_rollout(e, cache)
    assert cache["state"] == "red"
    wait.update_rollout(event("item_completed", item={"type": "UserMessage"}), cache)
    assert cache["state"] == "yellow"
    wait.update_rollout(event("task_complete"), cache)
    assert cache["state"] == "green"


def test_multiple_blocking_questions_and_abort():
    cache = {}
    for call in ("q1", "q2"):
        wait.update_rollout(response("function_call", name="request_user_input", call_id=call), cache)
    wait.update_rollout(response("function_call_output", call_id="q1", output='{"answers":{}}'), cache)
    assert cache["state"] == "red"
    wait.update_rollout(response("function_call_output", call_id="q2", output='{"answers":{}}'), cache)
    assert cache["state"] == "yellow"
    wait.update_rollout(response("function_call", name="request_user_input_async", call_id="q3"), cache)
    wait.update_rollout(event("turn_aborted"), cache)
    assert cache["state"] == "green"


def test_plain_question_text_does_not_trigger_red():
    cache = {}
    wait.update_rollout(response("message", role="assistant", content=[{"text": "Continue?"}]), cache)
    wait.update_rollout(event("task_complete"), cache)
    assert cache["state"] == "green"


def test_incremental_rollout_reads_question_and_answer(tmp_path):
    path = tmp_path / "rollout.jsonl"
    records = [{"type": "session_meta", "payload": {"id": "test"}}, event("task_started"),
               response("function_call", name="request_user_input_async", call_id="q")]
    path.write_text("".join(json.dumps(e) + "\n" for e in records), encoding="utf-8")
    cache = {}
    light._codex_log_state(str(path), cache)
    assert cache["state"] == "red"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event("user_message")) + "\n")
    light._codex_log_state(str(path), cache)
    assert cache["state"] == "yellow"


def hook(tmp_path, kind, timestamp, command="A", sid="test"):
    wait.record_hook({"session_id": sid, "turn_id": "turn", "hook_event_name": kind,
                      "tool_name": "Bash", "tool_input": {"command": command}}, str(tmp_path), timestamp)


def test_permission_post_tool_restores_yellow_and_isolates_sessions(tmp_path):
    cache = {"state": "yellow"}
    hook(tmp_path, "PermissionRequest", 100)
    assert wait.permission_state("test", cache, tmp_path, tmp_path / "absent.db") == "red"
    assert wait.permission_state("other", {}, tmp_path, tmp_path / "absent.db") == "green"
    hook(tmp_path, "PostToolUse", 101, "unrelated")
    assert wait.permission_state("test", cache, tmp_path, tmp_path / "absent.db") == "red"
    hook(tmp_path, "PostToolUse", 102)
    assert wait.permission_state("test", cache, tmp_path, tmp_path / "absent.db") == "yellow"


def test_approval_submission_restores_yellow_before_command_finishes(tmp_path):
    database = tmp_path / "logs.sqlite"
    with sqlite3.connect(str(database)) as conn:
        conn.execute("CREATE TABLE logs (id INTEGER, ts INTEGER, ts_nanos INTEGER, feedback_log_body TEXT, thread_id TEXT, target TEXT)")
        conn.execute("INSERT INTO logs VALUES (1,101,0,?,?,?)", (
            'operation="exec_approval" op=ExecApproval { turn_id: Some("turn"), decision: approved }',
            'test', 'codex_core::session::handlers'))
    cache = {"state": "yellow"}
    hook(tmp_path, "PermissionRequest", 100)
    assert wait.permission_state("test", cache, tmp_path, database) == "yellow"
    hook(tmp_path, "PermissionRequest", 102, "B")
    assert wait.permission_state("test", cache, tmp_path, database) == "red"


def test_parallel_permissions_remain_red_until_all_answered(tmp_path):
    cache = {"state": "yellow"}
    hook(tmp_path, "PermissionRequest", 100)
    hook(tmp_path, "PermissionRequest", 100.5, "B")
    with patch.object(wait, "_approval_responses", return_value=[{"ts": 101, "kind": "answered", "turn": "turn"}]):
        assert wait.permission_state("test", cache, tmp_path, "unused") == "red"
        hook(tmp_path, "PostToolUse", 102)
        assert wait.permission_state("test", cache, tmp_path, "unused") == "red"
        hook(tmp_path, "PostToolUse", 103, "B")
        assert wait.permission_state("test", cache, tmp_path, "unused") == "yellow"


def test_new_turn_ignores_stale_permission_and_restart_replays_wait(tmp_path):
    hook(tmp_path, "PermissionRequest", 100)
    assert wait.permission_state("test", {"state": "yellow"}, tmp_path, "missing") == "red"
    assert wait.permission_state("test", {"state": "yellow"}, tmp_path, "missing") == "red"
    cache = {"state": "green", "boundary_ts": "1970-01-01T00:02:00Z"}
    assert wait.permission_state("test", cache, tmp_path, "missing") == "green"


def test_installer_preserves_other_hooks_and_is_idempotent(tmp_path):
    spec = importlib.util.spec_from_file_location("installer", Path(__file__).resolve().parents[1] / "install-codex-hooks.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    path = tmp_path / "hooks.json"
    original = {"hooks": {"PermissionRequest": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "existing"}]}]}}
    path.write_text(json.dumps(original), encoding="utf-8")
    assert installer.configure(path)
    assert not installer.configure(path)
    assert json.loads(path.read_text(encoding="utf-8"))["hooks"]["PermissionRequest"][0] == original["hooks"]["PermissionRequest"][0]
    installer.configure(path, install=False)
    assert json.loads(path.read_text(encoding="utf-8"))["hooks"]["PermissionRequest"] == original["hooks"]["PermissionRequest"]


def test_hook_process_handles_utf8_without_affecting_approval(tmp_path):
    payload = {"session_id": "test", "turn_id": "turn", "hook_event_name": "PermissionRequest",
               "tool_name": "Bash", "tool_input": {"command": "echo \u4e2d\u6587"}}
    result = subprocess.run([sys.executable, str(Path(wait.__file__))],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(os.environ, APPDATA=str(tmp_path)))
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr == b""
    assert wait.permission_state("test", {"state": "yellow"}, tmp_path / "cc-light", tmp_path / "missing") == "red"
