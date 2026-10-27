import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cc_light as light


def test_cleanup_keeps_running_and_waiting_for_all_providers():
    now = time.time()
    sessions = {provider + state: {"provider": provider, "state": state, "ts": now}
                for provider in ("claude", "codex", "dsh")
                for state in ("red", "yellow", "green", "gray")}
    with patch.object(light, "read_sessions", return_value=sessions), \
         patch.object(light, "hide_session") as hide:
        assert light.keep_working_sessions(now) == 6
    assert {call.args[0] for call in hide.call_args_list} == {
        provider + state for provider in ("claude", "codex", "dsh") for state in ("green", "gray")}


def test_cleanup_refreshes_provider_state_and_empty_cleanup_is_safe():
    with patch.object(light, "_CODEX_CACHE", {"checked": 999, "sessions": {}}), \
         patch.object(light, "_DSH_CACHE", {"checked": 999, "sessions": {}}), \
         patch.object(light, "hide_session") as hide:
        def current_sessions(now):
            assert light._CODEX_CACHE["checked"] == 0
            assert light._DSH_CACHE["checked"] == 0
            return {}
        with patch.object(light, "read_sessions", side_effect=current_sessions):
            assert light.keep_working_sessions(1000) == 0
        hide.assert_not_called()


def test_cleanup_uses_same_timeout_state_as_visible_list():
    sessions = {"claude": {"state": "yellow", "ts": 1},
                "codex": {"provider": "codex", "state": "yellow", "ts": 1},
                "waiting": {"state": "red", "ts": 1}}
    with patch.object(light, "_config", {"timeout_fallback": True, "yellow_timeout": 30}), \
         patch.object(light, "read_sessions", return_value=sessions), \
         patch.object(light, "hide_session") as hide:
        assert light.keep_working_sessions(1000) == 1
        hide.assert_called_once_with("claude", sessions["claude"])


def test_hide_session_preserves_user_delete_and_invalidates_provider_cache():
    with patch.object(light, "archive_codex_session") as codex, \
         patch.object(light, "archive_dsh_session") as dsh, \
         patch.object(light, "archive_session") as claude, \
         patch.object(light, "_CODEX_CACHE", {"checked": 9, "sessions": {"c": {}}}), \
         patch.object(light, "_DSH_CACHE", {"checked": 9, "sessions": {"d": {}}}):
        light.hide_session("c", {"provider": "codex"})
        light.hide_session("d", {"provider": "dsh"})
        light.hide_session("a", {})
        codex.assert_called_once_with("c")
        dsh.assert_called_once_with("d")
        claude.assert_called_once_with("a", user_hidden=True)
        assert light._CODEX_CACHE == {"checked": 0, "sessions": {}}
        assert light._DSH_CACHE == {"checked": 0, "sessions": {}}
