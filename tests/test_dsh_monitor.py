import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cc_light as light


def _record(sid, cwd, *, title=None, blank=False, last_prompt_at=None,
            open_turn=None, open_step=None, pending_calls=None,
            next_turn=None, active_questions=None):
    rows = {
        "title": {"ver": 1, "seq": 1, "val": title},
        "sessionListMetadata": {"ver": 1, "seq": 1, "val": {
            "blank": blank, "lastPromptAt": last_prompt_at}},
        "turnBoundary": {"ver": 2, "seq": 1, "val": {
            "openTurnStartSeq": open_turn,
            "lastStepBoundary": {"kind": "end" if open_step is None else "start",
                                 "seq": 1}}},
        "sessionStats": {"ver": 1, "seq": 1, "val": {
            "turns": 1, "steps": 1, "lastTurn": 1,
            "openStep": open_step, "pendingCalls": pending_calls or {}}},
        "inbox": {"ver": 1, "seq": 1, "val": {
            "next-turn": next_turn or [], "next-step": []}},
        "userQuestions": {"ver": 2, "seq": 1, "val": {
            "inheritedEventCount": 0, "timed": False,
            "questions": {"active": active_questions or [], "settled": []}}},
        "workingDirectory": {"ver": 1, "seq": 1, "val": cwd},
    }
    return {"version": 7, "record": {
        "identity": {"formatVersion": 4, "createdAt": 1, "cwd": cwd},
        "rows": rows}}


def _write(cache, sid, record):
    path = Path(cache) / (sid + ".json")
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _scan(cache, now, *, processes=None, archived=None):
    with patch.object(light, "DSH_PROJ_CACHE_DIR", str(cache)), \
         patch.object(light, "DSH_WORKSPACE_FILE", str(Path(cache) / "workspace.json")), \
         patch.object(light, "DSH_ARCHIVED_FILE", str(Path(cache) / "archived.json")), \
         patch.object(light, "_dsh_process_starts",
                      return_value={1: now - 60} if processes is None else processes), \
         patch.object(light, "_DSH_CACHE", {"checked": 0, "sessions": {}}):
        if archived is not None:
            (Path(cache) / "workspace.json").write_text(json.dumps(
                {"global": {"archivedSessionIds": list(archived)}}), encoding="utf-8")
        return light.scan_dsh_sessions(now)


def test_dsh_state_mapping():
    now = time.time()
    assert light._dsh_state(_record("s", "C:/w/p", open_turn=3)["record"]["rows"]) == "yellow"
    assert light._dsh_state(_record("s", "C:/w/p", open_step={"turn": 1})["record"]["rows"]) == "yellow"
    assert light._dsh_state(_record("s", "C:/w/p", pending_calls={"a": {}})["record"]["rows"]) == "yellow"
    assert light._dsh_state(_record("s", "C:/w/p", next_turn=[{"id": "m"}])["record"]["rows"]) == "yellow"
    assert light._dsh_state(_record("s", "C:/w/p", active_questions=[{"callId": "c"}])["record"]["rows"]) == "red"
    assert light._dsh_state(_record("s", "C:/w/p")["record"]["rows"]) == "green"
    assert now  # keep the timestamp meaningful for readers


def test_scan_prefers_latest_turn_prompt_over_stale_title():
    now = time.time()
    with tempfile.TemporaryDirectory() as folder:
        cache = Path(folder)
        record = _record("session-x", "C:/work/alpha", title="old title", last_prompt_at=1)
        record["record"]["rows"]["turnOutline"] = {"ver": 2, "seq": 1, "val": {
            "turns": [{"turn": 1, "prompt": "switch to the new task"},
                      {"turn": 2, "prompt": "now fix the parser"}],
            "draft": ""}}
        _write(cache, "session-x", record)
        result = _scan(cache, now, processes={1: now - 3600})
        assert result["session-x"]["title"] == "now fix the parser"

        # 太短的选项回复(「1」)跳过,继续用最近一条有信息量的 prompt
        record["record"]["rows"]["turnOutline"]["val"]["turns"].append(
            {"turn": 3, "prompt": "1"})
        _write(cache, "session-x", record)
        result = _scan(cache, now, processes={1: now - 3600})
        assert result["session-x"]["title"] == "now fix the parser"


def test_scan_lists_busy_and_recent_idle_sessions():
    now = time.time()
    with tempfile.TemporaryDirectory() as folder:
        cache = Path(folder)
        busy = _write(cache, "session-busy",
                      _record("session-busy", "C:/work/alpha", title="fix it",
                              open_step={"turn": 1, "step": 2}, last_prompt_at=1))
        _write(cache, "session-idle", _record("session-idle", "C:/work/beta",
                                              title="done", last_prompt_at=1))
        # an idle session untouched for 2 hours fades out; a busy one never does
        old_idle = _write(cache, "session-old-idle",
                          _record("session-old-idle", "C:/work/gamma", last_prompt_at=1))
        old = now - 2 * light.DSH_ACTIVE_WINDOW
        os.utime(str(old_idle), (old, old))
        result = _scan(cache, now, processes={1: now - 4 * light.DSH_ACTIVE_WINDOW})
        assert set(result) == {"session-busy", "session-idle"}
        assert result["session-busy"]["state"] == "yellow"
        assert result["session-busy"]["name"] == "alpha"
        assert result["session-busy"]["title"] == "fix it"
        assert result["session-busy"]["window_title"] == "fix it"
        assert result["session-idle"]["state"] == "green"
        assert light.effective_state(result["session-busy"], now + 10_000) == "yellow"
        assert busy.exists()


def test_scan_skips_blank_archived_stale_and_dead_app():
    now = time.time()
    with tempfile.TemporaryDirectory() as folder:
        cache = Path(folder)
        _write(cache, "session-blank", _record("session-blank", "C:/w/a", blank=True))
        _write(cache, "session-archived", _record("session-archived", "C:/w/b",
                                                  last_prompt_at=1))
        # checkpoint from a previous app run (older than the current process)
        _write(cache, "session-stale", _record("session-stale", "C:/w/c",
                                               last_prompt_at=1))
        os.utime(str(cache / "session-stale.json"), (now - 600, now - 600))
        result = _scan(cache, now, archived={"session-archived"})
        assert result == {}
        assert _scan(cache, now, processes={}) == {}


def test_dsh_sessions_join_read_sessions():
    now = time.time()
    with tempfile.TemporaryDirectory() as folder:
        cache = Path(folder)
        _write(cache, "session-live", _record("session-live", "C:/work/gamma",
                                              title="task", open_turn=1,
                                              last_prompt_at=1))
        with patch.object(light, "DSH_PROJ_CACHE_DIR", str(cache)), \
             patch.object(light, "DSH_WORKSPACE_FILE", str(Path(cache) / "workspace.json")), \
             patch.object(light, "DSH_ARCHIVED_FILE", str(Path(cache) / "archived.json")), \
             patch.object(light, "_dsh_process_starts", return_value={1: now - 60}), \
             patch.object(light, "_DSH_CACHE", {"checked": 0, "sessions": {}}), \
             patch.object(light, "SESSIONS_DIR", str(Path(cache) / "hook")), \
             patch.object(light, "scan_codex_sessions", return_value={}):
            sessions = light.read_sessions(now)
        assert sessions["session-live"]["provider"] == "dsh"
        assert light.aggregate(sessions)[0] == "yellow"
