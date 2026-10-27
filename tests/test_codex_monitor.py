import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cc_light as light


def _event(kind):
    return json.dumps({"type": "event_msg", "payload": {"type": kind}}) + "\n"


def test_codex_cli_command_matches_npm_node_launcher():
    assert light._is_codex_cli_command(
        r'C:\Program Files\nodejs\node.exe C:\Program Files\nodejs\node_modules\@openai\codex\bin\codex.js --no-daemon'
    ) is True
    assert light._is_codex_cli_command(
        r'C:\tools\node.exe C:\work\server\index.js'
    ) is False


def test_codex_exec_is_not_an_interactive_terminal_client():
    assert light._is_codex_exec_command(
        r'C:\Program Files\nodejs\node.exe C:\Program Files\nodejs\node_modules\@openai\codex\bin\codex.js exec --json -'
    ) is True
    assert light._is_codex_exec_command(
        r'C:\tools\codex.exe exec --json --dangerously-bypass-approvals-and-sandbox'
    ) is True
    assert light._is_codex_interactive_client(
        r'C:\tools\codex.exe resume 01a13916-63e4-7dc1-95d2-923612c71341'
    ) is True


def _check_codex_turns_and_process_lifetime(tmp_path):
    now = time.time()
    sid = "codex-test-session"
    folder = tmp_path / datetime.fromtimestamp(now).strftime("%Y/%m/%d")
    folder.mkdir(parents=True)
    path = folder / ("rollout-test-" + sid + ".jsonl")
    meta = {"type": "session_meta", "payload": {
        "id": sid, "timestamp": datetime.fromtimestamp(now - 5, timezone.utc).isoformat(),
        "cwd": "C:/work/sample-project"}}
    path.write_text(json.dumps(meta) + "\n" + _event("task_started"), encoding="utf-8")
    with patch.object(light, "CODEX_SESSIONS_DIR", str(tmp_path)), \
         patch.object(light, "_codex_process_starts", return_value={1234: now - 10}), \
         patch.object(light, "_codex_session_owners", return_value=None), \
         patch.object(light, "_codex_process_context", return_value={}), \
         patch.object(light, "_CODEX_CACHE", {"checked": 0, "sessions": {}, "logs": {}}):
        first = light.scan_codex_sessions(now)[sid]
        assert first["state"] == "yellow"
        assert first["name"] == "sample-project"
        assert light.effective_state(first, now + 1000) == "yellow"
        with patch.object(light, "SESSIONS_DIR", str(tmp_path)):
            assert light.read_sessions(now)[sid]["provider"] == "codex"

        with path.open("a", encoding="utf-8") as stream:
            stream.write(_event("task_complete"))
        os.utime(path, (now + 4, now + 4))
        assert light.scan_codex_sessions(now + 4)[sid]["state"] == "green"

        with patch.object(light, "_codex_process_starts", return_value={}):
            assert light.scan_codex_sessions(now + 8) == {}


def test_codex_turns_and_process_lifetime():
    with tempfile.TemporaryDirectory() as folder:
        _check_codex_turns_and_process_lifetime(Path(folder))


def test_resumed_daemon_threads_use_live_ownership():
    now = time.time()
    sid = "11111111-1111-1111-1111-111111111111"
    closed_sid = "22222222-2222-2222-2222-222222222222"
    cwd = os.path.normcase(os.path.normpath("C:/work/sample-project"))
    with tempfile.TemporaryDirectory() as folder:
        logs = Path(folder) / datetime.fromtimestamp(now).strftime("%Y/%m/%d")
        logs.mkdir(parents=True)
        for thread in (sid, closed_sid):
            path = logs / ("rollout-test-" + thread + ".jsonl")
            meta = {"type": "session_meta", "payload": {
                "id": thread, "timestamp": datetime.fromtimestamp(now - 86400, timezone.utc).isoformat(),
                "cwd": cwd}}
            path.write_text(json.dumps(meta) + "\n" + _event("task_started"), encoding="utf-8")
            os.utime(path, (now - 86400, now - 86400))
        processes = {100: now - 3600, 200: now - 600, 300: now - 30}
        contexts = {100: {"command": "codex.exe app-server --managed-daemon"},
                    200: {"cwd": cwd}, 300: {"cwd": cwd, "command": "codex.exe resume " + sid}}
        with patch.object(light, "CODEX_SESSIONS_DIR", folder), \
             patch.object(light, "_codex_process_starts", return_value=processes), \
             patch.object(light, "_codex_process_context", side_effect=contexts.get), \
             patch.object(light, "_codex_session_owners",
                          return_value={sid: {"pid": 100, "ts": now - 5}}) as owners, \
             patch.object(light, "_CODEX_CACHE", {"checked": 0, "sessions": {}, "logs": {}}):
            sessions = light.scan_codex_sessions(now)
            assert set(sessions) == {sid}
            assert sessions[sid]["state"] == "yellow"
            assert sessions[sid]["termpid"] == 300
            owners.return_value = {}
            assert light.scan_codex_sessions(now + 4) == {}


def test_shared_daemon_uses_lock_time_to_choose_terminal():
    sid = "11111111-1111-1111-1111-111111111111"
    cwd = os.path.normcase(os.path.normpath("C:/work/sample-project"))
    processes = {100: 1, 200: 2, 300: 3}
    contexts = {100: {"command": "codex.exe app-server"},
                200: {"cwd": cwd}, 300: {"cwd": cwd}}
    assert light._codex_terminal_pid(sid, {"pid": 100, "ts": 2.1}, cwd,
                                     processes, contexts) == 200
    contexts[200]["command"] = 'codex.exe resume "' + sid + '"'
    assert light._codex_terminal_pid(sid, {"pid": 100, "ts": 3}, cwd,
                                     processes, contexts) == 200


def test_background_exec_writer_never_binds_to_same_project_tui():
    contexts = {
        100: {"command": "codex.exe exec --json -", "cwd": "c:/work"},
        200: {"command": "codex.exe", "cwd": "c:/work"},
    }
    assert light._codex_terminal_pid("thread", {"pid": 100, "ts": 2.1},
                                     "c:/work", {100: 1, 200: 2}, contexts) == 0


def test_codex_exec_rollout_is_marked_non_jumpable():
    now = time.time()
    sid = "33333333-3333-3333-3333-333333333333"
    with tempfile.TemporaryDirectory() as folder:
        logs = Path(folder) / datetime.fromtimestamp(now).strftime("%Y/%m/%d")
        logs.mkdir(parents=True)
        path = logs / ("rollout-test-" + sid + ".jsonl")
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": sid, "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "cwd": "C:/work", "originator": "codex_exec"}}) + "\n" +
                         _event("task_started"), encoding="utf-8")
        with patch.object(light, "CODEX_SESSIONS_DIR", str(folder)), \
             patch.object(light, "_codex_process_starts", return_value={100: now - 10}), \
             patch.object(light, "_codex_process_context",
                          return_value={"command": "codex.exe exec --json -"}), \
             patch.object(light, "_codex_session_owners",
                          return_value={sid: {"pid": 100, "ts": now - 1}}), \
             patch.object(light, "_CODEX_CACHE", {"checked": 0, "sessions": {}, "logs": {}}):
            session = light.scan_codex_sessions(now)[sid]
            assert session["termpid"] == 0
            assert session["jumpable"] is False


def test_shared_daemon_binds_to_node_codex_cli_client():
    now = time.time()
    sid = "11111111-1111-1111-1111-111111111111"
    cwd = os.path.normcase(os.path.normpath("C:/work/sample-project"))
    with tempfile.TemporaryDirectory() as folder:
        logs = Path(folder) / datetime.fromtimestamp(now).strftime("%Y/%m/%d")
        logs.mkdir(parents=True)
        path = logs / ("rollout-test-" + sid + ".jsonl")
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": sid, "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "cwd": cwd}}) + "\n" + _event("task_started"), encoding="utf-8")
        processes = {100: now - 3600, 200: now - 30}
        contexts = {
            100: {"command": "codex.exe app-server --managed-daemon"},
            200: {"command": r"node.exe C:\\node_modules\\@openai\\codex\\bin\\codex.js --no-daemon",
                  "cwd": cwd},
        }
        with patch.object(light, "CODEX_SESSIONS_DIR", folder), \
             patch.object(light, "_codex_process_starts", return_value=processes), \
             patch.object(light, "_codex_process_context", side_effect=contexts.get), \
             patch.object(light, "_codex_session_owners",
                          return_value={sid: {"pid": 100, "ts": now - 5}}), \
             patch.object(light, "_CODEX_CACHE", {"checked": 0, "sessions": {}, "logs": {}}):
            sessions = light.scan_codex_sessions(now)
            assert sessions[sid]["termpid"] == 200


def test_background_daemon_without_terminal_has_no_jump_pid():
    assert light._codex_terminal_pid("thread", {"pid": 100}, "C:/work",
        {100: 1}, {100: {"command": "codex.exe app-server"}}) == 0


def test_missing_cwd_falls_back_to_live_cli_client():
    sid = "11111111-1111-1111-1111-111111111111"
    assert light._codex_terminal_pid(sid, {"pid": 100, "ts": 2.1}, "C:/work",
        {100: 1, 200: 2, 300: 3},
        {100: {"command": "codex.exe app-server"}, 200: {}, 300: {}}) == 200


def test_unowned_rollouts_do_not_match_background_daemon():
    now = time.time()
    with tempfile.TemporaryDirectory() as folder:
        logs = Path(folder) / datetime.fromtimestamp(now).strftime("%Y/%m/%d")
        logs.mkdir(parents=True)
        path = logs / "rollout-daemon-test.jsonl"
        meta = {"type": "session_meta", "payload": {
            "id": "daemon-test", "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat()}}
        path.write_text(json.dumps(meta) + "\n" + _event("task_started"), encoding="utf-8")
        with patch.object(light, "_codex_process_starts", return_value={100: now}), \
             patch.object(light, "_codex_process_context",
                          return_value={"command": "codex.exe app-server daemon pid-update-loop"}), \
             patch.object(light, "_codex_session_owners", return_value=None), \
             patch.object(light, "CODEX_SESSIONS_DIR", folder), \
             patch.object(light, "_CODEX_CACHE", {"checked": 0, "sessions": {}, "logs": {}}):
            assert light.scan_codex_sessions(now) == {}


def test_archived_codex_rollout_is_hidden_without_deleting_log():
    now = time.time()
    sid = "codex-archived-session"
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        logs = root / datetime.fromtimestamp(now).strftime("%Y/%m/%d")
        logs.mkdir(parents=True)
        rollout = logs / ("rollout-test-" + sid + ".jsonl")
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": sid, "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "cwd": "C:/work/sample-project"}}) + "\n" + _event("task_started"),
            encoding="utf-8")
        archived = root / "codex-archived.json"
        with patch.object(light, "CODEX_SESSIONS_DIR", str(root)), \
             patch.object(light, "CODEX_ARCHIVED_FILE", str(archived)), \
             patch.object(light, "_codex_process_starts", return_value={1234: now - 10}), \
             patch.object(light, "_codex_session_owners", return_value=None), \
             patch.object(light, "_codex_process_context", return_value={}), \
             patch.object(light, "_CODEX_CACHE", {"checked": 0, "sessions": {}, "logs": {}}):
            assert sid in light.scan_codex_sessions(now)
            light.archive_codex_session(sid)
            light._CODEX_CACHE["checked"] = 0
            assert sid not in light.scan_codex_sessions(now + 4)
            assert rollout.exists()

            # Completion and ordinary log writes do not undo a user's delete.
            with rollout.open("a", encoding="utf-8") as stream:
                stream.write(_event("task_complete"))
            assert sid not in light.scan_codex_sessions(now + 8)

            # A restart must preserve the hide, then a new prompt restores it.
            light._CODEX_CACHE.update(checked=0, sessions={}, logs={})
            assert sid not in light.scan_codex_sessions(now + 12)
            with rollout.open("a", encoding="utf-8") as stream:
                stream.write(_event("task_started"))
            assert sid not in light.scan_codex_sessions(now + 16)
            with rollout.open("a", encoding="utf-8") as stream:
                stream.write(_event("user_message"))
            light._CODEX_CACHE["checked"] = 0
            restored = light.scan_codex_sessions(now + 16)[sid]
            assert restored["state"] == "yellow"
            assert light.effective_state(restored, now + 10000) == "yellow"
            assert sid not in json.loads(archived.read_text(encoding="utf-8"))

            # Once restored, it stays visible after completion and restart.
            with rollout.open("a", encoding="utf-8") as stream:
                stream.write(_event("task_complete"))
            light._CODEX_CACHE.update(checked=0, sessions={}, logs={})
            assert light.scan_codex_sessions(now + 20)[sid]["state"] == "green"

            # A whole quick turn between polls also restores the finished row.
            light.archive_codex_session(sid)
            with rollout.open("a", encoding="utf-8") as stream:
                stream.write(_event("task_started") + json.dumps({"type": "response_item", "payload": {
                    "type": "message", "role": "user", "content": []}}) + "\n" + _event("task_complete"))
            assert light.scan_codex_sessions(now + 24)[sid]["state"] == "green"


def test_legacy_codex_hides_wait_for_a_new_user_message():
    now = time.time()
    sid = "44444444-4444-4444-4444-444444444444"
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        logs = root / datetime.fromtimestamp(now).strftime("%Y/%m/%d")
        logs.mkdir(parents=True)
        rollout = logs / ("rollout-test-" + sid + ".jsonl")
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": sid, "cwd": "C:/work"}}) + "\n" + _event("task_complete"),
            encoding="utf-8")
        archived = root / "archived.json"
        archived.write_text(json.dumps([sid]), encoding="utf-8")
        with patch.object(light, "CODEX_SESSIONS_DIR", folder), \
             patch.object(light, "CODEX_ARCHIVED_FILE", str(archived)), \
             patch.object(light, "_codex_process_starts", return_value={100: now - 10}), \
             patch.object(light, "_codex_process_context", return_value={}), \
             patch.object(light, "_codex_session_owners",
                          return_value={sid: {"pid": 100, "ts": now - 1}}), \
             patch.object(light, "_CODEX_CACHE", {"checked": 0, "sessions": {}, "logs": {}}):
            assert sid not in light.scan_codex_sessions(now)
            assert isinstance(json.loads(archived.read_text(encoding="utf-8")), dict)
            with rollout.open("a", encoding="utf-8") as stream:
                stream.write(_event("task_started"))
            assert sid not in light.scan_codex_sessions(now + 4)
            with rollout.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "event_msg", "payload": {
                    "type": "item_completed", "item": {"type": "UserMessage", "content": []}}}) + "\n")
            light._CODEX_CACHE["checked"] = 0
            assert light.scan_codex_sessions(now + 4)[sid]["state"] == "yellow"

            archived.write_text(json.dumps([sid]), encoding="utf-8")
            light._CODEX_CACHE.update(checked=0, sessions={}, logs={})
            assert sid not in light.scan_codex_sessions(now + 8)


def test_windows_lock_ownership_ignores_closed_handles():
    if sys.platform != "win32":
        return
    sid = "11111111-1111-1111-1111-111111111111"
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / (sid + ".lock")
        path.touch()
        processes = {os.getpid(): time.time()}
        with patch.object(light, "CODEX_LOCKS_DIR", folder):
            with path.open("rb"):
                assert light._codex_session_owners(processes)[sid]["pid"] == os.getpid()
            assert light._codex_session_owners(processes) == {}


def test_guardian_review_detection_uses_internal_type_not_title():
    assert light._is_codex_guardian_review({"thread_source": "guardian_review"})
    assert light._is_codex_guardian_review({"source": {"subagent": {"other": "guardian"}}})
    assert light._is_codex_guardian_review({"source": '{"subagent":{"other":"guardian"}}'})
    for meta in ({"title": "Guardian review", "source": "cli"},
                 {"source": {"subagent": {"other": "explorer"}}},
                 {"source": None}, {"source": {"subagent": "guardian"}}):
        assert not light._is_codex_guardian_review(meta)


def test_guardian_sessions_stay_out_despite_new_machine_messages_and_new_ids():
    now = time.time()
    guardian = "11111111-1111-1111-1111-111111111111"
    recreated = "22222222-2222-2222-2222-222222222222"
    normal = "33333333-3333-3333-3333-333333333333"
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        logs = root / datetime.fromtimestamp(now).strftime("%Y/%m/%d")
        logs.mkdir(parents=True)
        for sid, metadata in [(guardian, {"thread_source": "guardian_review"}),
                              (recreated, {"source": {"subagent": {"other": "guardian"}}}),
                              (normal, {"title": "Guardian review", "source": "cli"})]:
            path = logs / ("rollout-test-" + sid + ".jsonl")
            path.write_text(json.dumps({"type": "session_meta", "payload": {
                "id": sid, "cwd": "C:/work", **metadata}}) + "\n" +
                _event("task_started") + _event("user_message"), encoding="utf-8")
        archive = root / "archived.json"
        hidden = {guardian: {"user_offset": 0}}
        archive.write_text(json.dumps(hidden), encoding="utf-8")
        with patch.object(light, "CODEX_SESSIONS_DIR", str(root)), \
             patch.object(light, "CODEX_ARCHIVED_FILE", str(archive)), \
             patch.object(light, "_codex_process_starts", return_value={100: now - 10}), \
             patch.object(light, "_codex_process_context", return_value={}), \
             patch.object(light, "_codex_session_owners", return_value={
                 sid: {"pid": 100, "ts": now} for sid in (guardian, recreated, normal)}), \
             patch.object(light, "_CODEX_CACHE", {"checked": 0, "sessions": {}, "logs": {}}):
            assert set(light.scan_codex_sessions(now)) == {normal}
            assert json.loads(archive.read_text(encoding="utf-8")) == hidden
            path = logs / ("rollout-test-" + guardian + ".jsonl")
            with path.open("a", encoding="utf-8") as stream:
                stream.write(_event("user_message") + _event("task_started"))
            light._CODEX_CACHE.update(checked=0, sessions={}, logs={})
            assert set(light.scan_codex_sessions(now + 4)) == {normal}
            assert json.loads(archive.read_text(encoding="utf-8")) == hidden


if __name__ == "__main__":
    test_codex_cli_command_matches_npm_node_launcher()
    test_codex_turns_and_process_lifetime()
    test_resumed_daemon_threads_use_live_ownership()
    test_shared_daemon_uses_lock_time_to_choose_terminal()
    test_background_exec_writer_never_binds_to_same_project_tui()
    test_codex_exec_rollout_is_marked_non_jumpable()
    test_shared_daemon_binds_to_node_codex_cli_client()
    test_background_daemon_without_terminal_has_no_jump_pid()
    test_missing_cwd_falls_back_to_live_cli_client()
    test_unowned_rollouts_do_not_match_background_daemon()
    test_archived_codex_rollout_is_hidden_without_deleting_log()
    test_windows_lock_ownership_ignores_closed_handles()
    print("ALL PASS")
