#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cc-light hooks 安装器 —— 把交通灯 hook 注入 ~/.claude/settings.json

用法:
  python install-hooks.py install     备份并注入 9 条 hook(幂等,可重复执行)
  python install-hooks.py uninstall   移除所有 cc-light hook
  python install-hooks.py status      查看注入情况

为什么装用户级 settings.json,还靠灯窗口自愈?
  用户的 ~/.claude 被坚果云双向同步,settings.json 会被「另一台机器的配置」
  整体覆盖、冲掉 cc-light hook(表现:同项目多窗口只亮一个灯)。
  managed-settings.json 虽优先级最高、免疫覆盖,但 Windows 下在
  C:\\Program Files\\ClaudeCode\\,需管理员才能写,不便。
  故:hook 装用户级 settings.json(无需管理员),由灯窗口定期自愈重注入
  (cc_light.py 的 ensure_hooks)抵消覆盖;运行数据移到 %APPDATA%\\cc-light,
  不被同步清空。

注入的事件 → 灯色:
  SessionStart     → green   会话就绪(空闲)
  UserPromptSubmit → yellow  你刚发消息,Claude 开干
  Stop             → green   回合结束
  PermissionRequest→ red     Claude 需要你确认 / 输入
"""
import json
import os
import shutil
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HOME = os.path.expanduser("~")
SETTINGS = os.path.join(HOME, ".claude", "settings.json")
DIR = os.path.dirname(os.path.abspath(__file__))
MARKER = "cc-light/"

# managed-settings.json 的可能路径 —— 仅用于清理历史误装,不再用作注入目标
MANAGED_PATHS = [
    r"C:\ProgramData\ClaudeCode\managed-settings.json",
    r"C:\Program Files\ClaudeCode\managed-settings.json",
    "/Library/Application Support/ClaudeCode/managed-settings.json",
    "/etc/claude-code/managed-settings.json",
]

# 事件 -> (动作, matcher, async)。动作 = 灯色 / "end"(删会话) / sub_start/sub_stop。
# 关键决策(基于官方 hooks 文档 + 实测):
#  - 红用 PermissionRequest(权限对话框出现时),不用 Notification ——
#    Notification 还含 idle_prompt(闲置等待时触发),会把空闲误判成红灯常亮。
#  - 不挂 PreToolUse。曾挂它(async)写入迟于 Stop、覆盖绿灯,导致会话结束后灯一直闪黄。
#  - SessionEnd 用 end 删该会话文件,避免已关闭窗口状态残留(失败靠超时清理兜底)。
INJECT = {
    "SessionStart":      ("green",      "*", False),
    "UserPromptSubmit":  ("yellow",     "*", False),
    "Stop":              ("green",      "*", False),
    "PermissionRequest": ("red",        "*", False),
    "PostToolUse":       ("yellow", "AskUserQuestion|ExitPlanMode", False),
    "Notification":      ("green", "idle_prompt", False),
    "SubagentStart":     ("sub_start", "*", False),
    "SubagentStop":      ("sub_stop",  "*", False),
    "SessionEnd":        ("end",        "*", False),
}


def find_node():
    for name in ("node.exe", "node"):
        p = shutil.which(name)
        if p:
            return p.replace("\\", "/")
    return "node"


HOOK_JS = os.path.join(DIR, "hook.js").replace("\\", "/")


def entry(node, action, matcher="*", timeout=10, async_=False):
    if action == "end":
        cmd = '"%s" "%s" end' % (node, HOOK_JS)
    else:
        cmd = '"%s" "%s" %s' % (node, HOOK_JS, action)
    h = {"type": "command", "command": cmd, "timeout": timeout}
    if async_:
        h["async"] = True
    return {"matcher": matcher, "hooks": [h]}


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_json(data, path):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return True
    except OSError as e:
        sys.stderr.write("[!] 写入 %s 失败: %s\n" % (path, e))
        return False


def strip_cc_light(hooks):
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


def inject_into(data):
    """就地注入 cc-light hook 到 data['hooks']"""
    hooks = data.setdefault("hooks", {})
    strip_cc_light(hooks)
    node = find_node()
    for evt, (color, matcher, async_) in INJECT.items():
        hooks.setdefault(evt, []).append(entry(node, color, matcher, async_=async_))


def backup(path):
    if not os.path.exists(path):
        return None
    bak = path + ".bak-cc-light-" + time.strftime("%Y%m%d-%H%M%S")
    try:
        shutil.copy2(path, bak)
    except OSError:
        return None
    return bak


def clean_managed():
    """清理历史误装到 managed-settings.json 的 cc-light hook(不再用 managed 方案)"""
    for path in MANAGED_PATHS:
        if not os.path.exists(path):
            continue
        data = load_json(path)
        hooks = data.get("hooks", {})
        if not (isinstance(hooks, dict) and count_cc_light(hooks)[0]):
            continue
        strip_cc_light(hooks)
        data["hooks"] = hooks
        try:
            if not any(v for v in data.values()):   # 删完空了 → 删整个文件
                os.remove(path)
            else:
                save_json(data, path)
            print("[OK] 已清理 managed 残留: %s" % path)
        except OSError:
            print("[!] 无权清理 %s(需管理员,可忽略)" % path)


def cmd_install():
    clean_managed()
    if not os.path.exists(SETTINGS):
        sys.stderr.write("[!] 找不到 %s\n" % SETTINGS)
        return 1
    data = load_json(SETTINGS)
    bak = backup(SETTINGS)
    inject_into(data)
    if not save_json(data, SETTINGS):
        return 1
    print("[OK] 已注入 cc-light hook 到 settings.json:")
    print("       %s" % SETTINGS)
    for evt in INJECT:
        print("       %-18s -> %s" % (evt, INJECT[evt]))
    print("       node: %s" % find_node())
    if bak:
        print("[BAK] 备份: %s" % bak)
    print("[TIP] 新开窗口立即生效;老窗口需重启。坚果云若覆盖 settings.json,灯窗口会自愈重注入。")
    return 0


def cmd_uninstall():
    clean_managed()
    removed = 0
    if os.path.exists(SETTINGS):
        data = load_json(SETTINGS)
        hooks = data.get("hooks", {})
        if isinstance(hooks, dict) and count_cc_light(hooks)[0]:
            backup(SETTINGS)
            strip_cc_light(hooks)
            data["hooks"] = hooks
            save_json(data, SETTINGS)
            removed = count_cc_light(load_json(SETTINGS).get("hooks", {}))[0]
            print("[OK] 已从 settings.json 移除 cc-light hook")
    if not removed:
        print("[--] 未发现 cc-light hook")
    return 0


def cmd_status():
    print("settings.json: %s" % SETTINGS)
    n, detail = count_cc_light(load_json(SETTINGS).get("hooks", {}))
    if n:
        print("[OK] 已注入 %d 条 cc-light hook:" % n)
        for evt, c in detail.items():
            print("       %-18s x %d" % (evt, c))
    else:
        print("[--] settings.json 无 cc-light hook(未安装或被同步冲掉)")
    for path in MANAGED_PATHS:
        if os.path.exists(path) and count_cc_light(load_json(path).get("hooks", {}))[0]:
            print("[!] 残留 managed hook: %s(建议 install 清理)" % path)
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "uninstall", "status"):
        sys.stderr.write("usage: install-hooks.py install|uninstall|status\n")
        return 2
    return {"install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status}[sys.argv[1]]()


if __name__ == "__main__":
    sys.exit(main())
