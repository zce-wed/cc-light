import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cc_light as light


def test_workers_merge_recursively_with_waiting_priority_and_independent_chats_remain():
    rows = {"root": {"state": "green", "name": "same-project", "ts": 1},
            "other": {"state": "green", "name": "same-project", "ts": 1},
            "worker": {"state": "yellow", "ts": 2},
            "nested": {"state": "red", "ts": 3}}
    result = light._fold_codex_subagents(rows, {"worker": "root", "nested": "worker"})
    assert set(result) == {"root", "other"}
    assert result["root"]["state"] == "red"
    assert result["root"]["subs"] == 2
    assert result["root"]["ts"] == 3
    assert result["other"]["state"] == "green"
    assert rows["root"]["state"] == "green"


def test_hidden_or_closed_parents_and_broken_cycles_do_not_show_workers():
    rows = {"worker": {"state": "yellow"}, "nested": {"state": "red"}}
    assert light._fold_codex_subagents(rows, {"worker": "missing", "nested": "worker"}) == {}
    assert light._fold_codex_subagents(rows, {"worker": "nested", "nested": "worker"}) == {}


def test_subagent_source_detection_keeps_normal_user_forks_separate():
    spawn = {"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}
    assert light._codex_subagent_parent({"source": spawn}) == "root"
    assert light._codex_subagent_parent({"source": json.dumps(spawn)}) == "root"
    assert light._codex_subagent_parent({"thread_source": "subagent", "parent_thread_id": "root"}) == "root"
    assert light._codex_subagent_parent({"thread_source": "user", "source": "vscode", "parent_thread_id": "root"}) is None


def test_scan_folds_three_workers_and_parent_delete_survives_worker_prompts():
    now = time.time()
    root_sid = "11111111-1111-7111-8111-111111111111"
    workers = ["22222222-2222-7222-8222-22222222222" + str(i) for i in range(3)]
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        logs = root / datetime.fromtimestamp(now).strftime("%Y/%m/%d")
        logs.mkdir(parents=True)
        for sid in [root_sid] + workers:
            meta = {"id": sid, "cwd": "C:/work/ProjectManager"}
            if sid in workers:
                meta.update(thread_source="subagent", parent_thread_id=root_sid)
            events = [{"type": "session_meta", "payload": meta},
                      {"type": "event_msg", "payload": {"type": "task_started" if sid in workers else "task_complete"}}]
            (logs / ("rollout-test-" + sid + ".jsonl")).write_text(
                "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        with patch.object(light, "CODEX_SESSIONS_DIR", folder), \
             patch.object(light, "CODEX_ARCHIVED_FILE", str(root / "hidden.json")), \
             patch.object(light, "_codex_process_starts", return_value={100: now - 10}), \
             patch.object(light, "_codex_process_context", return_value={}), \
             patch.object(light, "_codex_session_owners", return_value={
                 sid: {"pid": 100, "ts": now} for sid in [root_sid] + workers}), \
             patch.object(light, "_CODEX_CACHE", {"checked": 0, "sessions": {}, "logs": {}}):
            result = light.scan_codex_sessions(now)
            assert set(result) == {root_sid}
            assert result[root_sid]["state"] == "yellow"
            assert result[root_sid]["subs"] == 3
            light.archive_codex_session(root_sid)
            for sid in workers:
                with (logs / ("rollout-test-" + sid + ".jsonl")).open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message"}}) + "\n")
            light._CODEX_CACHE.update(checked=0, sessions={}, logs={})
            assert light.scan_codex_sessions(now + 4) == {}
            with (logs / ("rollout-test-" + root_sid + ".jsonl")).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message"}}) + "\n")
            assert set(light.scan_codex_sessions(now + 8)) == {root_sid}
