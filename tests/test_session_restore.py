import json
import os
import tempfile
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


if __name__ == "__main__":
    test_remove_inactive_scanned_sessions_keeps_active_and_hook_sessions()
    test_read_sessions_drops_scanned_session_when_terminal_is_closed()
    print("ALL PASS")
