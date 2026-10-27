import json
import os
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cc_light as light


def test_remove_inactive_scanned_sessions_keeps_active_and_hook_sessions():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / "active.json").write_text(json.dumps({"scanned": True}), encoding="utf-8")
        (root / "closed.json").write_text(json.dumps({"scanned": True}), encoding="utf-8")
        (root / "hook.json").write_text(json.dumps({"state": "green"}), encoding="utf-8")
        with patch.object(light, "SESSIONS_DIR", folder):
            light.remove_inactive_scanned_sessions({"active"})

        assert (root / "active.json").exists()
        assert not (root / "closed.json").exists()
        assert (root / "hook.json").exists()


def test_read_sessions_drops_scanned_session_when_terminal_is_closed():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        path = root / "closed.json"
        path.write_text(json.dumps({"scanned": True, "termpid": 4321,
                                    "state": "green"}), encoding="utf-8")
        with patch.object(light, "SESSIONS_DIR", folder), \
             patch.object(light, "is_process_alive", return_value=False), \
             patch.object(light, "scan_codex_sessions", return_value={}):
            assert "closed" not in light.read_sessions()
        assert not path.exists()


def test_claude_delete_blocks_scan_and_hooks_until_user_prompt():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        data = root / "cc-light"
        sessions = data / "sessions"
        sessions.mkdir(parents=True)
        sid = "deleted"
        session = sessions / (sid + ".json")
        session.write_text(json.dumps({"state": "yellow"}), encoding="utf-8")
        with patch.object(light, "DATA_DIR", str(data)), \
             patch.object(light, "SESSIONS_DIR", str(sessions)), \
             patch.object(light, "ARCHIVE_DIR", str(data / "archive")):
            light.archive_session(sid, user_hidden=True)
            light._write_session_file(sid, {"state": "yellow", "scanned": True})
            assert not session.exists()
            for event in ("PostToolUse", "Stop", "SessionStart", "PermissionRequest", "UserPromptSubmit"):
                code = "require('child_process').execFileSync=()=>'';process.argv=['node','hook.js','yellow_soft'];require('./hook.js')"
                result = subprocess.run(["node", "-e", code], cwd=str(Path(light.__file__).parent),
                    input=json.dumps({"session_id": sid, "hook_event_name": event}).encode(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ, APPDATA=folder))
                assert result.returncode == 0, result.stderr
                assert session.exists() == (event == "UserPromptSubmit")
            assert not Path(light._claude_hidden_file(sid)).exists()


if __name__ == "__main__":
    test_remove_inactive_scanned_sessions_keeps_active_and_hook_sessions()
    test_read_sessions_drops_scanned_session_when_terminal_is_closed()
    print("ALL PASS")
