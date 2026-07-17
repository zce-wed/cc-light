# -*- coding: utf-8 -*-
"""跳转链路纯函数回归 —— 守门「点击会话跳不到窗口」类回归(2026-07-16 起)。
Windows API 依赖运行环境,这里只测纯逻辑分支(空值/死 pid 不崩 + 合理返回);
端到端用 `python cc_light.py --diag` 验证当前所有会话能定位窗口。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cc_light as L


def test_find_window_by_name_empty():
    assert L.find_window_by_name(None) is None
    assert L.find_window_by_name("") is None
    assert L.find_window_by_name("x") is None          # len<2 守卫


def test_find_window_by_pid_invalid():
    assert L.find_window_by_pid(0) is None
    assert L.find_window_by_pid(None) is None


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


def test_get_session_title_invalid():
    # 不存在的 sid / jsonl → 空串,不抛
    assert L.get_session_title("nonexistent-sid-zzz") == ""
    assert L._read_session_title("C:/no/such/file.jsonl") == ""


if __name__ == "__main__":
    for k, fn in sorted(globals().items()):
        if k.startswith("test_"):
            fn()
    print("ALL PASS")
