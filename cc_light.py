#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cc-light —— Claude Code 桌面悬浮交通灯(多会话优先级聚合)

用法:
  pythonw cc_light.py                              启动悬浮灯窗口
  python  cc_light.py --hook red|yellow|green      hook 用:读 stdin 取 session_id/cwd
  python  cc_light.py --end                        SessionEnd 用:删该会话
  python  cc_light.py --set red|yellow|green|gray  手动:写/清 _manual 会话
  python  cc_light.py --state                      打印聚合态 + 会话数

聚合:任一 red > 任一 yellow > 任一 green > gray
"""
import sys
import os
import json
import time
import math

DIR = os.path.dirname(os.path.abspath(__file__))
# 运行数据放 %APPDATA%\cc-light —— ~/.claude 被坚果云同步会清空 sessions,必须移出
DATA_DIR = os.path.join(os.environ.get("APPDATA") or DIR, "cc-light")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
ARCHIVE_DIR = os.path.join(DATA_DIR, "archive")   # 归档被删会话,供「检测所有会话」还原
POS_FILE = os.path.join(DATA_DIR, "pos.json")
MISS_LOG = os.path.join(DATA_DIR, "hook-miss.log")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
NOTES_FILE = os.path.join(DATA_DIR, "notes.json")                  # 便签/历史窗口几何(位置+大小)
NOTES_HISTORY_FILE = os.path.join(DATA_DIR, "notes_history.json")  # 快捷记录历史条目数组(末尾最新)
MAX_HISTORY = 500   # 历史上限(超出截断最旧的)
STALE = 1 * 3600        # 1 小时无更新 → 归档(可被「检测所有会话」还原)
SCAN_ACTIVE = 6 * 3600  # 「检测所有会话」:jsonl 6h 内有写入视为会话仍开着(覆盖午休等长空闲)
DEFAULT_CONFIG = {"yellow_timeout": 30, "timeout_fallback": True}    # 默认 30 秒(PostToolUse 心跳刷 ts,中断/卡住 30s 降绿)

# ---- 自愈:坚果云覆盖 settings.json 冲掉 hook 时,灯窗口定期重注入 ----
MARKER = "cc-light/"   # hook 命令路径里都含这个,用于识别/清理 cc-light entry
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
HOOK_JS_SELF = os.path.join(DIR, "hook.js").replace("\\", "/")
SELF_INJECT = {   # 与 install-hooks.py 的 INJECT 保持一致
    "SessionStart": ("green", "*"), "UserPromptSubmit": ("yellow", "*"),
    "Stop": ("green", "*"), "PermissionRequest": ("red", "*"),
    "PostToolUse": ("yellow_soft", "*"),   # 工具心跳:刷 ts,长任务不超时;SOFT 跳过 getMeta
    "Notification": ("green", "idle_prompt"),
    "SubagentStart": ("sub_start", "*"), "SubagentStop": ("sub_stop", "*"),
    "SessionEnd": ("end", "*"),
}
_config = dict(DEFAULT_CONFIG)


def load_config():
    global _config
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            _config = {**DEFAULT_CONFIG, **json.load(f)}
    except Exception:
        _config = dict(DEFAULT_CONFIG)


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(_config, f)
    except Exception:
        pass


load_config()

COLORS = {
    "red":    {"on": "#ff4d4d", "dim": "#3a1416"},
    "yellow": {"on": "#f5a623", "dim": "#3a2e0e", "mid": "#9c6510"},
    "green":  {"on": "#34d058", "dim": "#0e3a1c"},
}
ORDER = ["red", "yellow", "green"]
PRIO = {"red": 0, "yellow": 1, "green": 2}
STATE_WORD = {"red": "等确认", "yellow": "运行中", "green": "完成", "gray": "待机"}
BG = "#161618"
BORDER = "#34343a"
MAGIC = "#010109"   # 透明色(不能与任何可见色相同)


def ensure_dir():
    try:
        os.makedirs(SESSIONS_DIR, exist_ok=True)
    except OSError:
        pass


# ---------------- per-session 读写 ----------------
def write_session(sid, state, msg="", name=""):
    ensure_dir()
    safe = "".join(c for c in sid if c.isalnum() or c in ("_", "-")) or "unknown"
    path = os.path.join(SESSIONS_DIR, safe + ".json")
    data = {"state": state, "msg": msg or "", "ts": time.time(), "name": name or ""}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def delete_session(sid):
    safe = "".join(c for c in sid if c.isalnum() or c in ("_", "-")) or "unknown"
    try:
        os.remove(os.path.join(SESSIONS_DIR, safe + ".json"))
    except (FileNotFoundError, OSError):
        pass


def _encode_home_prefix():
    """用户主目录 → Claude Code projects 目录的编码前缀(非字母数字字符逐个替成 '-')。
    如 C:\\Users\\zhongce-pm → C--Users-zhongce-pm,用于从编码目录名剥掉主目录前缀。"""
    home = os.environ.get("USERPROFILE") or ""
    return "".join(c if c.isalnum() else "-" for c in home)


def proj_name(enc_dir):
    """编码项目目录名(C--Users-...-zc-geo-frontend)→ 可读项目名(剥主目录前缀,保留剩余相对路径)。"""
    pre = _encode_home_prefix()
    name = enc_dir[len(pre):].lstrip("-") if pre and enc_dir.startswith(pre) else enc_dir
    return name or enc_dir or "?"


def _encode_path(path):
    """真实路径 → Claude Code projects 目录编码(非 [字母数字_-] 逐字符替成 -)。
    C:\\Users\\...\\zc-geo-frontend → C--Users-...-zc-geo-frontend。"""
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in (path or ""))


def _claude_proc_cwds():
    """当前所有 claude.exe 进程的工作目录 → 进程数(collections.Counter)。
    用 ctypes NtQueryInformationProcess 读 PEB 拿 cwd(无依赖,精确判活) —— 进程 cwd 即
    会话项目目录,能区分『开着』和『刚关』(后者无 claude.exe 进程)。读失败返回空 Counter。"""
    from collections import Counter
    cnt = Counter()
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        ntdll = ctypes.windll.ntdll
        psapi = ctypes.windll.psapi
        DWORD = ctypes.c_uint32
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        k.ReadProcessMemory.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]

        class PBI(ctypes.Structure):
            _fields_ = [("r1", ctypes.c_void_p), ("peb", ctypes.c_void_p),
                        ("r2", ctypes.c_void_p * 2), ("pid", ctypes.c_void_p), ("r3", ctypes.c_void_p)]

        arr = (DWORD * 4096)()
        ret = DWORD()
        if not psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(ret)):
            return cnt
        for i in range(ret.value):
            pid = arr[i]
            if pid == 0:
                continue
            h = k.OpenProcess(0x1000, False, pid)        # QUERY_LIMITED_INFORMATION:够拿 image name
            if not h:
                continue
            try:
                img = ctypes.create_unicode_buffer(260)
                n = DWORD(260)
                if not (k.QueryFullProcessImageNameW(h, 0, img, ctypes.byref(n)) and img.value.lower().endswith("claude.exe")):
                    continue
            finally:
                k.CloseHandle(h)
            h2 = k.OpenProcess(0x410, False, pid)        # QUERY_INFORMATION | VM_READ:读 PEB 拿 cwd
            if not h2:
                continue
            try:
                pbi = PBI()
                if ntdll.NtQueryInformationProcess(h2, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None):
                    continue
                peb = pbi.peb
                if not peb:
                    continue
                pp = ctypes.c_void_p(); rl = ctypes.c_size_t()
                if not k.ReadProcessMemory(h2, ctypes.c_void_p(peb + 0x20), ctypes.byref(pp), ctypes.sizeof(pp), ctypes.byref(rl)):
                    continue
                length = ctypes.c_ushort(); bufptr = ctypes.c_void_p()
                k.ReadProcessMemory(h2, ctypes.c_void_p(pp.value + 0x38), ctypes.byref(length), 2, ctypes.byref(rl))
                k.ReadProcessMemory(h2, ctypes.c_void_p(pp.value + 0x40), ctypes.byref(bufptr), ctypes.sizeof(bufptr), ctypes.byref(rl))
                if not bufptr.value or length.value < 2:
                    continue
                buf = ctypes.create_unicode_buffer(length.value // 2 + 1)
                if not k.ReadProcessMemory(h2, bufptr, buf, length.value, ctypes.byref(rl)):
                    continue
                cwd = buf.value.rstrip("\\/").lower()
                if cwd:
                    cnt[cwd] += 1
            except Exception:
                pass
            finally:
                k.CloseHandle(h2)
    except Exception:
        pass
    return cnt


def scan_active_jsonl(threshold_sec):
    """扫 ~/.claude/projects/<proj>/<sid>.jsonl,只保留【当前有 claude.exe 进程在跑】的项目
    (按进程 cwd 精确判活 —— 没进程的项目即已关,不补,避免误补刚关的旧会话)。同项目按其
    claude 进程数取最新 N 个 jsonl(PM 开 2 个就取 2 个)。返回 {sid: {"name", "mtime"}}。"""
    base = os.path.join(os.environ.get("USERPROFILE") or DIR, ".claude", "projects")
    cwd_cnt = _claude_proc_cwds()                 # cwd(小写) -> claude 进程数
    active_enc = {}                               # 编码项目目录(小写) -> 进程数
    for cwd, c in cwd_cnt.items():
        active_enc[_encode_path(cwd).lower()] = c
    out = {}
    now = time.time()
    try:
        projects = os.listdir(base)
    except OSError:
        return out
    for proj in projects:
        n = active_enc.get(proj.lower())
        if not n:                                 # 该项目无活 claude 进程 → 跳过
            continue
        pdir = os.path.join(base, proj)
        if not os.path.isdir(pdir):
            continue
        cands = []
        try:
            for fn in os.listdir(pdir):
                if not fn.endswith(".jsonl"):
                    continue
                path = os.path.join(pdir, fn)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if now - mtime > threshold_sec:
                    continue
                cands.append((mtime, fn[:-6]))
        except OSError:
            continue
        cands.sort(reverse=True)                  # 最新在前
        for mtime, sid in cands[:n]:              # 该项目取最新 n 个(= 其 claude 进程数)
            out[sid] = {"name": proj_name(proj), "mtime": mtime}
    return out


def _write_session_file(sid, d):
    """写 session 文件(灯窗口单线程,普通写即可)。"""
    safe = "".join(c for c in sid if c.isalnum() or c in ("_", "-")) or "unknown"
    path = os.path.join(SESSIONS_DIR, safe + ".json")
    ensure_dir()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except OSError:
        pass


def archive_session(sid):
    """把会话移到归档(不真删),供「检测所有会话」还原。会话真正结束(--end)走 delete_session。"""
    safe = "".join(c for c in sid if c.isalnum() or c in ("_", "-")) or "unknown"
    src = os.path.join(SESSIONS_DIR, safe + ".json")
    if not os.path.exists(src):
        return
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        os.replace(src, os.path.join(ARCHIVE_DIR, safe + ".json"))
    except OSError:
        pass


def read_sessions(now=None):
    ensure_dir()
    now = now if now is not None else time.time()
    out = {}
    try:
        files = os.listdir(SESSIONS_DIR)
    except OSError:
        return out
    for fn in files:
        if not fn.endswith(".json"):
            continue
        path = os.path.join(SESSIONS_DIR, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if not d.get("scanned") and not is_session_alive(d):   # scanned(扫 jsonl 补建的,hwnd/termpid=0)跳过归档,生命周期由「检测所有会话」按 jsonl 活跃度管
                try:
                    os.makedirs(ARCHIVE_DIR, exist_ok=True)
                    os.replace(path, os.path.join(ARCHIVE_DIR, fn))
                except OSError:
                    pass
                continue
            out[fn[:-5]] = d
        except Exception:
            continue
    return out


def effective_state(d, now=None):
    """单会话有效状态(按 config 应用超时降级)"""
    s = d.get("state", "gray")
    if (s == "yellow" and _config["timeout_fallback"]
            and (now if now is not None else time.time()) - d.get("ts", 0) > _config["yellow_timeout"]):
        s = "green"
    return s


def aggregate(sessions):
    if not sessions:
        return "gray", 0
    now = time.time()
    states = [effective_state(d, now) for d in sessions.values()]
    best = min((PRIO.get(s, 99) for s in states), default=99)
    inv = {v: k for k, v in PRIO.items()}
    return inv.get(best, "gray"), len(sessions)


def _find_node():
    import shutil as _sh
    for n in ("node.exe", "node"):
        p = _sh.which(n)
        if p:
            return p.replace("\\", "/")
    return "node"


def _self_entry(action, matcher):
    node = _find_node()
    if action == "end":
        cmd = '"%s" "%s" end' % (node, HOOK_JS_SELF)
    else:
        cmd = '"%s" "%s" %s' % (node, HOOK_JS_SELF, action)
    return {"matcher": matcher, "hooks": [{"type": "command", "command": cmd, "timeout": 10}]}


def ensure_hooks():
    """坚果云会覆盖 settings.json 冲掉 cc-light hook;检测到缺失就重注入(灯窗口周期调用)。"""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return
    have = sum(1 for evt in SELF_INJECT
               if MARKER in json.dumps(hooks.get(evt, []), ensure_ascii=False))
    if have >= len(SELF_INJECT):
        return   # 9 条都在,无需重注入
    for evt in list(hooks.keys()):
        if isinstance(hooks[evt], list):
            hooks[evt] = [e for e in hooks[evt] if MARKER not in json.dumps(e, ensure_ascii=False)]
    for evt, (action, matcher) in SELF_INJECT.items():
        hooks.setdefault(evt, []).append(_self_entry(action, matcher))
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except Exception:
        pass


def read_stdin_json():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def log_miss(reason, raw):
    try:
        with open(MISS_LOG, "a", encoding="utf-8") as f:
            f.write("%s %s raw=%r\n" % (time.time(), reason, raw))
    except Exception:
        pass


# ---------------- 快捷记录:历史 / 窗口几何 ----------------
def load_history():
    try:
        with open(NOTES_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_history(items):
    """原子写历史(末尾最新)。ensure_dir 保证 DATA_DIR 存在。"""
    try:
        ensure_dir()
        tmp = NOTES_HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp, NOTES_HISTORY_FILE)
    except Exception:
        pass


def load_notes_geo():
    try:
        with open(NOTES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_notes_geo(kind, x, y, w, h):
    """kind: 'note'(便签)或 'history'(历史窗口)。"""
    try:
        ensure_dir()
        d = load_notes_geo()
        d[kind] = {"x": x, "y": y, "w": w, "h": h}
        with open(NOTES_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


def usage():
    sys.stderr.write("usage: cc_light.py [--hook color | --end | --set color [msg] | --state]\n")


# ---------------- CLI ----------------
def cli():
    a = sys.argv[1:]
    if not a:
        return run_gui()
    if a[0] == "--state":
        c, n = aggregate(read_sessions())
        print("%s %d" % (c, n))
        return 0
    if a[0] == "--hook":
        if len(a) < 2 or a[1] not in COLORS:
            usage()
            return 2
        d = read_stdin_json()
        sid = d.get("session_id")
        if not sid:
            log_miss("miss-sid", d)   # CC 偶尔没传 session_id,不写文件避免污染聚合
            return 0
        cwd = d.get("cwd") or ""
        name = os.path.basename(cwd.replace("\\", "/").rstrip("/")) or sid[:8]
        write_session(sid, a[1], "", name)
        return 0
    if a[0] == "--end":
        d = read_stdin_json()
        sid = d.get("session_id")
        if sid:
            delete_session(sid)
        return 0
    if a[0] == "--set":
        if len(a) < 2 or (a[1] not in COLORS and a[1] != "gray"):
            usage()
            return 2
        if a[1] == "gray":
            delete_session("_manual")
        else:
            write_session("_manual", a[1], a[2] if len(a) > 2 else "", "手动")
        return 0
    usage()
    return 2


def human_ago(ts):
    if not ts:
        return ""
    d = time.time() - ts
    if d < 0:
        return "刚刚"
    if d < 60:
        return "%ds前" % int(d)
    if d < 3600:
        return "%dm前" % int(d / 60)
    if d < 86400:
        return "%dh前" % int(d / 3600)
    return "%dd前" % int(d / 86400)


# ---------------- Windows 辅助 ----------------
def set_dpi_aware():
    """让 tkinter 用物理像素,复位坐标和工作区 API 一致"""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def set_app_user_model_id(appid):
    """给进程设独立 AppUserModelID —— 任务栏按 AppID 缓存图标,不设的话会沿用 pythonw.exe 的 python 图标
    (这是 Python GUI 在 Windows 任务栏显示 python 图标的根因)。必须在创建窗口前调用。"""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)
    except Exception:
        pass


def frame_to_taskbar(win):
    """去掉系统标题栏/边框(保自定义 drag bar 外观),但让窗口出现在任务栏 ——
    这样 win.iconify() 能最小化到任务栏、点任务栏图标能复原(像普通应用)。
    overrideredirect 窗口是 WS_POPUP 不进任务栏,故用普通 Toplevel + 改样式实现。仅 Windows 有效。"""
    try:
        import ctypes
        win.update_idletasks()
        u = ctypes.windll.user32
        u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        u.GetAncestor.restype = ctypes.c_void_p
        u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        u.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        u.SetWindowLongW.restype = ctypes.c_long
        u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        hwnd = u.GetAncestor(win.winfo_id(), 3) or win.winfo_id()   # GA_ROOTOWNER=3 → 真正的顶层 HWND
        GWL_STYLE = -16
        WS_CAPTION = 0x00C00000
        WS_THICKFRAME = 0x00040000
        WS_SYSMENU = 0x00080000
        WS_MAXIMIZEBOX = 0x00010000
        style = u.GetWindowLongW(hwnd, GWL_STYLE)
        style &= ~(WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MAXIMIZEBOX)
        u.SetWindowLongW(hwnd, GWL_STYLE, style)
        SWP_NOMOVE = 0x0002; SWP_NOSIZE = 0x0001; SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010; SWP_FRAMECHANGED = 0x0020
        u.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                       SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
    except Exception:
        pass


_NOTE_ICON_REF = [None]   # 持有便签图标 PhotoImage,防被 GC(否则任务栏图标变回默认羽毛)


def _note_icon():
    """程序生成的便签图标(32x32):深底融入 Win11 深色任务栏 + 亮黄便签折角 + 三条记录线。
    需 Tk 已初始化(在 run_gui 内调用)。"""
    if _NOTE_ICON_REF[0] is not None:
        return _NOTE_ICON_REF[0]
    import tkinter
    W = H = 32
    bg = "#1f1f22"       # 近 Win11 深色任务栏,任务栏上融入背景
    note = "#f5a623"     # 交通灯黄
    fold = "#b9791a"
    img = tkinter.PhotoImage(width=W, height=H)
    img.put(bg, to=(0, 0, W, H))
    img.put(note, to=(6, 5, 26, 26))      # 便签主体
    img.put(fold, to=(20, 5, 26, 11))     # 右上折角
    img.put(bg, to=(9, 12, 23, 13))       # 三条横线(记录)
    img.put(bg, to=(9, 16, 23, 17))
    img.put(bg, to=(9, 20, 23, 21))
    _NOTE_ICON_REF[0] = img
    return img


_AUTO_ICO = os.path.join(DATA_DIR, "note-yellow.ico")   # 黄色便签图标缓存 .ico


def _make_yellow_note_ico(path, size=32):
    """生成黄色便签图标 .ico(透明背景 + 亮黄便签折角 + 深色记录线),供 iconbitmap。"""
    import struct
    W = H = size
    NOTE = (0xf5, 0xa6, 0x23)   # 亮黄(交通灯黄)
    FOLD = (0xb9, 0x79, 0x1a)   # 折角暗黄
    LINE = (0x2a, 0x2a, 0x2e)   # 记录线深色

    def bgra(x, y):
        if 6 <= x < 26 and 5 <= y < 26:                 # 便签主体
            if 20 <= x < 26 and 5 <= y < 11:            # 右上折角
                r, g, b = FOLD
            elif 9 <= x < 23 and (12 <= y < 13 or 16 <= y < 17 or 20 <= y < 21):  # 三条记录线
                r, g, b = LINE
            else:
                r, g, b = NOTE
            return bytes((b, g, r, 255))                # BGRA,不透明
        return bytes((0, 0, 0, 0))                      # 透明背景

    cb = bytearray()
    for y in range(H - 1, -1, -1):                      # ICO 位图 bottom-up
        for x in range(W):
            cb += bgra(x, y)
    mask_row = ((W + 31) // 32) * 4
    dib_header = struct.pack("<IiiHHIIiiII", 40, W, H * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    dib = dib_header + bytes(cb) + (b"\x00" * (mask_row * H))
    entry = struct.pack("<BBBBHHII", W, H, 0, 0, 1, 32, len(dib), 22)
    with open(path, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, 1) + entry + dib)
    return True


def _ensure_auto_ico():
    """生成黄色便签 .ico 到 AppData(缓存,只生成一次),返回路径或 None。"""
    if os.path.exists(_AUTO_ICO):
        return _AUTO_ICO
    try:
        ensure_dir()
        if _make_yellow_note_ico(_AUTO_ICO):
            return _AUTO_ICO
    except Exception:
        pass
    return None


def _set_note_icon(win):
    """任务栏图标,优先级:note.ico > 黄色便签 ico(iconbitmap)> iconphoto 兜底。
    iconbitmap 是 tkinter 原生,配合 AppUserModelID 任务栏会正确显示(不回退 python)。"""
    try:
        ico = os.path.join(DIR, "note.ico")
        if os.path.exists(ico):
            win.iconbitmap(ico)                       # 用户自备 ico,最高优先
            return
    except Exception:
        pass
    auto = _ensure_auto_ico()
    if auto:
        try:
            win.iconbitmap(auto)                      # 黄色便签图标(iconbitmap)
            return
        except Exception:
            pass
    try:
        win.iconphoto(True, _note_icon())             # 兜底:程序生成图标
    except Exception:
        pass


def work_area():
    """Windows 工作区(排除任务栏)-> (left, top, right, bottom)"""
    try:
        import ctypes
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
        return r.left, r.top, r.right, r.bottom
    except Exception:
        return None


def focus_window(hwnd):
    """把指定 HWND 的窗口恢复+前置(点击会话名跳转到对应终端)。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = int(hwnd)
        if not hwnd or not u.IsWindow(hwnd):
            return False
        if u.IsIconic(hwnd):                # 最小化则恢复
            u.ShowWindow(hwnd, 9)           # SW_RESTORE
        cur = u.GetWindowThreadProcessId(u.GetForegroundWindow(), 0)
        tgt = u.GetWindowThreadProcessId(hwnd, 0)
        if cur and tgt and cur != tgt:      # AttachThreadInput 绕过 SetForegroundWindow 前台限制
            u.AttachThreadInput(cur, tgt, True)
            u.SetForegroundWindow(hwnd)
            u.AttachThreadInput(cur, tgt, False)
        else:
            u.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def find_window_by_name(name):
    """hwnd 失效时的兜底:按项目名模糊匹配当前可见的顶层窗口。
    Trae/VSCode reload 或窗口重建后 hwnd 会变,记录的旧 hwnd 就死了;这时用会话的项目名
    去匹配窗口标题(如 'PortraitEdit.vue - zc-geo-frontend - Trae CN' 含 'zc-geo-frontend'),
    返回真实可见窗口的 hwnd,没有则返回 None。"""
    if not name or len(name) < 2:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.IsWindowVisible.argtypes = [wintypes.HWND]; u.IsWindowVisible.restype = wintypes.BOOL
        u.GetParent.argtypes = [wintypes.HWND]; u.GetParent.restype = wintypes.HWND
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]; u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        ENUMWNDPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = [ENUMWNDPROC, wintypes.LPARAM]; u.EnumWindows.restype = wintypes.BOOL
        target = name.lower()
        # 终端/编辑器宿主窗口类(VSCode/Trae/Cursor 都是 Chrome_WidgetWin_1,WindowsTerminal 是 CASCADIA)
        term_classes = ("Chrome_WidgetWin_1", "CASCADIA_HOSTING_WINDOW_CLASS",
                        "WezTerm", "GHOSTTTY", "Xwindow", "Qt application")
        best = [None, 0]  # [hwnd, score]

        def _cb(hwnd, _lp):
            if not u.IsWindowVisible(hwnd) or u.GetParent(hwnd):
                return True
            n = u.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            tb = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, tb, n + 1)
            title = tb.value
            if not title or target not in title.lower():
                return True
            cb = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, cb, 256)
            cls = cb.value
            score = 1
            if any(cls == c or cls.startswith(c) for c in term_classes):
                score += 5                       # 终端/编辑器宿主类优先
            tl = title.lower()
            # 标题里 name 单独成段(典型 'file - name - Trae CN')比单纯子串更可信
            if (" - " + target + " - ") in tl or tl.startswith(target + " -") or tl.endswith(" - " + target) or tl == target:
                score += 3
            if score > best[1]:
                best[0] = hwnd; best[1] = score
            return True

        u.EnumWindows(ENUMWNDPROC(_cb), 0)
        return best[0]
    except Exception:
        return None


def resolve_hwnd(hwnd, name):
    """优先用记录的 hwnd;窗口已失效(重建/reload)则按项目名兜底匹配当前可见窗口。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        if hwnd and u.IsWindow(hwnd):
            return hwnd
    except Exception:
        pass
    return find_window_by_name(name)


def is_process_alive(pid):
    """进程是否仍在运行。OpenProcess 对已退出但对象尚未释放的进程也会返回句柄(Windows 经典坑),
    所以必须再调 GetExitCodeProcess —— 只有 STILL_ACTIVE(259) 才算真活着,否则是僵尸/已退出。"""
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetExitCodeProcess.restype = wintypes.BOOL
        h = k.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = wintypes.DWORD()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259        # STILL_ACTIVE
    except Exception:
        return False


def is_session_alive(d):
    """会话是否仍开着。有 termpid 时以终端进程为准 —— 关了那个终端 tab 就算结束,
    即便宿主窗口(Trae 主窗口)还在;无 termpid 则看窗口(hwnd 活或按项目名找到当前窗口)。"""
    tp = d.get("termpid")
    if tp:
        return is_process_alive(tp)
    return bool(resolve_hwnd(d.get("hwnd"), d.get("name")))


def round_rect(cv, x0, y0, x1, y1, r, **kw):
    """tkinter 圆角矩形(create_polygon 折线近似)"""
    pts = []
    steps = 5
    corners = [
        (x0 + r, y0 + r, 180, 270),   # 左上
        (x1 - r, y0 + r, 270, 360),   # 右上
        (x1 - r, y1 - r, 0,   90),    # 右下
        (x0 + r, y1 - r, 90,  180),   # 左下
    ]
    for (cx, cy, a0, a1) in corners:
        for i in range(steps + 1):
            ang = a0 + (a1 - a0) * i / steps
            pts.append(cx + r * math.cos(math.radians(ang)))
            pts.append(cy + r * math.sin(math.radians(ang)))
    return cv.create_polygon(pts, **kw)


# ---------------- GUI ----------------
def run_gui():
    import tkinter as tk
    set_dpi_aware()
    set_app_user_model_id("cc-light.NotePad")   # 独立 AppID:任务栏按它缓存图标,不再沿用 pythonw 的 python 图标

    W, H = 52, 150
    R_LAMP = 12
    R_GLOW = 16
    cx = W // 2
    cy_l = [29, 65, 101]
    PAD = 3
    R_BACK = 12

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=MAGIC)
    try:
        root.wm_attributes("-transparentcolor", MAGIC)   # 圆角悬浮:窗口方角透明
    except Exception:
        pass
    try:
        root.attributes("-alpha", 0.96)
    except Exception:
        pass

    def default_geo():
        wa = work_area()
        if wa:
            wl, wt, wr, wb = wa
            x = wl                       # 左侧贴紧左屏
            y = wb - H                   # 下边贴紧任务栏上沿
        else:
            x = 0
            y = root.winfo_screenheight() - H - 48
        return "%dx%d+%d+%d" % (W, H, x, y)

    try:
        with open(POS_FILE, "r", encoding="utf-8") as f:
            p = json.load(f)
        root.geometry("%dx%d+%d+%d" % (W, H, int(p["x"]), int(p["y"])))
    except Exception:
        root.geometry(default_geo())

    cv = tk.Canvas(root, width=W, height=H, bg=MAGIC, highlightthickness=0, bd=0)
    cv.pack()

    round_rect(cv, PAD, PAD, W - PAD, H - PAD, R_BACK, fill=BG, outline=BORDER, width=1)

    lamps = {}
    for i, key in enumerate(ORDER):
        glow = cv.create_oval(cx - R_GLOW, cy_l[i] - R_GLOW, cx + R_GLOW, cy_l[i] + R_GLOW,
                              fill=COLORS[key]["on"], outline="", stipple="gray50", state="hidden")
        base = cv.create_oval(cx - R_LAMP, cy_l[i] - R_LAMP, cx + R_LAMP, cy_l[i] + R_LAMP,
                              fill=COLORS[key]["dim"], outline="#2a2a2a", width=1)
        hi = cv.create_oval(cx - R_LAMP + 3, cy_l[i] - R_LAMP + 2, cx - 3, cy_l[i] - R_LAMP + 8,
                            fill="#ffffff", outline="", state="hidden")
        lamps[key] = {"base": base, "glow": glow, "hi": hi}

    dots = []   # 横排小点 item ids(每个会话一个,颜色=该会话有效状态)

    cur = {"s": None, "pulse": False}

    def paint(state):
        cur["pulse"] = False
        for key, L in lamps.items():
            on = (state == key)
            cv.itemconfig(L["base"],
                          fill=COLORS[key]["on"] if on else COLORS[key]["dim"],
                          outline="#000000" if on else "#2a2a2a",
                          width=2 if on else 1)
            cv.itemconfig(L["glow"], state="normal" if on else "hidden")
            cv.itemconfig(L["hi"], state="normal" if on else "hidden")
        if state == "yellow":
            start_pulse()

    def start_pulse():
        if cur["pulse"]:
            return
        cur["pulse"] = True

        def tick(bright):
            if not cur["pulse"] or cur["s"] != "yellow":
                return
            cv.itemconfig(lamps["yellow"]["base"],
                          fill=COLORS["yellow"]["on"] if bright else COLORS["yellow"]["mid"])
            cv.itemconfig(lamps["yellow"]["glow"], state="normal" if bright else "hidden")
            root.after(650, lambda: tick(not bright))
        tick(False)

    def apply(state):
        if state not in COLORS:
            state = "gray"
        if state != cur["s"]:
            cur["s"] = state
            paint(state)

    def render_dots(sessions, now):
        for it in dots:
            cv.delete(it)
        dots.clear()
        if not sessions:
            return
        items = sorted(sessions.items(),
                       key=lambda kv: (PRIO.get(effective_state(kv[1], now), 99), -kv[1].get("ts", 0)))
        n = len(items)
        gap = 2.5
        avail = W - 2 * PAD - 2
        r = min(3.0, max(1.8, (avail - (n - 1) * gap) / (2.5 * n)))   # 点多则更小,5个不挤
        total = n * 2 * r + (n - 1) * gap
        x = (W - total) / 2 + r
        y = 126
        for (sid, d) in items:
            st = effective_state(d, now)
            color = COLORS.get(st, {}).get("on", "#666666")
            dots.append(cv.create_oval(x - r, y - r, x + r, y + r, fill=color, outline=""))
            x += 2 * r + gap

    heal_counter = [0]
    def poll():
        sessions = read_sessions()
        now = time.time()
        c, n = aggregate(sessions)
        apply(c)
        render_dots(sessions, now)
        heal_counter[0] += 1
        if heal_counter[0] % 40 == 0:   # 40 × 500ms = 20s,定期自愈
            ensure_hooks()
        root.after(500, poll)

    # ---- 交互 ----
    drag = {"x": 0, "y": 0}

    def down(e):
        drag["x"], drag["y"] = e.x, e.y

    def move(e):
        root.geometry("+%d+%d" % (root.winfo_x() + e.x - drag["x"], root.winfo_y() + e.y - drag["y"]))

    def save_pos():
        try:
            with open(POS_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": root.winfo_x(), "y": root.winfo_y()}, f)
        except Exception:
            pass

    cv.bind("<ButtonPress-1>", down)
    cv.bind("<B1-Motion>", move)
    cv.bind("<ButtonRelease-1>", lambda e: save_pos())
    cv.bind("<Enter>", lambda e: details_hover())
    cv.bind("<Leave>", lambda e: schedule_close())

    # ---- 会话明细 ----
    detail_win = [None]
    close_timer = [None]

    def cancel_close():
        if close_timer[0]:
            root.after_cancel(close_timer[0])
            close_timer[0] = None

    def close_details():
        cancel_close()
        if detail_win[0] is not None:
            try:
                detail_win[0].destroy()
            except Exception:
                pass
            detail_win[0] = None

    def schedule_close():
        cancel_close()
        close_timer[0] = root.after(300, close_details)

    def details_hover():
        cancel_close()
        if detail_win[0] is not None:
            try:
                detail_win[0].destroy()
            except Exception:
                pass
        sessions = read_sessions()
        now = time.time()
        # 统计每个 hwnd 被几个会话共享(同窗口多终端 = 共享 hwnd,需要 termpid 区分)
        hwnd_count = {}
        for _s in sessions.values():
            _hh = _s.get("hwnd")
            if _hh:
                hwnd_count[_hh] = hwnd_count.get(_hh, 0) + 1
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG)
        detail_win[0] = win
        order = sorted(sessions.items(),
                       key=lambda kv: (PRIO.get(effective_state(kv[1], now), 99), -kv[1].get("ts", 0)))
        names = [d.get("name", "") for _, d in sessions.items()]
        tk.Label(win, text=" 会话明细(悬停查看,移开关闭)", fg="#9a9a9a", bg=BG,
                 font=("Microsoft YaHei", 8)).grid(row=0, column=0, sticky="w", padx=6, pady=(4, 2))
        if not order:
            tk.Label(win, text=" (无活跃会话)", fg="#888888", bg=BG,
                     font=("Microsoft YaHei", 8)).grid(row=1, column=0, sticky="w", padx=6, pady=2)
        for i, (sid, d) in enumerate(order, start=1):
            raw = d.get("state", "gray")
            st = effective_state(d, now)
            stale = (raw == "yellow" and st == "green")
            color = COLORS.get(st, {}).get("on", "#666666")
            name = d.get("name") or sid[:8]
            if names.count(d.get("name", "")) > 1:
                name = name + " " + sid[:4]
            hwnd = d.get("hwnd")
            termpid = d.get("termpid")
            shared = hwnd_count.get(hwnd, 0) > 1
            row = tk.Frame(win, bg=BG)
            row.grid(row=i, column=0, sticky="w", padx=6, pady=1)
            tk.Label(row, text="●", fg=color, bg=BG, font=("Consolas", 10)).pack(side="left")
            word = STATE_WORD.get(st, "") + (" ·超时" if stale else "")
            tk.Label(row, text=" " + word, fg=color, bg=BG,
                     font=("Microsoft YaHei", 8, "bold")).pack(side="left")
            tk.Label(row, text="  " + name, fg="#dddddd", bg=BG,
                     font=("Microsoft YaHei", 8)).pack(side="left")
            if shared and termpid:                       # 同窗口多终端 → 带 #pid,和诊断命令输出对得上
                tk.Label(row, text="  #" + str(termpid), fg="#9ab8e8", bg=BG,
                         font=("Consolas", 7)).pack(side="left")
            tk.Label(row, text="  " + human_ago(d.get("ts", 0)), fg="#777777", bg=BG,
                     font=("Consolas", 7)).pack(side="left")
            if hwnd or termpid:
                def _jump(e, h=hwnd, tp=termpid, shared=shared, nm=(d.get("name") or sid[:8])):
                    tgt = resolve_hwnd(h, nm)            # 记录的 hwnd 死了 → 按项目名兜底匹配当前窗口
                    if tgt:
                        focus_window(tgt)               # 聚焦宿主窗口
                    if tp and shared:                   # 同窗口多终端 → 额外发 URI 精确切到对应终端
                        try:
                            os.startfile("trae-cn://cc-light.cc-light-helper/focus?pid=" + str(tp))
                        except Exception:
                            pass
                    close_details()
                row.bind("<Button-1>", _jump)
                row.configure(cursor="hand2")
                for _w in row.winfo_children():
                    _w.bind("<Button-1>", _jump)
                    _w.configure(cursor="hand2")
            # 右键会话行:删除该会话(清理残留/不想要的条目;若会话仍活跃,下次 hook 会重建)
            def _on_right(e, s=sid):
                m = tk.Menu(root, tearoff=0, bg=BG, fg="#dddddd",
                            activebackground="#3a3a3a", activeforeground="#ffffff")
                def _do():
                    archive_session(s)               # 归档(可还原),非真删
                    close_details()
                    root.after(20, details_hover)    # 关闭后重开 → 刷新列表
                m.add_command(label="删除该会话", command=_do)
                try:
                    m.tk_popup(e.x_root, e.y_root)
                finally:
                    m.grab_release()
            row.bind("<Button-3>", _on_right)
            for _w in row.winfo_children():
                _w.bind("<Button-3>", _on_right)
        win.update_idletasks()
        ww = win.winfo_reqwidth()
        wh = win.winfo_reqheight()
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = root.winfo_rootx() + W + 4
        if x + ww > sw:
            x = root.winfo_rootx() - ww - 4
        y = root.winfo_rooty()
        if y + wh > sh - 4:            # 超屏底 → 向上展开(底部对齐灯底)
            y = root.winfo_rooty() + H - wh
        if y < 0:
            y = 0
        win.geometry("+%d+%d" % (x, y))
        win.bind("<Enter>", lambda e: cancel_close())
        win.bind("<Leave>", lambda e: schedule_close())

    def cleanup_stale():
        """归档超过超时阈值无更新的会话(归档而非真删,可用「检测所有会话」还原)。"""
        thr = _config["yellow_timeout"]
        now = time.time()
        for sid, d in list(read_sessions().items()):
            if now - d.get("ts", 0) > thr:
                archive_session(sid)
        sessions = read_sessions()
        c, n = aggregate(sessions)
        apply(c)
        render_dots(sessions, time.time())

    def restore_sessions():
        """「检测所有会话」:
        扫 ~/.claude/projects 下 SCAN_ACTIVE 内的 jsonl,只保留【有 claude.exe 进程在跑】
        的项目(scan_active_jsonl 按进程 cwd 判活 —— 已关的不会误补),补建 sessions 里缺失的
        (标 scanned)。救那些 hook 没注入、从没写过 session 文件的开着的会话。
        兼容:从 archive 还原重新开着的会话。"""
        now = time.time()
        active = scan_active_jsonl(SCAN_ACTIVE)   # 只含活会话(进程 cwd 过滤过)
        # 已在列表的真实会话 sid(非 scanned —— hook 真写过的;scanned 是上次扫描补的,不算该项目已占用)
        existing = set()
        try:
            for fn in os.listdir(SESSIONS_DIR):
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(SESSIONS_DIR, fn), "r", encoding="utf-8") as f:
                        d = json.load(f)
                except Exception:
                    continue
                if not d.get("scanned"):
                    existing.add(fn[:-5])
        except OSError:
            pass
        to_build = {sid: info for sid, info in active.items() if sid not in existing}
        # 刷已存在且仍活跃会话的 ts(防被判归档)
        for sid in existing:
            if sid in active:
                try:
                    with open(os.path.join(SESSIONS_DIR, sid + ".json"), "r", encoding="utf-8") as f:
                        d = json.load(f)
                    d["ts"] = now
                    _write_session_file(sid, d)
                except Exception:
                    pass
        # 补建缺失(标 scanned 等 hook 接管)
        for sid, info in to_build.items():
            _write_session_file(sid, {
                "state": "yellow", "msg": "", "ts": now, "name": info["name"],
                "subs": 0, "hwnd": 0, "termpid": 0, "scanned": True,
            })
        # 清理:scanned 会话其进程已没了(不在 active) → 删
        try:
            for fn in os.listdir(SESSIONS_DIR):
                if not fn.endswith(".json"):
                    continue
                sid = fn[:-5]
                p = os.path.join(SESSIONS_DIR, fn)
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        d = json.load(f)
                except Exception:
                    continue
                if d.get("scanned") and sid not in active:
                    try: os.remove(p)
                    except OSError: pass
        except OSError:
            pass
        # ---- 2) archive 还原(兼容老数据) ----
        try:
            files = os.listdir(ARCHIVE_DIR)
        except OSError:
            files = []
        for fn in files:
            if not fn.endswith(".json"):
                continue
            src = os.path.join(ARCHIVE_DIR, fn)
            dst = os.path.join(SESSIONS_DIR, fn)
            if os.path.exists(dst):              # 列表已有(活跃),归档副本作废
                try: os.remove(src)
                except OSError: pass
                continue
            try:
                with open(src, "r", encoding="utf-8") as f:
                    dd = json.load(f)
                if is_session_alive(dd):         # 重新开着 → 还原(刷 ts 避免刚还原又被归档)
                    dd["ts"] = now
                    with open(dst, "w", encoding="utf-8") as f:
                        json.dump(dd, f)
                os.remove(src)                   # 无论还原与否,归档原文件都清掉
            except Exception:
                pass
        sessions = read_sessions()
        c, n = aggregate(sessions)
        apply(c)
        render_dots(sessions, time.time())

    fallback_var = tk.IntVar(value=1 if _config["timeout_fallback"] else 0)
    timeout_var = tk.IntVar(value=_config["yellow_timeout"])

    def on_toggle_fallback():
        _config["timeout_fallback"] = bool(fallback_var.get())
        save_config()

    def on_set_timeout():
        _config["yellow_timeout"] = int(timeout_var.get())
        save_config()

    timeout_menu = tk.Menu(root, tearoff=0)
    for sec in (30, 60, 120, 180, 300, 600):
        timeout_menu.add_radiobutton(label="%d 秒" % sec, value=sec, variable=timeout_var, command=on_set_timeout)

    topmost_var = tk.IntVar(value=1)   # 灯窗口默认置顶(原双击切换,易误触 → 改菜单)

    def on_toggle_topmost():
        root.attributes("-topmost", bool(topmost_var.get()))

    # ---------------- 快捷记录:悬浮便签 + 历史 ----------------
    note_win = [None]          # 当前便签 Toplevel(单例;已开则前置聚焦)
    note_text = [None]         # 便签内的 Text 控件
    hist_win = [None]          # 历史窗口(单例;重复打开只保留一个)
    NOTE_MIN_W, NOTE_MIN_H = 240, 140

    def _close_note():
        """关闭便签:关闭即存(内容非空且与最新一条不同 → 归档)+ 去重 + 存几何。"""
        if note_text[0] is not None:
            try:
                t = note_text[0].get("1.0", "end-1c").strip()
            except Exception:
                t = ""
            if t:
                hist = load_history()
                if not hist or hist[-1].get("text") != t:    # 与最新一条相同则不重复存
                    hist.append({"ts": time.time(), "text": t})
                    save_history(hist[-MAX_HISTORY:])
        if note_win[0] is not None:
            try:
                save_notes_geo("note", note_win[0].winfo_x(), note_win[0].winfo_y(),
                               note_win[0].winfo_width(), note_win[0].winfo_height())
            except Exception:
                pass
            try:
                note_win[0].destroy()
            except Exception:
                pass
        note_win[0] = None
        note_text[0] = None

    def toggle_note():
        """打开/前置便签。已开(含任务栏最小化态)→ 复原+前置聚焦;未开 → 新建空便签。"""
        if note_win[0] is not None and note_win[0].winfo_exists():
            try:
                if note_win[0].wm_state() == "iconic":    # 任务栏最小化态 → 复原
                    note_win[0].deiconify()
                    note_win[0].attributes("-topmost", True)
                note_win[0].lift()
                note_win[0].attributes("-topmost", True)
                note_win[0].focus_force()
            except Exception:
                pass
            return
        win = tk.Toplevel(root)
        win.configure(bg=BG)
        win.title("快捷记录")
        try:
            win.attributes("-alpha", 0.98)
        except Exception:
            pass

        geo = load_notes_geo().get("note")
        if geo:
            win.geometry("%dx%d+%d+%d" % (geo.get("w", 300), geo.get("h", 220),
                                          geo.get("x", 0), geo.get("y", 0)))
        else:
            win.geometry("%dx%d+%d+%d" % (300, 220, root.winfo_rootx() + W + 6,
                                          max(0, root.winfo_rooty())))
        win.update_idletasks()
        frame_to_taskbar(win)               # 去系统标题栏(保自定义外观)+ 进任务栏(可最小化/点任务栏复原)
        _set_note_icon(win)                 # 任务栏图标(note.ico 优先,否则程序生成的便签图标)
        win.attributes("-topmost", True)

        # 顶部 drag bar(空白区 + 标题可拖动整窗;↻ 记录并清空、— 最小化、✕ 关闭)
        bar = tk.Frame(win, bg=BORDER, height=26)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)
        title_lbl = tk.Label(bar, text=" 快捷记录", fg="#cfcfcf", bg=BORDER,
                             font=("Microsoft YaHei", 9))
        title_lbl.pack(side="left", padx=2)
        BTN_FONT = ("Segoe UI Symbol", 11)
        close_btn = tk.Label(bar, text="✕", fg="#9a9a9a", bg=BORDER, font=BTN_FONT, padx=8)
        close_btn.pack(side="right")
        min_btn = tk.Label(bar, text="—", fg="#9a9a9a", bg=BORDER, font=BTN_FONT, padx=8)
        min_btn.pack(side="right")
        refresh_btn = tk.Label(bar, text="↻", fg="#9a9a9a", bg=BORDER, font=BTN_FONT, padx=8)
        refresh_btn.pack(side="right")
        for _b in (close_btn, min_btn, refresh_btn):
            _b.configure(cursor="arrow")
            _b.bind("<Enter>", lambda e, w=_b: w.configure(fg="#ffffff"))   # hover 变亮
            _b.bind("<Leave>", lambda e, w=_b: w.configure(fg="#9a9a9a"))

        # 多行输入区
        txt = tk.Text(win, bg=BG, fg="#e8e8e8", insertbackground="#e8e8e8",
                      bd=0, highlightthickness=0, wrap="word", relief="flat",
                      font=("Microsoft YaHei", 10), padx=8, pady=6, spacing1=2, spacing3=2)
        txt.pack(side="top", fill="both", expand=True)

        # 右下角缩放手柄
        handle = tk.Label(win, text="◢", fg="#6a6a72", bg=BG, font=("Consolas", 11))
        handle.place(relx=1.0, rely=1.0, x=-3, y=-3, anchor="se")
        handle.configure(cursor="size_nw_se")

        note_win[0] = win
        note_text[0] = txt

        # 拖动整窗(bar 空白区 + 标题触发;✕ 只绑关闭)。
        # 关键:用 e.x_root(屏幕绝对坐标)+ bind_all —— bar 内有 title_lbl/close_btn 子控件,
        # 鼠标划过子控件时 e.x(相对坐标)基准会变导致跳动;bind_all 让按下后全局接管 Motion,不丢帧。
        drag = {"x": 0, "y": 0, "wx": 0, "wy": 0}
        def _save_geo(_e=None):
            try:
                save_notes_geo("note", win.winfo_x(), win.winfo_y(),
                               win.winfo_width(), win.winfo_height())
            except Exception:
                pass
        def _d_move(e):
            win.geometry("+%d+%d" % (drag["wx"] + e.x_root - drag["x"], drag["wy"] + e.y_root - drag["y"]))
        def _d_up(e):
            win.unbind_all("<B1-Motion>")
            win.unbind_all("<ButtonRelease-1>")
            _save_geo()
        def _d_down(e):
            drag["x"], drag["y"] = e.x_root, e.y_root
            drag["wx"], drag["wy"] = win.winfo_x(), win.winfo_y()
            win.bind_all("<B1-Motion>", _d_move)
            win.bind_all("<ButtonRelease-1>", _d_up)
        for _w in (bar, title_lbl):
            _w.bind("<ButtonPress-1>", _d_down)
            _w.configure(cursor="fleur")
        def _commit_and_clear():
            """记录当前内容到历史(去重)+ 清空便签,窗口保持开着继续写下一条。"""
            try:
                t = note_text[0].get("1.0", "end-1c").strip()
            except Exception:
                t = ""
            if t:
                hist = load_history()
                if not hist or hist[-1].get("text") != t:    # 与最新一条相同则不重复存
                    hist.append({"ts": time.time(), "text": t})
                    save_history(hist[-MAX_HISTORY:])
            try:
                note_text[0].delete("1.0", "end")
                note_text[0].focus_set()
            except Exception:
                pass
        def _minimize_note():
            try:
                save_notes_geo("note", win.winfo_x(), win.winfo_y(),
                               win.winfo_width(), win.winfo_height())
            except Exception:
                pass
            try:
                win.iconify()              # 最小化到任务栏;点任务栏图标或再点「快捷记录」复原
            except Exception:
                pass
        refresh_btn.bind("<Button-1>", lambda e: _commit_and_clear())
        min_btn.bind("<Button-1>", lambda e: _minimize_note())
        close_btn.bind("<Button-1>", lambda e: _close_note())

        # 右下角缩放(同样 bind_all:窗口缩小时 ◢ 跟着移,鼠标可能脱离 handle,全局接管不卡)
        rsz = {"sx": 0, "sy": 0, "w": 0, "h": 0}
        def _r_move(e):
            nw = max(NOTE_MIN_W, rsz["w"] + (e.x_root - rsz["sx"]))
            nh = max(NOTE_MIN_H, rsz["h"] + (e.y_root - rsz["sy"]))
            win.geometry("%dx%d" % (nw, nh))
        def _r_up(e):
            win.unbind_all("<B1-Motion>")
            win.unbind_all("<ButtonRelease-1>")
            _save_geo()
        def _r_down(e):
            rsz["sx"], rsz["sy"] = e.x_root, e.y_root
            rsz["w"], rsz["h"] = win.winfo_width(), win.winfo_height()
            win.bind_all("<B1-Motion>", _r_move)
            win.bind_all("<ButtonRelease-1>", _r_up)
        handle.bind("<ButtonPress-1>", _r_down)

        win.bind("<Escape>", lambda e: _close_note())
        txt.bind("<Escape>", lambda e: _close_note())
        txt.focus_set()

    def fill_note(text):
        """把内容回填到便签(便签没开就先打开并前置),聚焦方便继续编辑。"""
        toggle_note()
        try:
            if note_text[0] is not None:
                note_text[0].delete("1.0", "end")
                note_text[0].insert("1.0", text)
                note_text[0].focus_set()
        except Exception:
            pass

    def show_history():
        """快捷记录历史:列表展示所有记录(最新在上),左键回填便签,右键删除。单例,重复打开只留一个。"""
        if hist_win[0] is not None:           # 单例:旧的先销毁,避免开出一堆
            try:
                hist_win[0].destroy()
            except Exception:
                pass
            hist_win[0] = None
        items = list(reversed(load_history()))
        win = tk.Toplevel(root)
        win.title("快捷记录历史")
        win.attributes("-topmost", True)
        win.configure(bg=BG)
        _set_note_icon(win)
        hist_win[0] = win
        geo = load_notes_geo().get("history")
        if geo:
            win.geometry("%dx%d+%d+%d" % (geo.get("w", 360), geo.get("h", 420),
                                          geo.get("x", 0), geo.get("y", 0)))
        else:
            win.geometry("%dx%d+%d+%d" % (360, 420, root.winfo_rootx() + W + 6,
                                          max(0, root.winfo_rooty())))

        tk.Label(win, text=" 快捷记录历史(%d 条)" % len(items), fg="#9a9a9a", bg=BG,
                 font=("Microsoft YaHei", 9)).pack(side="top", fill="x", padx=6, pady=(6, 4))

        body = tk.Frame(win, bg=BG)
        body.pack(side="top", fill="both", expand=True, padx=6, pady=(0, 6))
        cv = tk.Canvas(body, bg=BG, bd=0, highlightthickness=0)
        sb = tk.Scrollbar(body, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(cv, bg=BG)
        inner_win = cv.create_window((0, 0), window=inner, anchor="nw")
        t_labels = []

        def _on_inner_cfg(_e):
            cv.configure(scrollregion=cv.bbox("all"))
        def _on_cv_cfg(e):
            cv.itemconfig(inner_win, width=e.width)
            wl = max(120, e.width - 70)           # 减「时间标签 + padding」,让预览自适应窗口宽
            for lbl in t_labels:
                try:
                    lbl.configure(wraplength=wl)
                except Exception:
                    pass
        inner.bind("<Configure>", _on_inner_cfg)
        cv.bind("<Configure>", _on_cv_cfg)

        def _wheel(e):
            try:
                cv.yview_scroll(int(-1 * (e.delta / 120)), "units")
            except Exception:
                pass
        def _unbind_wheel(_e=None):
            try:
                cv.unbind_all("<MouseWheel>")
            except Exception:
                pass
        cv.bind("<Enter>", lambda e: cv.bind_all("<MouseWheel>", _wheel))
        cv.bind("<Leave>", _unbind_wheel)
        win.bind("<Destroy>", _unbind_wheel, add="+")

        if not items:
            tk.Label(inner, text="(无记录)", fg="#888888", bg=BG,
                     font=("Microsoft YaHei", 9)).pack(anchor="w", padx=4, pady=8)
        else:
            for it in items:
                ts = it.get("ts", 0)
                text = it.get("text", "")
                row = tk.Frame(inner, bg=BG)
                row.pack(side="top", fill="x", padx=2, pady=2)
                flat = " ".join(text.split())
                preview = flat[:14] + (" …" if (len(flat) > 14 or "\n" in text) else "")
                tk.Label(row, text=human_ago(ts), fg="#777777", bg=BG,
                         font=("Consolas", 7)).pack(side="left", padx=(2, 6))
                t_lbl = tk.Label(row, text=preview, fg="#dddddd", bg=BG,
                                 font=("Microsoft YaHei", 9), wraplength=300, justify="left")
                t_lbl.pack(side="left", fill="x", expand=True)
                t_labels.append(t_lbl)
                row.configure(cursor="hand2")
                t_lbl.configure(cursor="hand2")

                def _click(e, tx=text):
                    fill_note(tx)
                    try:
                        win.destroy()
                    except Exception:
                        pass
                def _right(e, _ts=ts, _tx=text):
                    m = tk.Menu(root, tearoff=0, bg=BG, fg="#dddddd",
                                activebackground="#3a3a3a", activeforeground="#ffffff")
                    def _do():
                        save_history([x for x in load_history()
                                      if not (x.get("ts") == _ts and x.get("text") == _tx)])
                        try:
                            win.destroy()
                        except Exception:
                            pass
                        show_history()
                    m.add_command(label="删除该记录", command=_do)
                    try:
                        m.tk_popup(e.x_root, e.y_root)
                    finally:
                        m.grab_release()
                row.bind("<Button-1>", _click)
                t_lbl.bind("<Button-1>", _click)
                row.bind("<Button-3>", _right)
                t_lbl.bind("<Button-3>", _right)

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="清理不活跃会话", command=cleanup_stale)
    menu.add_command(label="检测所有会话", command=restore_sessions)   # 还原被清理/删除的会话
    menu.add_separator()
    menu.add_command(label="快捷记录", command=toggle_note)
    menu.add_command(label="快捷记录历史", command=show_history)
    menu.add_separator()
    menu.add_checkbutton(label="超时兜底", variable=fallback_var, command=on_toggle_fallback)
    menu.add_cascade(label="超时时间", menu=timeout_menu)
    menu.add_separator()
    menu.add_command(label="手动 · 红(等确认)", command=lambda: write_session("_manual", "red", "", "手动"))
    menu.add_command(label="手动 · 黄(运行中)", command=lambda: write_session("_manual", "yellow", "", "手动"))
    menu.add_command(label="手动 · 绿(完成)",   command=lambda: write_session("_manual", "green", "", "手动"))
    menu.add_command(label="手动 · 清除",       command=lambda: delete_session("_manual"))
    def restart():
        import subprocess
        try:
            subprocess.Popen([sys.executable, os.path.abspath(__file__)])
        except Exception:
            pass
        root.destroy()

    menu.add_separator()
    menu.add_checkbutton(label="窗口置顶", variable=topmost_var, command=on_toggle_topmost)
    menu.add_command(label="复位位置", command=lambda: root.geometry(default_geo()))
    menu.add_command(label="重启", command=restart)
    menu.add_command(label="退出", command=root.destroy)
    cv.bind("<ButtonPress-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    poll()
    root.mainloop()


if __name__ == "__main__":
    sys.exit(cli())
