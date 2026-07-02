#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cc-light hooks 安装器 —— 把交通灯 hook 注入 ~/.claude/settings.json

用法:
  python install-hooks.py install     备份并注入 4 条 hook(幂等,可重复执行)
  python install-hooks.py uninstall   移除所有 cc-light hook
  python install-hooks.py status      查看注入情况

注入的事件 → 灯色:
  SessionStart     → yellow  会话开始
  UserPromptSubmit → yellow  你刚发消息,Claude 开干
  Notification     → red     Claude 需要你确认 / 输入
  Stop             → green   回合结束,空闲
"""
import json
import os
import shutil
import sys
import time

# 让中文在 Windows 终端正常输出(避免默认 GBK 乱码)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HOME = os.path.expanduser("~")
SETTINGS = os.path.join(HOME, ".claude", "settings.json")
DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(DIR, "cc_light.py").replace("\\", "/")
MARKER = "cc-light/"   # 通用前缀:既能 strip 旧 pythonw 版,也能 strip 新 node 版

# 事件 -> (动作, matcher, async)。动作 = 灯色 或 "end"(SessionEnd 删会话)。
# 多会话:每条 hook 调 `cc_light.py --hook <色>`,从 stdin 读 session_id/cwd,
# 写 sessions/<sid>.json;灯窗口扫描后按优先级聚合(红>黄>绿)。
# 关键决策(基于官方 hooks 文档 + 实测):
#  - 红用 PermissionRequest(权限对话框出现时),不用 Notification ——
#    Notification 还含 idle_prompt(Claude 闲置等待时触发),会把空闲误判成红灯常亮。
#  - 不挂 PreToolUse。曾挂它(async)写入迟于 Stop、覆盖绿灯,导致会话结束后灯一直闪黄。
#  - SessionEnd 用 --end 删该会话文件,避免已关闭窗口状态残留(失败靠 6h 过期清理兜底)。
INJECT = {
    "SessionStart":      ("yellow", "*", False),
    "UserPromptSubmit":  ("yellow", "*", False),
    "Stop":              ("green",  "*", False),
    "PermissionRequest": ("red",    "*", False),
    "Notification":      ("green",  "idle_prompt", False),  # 答完/中断后等你 → 绿(Stop 不触发用户中断)
    "SessionEnd":        ("end",    "*", False),
}


def find_node():
    """node 用来跑 hook.js(读 stdin 比 pythonw 在 Windows 上稳定)"""
    for name in ("node.exe", "node"):
        p = shutil.which(name)
        if p:
            return p.replace("\\", "/")
    return "node"


HOOK_JS = os.path.join(DIR, "hook.js").replace("\\", "/")


def entry(node, action, matcher="*", timeout=5, async_=False):
    # action = 灯色(red/yellow/green)或 "end"(SessionEnd 删会话)
    if action == "end":
        cmd = '"%s" "%s" end' % (node, HOOK_JS)
    else:
        cmd = '"%s" "%s" %s' % (node, HOOK_JS, action)
    h = {"type": "command", "command": cmd, "timeout": timeout}
    if async_:
        h["async"] = True
    return {"matcher": matcher, "hooks": [h]}


def load():
    with open(SETTINGS, "r", encoding="utf-8") as f:
        return json.load(f)


def save(data):
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def strip_cc_light(hooks):
    """移除所有 command 含 cc-light 标记的 hook entry(就地修改)"""
    for evt, arr in list(hooks.items()):
        if not isinstance(arr, list):
            continue
        hooks[evt] = [e for e in arr if MARKER not in json.dumps(e, ensure_ascii=False)]


def count_cc_light(hooks):
    n = 0
    detail = {}
    for evt, arr in hooks.items():
        if not isinstance(arr, list):
            continue
        c = sum(1 for e in arr if MARKER in json.dumps(e, ensure_ascii=False))
        if c:
            detail[evt] = c
            n += c
    return n, detail


def backup():
    bak = SETTINGS + ".bak-cc-light-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(SETTINGS, bak)
    return bak


def cmd_install():
    data = load()
    hooks = data.setdefault("hooks", {})
    strip_cc_light(hooks)              # 先清掉旧的,保证幂等
    node = find_node()
    for evt, (color, matcher, async_) in INJECT.items():
        hooks.setdefault(evt, []).append(entry(node, color, matcher, async_=async_))
    bak = backup()                     # 备份的是磁盘原文件(save 之前)
    save(data)
    print("[OK] 已注入 cc-light hook,事件映射:")
    for evt in INJECT:
        print("       %-18s -> %s" % (evt, INJECT[evt]))
    print("       node: %s" % node)
    print("[BAK] 备份: %s" % bak)
    print("[TIP] 重启 Claude Code 后生效(本会话已加载旧配置)。")


def cmd_uninstall():
    data = load()
    hooks = data.setdefault("hooks", {})
    before, _ = count_cc_light(hooks)
    bak = backup()
    strip_cc_light(hooks)
    save(data)
    after, _ = count_cc_light(data["hooks"])
    print("[OK] 移除 %d 条,剩余 %d 条。" % (before - after, after))
    print("[BAK] 备份: %s" % bak)


def cmd_status():
    data = load()
    hooks = data.get("hooks", {})
    n, detail = count_cc_light(hooks)
    print("settings.json: %s" % SETTINGS)
    if n:
        print("[OK] 已注入 %d 条 cc-light hook:" % n)
        for evt, c in detail.items():
            print("       %-18s x %d" % (evt, c))
    else:
        print("[--] 未发现 cc-light hook(未安装)")
    print("\n各事件 hook 总数(含其他插件,供核对没误伤):")
    for evt in sorted(hooks):
        arr = hooks[evt]
        if isinstance(arr, list):
            print("       %-20s %d" % (evt, len(arr)))


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "uninstall", "status"):
        sys.stderr.write("usage: install-hooks.py install|uninstall|status\n")
        return 2
    if not os.path.exists(SETTINGS):
        sys.stderr.write("找不到 settings.json: %s\n" % SETTINGS)
        return 1
    {"install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status}[sys.argv[1]]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
