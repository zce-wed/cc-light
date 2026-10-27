# -*- coding: utf-8 -*-
"""会话扫描回归 —— 守门「重启后多出同名幽灵会话、点击跳错窗口」类回归(2026-10-20 起)。

Claude Code daemon 恢复后台会话时会留下两类假活跃文件:
  1. `--bg-pty-host` 包装进程 / `daemon run` 进程被算成「项目进程」,让补建数量 N 虚高;
  2. `--resume` 的源 jsonl 被 fork 时刷新 mtime,但它没有自己的进程。
两者都会让「检测所有会话」补出一个与终端会话同名、termpid 借用别人终端、点击跳错窗口的
scanned 会话。"""
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cc_light as light


def test_parse_claude_cmdline_recognizes_daemon_layers():
    daemon = ('C:\\...\\claude.exe daemon run --origin transient '
              '--spawned-by "{\\"label\\":\\"claude\\",\\"cwd\\":\\"C:\\\\work\\\\demo\\"}"')
    assert light._parse_claude_cmdline(daemon)["kind"] == "daemon"

    pty = ('claude.exe --bg-pty-host \\\\.\\pipe\\cc-daemon-x-pty-9af3164d 120 30 -- '
           'claude.exe --session-id 9af3164d-ce26-42b3-8078-5a49c1a061d6 --fork-session '
           '--resume C:\\Users\\u\\.claude\\projects\\P\\0ca4ef0d-d130-4f49-80f6-26fcc2e42ce4.jsonl')
    info = light._parse_claude_cmdline(pty)
    assert info["kind"] == "ptyhost"
    assert info["sid"] == "9af3164d-ce26-42b3-8078-5a49c1a061d6"
    assert info["resume_sid"] == "0ca4ef0d-d130-4f49-80f6-26fcc2e42ce4"

    session = ('claude.exe --session-id 9af3164d-ce26-42b3-8078-5a49c1a061d6 --fork-session '
               '--resume "C:\\Users\\u\\.claude\\projects\\P\\0ca4ef0d-d130-4f49-80f6-26fcc2e42ce4.jsonl"')
    info = light._parse_claude_cmdline(session)
    assert info["kind"] == "session"
    assert info["sid"] == "9af3164d-ce26-42b3-8078-5a49c1a061d6"
    assert info["resume_sid"] == "0ca4ef0d-d130-4f49-80f6-26fcc2e42ce4"

    plain = light._parse_claude_cmdline("claude.exe --dangerously-skip-permissions")
    assert plain == {"kind": "session", "sid": "", "resume_sid": ""}
    assert light._parse_claude_cmdline(None)["kind"] == "session"


def test_scan_skips_resumed_source_and_background_sessions():
    with tempfile.TemporaryDirectory() as folder:
        home = Path(folder)
        cwd = r"C:\work\demo"
        pdir = home / ".claude" / "projects" / light._encode_path(cwd)
        pdir.mkdir(parents=True)
        now = time.time()
        start = now - 60                       # 终端会话进程启动时间
        terminal_sid = "aaaaaaaa-0000-0000-0000-000000000001"
        resumed_sid = "bbbbbbbb-0000-0000-0000-000000000002"
        bg_sid = "cccccccc-0000-0000-0000-000000000003"
        # terminal:进程启动后写入;resumed:启动前被 fork 触碰;bg:后台会话自己的日志
        for sid, age in ((terminal_sid, 5), (resumed_sid, 90), (bg_sid, 1)):
            p = pdir / (sid + ".jsonl")
            p.write_text("{}", encoding="utf-8")
            os.utime(p, (now - age, now - age))
        procs = [
            {"cwd": cwd.lower(), "pid": 10, "termpid": 100, "kind": "session",
             "sid": "", "resume_sid": "", "start": start},
            {"cwd": cwd.lower(), "pid": 11, "termpid": 200, "kind": "session",
             "sid": bg_sid, "resume_sid": resumed_sid, "start": start + 1},
            {"cwd": cwd.lower(), "pid": 12, "termpid": 300, "kind": "ptyhost",
             "sid": bg_sid, "resume_sid": resumed_sid, "start": start},
            {"cwd": cwd.lower(), "pid": 13, "termpid": 400, "kind": "daemon",
             "sid": "", "resume_sid": "", "start": start},
        ]
        with patch.dict(os.environ, {"USERPROFILE": str(home)}), \
             patch.object(light, "_claude_procs", return_value=procs), \
             patch.object(light, "find_window_by_pid", return_value=0), \
             patch.object(light, "find_window_by_name", return_value=None):
            found = light.scan_active_jsonl(3600)

        assert set(found) == {terminal_sid}
        assert found[terminal_sid]["termpid"] == 100
        assert found[terminal_sid]["name"] == "demo"


def test_scan_ignores_project_with_only_background_sessions():
    with tempfile.TemporaryDirectory() as folder:
        home = Path(folder)
        cwd = r"C:\work\bgonly"
        pdir = home / ".claude" / "projects" / light._encode_path(cwd)
        pdir.mkdir(parents=True)
        sid = "dddddddd-0000-0000-0000-000000000004"
        (pdir / (sid + ".jsonl")).write_text("{}", encoding="utf-8")
        procs = [{"cwd": cwd.lower(), "pid": 20, "termpid": 500, "kind": "session",
                  "sid": sid, "resume_sid": "", "start": time.time() - 30}]
        with patch.dict(os.environ, {"USERPROFILE": str(home)}), \
             patch.object(light, "_claude_procs", return_value=procs), \
             patch.object(light, "find_window_by_pid", return_value=0), \
             patch.object(light, "find_window_by_name", return_value=None):
            assert light.scan_active_jsonl(3600) == {}


def test_background_kind_distinguishes_cc_daemon_from_pm():
    with patch.object(light, "is_process_alive", return_value=True), \
         patch.object(light, "_process_image_name", return_value="claude.exe"):
        assert light.background_kind({"termpid": 123}) == "cc"

    with patch.object(light, "is_process_alive", return_value=True), \
         patch.object(light, "_process_image_name", return_value="node.exe"), \
         patch.object(light, "_is_terminal_session", return_value=False):
        assert light.background_kind({"termpid": 123}) == "pm"

    with patch.object(light, "is_process_alive", return_value=True), \
         patch.object(light, "_process_image_name", return_value="cmd.exe"), \
         patch.object(light, "_is_terminal_session", return_value=True):
        assert light.background_kind({"termpid": 123}) == ""

    assert light.background_kind({"bg": True}) == "pm"
    assert light.background_kind({"scanned": True, "hwnd": 0, "termpid": 0}) == "pm"
    assert light.background_kind(None) == ""


if __name__ == "__main__":
    test_parse_claude_cmdline_recognizes_daemon_layers()
    test_scan_skips_resumed_source_and_background_sessions()
    test_scan_ignores_project_with_only_background_sessions()
    test_background_kind_distinguishes_cc_daemon_from_pm()
    print("ALL PASS")
