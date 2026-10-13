import importlib.util
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


script = Path(__file__).resolve().parents[1] / "install-hooks.py"
spec = importlib.util.spec_from_file_location("install_hooks", script)
hooks_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hooks_module)


def test_strip_stale_ecc_keeps_other_hooks():
    data = {"Stop": [
        {"matcher": "*", "hooks": [{"command": "node -e run-with-flags.js"},
                                    {"command": "node hook.js green"}]},
        {"matcher": "*", "hooks": [{"command": "node -e run-with-flags.js"}]},
    ], "SessionStart": [{"hooks": [{"command": "node hook.js green"}]}]}
    with patch.object(hooks_module, "ecc_runner_exists", return_value=False):
        assert hooks_module.strip_stale_ecc(data) == 2
    assert data == {"Stop": [{"matcher": "*", "hooks": [{"command": "node hook.js green"}]}],
                    "SessionStart": [{"hooks": [{"command": "node hook.js green"}]}]}


def test_installed_ecc_is_preserved():
    data = {"Stop": [{"hooks": [{"command": "node -e run-with-flags.js"}]}]}
    with patch.object(hooks_module, "ecc_runner_exists", return_value=True):
        assert hooks_module.strip_stale_ecc(data) == 0
    assert len(data["Stop"]) == 1


def test_repair_backs_up_settings():
    with tempfile.TemporaryDirectory() as folder:
        settings = os.path.join(folder, "settings.json")
        original = {"model": "test", "hooks": {"Stop": [
            {"hooks": [{"command": "node -e run-with-flags.js"}]},
            {"hooks": [{"command": "node hook.js green"}]},
        ]}}
        Path(settings).write_text(json.dumps(original), encoding="utf-8")
        with patch.object(hooks_module, "SETTINGS", settings), \
             patch.object(hooks_module, "ecc_runner_exists", return_value=False):
            assert hooks_module.cmd_repair_stale_ecc() == 0
        updated = json.loads(Path(settings).read_text(encoding="utf-8"))
        assert updated["model"] == "test"
        assert updated["hooks"]["Stop"] == original["hooks"]["Stop"][1:]
        backups = list(Path(folder).glob("settings.json.bak-cc-light-*"))
        assert len(backups) == 1
        assert json.loads(backups[0].read_text(encoding="utf-8")) == original


def test_repair_codex_hooks_keeps_unrelated_commands():
    with tempfile.TemporaryDirectory() as folder:
        target = os.path.join(folder, "hooks.json")
        original = {"hooks": {"PreToolUse": [
            {"hooks": [{"command": "npx block-no-verify@1.1.2"}]},
            {"hooks": [{"command": 'node "/home/ubuntu/.claude/scripts/hooks/a.js"'}]},
        ], "Stop": [
            {"hooks": [{"command": "node -e run-with-flags.js"}]},
            {"hooks": [{"command": 'node "C:/Users/pm/.claude/cc-light/hook.js" green'}]},
        ]}}
        Path(target).write_text(json.dumps(original), encoding="utf-8")
        with patch.object(hooks_module, "CODEX_HOOKS", target):
            assert hooks_module.cmd_repair_codex_hooks() == 0
        updated = json.loads(Path(target).read_text(encoding="utf-8"))
        assert updated["hooks"]["PreToolUse"] == original["hooks"]["PreToolUse"][:1]
        assert updated["hooks"]["Stop"] == []
        backups = list(Path(folder).glob("hooks.json.bak-cc-light-*"))
        assert len(backups) == 1
        assert json.loads(backups[0].read_text(encoding="utf-8")) == original
