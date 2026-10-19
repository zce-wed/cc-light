# -*- coding: utf-8 -*-
"""跳转链路纯函数回归 —— 守门「点击会话跳不到窗口」类回归(2026-07-16 起)。
Windows API 依赖运行环境,这里只测纯逻辑分支(空值/死 pid 不崩 + 合理返回);
端到端用 `python cc_light.py --diag` 验证当前所有会话能定位窗口。"""
import sys, os
from unittest.mock import MagicMock, patch
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cc_light as L


def test_find_window_by_name_empty():
    assert L.find_window_by_name(None) is None
    assert L.find_window_by_name("") is None
    assert L.find_window_by_name("x") is None          # len<2 守卫


def test_find_window_by_pid_invalid():
    assert L.find_window_by_pid(0) is None
    assert L.find_window_by_pid(None) is None


def test_find_window_by_pid_chooses_nearest_host_not_first_window():
    if sys.platform != "win32":
        return
    import ctypes
    api = MagicMock()
    api.IsWindowVisible.return_value = True
    api.GetParent.return_value = 0
    window_pids = {10: 300, 20: 200, 30: 999}

    def window_pid(hwnd, ptr):
        ptr._obj.value = window_pids[hwnd]
        return 1

    def enum_windows(callback, arg):
        for hwnd in (10, 20, 30):
            if not callback(hwnd, arg):
                break
        return True

    api.GetWindowThreadProcessId.side_effect = window_pid
    api.EnumWindows.side_effect = enum_windows
    with patch.object(L, "_ancestor_pids", return_value=[100, 200, 300]), \
         patch.object(ctypes, "windll", MagicMock(user32=api)):
        assert L.find_window_by_pid(100) == 20


def test_console_window_map_links_pseudo_console_to_owner_window():
    if sys.platform != "win32":
        return
    import ctypes
    api = MagicMock()
    classes = {10: "PseudoConsoleWindow", 11: "PseudoConsoleWindow",
               100: "CASCADIA_HOSTING_WINDOW_CLASS", 101: "CASCADIA_HOSTING_WINDOW_CLASS"}
    window_pids = {10: 500, 11: 501, 100: 900, 101: 900}
    owners = {10: 100, 11: 101}
    api.IsWindowVisible.return_value = True
    api.GetWindow.side_effect = lambda hwnd, cmd: owners.get(hwnd, 0)

    def get_class(hwnd, buf, n):
        buf.value = classes[hwnd]
        return len(classes[hwnd])

    def window_pid(hwnd, ptr):
        ptr._obj.value = window_pids[hwnd]
        return 1

    def enum_windows(callback, arg):
        for hwnd in (10, 11, 100, 101):
            if not callback(hwnd, arg):
                break
        return True

    api.GetClassNameW.side_effect = get_class
    api.GetWindowThreadProcessId.side_effect = window_pid
    api.EnumWindows.side_effect = enum_windows
    with patch.object(ctypes, "windll", MagicMock(user32=api)):
        assert L._console_window_map() == {500: 100, 501: 101}


def test_window_for_console_pids_uses_nearest_ancestor():
    with patch.object(L, "_console_window_map",
                      return_value={500: 55, 900: 77}) as mapping:
        assert L._window_for_console_pids([100, 500, 900]) == 55
        mapping.assert_called_once_with()


def test_find_window_by_pid_prefers_exact_console_window():
    # WT 多窗口共用一个 PID;控制台映射命中时必须直接采用,不能再按 PID 猜第一个窗口。
    with patch.object(L, "_ancestor_pids", return_value=[100, 500, 900]), \
         patch.object(L, "_console_window_map", return_value={500: 55}):
        assert L.find_window_by_pid(100) == 55
        assert L.find_window_by_pid(100, unique=True) == 55


def test_find_window_by_pid_unique_rejects_shared_windows_terminal_host():
    if sys.platform != "win32":
        return
    import ctypes
    api = MagicMock()
    api.IsWindowVisible.return_value = True
    api.GetParent.return_value = 0
    window_pids = {10: 300, 20: 300, 30: 999}

    def window_pid(hwnd, ptr):
        ptr._obj.value = window_pids[hwnd]
        return 1

    def enum_windows(callback, arg):
        for hwnd in (10, 20, 30):
            if not callback(hwnd, arg):
                break
        return True

    api.GetWindowThreadProcessId.side_effect = window_pid
    api.EnumWindows.side_effect = enum_windows
    with patch.object(L, "_ancestor_pids", return_value=[100, 300]), \
         patch.object(ctypes, "windll", MagicMock(user32=api)):
        assert L.find_window_by_pid(100, unique=True) is None


def test_ancestor_pids_invalid_no_crash():
    # 0 → 空(进不去循环)
    assert L._ancestor_pids(0) == []
    # 死 pid:append 后 OpenProcess 失败中断,返回 [pid] 本身 —— 不循环、不抛
    out = L._ancestor_pids(99999999)
    assert out == [99999999]


def test_resolve_hwnd_zero_name_empty():
    # hwnd=0 且 name 空 → find_window_by_name("")=None,不崩
    assert L.resolve_hwnd(0, "") is None
    assert L.resolve_hwnd(None, None) is None


def test_encode_path_basics():
    # session 的 name 兜底匹配窗口标题用的是 basename(cwd 末段);
    # _encode_path 仅用于匹配 ~/.claude/projects 目录名,勿与 name 混淆。
    assert L._encode_path(r"C:\Users\pm\zcvip-front") == "C--Users-pm-zcvip-front"


def test_trae_focus_uri_uses_pid_chain():
    with patch.object(L, "_ancestor_pids", return_value=[3448, 33716, 21760]):
        assert L.trae_focus_uri(3448) == (
            "trae-cn://cc-light.cc-light-helper/focus?pid=3448&pid=33716&pid=21760"
        )


def test_trae_focus_uri_invalid_pid():
    assert L.trae_focus_uri(0) is None
    assert L.trae_focus_uri(None) is None


def test_focus_trae_terminal_does_not_require_shared_hwnd():
    uri = "trae-cn://cc-light.cc-light-helper/focus?pid=123&pid=456"
    with patch.object(L, "_is_trae_wnd", return_value=True), \
         patch.object(L, "trae_focus_uri", return_value=uri), \
         patch.object(L.os, "startfile", create=True) as startfile:
        assert L.focus_trae_terminal(789, 123) is True
        startfile.assert_called_once_with(uri)


def test_focus_trae_terminal_rejects_non_trae_window():
    with patch.object(L, "_is_trae_wnd", return_value=False), \
         patch.object(L.os, "startfile", create=True) as startfile:
        assert L.focus_trae_terminal(789, 123) is False
        startfile.assert_not_called()


def test_codex_jump_focuses_trae_terminal():
    uri = "trae-cn://cc-light.cc-light-helper/focus?pid=123&pid=456"
    with patch.object(L, "_find_codex_window", return_value=None) as by_title, \
         patch.object(L, "_terminal_window_for_pid", return_value=None) as by_console, \
         patch.object(L, "find_window_by_pid", return_value=789) as by_pid, \
         patch.object(L, "_codex_process_context", return_value={}), \
         patch.object(L, "_window_process_name", return_value="Trae CN.exe"), \
         patch.object(L, "focus_window", return_value=True) as focus, \
         patch.object(L, "_is_trae_wnd", return_value=True), \
         patch.object(L, "trae_focus_uri", return_value=uri), \
         patch.object(L.os, "startfile", create=True) as startfile:
        assert L.jump_codex_session(123, "project") is True
        focus.assert_called_once_with(789)
        startfile.assert_called_once_with(uri)
        by_console.assert_called_once_with(123)
        by_title.assert_called_once_with(("project",))
        by_pid.assert_called_once_with(123, unique=True)


def test_codex_jump_does_not_guess_another_project_window():
    with patch.object(L, "_find_codex_window", return_value=None) as by_title, \
         patch.object(L, "_terminal_window_for_pid", return_value=None) as by_console, \
         patch.object(L, "find_window_by_pid", return_value=None), \
         patch.object(L, "focus_window", return_value=True) as focus, \
         patch.object(L.os, "startfile", create=True) as startfile:
        assert L.jump_codex_session(123, "project") is False
        by_title.assert_called_once_with(("project",))
        by_console.assert_called_once_with(123)
        focus.assert_not_called()
        startfile.assert_not_called()


def test_codex_jump_keeps_windows_terminal_host():
    with patch.object(L, "_terminal_window_for_pid", return_value=None), \
         patch.object(L, "_codex_process_context", return_value={}), \
         patch.object(L, "find_window_by_pid", return_value=456), \
         patch.object(L, "find_trae_window", return_value=789) as trae, \
         patch.object(L, "_window_process_name", return_value="WindowsTerminal.exe"), \
         patch.object(L, "_is_trae_wnd", return_value=False), \
         patch.object(L, "focus_window", return_value=True) as focus, \
         patch.object(L.os, "startfile", create=True) as startfile:
        assert L.jump_codex_session(123, "project") is True
        focus.assert_called_once_with(456)
        trae.assert_not_called()
        startfile.assert_not_called()


def test_codex_jump_returns_false_when_no_window_exists():
    with patch.object(L, "_terminal_window_for_pid", return_value=None), \
         patch.object(L, "find_window_by_pid", return_value=None) as by_pid, \
         patch.object(L.os, "startfile", create=True) as startfile:
        assert L.jump_codex_session(123, "project") is False
        by_pid.assert_called_once_with(123, unique=True)
        startfile.assert_not_called()


def test_codex_jump_focuses_desktop_codex_window():
    with patch.object(L, "_find_codex_window", return_value=None), \
         patch.object(L, "_terminal_window_for_pid", return_value=None), \
         patch.object(L, "find_window_by_pid", return_value=789), \
         patch.object(L, "_window_process_name", return_value="Codex.exe"), \
         patch.object(L, "focus_window", return_value=True) as focus, \
         patch.object(L, "_is_trae_wnd", return_value=False), \
         patch.object(L.os, "startfile", create=True) as startfile:
        assert L.jump_codex_session(123, "project") is True
        focus.assert_called_once_with(789)
        startfile.assert_not_called()


def test_codex_jump_can_recover_by_codex_window_title():
    with patch.object(L, "_find_codex_window", return_value=789) as by_title, \
         patch.object(L, "_terminal_window_for_pid", return_value=None) as by_console, \
         patch.object(L, "find_window_by_pid", return_value=None), \
         patch.object(L, "focus_window", return_value=True) as focus, \
         patch.object(L, "_is_trae_wnd", return_value=False):
        assert L.jump_codex_session(0, "project", "thread title") is True
        by_title.assert_called_once_with(("thread title | project", "thread title"))
        by_console.assert_not_called()
        focus.assert_called_once_with(789)


def test_codex_jump_does_not_fallback_to_shared_project_after_title_miss():
    with patch.object(L, "_find_codex_window", return_value=None) as by_title, \
         patch.object(L, "_terminal_window_for_pid", return_value=None) as by_console, \
         patch.object(L, "find_window_by_pid", return_value=789) as by_pid, \
         patch.object(L, "focus_window", return_value=True) as focus, \
         patch.object(L, "_is_trae_wnd", return_value=False):
        assert L.jump_codex_session(123, "shared-project", "third-thread") is True
        # A failed title lookup must not immediately search shared-project;
        # that lookup is what previously selected the second WT window.
        by_title.assert_called_once_with(("third-thread | shared-project", "third-thread"))
        by_console.assert_called_once_with(123)
        by_pid.assert_called_once_with(123, unique=True)
        focus.assert_called_once_with(789)


def test_codex_jump_prefers_console_window_over_title():
    with patch.object(L, "_find_codex_window", return_value=None) as by_title, \
         patch.object(L, "_terminal_window_for_pid", return_value=901) as by_console, \
         patch.object(L, "find_window_by_pid", return_value=456) as by_pid, \
         patch.object(L, "focus_window", return_value=True) as focus, \
         patch.object(L, "_is_trae_wnd", return_value=False):
        assert L.jump_codex_session(123, "project", "unique task") is True
        # The console mapping is exact; a title match can pick another window.
        by_console.assert_called_once_with(123)
        by_title.assert_not_called()
        by_pid.assert_not_called()
        focus.assert_called_once_with(901)


def test_codex_jump_uses_title_when_console_misses():
    with patch.object(L, "_find_codex_window", return_value=901) as by_title, \
         patch.object(L, "_terminal_window_for_pid", return_value=None) as by_console, \
         patch.object(L, "find_window_by_pid", return_value=456) as by_pid, \
         patch.object(L, "focus_window", return_value=True) as focus, \
         patch.object(L, "_is_trae_wnd", return_value=False):
        assert L.jump_codex_session(123, "project", "unique task") is True
        by_console.assert_called_once_with(123)
        by_title.assert_called_once_with(("unique task | project", "unique task"))
        by_pid.assert_not_called()
        focus.assert_called_once_with(901)


def test_resolve_session_hwnd_prefers_live_terminal_process():
    with patch.object(L, "find_window_by_pid", return_value=456) as by_pid, \
         patch.object(L, "resolve_hwnd", return_value=123) as by_hwnd:
        assert L.resolve_session_hwnd(123, 999, "project") == 456
        by_pid.assert_called_once_with(999)
        by_hwnd.assert_not_called()


def test_resolve_session_hwnd_falls_back_to_recorded_window():
    with patch.object(L, "find_window_by_pid", return_value=None) as by_pid, \
         patch.object(L, "resolve_hwnd", return_value=123) as by_hwnd:
        assert L.resolve_session_hwnd(123, 999, "project") == 123
        by_pid.assert_called_once_with(999)
        by_hwnd.assert_called_once_with(123, "project")


def test_find_trae_window_falls_back_without_project_title():
    with patch.object(L, "find_window_by_name", side_effect=[None, 456]) as by_name:
        assert L.find_trae_window("project") == 456
        assert by_name.call_args_list[0].args == ("project",)
        assert by_name.call_args_list[0].kwargs == {"process_keyword": "trae"}
        assert by_name.call_args_list[1].args == (None,)
        assert by_name.call_args_list[1].kwargs == {"process_keyword": "trae"}


def test_get_session_title_invalid():
    # 不存在的 sid / jsonl → 空串,不抛
    assert L.get_session_title("nonexistent-sid-zzz") == ""
    assert L._read_session_title("C:/no/such/file.jsonl") == ""


if __name__ == "__main__":
    for k, fn in sorted(globals().items()):
        if k.startswith("test_"):
            fn()
    print("ALL PASS")
