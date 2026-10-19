#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cc-light —— Claude Code 桌面悬浮交通灯(多会话优先级聚合)

用法:
  pythonw cc_light.py                              启动悬浮灯窗口
  python  cc_light.py --hook red|yellow|green      hook 用:读 stdin 取 session_id/cwd
  python  cc_light.py --end                        SessionEnd 用:删该会话
  python  cc_light.py --state                      打印聚合态 + 会话数
  python  cc_light.py --diag                       自检:打印每个会话能否定位窗口(诊断点击跳不动)

聚合:任一 red > 任一 yellow > 任一 green > gray
"""
import sys
import os
import json
import time
import math
from datetime import datetime

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
SNIPPETS_FILE = os.path.join(DATA_DIR, "snippets.json")            # 快捷指令:[{id,title,text}, ...]
MAX_HISTORY = 500   # 历史上限(超出截断最旧的)
STALE = 1 * 3600        # 1 小时无更新 → 归档(可被「检测所有会话」还原)
SCAN_ACTIVE = 6 * 3600  # 「检测所有会话」:jsonl 6h 内有写入视为会话仍开着(覆盖午休等长空闲)
SCANNED_ACTIVE = 60     # scanned 会话(无 hook 心跳)按 jsonl mtime 判忙/闲:60s 内有写=运行中(yellow),否则空闲(green)
CODEX_SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".codex", "sessions")
CODEX_INDEX_FILE = os.path.join(os.path.expanduser("~"), ".codex", "session_index.jsonl")
CODEX_LOCKS_DIR = os.path.join(os.path.expanduser("~"), ".codex", "thread-writer-locks")
CODEX_SCAN_INTERVAL = 3
CODEX_ARCHIVED_FILE = os.path.join(DATA_DIR, "codex-archived.json")
_CODEX_CACHE = {"checked": 0, "sessions": {}, "logs": {}}
_CODEX_TITLES = {"mtime": None, "titles": {}}
# DeepSeek Harness 桌面版:会话事件实时折叠进 projection cache(每会话一个 JSON),
# 不需要 hook,也不需要解压 session.v4.jsonl.zstd。
DSH_PROJ_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".dsh",
                                  "storages", "session_projcache", "sessions")
DSH_WORKSPACE_FILE = os.path.join(os.path.expanduser("~"), ".dsh",
                                  "storages", "workspace.json")
DSH_IMAGE = "deepseek harness.exe"
DSH_SCAN_INTERVAL = 2
DSH_ACTIVE_WINDOW = 3600    # 空闲会话 1h 未更新后不再展示(忙碌中的会话不受限)
DSH_ARCHIVED_FILE = os.path.join(DATA_DIR, "dsh-archived.json")
_DSH_CACHE = {"checked": 0, "sessions": {}}
DEFAULT_CONFIG = {"yellow_timeout": 30, "timeout_fallback": True}    # 默认 30 秒(PostToolUse 心跳刷 ts,中断/卡住 30s 降绿)
ERROR_LOG = os.path.join(DATA_DIR, "error.log")          # 异常日志(右键「错误日志」查看,复制丢给排查)
MAX_ERROR_LOG = 200   # 异常日志保留最近 N 条(超 500KB 自动截断)
ORDER_FILE = os.path.join(DATA_DIR, "order.json")   # 用户拖拽排序的会话顺序 [sid,...]

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


# ---------------- 错误日志 ----------------
def log_error(exc=None, context="", tb=None):
    """记录异常或逻辑失败事件到 error.log(时间+traceback+上下文,jsonl 追加写),供右键「错误日志」
    查看、复制丢给排查。exc=None 时记纯事件(如「点击跳转失败」—— 触发了行为但结果不符预期,非崩溃)。
    超 500KB 截断到最近 MAX_ERROR_LOG 条。写日志本身出错则静默(绝不再抛)。"""
    import traceback
    try:
        ensure_dir()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        if exc is not None:
            tb_text = "".join(traceback.format_exception(type(exc), exc, tb or exc.__traceback__))
        else:
            tb_text = "(事件,无异常)"
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts, "ctx": context, "tb": tb_text}, ensure_ascii=False) + "\n")
        if os.path.getsize(ERROR_LOG) > 500_000:
            _trim_error_log()
    except Exception:
        pass


def _trim_error_log():
    """重写错误日志,只保留最近 MAX_ERROR_LOG 条。"""
    try:
        entries = read_error_log_entries()
        with open(ERROR_LOG, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_error_log_entries():
    """读错误日志(最近 MAX_ERROR_LOG 条,[{ts,ctx,tb}];文件无返回 [])。"""
    out = []
    try:
        with open(ERROR_LOG, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        return out
    for line in lines[-MAX_ERROR_LOG:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def clear_error_log():
    try:
        if os.path.exists(ERROR_LOG):
            os.remove(ERROR_LOG)
    except OSError:
        pass


# ---------------- 会话任务标题(同项目多会话时区分"在干啥") ----------------
_SID_JSONL_CACHE = {}   # sid -> jsonl 路径(找一次复用)
_TITLE_CACHE = {}       # jsonl 路径 -> (mtime, title);mtime 变才重读(hover 频繁,免反复读大 jsonl)


def _find_jsonl(sid):
    """按 sid 在 ~/.claude/projects/*/ 下找 <sid>.jsonl(sid 全局唯一,只在一个项目目录)。"""
    p = _SID_JSONL_CACHE.get(sid)
    if p and os.path.exists(p):
        return p
    base = os.path.join(os.environ.get("USERPROFILE") or DIR, ".claude", "projects")
    try:
        for proj in os.listdir(base):
            cand = os.path.join(base, proj, sid + ".jsonl")
            if os.path.exists(cand):
                _SID_JSONL_CACHE[sid] = cand
                return cand
    except OSError:
        pass
    return None


def _read_session_title(path):
    """读 jsonl:取【最后一条有意义的 user message】(反映最近在干啥,不是会话整体 aiTitle ——
    同会话从任务a切到b时,aiTitle 往往还停在a,最近 user 才是b)。user 太短(<6字,如"修"/"继续")
    跳过往前找;都短则 fallback 最新 aiTitle,再 fallback 首条 user。"""
    last_user = ""       # 最后一条 ≥MIN 字的用户输入
    last_ai = ""
    first_user = ""
    MIN = 6
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                t = e.get("type")
                if t == "ai-title":
                    v = e.get("aiTitle", "")
                    if v:
                        last_ai = v
                elif t == "user":
                    msg = e.get("message", {})
                    c = msg.get("content")
                    if isinstance(c, str):
                        txt = c
                    elif isinstance(c, list):
                        # 只取用户输入的 text part;tool_result(工具返回)也在 user entry 里,排除
                        txt = next((p.get("text", "") for p in c
                                    if isinstance(p, dict) and p.get("type") == "text"), "")
                    else:
                        txt = ""
                    txt = txt.strip()
                    if txt:
                        first_user = first_user or txt
                        if len(txt) >= MIN:
                            last_user = txt           # 不断覆盖,留在最后一条
    except OSError:
        pass
    title = last_user or last_ai or first_user
    if title:
        return title.replace("\n", " ").strip()[:200]
    return ""


def get_session_title(sid, jsonl=None):
    """会话的任务标题(同项目多会话时显示,区分"在干啥")。
    jsonl 给定则用,否则按 sid 扫 projects 找;取最新 aiTitle,无则首条 user。
    按 jsonl mtime 缓存(hover 频繁,避免每次全读大 jsonl)。"""
    path = jsonl or _find_jsonl(sid)
    if not path:
        return ""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    cached = _TITLE_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    title = _read_session_title(path)
    _TITLE_CACHE[path] = (mtime, title)
    return title


def read_order():
    """读用户拖拽排序的会话顺序 [sid,...]。"""
    try:
        with open(ORDER_FILE, "r", encoding="utf-8") as f:
            o = json.load(f)
        return [s for s in o if isinstance(s, str)]
    except Exception:
        return []


def write_order(sids):
    """保存用户拖拽排序后的会话顺序(只含当前活会话,死 sid 自然淘汰)。"""
    try:
        ensure_dir()
        with open(ORDER_FILE, "w", encoding="utf-8") as f:
            json.dump(sids, f)
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


def _claude_procs():
    """所有 claude.exe 进程:返回 [{cwd, pid, termpid}, ...]。
    cwd=进程工作目录(会话项目目录,精确判活);termpid=父进程 PID(终端 shell —— PowerShell/
    WindowsTerminal 等,点击切换时 focus 它的窗口)。用 ctypes NtQueryInformationProcess 读 PEB
    (ProcessBasicInformation 里 InheritedFromUniqueProcessId 即父 PID,纯 ctypes 无依赖)。读失败返回空列表。"""
    out = []
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
                    out.append({"cwd": cwd, "pid": pid, "termpid": pbi.r3 or 0})
            except Exception:
                pass
            finally:
                k.CloseHandle(h2)
    except Exception:
        pass
    return out


def scan_active_jsonl(threshold_sec):
    """扫 ~/.claude/projects/<proj>/<sid>.jsonl,只保留【当前有 claude.exe 进程在跑】的项目
    (按进程 cwd 精确判活 —— 没进程的项目即已关,不补,避免误补刚关的旧会话)。同项目按其
    claude 进程数取最新 N 个 jsonl(PM 开 2 个就取 2 个)。返回 {sid: {"name", "mtime", "termpid"}},
    termpid 取该会话对应终端 shell 的 PID(补建的 scanned 会话用它点击切换)。"""
    base = os.path.join(os.environ.get("USERPROFILE") or DIR, ".claude", "projects")
    procs = _claude_procs()                       # [{cwd, pid, termpid}]
    proj_terms = {}                               # 编码项目目录(小写) -> [termpid, ...]
    proj_leaf = {}                                # 同 -> 项目目录 basename(找 Trae/VSCode 宿主窗口用)
    for p in procs:
        enc = _encode_path(p["cwd"]).lower()
        proj_terms.setdefault(enc, []).append(p["termpid"])
        proj_leaf[enc] = p["cwd"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    out = {}
    now = time.time()
    try:
        projects = os.listdir(base)
    except OSError:
        return out
    for proj in projects:
        pl = proj.lower()
        terms = proj_terms.get(pl)
        if not terms:                             # 该项目无活 claude 进程 → 跳过
            continue
        # 宿主窗口:优先沿 termpid 父链找(termpid=powershell 无窗口,窗口在祖父 WT/Trae 主进程;
        # WT 多 tab 标题随前台 tab 变、记录的 hwnd 易失效,按进程归属更稳)。找不到再按 basename 匹配标题。
        host_hwnd = (find_window_by_pid(terms[0]) if terms else None) or find_window_by_name(proj_leaf.get(pl, ""))
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
        n = len(terms)
        for idx, (mtime, sid) in enumerate(cands[:n]):   # 取最新 n 个(= 进程数)
            # name 用 basename(cwd 末段,如 zcvip-front),与 hook 写的 name、_jump 兜底匹配的窗口标题
            # (file - zcvip-front - Trae CN 只含 basename)三方一致。曾用 proj_name(含父目录,如
            # Desktop-newWork-zcvip-front):匹配不了标题,hwnd 失效后 _jump 兜底全失败 → 跳不动。
            out[sid] = {"name": proj_leaf.get(pl, "") or proj_name(proj), "mtime": mtime,
                        "termpid": terms[idx % n],         # round-robin 终端 pid(独立终端窗口场景;同项目多终端无法 1:1 精确)
                        "hwnd": host_hwnd or 0,            # 宿主窗口(Trae/VSCode:同项目会话共享一个,点击 focus 它)
                        "jsonl": os.path.join(pdir, sid + ".jsonl")}  # scanned 会话按其 mtime 判忙/闲
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


def _read_archived_ids(path):
    """Read a list of session ids hidden by the user for a scanned provider."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return {sid for sid in value if isinstance(sid, str)} if isinstance(value, list) else set()
    except (OSError, ValueError, TypeError):
        return set()


def _archive_scanned_session(path, sid):
    """Hide one dynamically scanned session without touching its own log."""
    archived = _read_archived_ids(path)
    archived.add(sid)
    try:
        ensure_dir()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(archived), f)
        try:
            os.replace(tmp, path)
        except OSError:
            # Some managed Windows folders deny rename-on-close; preserve the
            # marker with the same direct-write fallback used by other settings.
            with open(path, "w", encoding="utf-8") as f:
                json.dump(sorted(archived), f)
            try:
                os.remove(tmp)
            except OSError:
                pass
    except OSError:
        pass


def _read_codex_archived():
    """Return Codex sessions hidden from the cc-light list by the user."""
    return _read_archived_ids(CODEX_ARCHIVED_FILE)


def archive_codex_session(sid):
    """Hide a dynamically scanned Codex session without touching its rollout log."""
    _archive_scanned_session(CODEX_ARCHIVED_FILE, sid)


def _read_dsh_archived():
    """Sessions hidden by the user: cc-light's own list plus the app's archive.

    The desktop app records archived sessions in ``workspace.json``; those are
    no longer shown in its sidebar, so they must not appear in the light either.
    """
    archived = _read_archived_ids(DSH_ARCHIVED_FILE)
    try:
        with open(DSH_WORKSPACE_FILE, "r", encoding="utf-8") as f:
            workspace = json.load(f)
        ids = (workspace.get("global") or {}).get("archivedSessionIds")
        if isinstance(ids, list):
            archived.update(sid for sid in ids if isinstance(sid, str))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return archived


def archive_dsh_session(sid):
    """Hide a DeepSeek Harness session; its own checkpoint stays untouched."""
    _archive_scanned_session(DSH_ARCHIVED_FILE, sid)


def remove_inactive_scanned_sessions(active):
    """Drop persisted sessions created by the manual session scan when closed.

    Scanned sessions do not have a hook to remove their state on SessionEnd, so
    they must be reconciled against the current process scan after startup.
    """
    active = set(active or ())
    try:
        files = os.listdir(SESSIONS_DIR)
    except OSError:
        return
    for fn in files:
        if not fn.endswith(".json"):
            continue
        sid = fn[:-5]
        path = os.path.join(SESSIONS_DIR, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if d.get("scanned") and sid not in active:
            try:
                os.remove(path)
            except OSError:
                pass


def _refresh_scanned_state(d, now):
    """scanned 会话无 hook 心跳,按其 jsonl 最后写入判忙/闲:
    jsonl 近期(SCANNED_ACTIVE 内)有写 → 运行中(yellow);停了 → 空闲(green)。
    ts 同步成 jsonl mtime(effective_state/明细排序都用)。只改内存副本,不写文件。"""
    jsonl = d.get("jsonl")
    if not jsonl:
        return
    try:
        mtime = os.path.getmtime(jsonl)
    except OSError:
        return
    d["state"] = "yellow" if (now - mtime < SCANNED_ACTIVE) else "green"
    d["ts"] = mtime


def _is_codex_cli_command(command):
    """Return whether a Node command line belongs to the Codex CLI.

    On Windows the npm launcher runs the TUI through ``node.exe`` while the
    shared app-server is a separate ``codex.exe`` process.  Looking only at
    executable names therefore misses the terminal process that owns the
    session lock.  Keep this predicate narrow so unrelated Node services are
    never treated as Codex terminals.
    """
    text = (command or "").replace("\\", "/").lower()
    return "/@openai/codex/" in text or "/node_modules/@openai/codex/" in text


def _is_codex_exec_command(command):
    """Return whether a Codex command is the non-interactive ``codex exec``.

    ProjectManager launches Codex with ``exec --json`` through pipes and hides
    its child window.  That process can own a rollout lock, but it has no
    terminal HWND to focus.  Treating it as a TUI client makes its parent
    chain resolve to an unrelated Windows Terminal window.
    """
    tokens = ((command or "").replace('"', " ").replace("'", " ")
              .replace("\\", "/").lower().split())
    return "exec" in tokens and any(
        token == "codex.exe" or token.endswith("/codex.exe") or
        token == "codex" or token.endswith("/codex") or
        "@openai/codex" in token
        for token in tokens
    )


def _is_codex_interactive_client(command):
    """Return whether a Codex process can own an interactive terminal."""
    text = (command or "").lower()
    return "app-server" not in text and not _is_codex_exec_command(command)


def _codex_process_starts():
    """Return ``{pid: creation_time}`` for live Codex server and CLI processes."""
    out = {}
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        pids = (wintypes.DWORD * 4096)()
        used = wintypes.DWORD()
        if not psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(used)):
            return out
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        for pid in pids[:used.value // ctypes.sizeof(wintypes.DWORD)]:
            if not pid:
                continue
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                continue
            try:
                image = ctypes.create_unicode_buffer(1024)
                length = wintypes.DWORD(len(image))
                if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
                    continue
                image_name = os.path.basename(image.value).lower()
                if image_name == "codex.exe":
                    is_codex = True
                elif image_name in ("node.exe", "nodejs.exe", "node"):
                    # The npm-installed Windows launcher starts Codex TUI as
                    # node.exe ...\\node_modules\\@openai\\codex\\bin\\codex.js.
                    # _codex_process_context also reads cwd, but command-line
                    # matching is all we need here and avoids broad Node PID
                    # candidates later in session binding.
                    is_codex = _is_codex_cli_command(
                        _codex_process_context(int(pid)).get("command", ""))
                else:
                    is_codex = False
                if not is_codex:
                    continue
                created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
                if kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(kernel_time), ctypes.byref(user_time)):
                    ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
                    out[int(pid)] = ticks / 10_000_000 - 11_644_473_600
            finally:
                kernel.CloseHandle(handle)
    except Exception:
        pass
    return out


def _codex_session_owners(processes):
    """Find live writers, including resumed threads served by the shared daemon.

    None means this Codex/Windows version cannot provide writer ownership.
    An empty dict means ownership was checked and no live writers were found.
    """
    try:
        import ctypes
        from ctypes import wintypes
        files = os.listdir(CODEX_LOCKS_DIR)
        kernel = ctypes.windll.kernel32
        ntdll = ctypes.windll.ntdll
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]

        class IO_STATUS_BLOCK(ctypes.Structure):
            _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

        class FILE_PROCESS_IDS(ctypes.Structure):
            _fields_ = [("count", wintypes.ULONG), ("pids", ctypes.c_size_t * 256)]

        ntdll.NtQueryInformationFile.argtypes = [wintypes.HANDLE,
            ctypes.POINTER(IO_STATUS_BLOCK), ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG]
        out = {}
        for filename in files:
            if filename.startswith(".") or not filename.endswith(".lock"):
                continue
            path = os.path.join(CODEX_LOCKS_DIR, filename)
            handle = kernel.CreateFileW(path, 0x80, 7, None, 3, 0, None)
            if handle == ctypes.c_void_p(-1).value:
                continue
            try:
                status, ids = IO_STATUS_BLOCK(), FILE_PROCESS_IDS()
                # FileProcessIdsUsingFileInformation is a read-only handle-owner query.
                if ntdll.NtQueryInformationFile(handle, ctypes.byref(status),
                        ctypes.byref(ids), ctypes.sizeof(ids), 47):
                    return None
                owners = [int(pid) for pid in ids.pids[:ids.count] if pid in processes]
                if owners:
                    out[filename[:-5]] = {"pid": owners[0], "ts": os.path.getmtime(path)}
            except OSError:
                continue
            finally:
                kernel.CloseHandle(handle)
        return out
    except (OSError, AttributeError):
        return None


def _codex_process_context(pid):
    """Read the command line and cwd to locate a daemon thread's terminal client."""
    out = {}
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        ntdll = ctypes.windll.ntdll
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG,
            ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [("length", wintypes.USHORT), ("maximum", wintypes.USHORT),
                        ("buffer", ctypes.c_void_p)]

        class PBI(ctypes.Structure):
            _fields_ = [("r1", ctypes.c_void_p), ("peb", ctypes.c_void_p),
                        ("r2", ctypes.c_void_p * 2), ("pid", ctypes.c_void_p),
                        ("parent", ctypes.c_void_p)]

        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            try:
                buf = ctypes.create_string_buffer(32768)
                if not ntdll.NtQueryInformationProcess(handle, 60, buf, len(buf), None):
                    command = UNICODE_STRING.from_buffer(buf)
                    out["command"] = ctypes.wstring_at(command.buffer, command.length // 2)
            finally:
                kernel.CloseHandle(handle)
        handle = kernel.OpenProcess(0x410, False, pid)
        if handle:
            try:
                pbi = PBI()
                if ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(pbi),
                                                  ctypes.sizeof(pbi), None) or not pbi.peb:
                    return out
                params, read = ctypes.c_void_p(), ctypes.c_size_t()
                params_offset = 0x20 if ctypes.sizeof(ctypes.c_void_p) == 8 else 0x10
                cwd_offset = 0x38 if ctypes.sizeof(ctypes.c_void_p) == 8 else 0x24
                if not kernel.ReadProcessMemory(handle, pbi.peb + params_offset,
                        ctypes.byref(params), ctypes.sizeof(params), ctypes.byref(read)):
                    return out
                cwd = UNICODE_STRING()
                if kernel.ReadProcessMemory(handle, params.value + cwd_offset,
                        ctypes.byref(cwd), ctypes.sizeof(cwd), ctypes.byref(read)) and cwd.buffer:
                    buf = ctypes.create_unicode_buffer(cwd.length // 2 + 1)
                    if kernel.ReadProcessMemory(handle, cwd.buffer, buf, cwd.length,
                                                ctypes.byref(read)):
                        out["cwd"] = os.path.normcase(os.path.normpath(buf.value))
            finally:
                kernel.CloseHandle(handle)
    except (OSError, AttributeError, ValueError, TypeError):
        pass
    return out


def _codex_log_state(path, cache):
    """Read new complete JSONL lines and track the latest turn event."""
    try:
        if os.path.getsize(path) < cache.get("offset", 0):
            cache.clear()
        with open(path, "rb") as stream:
            if not cache.get("meta"):
                first = json.loads(stream.readline())
                if first.get("type") != "session_meta":
                    return None
                cache["meta"] = first.get("payload", {})
                cache["offset"] = stream.tell()
            stream.seek(cache["offset"])
            while True:
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    break
                cache["offset"] = stream.tell()
                if not any(marker in line for marker in (b'"task_started"', b'"task_complete"', b'"turn_aborted"')):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "event_msg":
                    kind = event.get("payload", {}).get("type")
                    if kind in ("task_started", "task_complete", "turn_aborted"):
                        cache["state"] = "yellow" if kind == "task_started" else "green"
    except (OSError, ValueError, TypeError):
        return None
    return cache.get("meta")


def _codex_terminal_pid(sid, owner, cwd, processes, contexts):
    """Resolve a writer to the most likely live Codex CLI client."""
    owner_command = contexts.get(owner.get("pid"), {}).get("command", "")
    # A background ``codex exec`` writer has no terminal.  Never bind it to a
    # same-project TUI merely because their lock/cwd timestamps are close.
    if _is_codex_exec_command(owner_command):
        return 0
    clients = [pid for pid in processes
               if _is_codex_interactive_client(contexts[pid].get("command", ""))]
    explicit = [pid for pid in clients
                if sid in [arg.strip('\"\'') for arg in
                           contexts[pid].get("command", "").split()]]
    if len(explicit) == 1:
        return explicit[0]
    if len(explicit) > 1:
        return 0
    pid = owner["pid"]
    if pid in clients:
        return pid
    candidates = [client for client in clients if
                  contexts[client].get("cwd") == os.path.normcase(os.path.normpath(cwd))]
    if not candidates:
        # Querying a protected Codex process can hide its cwd. Keep the
        # session usable by choosing the client whose lifetime is closest to
        # the lock owner; this is still better than dropping the terminal id.
        candidates = clients
    if not candidates:
        return 0
    # Lock mtime is updated by the active writer. It is the only signal that
    # separates several Codex terminals opened in the same working directory.
    return min(candidates, key=lambda p: abs(owner.get("ts", 0) - processes[p]))


def scan_codex_sessions(now=None):
    """Collect Codex rollouts owned by a currently running codex.exe."""
    now = time.time() if now is None else now
    if now - _CODEX_CACHE["checked"] < CODEX_SCAN_INTERVAL:
        return _CODEX_CACHE["sessions"]
    processes = _codex_process_starts()
    result = {}
    bindings = _CODEX_CACHE.setdefault("bindings", {})
    archived = _read_codex_archived()
    if processes:
        owners = _codex_session_owners(processes)
        contexts = {pid: _codex_process_context(pid) for pid in processes}
        clients = {pid: created for pid, created in processes.items()
                   if _is_codex_interactive_client(contexts[pid].get("command", ""))}
        try:
            years = os.listdir(CODEX_SESSIONS_DIR)
        except OSError:
            years = []
        for year in years:
            if not year.isdigit() or (owners is None and int(year) < datetime.fromtimestamp(now).year - 1):
                continue
            for root, _dirs, files in os.walk(os.path.join(CODEX_SESSIONS_DIR, year)):
                for fn in files:
                    if not fn.startswith("rollout-") or not fn.endswith(".jsonl"):
                        continue
                    path = os.path.join(root, fn)
                    # The filename lets us skip history without parsing its large metadata.
                    filename_sid = fn[-42:-6]
                    if owners is not None and filename_sid not in owners:
                        continue
                    try:
                        mtime = os.path.getmtime(path)
                    except OSError:
                        continue
                    if owners is None and (not clients or mtime < min(clients.values()) - 30):
                        continue
                    entry = _CODEX_CACHE["logs"].setdefault(path, {})
                    meta = _codex_log_state(path, entry)
                    if not meta:
                        continue
                    sid = meta.get("id") or meta.get("session_id")
                    if not sid:
                        continue
                    if sid in archived:
                        continue
                    cwd = meta.get("cwd") or ""
                    # ProjectManager uses ``codex exec --json`` through pipes.
                    # It creates the same rollout/lock records as the TUI but
                    # has no visible terminal to return to.
                    background = str(meta.get("originator") or "").lower() == "codex_exec"
                    if owners is not None:
                        owner = owners.get(sid)
                        if not owner:
                            continue
                        bound = bindings.get(sid)
                        if isinstance(bound, (tuple, list)):
                            pid, bound_created = bound
                        else:
                            pid, bound_created = bound, None
                        if (background or pid not in processes or
                                bound_created is not None and processes[pid] != bound_created or
                                "app-server" in contexts.get(pid, {}).get("command", "")):
                            pid = (0 if background else
                                   _codex_terminal_pid(sid, owner, cwd, processes, contexts))
                    else:
                        try:
                            started = datetime.fromisoformat(meta["timestamp"].replace("Z", "+00:00")).timestamp()
                        except (KeyError, ValueError, TypeError):
                            continue
                        matches = [(abs(started - created), client) for client, created in clients.items()
                                   if -2 <= started - created <= 30]
                        if not matches:
                            continue
                        _delta, pid = min(matches)
                    name = os.path.basename(cwd.replace("\\", "/").rstrip("/")) or sid[:8]
                    result[sid] = {"provider": "codex", "state": entry.get("state", "green"),
                                   "ts": mtime, "name": name, "termpid": pid,
                                   "jsonl": path, "hwnd": 0, "subs": 0,
                                   "jumpable": not background}
                    if pid:
                        bindings[sid] = (pid, processes.get(pid))
    for sid in list(bindings):
        if sid not in result:
            bindings.pop(sid, None)
    _CODEX_CACHE.update(checked=now, sessions=result)
    return result


def get_codex_title(sid):
    try:
        mtime = os.path.getmtime(CODEX_INDEX_FILE)
        if _CODEX_TITLES["mtime"] != mtime:
            titles = {}
            with open(CODEX_INDEX_FILE, "r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                        if item.get("id") and item.get("thread_name"):
                            titles[item["id"]] = item["thread_name"]
                    except ValueError:
                        continue
            _CODEX_TITLES.update(mtime=mtime, titles=titles)
        return _CODEX_TITLES["titles"].get(sid, "")
    except OSError:
        return ""


def _dsh_process_starts():
    """Return ``{pid: creation_time}`` for every live DeepSeek Harness process.

    Electron spawns renderer/GPU/utility children with the same image name; the
    caller only needs the app's lifetime, so all of them count.  An empty dict
    means the desktop app is not running (checkpoints on disk are then stale).
    """
    out = {}
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        pids = (wintypes.DWORD * 4096)()
        used = wintypes.DWORD()
        if not psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(used)):
            return out
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                      wintypes.LPWSTR,
                                                      ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        for pid in pids[:used.value // ctypes.sizeof(wintypes.DWORD)]:
            if not pid:
                continue
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                continue
            try:
                image = ctypes.create_unicode_buffer(1024)
                length = wintypes.DWORD(len(image))
                if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
                    continue
                if os.path.basename(image.value).lower() != DSH_IMAGE:
                    continue
                created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
                if kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(kernel_time), ctypes.byref(user_time)):
                    ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
                    out[int(pid)] = ticks / 10_000_000 - 11_644_473_600
            finally:
                kernel.CloseHandle(handle)
    except Exception:
        pass
    return out


def _dsh_row(rows, key):
    """One projection checkpoint row's value (or None for a missing/invalid row)."""
    row = rows.get(key) if isinstance(rows, dict) else None
    return row.get("val") if isinstance(row, dict) else None


def _dsh_state(rows):
    """Map DeepSeek Harness projection rows to a light state.

    red    -- ``ask_user_question`` is waiting for an answer.
    yellow -- a turn or step is open, tool calls are pending, or queued input
              still has to run.
    green  -- idle.

    Permission approvals are only streamed to the renderer (the durable
    ``approval/asked`` audit event lives inside the compressed session log),
    so a waiting approval still reads as yellow rather than red.
    """
    questions = _dsh_row(rows, "userQuestions") or {}
    active = (questions.get("questions") or {}).get("active") or []
    if active:
        return "red"
    turn = _dsh_row(rows, "turnBoundary") or {}
    stats = _dsh_row(rows, "sessionStats") or {}
    inbox = _dsh_row(rows, "inbox") or {}
    busy = (turn.get("openTurnStartSeq") is not None or
            stats.get("openStep") is not None or
            bool(stats.get("pendingCalls")) or
            bool(inbox.get("next-turn")) or bool(inbox.get("next-step")))
    return "yellow" if busy else "green"


def scan_dsh_sessions(now=None):
    """Collect DeepSeek Harness sessions from its live projection cache.

    The desktop app rewrites one checkpoint document per session whenever the
    session advances, so the light needs no hook.  Only sessions touched during
    the current app run are listed -- otherwise checkpoints left by a previous
    run would keep ghost sessions visible after the app was closed.  Idle
    sessions fade out after ``DSH_ACTIVE_WINDOW``; busy ones never do.
    """
    now = time.time() if now is None else now
    if now - _DSH_CACHE["checked"] < DSH_SCAN_INTERVAL:
        return _DSH_CACHE["sessions"]
    processes = _dsh_process_starts()
    result = {}
    if processes:
        started = min(processes.values())
        archived = _read_dsh_archived()
        try:
            files = os.listdir(DSH_PROJ_CACHE_DIR)
        except OSError:
            files = []
        for fn in files:
            if not fn.endswith(".json"):
                continue
            sid = fn[:-5]
            if sid in archived:
                continue
            path = os.path.join(DSH_PROJ_CACHE_DIR, fn)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime < started - 120:      # 上一次运行留下的缓存,不当作活跃会话
                continue
            try:
                with open(path, "r", encoding="utf-8") as stream:
                    record = (json.load(stream) or {}).get("record") or {}
            except (OSError, ValueError):
                continue
            rows = record.get("rows") or {}
            meta = _dsh_row(rows, "sessionListMetadata") or {}
            if meta.get("blank", True) or not meta.get("lastPromptAt"):
                continue                    # 从未发过消息的空会话
            state = _dsh_state(rows)
            if state == "green" and now - mtime > DSH_ACTIVE_WINDOW:
                continue                    # 空闲会话放久后隐藏;运行中/等回答不受限
            identity = record.get("identity") or {}
            cwd = identity.get("cwd") or _dsh_row(rows, "workingDirectory") or ""
            name = os.path.basename(cwd.replace("\\", "/").rstrip("/")) or sid[:8]
            # 任务标签与 CC 侧一致:优先最近一轮的 prompt(同会话切任务时不会停在旧标题),
            # 太短的输入(如回复「1」选项)跳过,避免标签失去信息量;都没有时退回 LLM 标题。
            outline = _dsh_row(rows, "turnOutline") or {}
            turns = [t for t in (outline.get("turns") or []) if isinstance(t, dict)]
            latest_prompt = next(
                ((t.get("prompt") or "").strip() for t in reversed(turns)
                 if len((t.get("prompt") or "").strip()) >= 6), "")
            fallback_prompt = (turns[-1].get("prompt") or "").strip() if turns else ""
            title = latest_prompt or (_dsh_row(rows, "title") or "") or fallback_prompt
            result[sid] = {
                "provider": "dsh", "state": state, "ts": mtime,
                "name": name, "termpid": 0, "hwnd": 0, "subs": 0,
                "jsonl": path, "title": title,
                # 窗口标题用的是 LLM 会话标题(不是最近 prompt),跳转匹配按它来
                "window_title": (_dsh_row(rows, "title") or ""),
                "jumpable": True,
            }
    _DSH_CACHE.update(checked=now, sessions=result)
    return result


def _dsh_window(title=""):
    """Find the DeepSeek Harness desktop window, preferring this session's one.

    The app is single-instance and normally owns one window; its caption is
    ``<session title> ● DeepSeek Harness``.  If a future build opens several
    windows, the caption match picks the one currently showing the session.
    """
    want = (title or "").strip().lower()
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.GetParent.argtypes = [wintypes.HWND]
        u.GetParent.restype = wintypes.HWND
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        ENUMWNDPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = [ENUMWNDPROC, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        best = [None, 0]

        def _cb(hwnd, _lp):
            if not u.IsWindowVisible(hwnd) or u.GetParent(hwnd):
                return True
            if DSH_IMAGE not in (_window_process_name(hwnd) or "").lower():
                return True
            n = u.GetWindowTextLengthW(hwnd)
            tb = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, tb, n + 1)
            caption = tb.value.lower()
            score = 1
            if "deepseek harness" in caption:
                score += 4
            if want and want in caption:
                score += 8
            if score > best[1]:
                best[0], best[1] = hwnd, score
            return True

        u.EnumWindows(ENUMWNDPROC(_cb), 0)
        return best[0]
    except Exception:
        return None


def jump_dsh_session(title=""):
    """Focus the DeepSeek Harness window that belongs to this session."""
    hwnd = _dsh_window(title)
    return bool(hwnd and focus_window(hwnd))


def read_sessions(now=None):
    ensure_dir()
    now = now if now is not None else time.time()
    out = {}
    try:
        files = os.listdir(SESSIONS_DIR)
    except OSError:
        files = []
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
            if d.get("scanned"):
                if d.get("termpid") and not is_process_alive(d["termpid"]):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    continue
                _refresh_scanned_state(d, now)   # 无 hook 心跳 → 按 jsonl mtime 实时判忙/闲
            out[fn[:-5]] = d
        except Exception:
            continue
    out.update(scan_codex_sessions(now))
    out.update(scan_dsh_sessions(now))
    return out


def effective_state(d, now=None):
    """单会话有效状态(按 config 应用超时降级)"""
    s = d.get("state", "gray")
    if (s == "yellow" and d.get("provider") not in ("codex", "dsh")
            and _config["timeout_fallback"]
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


def load_snippets():
    """快捷指令列表:[{id,title,text}, ...]。"""
    try:
        with open(SNIPPETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_snippets(items):
    """原子写快捷指令。"""
    try:
        ensure_dir()
        tmp = SNIPPETS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp, SNIPPETS_FILE)
    except Exception:
        pass


def match_record(text, query):
    """记录 text 是否匹配搜索 query(不区分大小写子串;空 query 恒匹配)。"""
    if not query:
        return True
    return query.lower() in " ".join(text.split()).lower()


def preview_snippet(text, query, head=14, ctx=8):
    """生成历史预览片段 + 命中区间(相对片段,用于 Text 高亮)。
    空 query:返回压平后前 head 字(超长补 ' …'),空区间。
    有 query 且命中:返回命中词前后各 ctx 字的片段(头尾按需补 '…'),1 个区间。
    未命中:返回 (None, []) —— 调用方应先用 match_record 过滤。"""
    flat = " ".join(text.split())
    if not query:
        snippet = flat[:head] + (" …" if len(flat) > head else "")
        return snippet, []
    fl = flat.lower()
    idx = fl.find(query.lower())
    if idx < 0:
        return None, []
    start = max(0, idx - ctx)
    end = min(len(flat), idx + len(query) + ctx)
    span_off = 1 if start > 0 else 0       # 头部 '…' 占 1 字符,命中区间随之偏移
    snippet = ("…" if start > 0 else "") + flat[start:end] + ("…" if end < len(flat) else "")
    span_start = (idx - start) + span_off
    return snippet, [(span_start, span_start + len(query))]


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
    sys.stderr.write("usage: cc_light.py [--hook color | --end | --state | --diag]\n")


# ---------------- CLI ----------------
def diag_jump():
    """自检:对每个会话模拟 _jump 跳转链(find_window_by_pid(termpid 父链/控制台映射) 失败
    → resolve_hwnd(hwnd,name)),打印每步与能否定位窗口。诊断「点击项目跳不到对应窗口」。
    只读 session 文件,不归档/不写。"""
    ensure_dir()
    try:
        files = sorted(f for f in os.listdir(SESSIONS_DIR) if f.endswith(".json"))
    except OSError:
        files = []
    if not files:
        print("(无会话)")
        return 0
    print("跳转自检 — 模拟 _jump: find_window_by_pid(termpid 父链/控制台映射) 失败 → resolve_hwnd(hwnd,name)")
    n_ok = 0
    for fn in files:
        sid = fn[:-5]
        try:
            with open(os.path.join(SESSIONS_DIR, fn), "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            print("  [ERR ] %s  读 session 文件失败" % sid[:8])
            continue
        name = d.get("name") or sid[:8]
        hwnd = d.get("hwnd")
        tp = d.get("termpid")
        kind = "scan" if d.get("scanned") else "hook"
        # 与 GUI 的 resolve_session_hwnd 一致:父链/控制台映射优先(WT 多窗口靠它才准),
        # 再退回记录的 hwnd/项目名。
        tgt, via = None, ""
        if tp:
            tgt = find_window_by_pid(tp)
            via = "termpid父链/控制台"
        if not tgt:
            tgt = resolve_hwnd(hwnd, name)
            via = "hwnd/name"
        if tgt:
            n_ok += 1
            print("  [ OK ] %s  %-4s name=%r hwnd=%s tp=%s -> %s (%s)"
                  % (sid[:8], kind, name, hwnd, tp, tgt, via))
        else:
            print("  [FAIL] %s  %-4s name=%r hwnd=%s tp=%s -> 定位不到窗口(进程已退?终端非前台?)"
                  % (sid[:8], kind, name, hwnd, tp))
    print("可跳 %d / 共 %d" % (n_ok, len(files)))
    return 0


def cli():
    a = sys.argv[1:]
    if not a:
        return run_gui()
    if a[0] == "--diag":
        return diag_jump()
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
    """把指定 HWND 的窗口恢复+前置(点击会话名跳转)。返回是否【真正】抢到前台。
    偶发抢不到(Windows 前台锁定:后台 topmost 进程长期运行后 SetForegroundWindow 被静默拒,
    表现为点了不跳、重启才恢复)。ALT 键 trick 打破锁定 + AttachThreadInput + BringWindowToTop
    多管齐下;用 GetForegroundWindow 验证真实结果(SetForegroundWindow 返回值不可靠、会误报失败)。"""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.IsIconic.argtypes = [wintypes.HWND]; u.IsIconic.restype = wintypes.BOOL
        u.IsWindow.argtypes = [wintypes.HWND]; u.IsWindow.restype = wintypes.BOOL
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]; u.ShowWindow.restype = wintypes.BOOL
        u.GetForegroundWindow.argtypes = []; u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.SetForegroundWindow.argtypes = [wintypes.HWND]; u.SetForegroundWindow.restype = wintypes.BOOL
        u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        u.BringWindowToTop.argtypes = [wintypes.HWND]; u.BringWindowToTop.restype = wintypes.BOOL
        hwnd = int(hwnd)
        if not hwnd or not u.IsWindow(hwnd):
            return False
        if u.IsIconic(hwnd):                # 最小化则恢复
            u.ShowWindow(hwnd, 9)           # SW_RESTORE
        # ALT 键 trick:模拟用户按键,系统才放行后台进程抢前台(否则 SetForegroundWindow 被静默拒)
        u.keybd_event(0x12, 0, 0, 0)        # VK_MENU down
        u.keybd_event(0x12, 0, 0x02, 0)     # VK_MENU up
        # 调用线程 attach 到前台线程 → 借前台权限(经典绕过前台锁定):AttachThreadInput(调用,前台)
        k = ctypes.windll.kernel32
        cur = k.GetCurrentThreadId()
        fg = u.GetWindowThreadProcessId(u.GetForegroundWindow(), None)
        attached = False
        if fg and cur != fg:
            u.AttachThreadInput(cur, fg, True)
            attached = True
        u.ShowWindow(hwnd, 5)               # SW_SHOW 确保可见
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
        if attached:
            u.AttachThreadInput(cur, fg, False)
        # SetForegroundWindow 跨进程可能异步:立即查会误报失败,短暂重试验证
        for _ in range(6):
            if u.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.012)
        return False
    except Exception:
        return False


def find_window_by_name(name, process_keyword=None, terminal_only=False):
    """hwnd 失效时的兜底:按项目名模糊匹配当前可见的顶层窗口。
    Trae/VSCode reload 或窗口重建后 hwnd 会变,记录的旧 hwnd 就死了;这时用会话的项目名
    去匹配窗口标题(如 'PortraitEdit.vue - zc-geo-frontend - Trae CN' 含 'zc-geo-frontend'),
    返回真实可见窗口的 hwnd,没有则返回 None。"""
    if (not name or len(name) < 2) and not process_keyword:
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
        terminal_classes = ("CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass",
                            "PseudoConsoleWindow", "ConsoleHostClass")
        best = [None, 0]  # [hwnd, score]

        def _cb(hwnd, _lp):
            if not u.IsWindowVisible(hwnd) or u.GetParent(hwnd):
                return True
            n = u.GetWindowTextLengthW(hwnd)
            if n <= 0 and not process_keyword:
                return True
            tb = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, tb, n + 1)
            title = tb.value
            if target and (not title or target not in title.lower()):
                return True
            pname = (_window_process_name(hwnd) or "").lower()
            if process_keyword:
                if process_keyword.lower() not in pname:
                    return True
            cb = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, cb, 256)
            cls = cb.value
            if terminal_only:
                is_terminal_class = any(cls == c or cls.startswith(c)
                                        for c in terminal_classes)
                is_terminal_process = any(token in pname for token in
                                          ("terminal", "conhost", "cmd.exe",
                                           "powershell", "pwsh", "wezterm",
                                           "alacritty", "conemu"))
                if not (is_terminal_class or is_terminal_process):
                    return True
            score = 1
            if any(cls == c or cls.startswith(c) for c in term_classes):
                score += 5                       # 终端/编辑器宿主类优先
            tl = title.lower()
            # 标题里 name 单独成段(典型 'file - name - Trae CN')比单纯子串更可信
            if (" - " + target + " - ") in tl or tl.startswith(target + " -") or tl.endswith(" - " + target) or tl == target:
                score += 3
            # 标题尾段恰好是项目名(如 TAPD 专用执行终端 'TAPD-exec <name>'):是专为该项目开的
            # 会话终端,比同名项目的编辑器窗口(Trae 中间段模式)更属于该会话,hwnd 失效兜底时优先
            if tl.endswith(" " + target):
                score += 4
            if score > best[1]:
                best[0] = hwnd; best[1] = score
            return True

        u.EnumWindows(ENUMWNDPROC(_cb), 0)
        return best[0]
    except Exception:
        return None


def find_trae_window(name):
    """Find the Trae workbench for a project title."""
    return (find_window_by_name(name, process_keyword="trae") or
            find_window_by_name(None, process_keyword="trae"))


def _ancestor_pids(pid, max_depth=8):
    """pid 及其父进程链(沿 InheritedFromUniqueProcessId 向上),用于沿链找宿主窗口:
    termpid 是 powershell(无窗口),真正的终端/编辑器窗口在祖父 WindowsTerminal/Trae 等。
    纯 ctypes,去重防循环。读失败/到顶返回已收集部分。"""
    out = []
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        ntdll = ctypes.windll.ntdll
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]

        class PBI(ctypes.Structure):
            _fields_ = [("r1", ctypes.c_void_p), ("peb", ctypes.c_void_p),
                        ("r2", ctypes.c_void_p * 2), ("pid", ctypes.c_void_p), ("r3", ctypes.c_void_p)]
        cur = int(pid)
        for _ in range(max_depth):
            if not cur or cur in out:
                break
            out.append(cur)
            h = k.OpenProcess(0x1000, False, cur)   # QUERY_LIMITED_INFORMATION
            if not h:
                break
            try:
                pbi = PBI()
                if ntdll.NtQueryInformationProcess(h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None):
                    break
                cur = int(pbi.r3 or 0)
            finally:
                k.CloseHandle(h)
    except Exception:
        pass
    return out


def trae_focus_uri(pid):
    pids = _ancestor_pids(pid)
    if not pids:
        return None
    return "trae-cn://cc-light.cc-light-helper/focus?" + "&".join("pid=" + str(p) for p in pids)


def focus_trae_terminal(hwnd, termpid):
    """Select a session's terminal in Trae after focusing its host window."""
    if not hwnd or not termpid or not _is_trae_wnd(hwnd):
        return False
    try:
        uri = trae_focus_uri(termpid)
        if uri:
            os.startfile(uri)
            return True
    except Exception as exc:
        log_error(exc, "Trae 终端会话切换失败")
    return False


def _console_window_map():
    """把「拥有控制台的 shell 进程 PID」映射到它所在的精确顶层窗口。

    WindowsTerminal.exe 的所有顶层窗口共用同一个进程,PID 无法区分窗口 —— 按 PID 匹配只会
    拿到 EnumWindows 最先枚举到的那个(用户表现为「点 A 会话跳到 B 窗口」)。但每个 ConPTY
    tab 都有一个 `PseudoConsoleWindow`,它的 GW_OWNER 正是该 tab 所属的 WT 顶层窗口,而它
    自己的 PID 就是该 tab 的 shell(powershell/cmd)。经典 conhost 则相反:窗口类
    `ConsoleWindowClass` 属于 conhost.exe,而 conhost 是 shell 的子进程。

    返回 {shell_pid: hwnd};只收录可见宿主窗口。
    """
    mapping = {}
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        ENUMWNDPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = [ENUMWNDPROC, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        u.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
        u.GetWindow.restype = wintypes.HWND
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        GW_OWNER = 4

        def _pid_of(hwnd):
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return int(pid.value)

        def _class_of(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            return buf.value if u.GetClassNameW(hwnd, buf, len(buf)) else ""

        def _cb(hwnd, _lp):
            cls = _class_of(hwnd)
            if cls == "PseudoConsoleWindow":
                owner = u.GetWindow(hwnd, GW_OWNER)
                if owner and u.IsWindowVisible(owner):
                    mapping.setdefault(_pid_of(hwnd), int(owner))
            elif cls in ("ConsoleWindowClass", "ConsoleHostClass"):
                if u.IsWindowVisible(hwnd):
                    conhost_pid = _pid_of(hwnd)
                    mapping.setdefault(conhost_pid, int(hwnd))
                    chain = _ancestor_pids(conhost_pid, max_depth=2)
                    if len(chain) > 1:
                        mapping.setdefault(chain[1], int(hwnd))
            return True

        u.EnumWindows(ENUMWNDPROC(_cb), 0)
    except Exception:
        return {}
    return mapping


def _window_for_console_pids(pids):
    """沿父链(近→远)找第一个拥有控制台窗口的进程,返回其精确宿主窗口。"""
    if not pids:
        return None
    mapping = _console_window_map()
    for p in pids:
        hwnd = mapping.get(int(p))
        if hwnd:
            return hwnd
    return None


def find_window_by_pid(pid, unique=False):
    """按进程 PID 沿【父链】找一个可见顶层窗口。termpid 是 powershell(无窗口),真正的终端/
    编辑器窗口在祖父(WindowsTerminal/Trae 主进程),所以收集 termpid 及其祖先 pid,一次
    EnumWindows 选父链中最近进程拥有的可见顶层窗口。scanned 会话 hwnd 失效/为 0 时,
    _jump 用它兜底定位 WT/Trae 宿主窗口 focus(只查 termpid 本身永远 None —— powershell 无窗)。"""
    pids = _ancestor_pids(pid)
    if not pids:
        return None
    # 先走控制台映射:WT 所有顶层窗口共享同一 PID,按 PID 只能拿到第一个窗口(必错)。
    exact = _window_for_console_pids(pids)
    if exact:
        return exact
    ranks = {p: rank for rank, p in enumerate(pids)}
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.IsWindowVisible.argtypes = [wintypes.HWND]; u.IsWindowVisible.restype = wintypes.BOOL
        u.GetParent.argtypes = [wintypes.HWND]; u.GetParent.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        ENUMWNDPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = [ENUMWNDPROC, wintypes.LPARAM]; u.EnumWindows.restype = wintypes.BOOL
        found = [None, len(pids)]
        same_rank = []

        def _cb(hwnd, _lp):
            if not u.IsWindowVisible(hwnd) or u.GetParent(hwnd):
                return True
            v = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(v))
            rank = ranks.get(v.value, len(pids))
            if rank < found[1]:
                found[:] = [hwnd, rank]
                same_rank[:] = [hwnd]
                if not unique and rank == 0:
                    return False
            elif unique and rank == found[1]:
                same_rank.append(hwnd)
            return True

        u.EnumWindows(ENUMWNDPROC(_cb), 0)
        if unique:
            return same_rank[0] if len(same_rank) == 1 else None
        return found[0]
    except Exception:
        return None


def _focus_browser():
    """把已打开的浏览器窗口(Chrome/Edge)带到前台——不新开 tab,配合焦点文件由页面自行切项目。"""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.IsWindowVisible.argtypes = [wintypes.HWND]; u.IsWindowVisible.restype = wintypes.BOOL
        u.GetParent.argtypes = [wintypes.HWND]; u.GetParent.restype = wintypes.HWND
        ENUMWNDPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = [ENUMWNDPROC, wintypes.LPARAM]; u.EnumWindows.restype = wintypes.BOOL
        found = [None]

        def _cb(hwnd, _lp):
            if not u.IsWindowVisible(hwnd) or u.GetParent(hwnd):
                return True
            n = (_window_process_name(hwnd) or "").lower()
            if n in ("chrome.exe", "msedge.exe"):
                found[0] = hwnd
                return False
            return True

        u.EnumWindows(ENUMWNDPROC(_cb), 0)
        if found[0]:
            focus_window(found[0])
    except Exception:
        pass


def _window_process_name(hwnd):
    """窗口所属进程的可执行文件名(如 'Trae CN.exe'/'WindowsTerminal.exe')。"""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        v = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(v))
        pid = int(v.value)
        if not pid:
            return ""
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        h = k.OpenProcess(0x1000, False, pid)            # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260); n = wintypes.DWORD(260)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return os.path.basename(buf.value)
        finally:
            k.CloseHandle(h)
    except Exception:
        pass
    return ""


def _window_title(hwnd):
    """窗口标题(GetWindowTextW),取不到返回空串。"""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        tb = ctypes.create_unicode_buffer(512)
        return tb.value if u.GetWindowTextW(hwnd, tb, 512) else ""
    except Exception:
        return ""


def _is_trae_wnd(hwnd):
    """窗口是否属于 Trae 进程。trae-cn:// URI 只该对 Trae 多终端发:WT 多 tab 也共享宿主窗口 hwnd,
    误判 shared 发 URI → Trae helper 兜底 workbench.action.terminal.focus 激活 Trae 抢前台,把刚 focus
    的 WT 挤走(表现为「点 WT 会话,窗口出来几秒后又最小化」)。"""
    return "trae" in _window_process_name(hwnd).lower()


def _find_codex_window(names):
    """Find a terminal window whose title belongs to a Codex session.

    Windows Terminal can host several top-level windows in one process, so a
    parent PID identifies the host process but not the correct HWND. Codex
    updates the terminal title with the thread title/project; use that signal
    only when it was actually supplied.  The caller deliberately decides when
    a project-name fallback is safe: several Codex sessions in one project
    have the same project name and must never be resolved by that name alone.
    """
    seen = set()
    for value in names or ():
        value = (value or "").strip()
        key = value.lower()
        if len(value) < 2 or key in seen:
            continue
        seen.add(key)
        # A desktop Codex window can also contain the same thread title.  Its
        # process name is therefore not a reliable terminal discriminator;
        # prefer Windows Terminal/conhost and then terminal window classes.
        for keyword in ("windowsterminal", "conhost"):
            target = find_window_by_name(value, process_keyword=keyword)
            if target:
                return target
        target = find_window_by_name(value, terminal_only=True)
        if target:
            return target
    return None


def _window_class(hwnd):
    """Return a window class name without raising on stale HWNDs."""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        buf = ctypes.create_unicode_buffer(256)
        return buf.value if u.GetClassNameW(hwnd, buf, len(buf)) else ""
    except Exception:
        return ""


def _terminal_window_for_pid(pid):
    """Resolve a process to the exact terminal window hosting its console.

    ``WindowsTerminal.exe`` is shared by all top-level windows, so walking the
    process parent chain cannot distinguish them.  The ConPTY mapping below is
    exact and needs no process attachment; ``_attached_console_window`` is the
    classic-conhost fallback for hosts whose console window is not linked to
    the shell through ``PseudoConsoleWindow``.
    """
    if not pid:
        return None
    pids = _ancestor_pids(pid)
    exact = _window_for_console_pids(pids)
    if exact:
        return exact
    return _attached_console_window(pids)


def _attached_console_window(pids):
    """Fallback: attach to the shell and walk its console window to the host.

    The helper is best effort.  Some Codex processes are detached from a
    console (for example ``codex exec``), in which case the normal title/PID
    fallbacks remain available.
    """
    if not pids:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        u = ctypes.windll.user32
        k.GetConsoleWindow.argtypes = []
        k.GetConsoleWindow.restype = wintypes.HWND
        k.AttachConsole.argtypes = [wintypes.DWORD]
        k.AttachConsole.restype = wintypes.BOOL
        k.FreeConsole.argtypes = []
        k.FreeConsole.restype = wintypes.BOOL
        u.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
        u.GetAncestor.restype = wintypes.HWND
        u.GetParent.argtypes = [wintypes.HWND]
        u.GetParent.restype = wintypes.HWND
        u.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
        u.GetWindow.restype = wintypes.HWND
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsWindow.restype = wintypes.BOOL
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL

        # A GUI pythonw process normally has no console.  Never detach a
        # console that belongs to the caller if cc-light was started from a
        # console for debugging.
        if k.GetConsoleWindow():
            return None

        # Try the client and its shell/conhost ancestors.  AttachConsole only
        # succeeds for a process that still owns a console, so this also deals
        # with cmd/PowerShell wrappers around the native Codex client.
        for candidate_pid in pids:
            attached = bool(k.AttachConsole(int(candidate_pid)))
            if not attached:
                continue
            try:
                console = k.GetConsoleWindow()
                if not console:
                    continue
                # PseudoConsoleWindow is usually owned by the WT host.  Walk
                # both owner and parent links because the exact relationship
                # differs between Windows Terminal releases.
                pending = [int(console)]
                seen = set()
                while pending:
                    hwnd = pending.pop(0)
                    if not hwnd or hwnd in seen or not u.IsWindow(hwnd):
                        continue
                    seen.add(hwnd)
                    cls = _window_class(hwnd)
                    if cls == "CASCADIA_HOSTING_WINDOW_CLASS" and u.IsWindowVisible(hwnd):
                        return hwnd
                    for relation in (4,):  # GW_OWNER; parent/ancestor below
                        try:
                            related = u.GetWindow(hwnd, relation)
                        except Exception:
                            related = 0
                        if related and related not in seen:
                            pending.append(int(related))
                    parent = u.GetParent(hwnd)
                    if parent and int(parent) not in seen:
                        pending.append(int(parent))
                    root = u.GetAncestor(hwnd, 2)  # GA_ROOT
                    if root and int(root) not in seen:
                        pending.append(int(root))
            finally:
                k.FreeConsole()
            # If this process exposed a console but the relationship did not
            # lead back to a WT host, try the next ancestor (cmd/PowerShell or
            # conhost can expose a different pseudo-console handle).
    except Exception:
        return None
    return None


def jump_codex_session(pid, name, title=None):
    """Focus the existing terminal that owns this session's CLI process.

    Project names and shared app-server processes do not identify a terminal.
    Prefer the live process ancestry.  If the process has already detached,
    use a Codex window title as a final recovery path.
    """
    # The process's console is the exact locator: each ConPTY tab's
    # PseudoConsoleWindow is owned by its Windows Terminal top-level window.
    target = _terminal_window_for_pid(pid) if pid else None
    # Fall back to the unique Codex thread title. Do not pass the project name
    # in the same lookup: if three Codex sessions share ``cc-light``, the first
    # title-less match is exactly the wrong second-window jump users see.
    title_keys = []
    if title:
        # Current Codex captions are usually ``<thread> | <project>``.  The
        # combined key avoids selecting an older window with the same short
        # thread name when several sessions are open at once.
        if name:
            title_keys.append("%s | %s" % (title, name))
        title_keys.append(title)
    if not target and title_keys:
        target = _find_codex_window(tuple(title_keys))
    # A project-name lookup is safe only when no per-thread title exists.  It
    # is retained for old Codex builds that never set a title, but never used
    # as a fallback after a title lookup failed.
    if not target and not title:
        target = _find_codex_window((name,))
    if not target:
        # A shared WindowsTerminal.exe owns several top-level HWNDs.  The
        # ancestry fallback is acceptable only when it yields one candidate;
        # choosing the first enumeration result recreates the third-to-second
        # jump.
        target = find_window_by_pid(pid, unique=True) if pid else None
    if not target:
        return False
    focused = focus_window(target)
    if _is_trae_wnd(target):
        return bool(focused and focus_trae_terminal(target, pid))
    return bool(focused)


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


def resolve_session_hwnd(hwnd, termpid, name):
    """Resolve a session's host window, preferring its live process ancestry.

    A stored HWND identifies only the host window. Multiple terminal sessions
    can share that HWND, so a live terminal PID is the more specific locator.
    """
    if termpid:
        target = find_window_by_pid(termpid)
        if target:
            return target
    return resolve_hwnd(hwnd, name)


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

    # 全局兜底:GUI 回调里的未处理异常一律记日志(pythonw 下 stderr 看不到,只能靠它)
    def _on_gui_exception(exc, val, tb):
        log_error(val, "GUI回调", tb)
    root.report_callback_exception = _on_gui_exception

    # 明细窗口命名字体(单例):滚轮 configure size 时所有引用的 Label 自动跟随更新;
    # 单例避免每次 hover 重建泄漏。_detail_base 每次 hover 重置 → 移开重进不保留缩放。
    import tkinter.font as _tkfont
    _detail_fonts = {
        "name":  _tkfont.Font(family="Microsoft YaHei", size=8),
        "word":  _tkfont.Font(family="Microsoft YaHei", size=8, weight="bold"),
        "small": _tkfont.Font(family="Microsoft YaHei", size=7),
        "dot":   _tkfont.Font(family="Consolas", size=10),
        "mono":  _tkfont.Font(family="Consolas", size=7),
    }
    _detail_base = [8]   # 当前缩放基数(单例保留:移开重进沿用上次比例)

    def _add_tooltip(widget, text):
        """widget 悬停弹出 tooltip 显示完整 text(任务标签超 20 字时悬浮看全)。"""
        tip = [None]
        def _show(_e):
            if not text:
                return
            try:
                t = tk.Toplevel(widget)
                t.overrideredirect(True)
                t.attributes("-topmost", True)
                tk.Label(t, text=text, bg="#2a2a2a", fg="#e8e8e8",
                         font=("Microsoft YaHei", 8), padx=6, pady=3).pack()
                t.update_idletasks()
                t.geometry("+%d+%d" % (widget.winfo_rootx() + 10,
                                       widget.winfo_rooty() + widget.winfo_height() + 4))
                tip[0] = t
            except Exception:
                pass
        def _hide(_e):
            if tip[0]:
                try: tip[0].destroy()
                except Exception: pass
                tip[0] = None
        widget.bind("<Enter>", _show, add="+")
        widget.bind("<Leave>", _hide, add="+")

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
    detail_menu_active = [False]   # 右键菜单弹出期间抑制明细窗口的 Leave 自动关闭(防菜单随之消失)

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
        if detail_menu_active[0]:       # 菜单打开中(鼠标移到菜单上触发了 Leave)→ 别关,否则菜单跟着没
            return
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
        # 显示顺序:用户拖拽顺序(order.json)优先,其余按优先级+ts 追加
        auto = sorted(sessions.items(),
                      key=lambda kv: (PRIO.get(effective_state(kv[1], now), 99), -kv[1].get("ts", 0)))
        manual = read_order()
        if manual:
            smap = dict(auto)
            order = [(s, smap[s]) for s in manual if s in smap]
            seen = {s for s, _ in order}
            order += [(s, d) for s, d in auto if s not in seen]
        else:
            order = auto
        names = [d.get("name", "") for _, d in sessions.items()]
        tk.Label(win, text=" 会话明细(悬停查看 · 滚轮缩放 · 长按拖拽排序)", fg="#9a9a9a", bg=BG,
                 font=_detail_fonts["name"]).grid(row=0, column=0, sticky="w", padx=6, pady=(4, 2))
        if not order:
            tk.Label(win, text=" (无活跃会话)", fg="#888888", bg=BG,
                     font=_detail_fonts["name"]).grid(row=1, column=0, sticky="w", padx=6, pady=2)

        rows_info = []   # [(sid, row, hwnd, termpid, shared, name), ...] 拖拽交换 / 单击跳转用
        drag = {"sid": None, "start_y": 0, "moved": False}
        drag_card = [None]   # 跟随鼠标的浮动卡片(↕ 项目名),拖拽时显示

        def _drag_style(on, drag_sid=None):           # 拖拽视觉反馈:被拖行高亮(深色),所有行换移动光标(fleur)
            for _s, _r, *_ in rows_info:
                bg = "#3a3a3a" if (on and _s == drag_sid) else BG
                cur = "fleur" if on else "hand2"
                try:
                    _r.configure(bg=bg, cursor=cur)
                    for _c in _r.winfo_children():
                        _c.configure(bg=bg, cursor=cur)
                except Exception:
                    pass

        def _drag_card_show(sid):                     # 显示浮动卡片(↕ + 被拖会话名)
            nm = next((t[5] for t in rows_info if t[0] == sid), sid[:8])
            c = tk.Toplevel(win)
            c.overrideredirect(True)
            c.attributes("-topmost", True)
            c.configure(bg="#2a2a2a")
            tk.Label(c, text="↕ " + nm, fg="#e8e8e8", bg="#2a2a2a",
                     font=("Microsoft YaHei", 8, "bold"), padx=8, pady=4).pack()
            drag_card[0] = c

        def _drag_card_move(x, y):                    # 卡片跟随鼠标(右下偏移,不遮挡光标)
            if drag_card[0]:
                try:
                    drag_card[0].geometry("+%d+%d" % (x + 14, y + 10))
                except Exception:
                    pass

        def _drag_card_hide():
            if drag_card[0]:
                try: drag_card[0].destroy()
                except Exception: pass
                drag_card[0] = None

        def _do_jump(sid):                            # 短按:跳该会话窗口(失败记日志,成功不记)
            for _s, _r, _h, _t, _sh, _nm, _sc in rows_info:
                if _s != sid:
                    continue
                if sessions.get(sid, {}).get("provider") == "codex":
                    _codex = sessions.get(sid, {})
                    if _codex.get("jumpable") is False:
                        log_error(None, "Codex 会话没有可返回的终端: sid=%s name=%r"
                                  % (sid[:8], _nm))
                        break
                    _title = get_codex_title(sid)
                    if not jump_codex_session(_t, _codex.get("name") or "", _title):
                        log_error(None, "Codex 跳转失败: sid=%s pid=%s name=%r"
                                  % (sid[:8], _t, _nm))
                    break
                if sessions.get(sid, {}).get("provider") == "dsh":
                    _dsh = sessions.get(sid, {})
                    if not jump_dsh_session(_dsh.get("window_title") or _dsh.get("title") or ""):
                        log_error(None, "DeepSeek 跳转失败: sid=%s name=%r"
                                  % (sid[:8], _nm))
                    break
                # scanned=后台隐藏任务:不做任何窗口查找 —— 按项目名兜底会误中同名的 IDE 窗口,
                # 按父链会误中后端宿主窗口;直接走焦点文件+打开管理页
                tgt = None
                if not _sc:
                    tgt = resolve_session_hwnd(_h, _t, _nm)
                if tgt:
                    if not focus_window(tgt):
                        log_error(None, "跳转 focus 失败(前台锁定?): sid=%s name=%r hwnd=%s tgt=%s"
                                  % (sid[:8], _nm, _h, tgt))
                else:
                    # 后台隐藏任务(ProjectManager 管理页 spawn 的 claude,无终端窗口):
                    # 写焦点文件(页面 2s 内自动切对应项目) + 打开/跳转管理页
                    log_error(None, "跳转找不到窗口(后台任务→切管理页): sid=%s name=%r termpid=%s"
                              % (sid[:8], _nm, _t))
                    try:
                        import json, time
                        with open(r"C:\Users\zhongce-pm\Desktop\newWork\ProjectManager\data\tapd_sync\focus.json",
                                  "w", encoding="utf-8") as _f:
                            json.dump({"sid": sid, "proj": _nm or "", "ts": int(time.time() * 1000)}, _f)
                    except Exception:
                        pass
                    try:
                        import webbrowser
                        webbrowser.open("http://192.168.1.165:3667/#/work/tapd")
                    except Exception:
                        pass
                # The hwnd may be missing or recorded differently for each
                # session even when Trae hosts them in one workbench. The PID
                # chain is the source of truth for selecting the terminal.
                focus_trae_terminal(tgt, _t)
                break
            close_details()

        def _row_sid_at(y_root):                      # 鼠标 y 落在哪一行
            for _s, _r, *_ in rows_info:
                try:
                    if not _r.winfo_ismapped():
                        continue
                    ry = _r.winfo_rooty(); rh = _r.winfo_height()
                except Exception:
                    continue
                if ry <= y_root <= ry + rh:
                    return _s
            return None

        def _swap(sida, sidb):                        # 交换两行在 rows_info 的顺序 + regrid(丝滑换位)
            if sida == sidb:
                return
            ia = next((i for i, t in enumerate(rows_info) if t[0] == sida), -1)
            ib = next((i for i, t in enumerate(rows_info) if t[0] == sidb), -1)
            if ia < 0 or ib < 0:
                return
            rows_info[ia], rows_info[ib] = rows_info[ib], rows_info[ia]
            for i, t in enumerate(rows_info, start=1):
                t[1].grid(row=i, column=0, sticky="w", padx=6, pady=1)

        def _on_press(e, sid):                        # 左键按下:记录,临时全局绑 motion/release
            drag.update(sid=sid, start_y=e.y_root, moved=False)
            win.bind_all("<B1-Motion>", _on_motion)
            win.bind_all("<ButtonRelease-1>", _on_release)

        def _on_motion(e):                            # 拖动 >5px 视为排序:高亮+浮动卡片,和鼠标所在行交换
            if abs(e.y_root - drag["start_y"]) > 5:
                if not drag["moved"]:
                    drag["moved"] = True
                    _drag_style(True, drag["sid"])
                    _drag_card_show(drag["sid"])
                _drag_card_move(e.x_root, e.y_root)
                tgt = _row_sid_at(e.y_root)
                if tgt and tgt != drag["sid"]:
                    _swap(drag["sid"], tgt)

        def _on_release(_e):                          # 释放:拖了→存顺序;没拖→单击跳转
            win.unbind_all("<B1-Motion>")
            win.unbind_all("<ButtonRelease-1>")
            _drag_card_hide()
            if drag["moved"]:
                _drag_style(False)                    # 恢复行样式
                write_order([t[0] for t in rows_info])
            elif drag["sid"] is not None:
                _do_jump(drag["sid"])

        for i, (sid, d) in enumerate(order, start=1):
            raw = d.get("state", "gray")
            st = effective_state(d, now)
            stale = (raw == "yellow" and st == "green")
            color = COLORS.get(st, {}).get("on", "#666666")
            name = d.get("name") or sid[:8]
            if d.get("provider") == "codex":
                name = "Codex · " + name
            elif d.get("provider") == "dsh":
                name = "DeepSeek · " + name
            multi = names.count(d.get("name", "")) > 1
            hwnd = d.get("hwnd")
            termpid = d.get("termpid")
            shared = hwnd_count.get(hwnd, 0) > 1
            row = tk.Frame(win, bg=BG)
            row.grid(row=i, column=0, sticky="w", padx=6, pady=1)
            rows_info.append((sid, row, hwnd, termpid, shared, name, bool(d.get("scanned"))))
            tk.Label(row, text="●", fg=color, bg=BG, font=_detail_fonts["dot"]).pack(side="left")
            word = STATE_WORD.get(st, "") + (" ·超时" if stale else "")
            tk.Label(row, text=" " + word, fg=color, bg=BG, font=_detail_fonts["word"]).pack(side="left")
            tk.Label(row, text="  " + name, fg="#dddddd", bg=BG, font=_detail_fonts["name"]).pack(side="left")
            _title = (get_codex_title(sid) if d.get("provider") == "codex"
                      else (d.get("title") or "") if d.get("provider") == "dsh"
                      else get_session_title(sid, d.get("jsonl")))   # 任务标签
            _label = _title or ("#" + sid[:4] if multi else "")  # 无标题:多会话用 sid 区分,单会话省略
            if _label:
                _disp = (_title[:20] + "…") if (len(_title) > 20) else _label   # 超 20 字截断,悬浮看全
                _tlbl = tk.Label(row, text="  " + _disp, fg="#8590a0", bg=BG, font=_detail_fonts["small"])
                _tlbl.pack(side="left")
                if _title and len(_title) > 20:
                    _add_tooltip(_tlbl, _title)
            if shared and termpid:                       # 同窗口多终端 → 带 #pid
                tk.Label(row, text="  #" + str(termpid), fg="#9ab8e8", bg=BG, font=_detail_fonts["mono"]).pack(side="left")
            tk.Label(row, text="  " + human_ago(d.get("ts", 0)), fg="#777777", bg=BG,
                     font=_detail_fonts["mono"]).pack(side="left")
            # 左键:短按跳转 / 长按拖拽排序(>5px 视为拖拽);右键删除
            row.bind("<ButtonPress-1>", lambda e, sid=sid: _on_press(e, sid))
            row.configure(cursor="hand2")
            def _on_right(e, s=sid):
                cancel_close()                      # 清掉已排的自动关闭,给菜单留出操作时间
                detail_menu_active[0] = True        # 菜单期间抑制 Leave→destroy,否则菜单一弹出就消失
                m = tk.Menu(root, tearoff=0, bg=BG, fg="#dddddd",
                            activebackground="#3a3a3a", activeforeground="#ffffff")
                def _do():
                    detail_menu_active[0] = False
                    if sessions.get(s, {}).get("provider") == "codex":
                        archive_codex_session(s)
                        _CODEX_CACHE["checked"] = 0
                        _CODEX_CACHE["sessions"].pop(s, None)
                    elif sessions.get(s, {}).get("provider") == "dsh":
                        archive_dsh_session(s)
                        _DSH_CACHE["checked"] = 0
                        _DSH_CACHE["sessions"].pop(s, None)
                    else:
                        archive_session(s)
                    close_details()
                    root.after(20, details_hover)
                m.add_command(label="删除该会话", command=_do)
                try:
                    m.tk_popup(e.x_root, e.y_root)
                finally:
                    m.grab_release()
                    detail_menu_active[0] = False   # 菜单关闭(选中或 Esc/点外)→ 恢复 Leave 自动关闭
            row.bind("<Button-3>", _on_right)
            for _w in row.winfo_children():
                _w.bind("<ButtonPress-1>", lambda e, sid=sid: _on_press(e, sid))
                _w.configure(cursor="hand2")
                _w.bind("<Button-3>", _on_right)

        def _place():
            win.update_idletasks()
            ww = win.winfo_reqwidth(); wh = win.winfo_reqheight()
            sw = root.winfo_screenwidth(); sh = root.winfo_screenheight()
            x = root.winfo_rootx() + W + 4
            if x + ww > sw:
                x = root.winfo_rootx() - ww - 4
            y = root.winfo_rooty()
            if y + wh > sh - 4:            # 超屏底 → 向上展开(底部对齐灯底)
                y = root.winfo_rooty() + H - wh
            x = max(0, min(x, sw - ww))    # 缩放后可能很宽/高 → 左右上下贴边,不出屏
            y = max(0, min(y, sh - wh))
            win.geometry("+%d+%d" % (x, y))

        def _on_wheel(e):                  # 滚轮缩放:调命名字体 size(所有 Label 自动跟)+ 重新定位(不超屏)
            _detail_base[0] = max(5, min(22, _detail_base[0] + (1 if e.delta > 0 else -1)))
            b = _detail_base[0]
            _detail_fonts["name"].configure(size=b)
            _detail_fonts["word"].configure(size=b)
            _detail_fonts["small"].configure(size=b - 1)
            _detail_fonts["dot"].configure(size=b + 2)
            _detail_fonts["mono"].configure(size=b - 1)
            _place()

        def _bind_wheel(w):                # 递归绑滚轮到明细内所有 widget(鼠标在任意子元素上滚都能缩放)
            w.bind("<MouseWheel>", _on_wheel)
            for _c in w.winfo_children():
                _bind_wheel(_c)
        _bind_wheel(win)

        _place()
        win.bind("<Enter>", lambda e: cancel_close())
        win.bind("<Leave>", lambda e: schedule_close())

    def cleanup_stale():
        """归档超过超时阈值无更新的会话(归档而非真删,可用「检测所有会话」还原)。
        scanned 会话(无 hook 心跳)不按 ts 归档 —— 其 ts 被 _refresh_scanned_state 同步成
        jsonl mtime,claude 跑长工具停更几十秒属正常,按 ts 归档会和后端心跳形成删/写循环
        (会话点反复闪现)。只在 jsonl 停更超 1 小时(真死)才清。"""
        thr = _config["yellow_timeout"]
        now = time.time()
        for sid, d in list(read_sessions().items()):
            if d.get("provider") in ("codex", "dsh"):
                continue
            if d.get("scanned"):
                j = d.get("jsonl")
                if j:
                    try:
                        if now - os.path.getmtime(j) > 3600:
                            archive_session(sid)
                    except OSError:
                        pass
                continue
            if now - d.get("ts", 0) > thr:
                tp = d.get("termpid")
                if tp and is_process_alive(tp):
                    try:
                        d["ts"] = now
                        _write_session_file(sid, d)
                    except Exception:
                        pass
                    continue
                archive_session(sid)
        sessions = read_sessions()
        c, n = aggregate(sessions)
        apply(c)
        render_dots(sessions, time.time())

    def restore_sessions():
        _CODEX_CACHE["checked"] = 0
        scan_codex_sessions(time.time())
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
                "subs": 0, "hwnd": info.get("hwnd", 0), "termpid": info.get("termpid", 0),
                "jsonl": info.get("jsonl", ""), "scanned": True,
            })
        # 清理:scanned 会话其进程已没了(不在 active) → 删
        remove_inactive_scanned_sessions(active)
        # ---- 2) archive 还原(兼容老数据) ----
        try:
            files = os.listdir(ARCHIVE_DIR)
        except OSError:
            files = []
        for fn in files:
            if not fn.endswith(".json"):
                continue
            sid = fn[:-5]
            src = os.path.join(ARCHIVE_DIR, fn)
            dst = os.path.join(SESSIONS_DIR, fn)
            if os.path.exists(dst):              # 列表已有(活跃),归档副本作废
                try: os.remove(src)
                except OSError: pass
                continue
            # cwd 判活:该 sid 当前真有 claude.exe 在跑(在 active 里)才还原。
            # 旧版用 is_session_alive 按 termpid/hwnd 判 → PID 复用/项目名误匹配会把
            # 已结束的会话当活还原 → 全显示运行中且 hwnd/termpid 已死、点不动切不过。
            if sid not in active:
                try: os.remove(src)
                except OSError: pass
                continue
            try:
                with open(src, "r", encoding="utf-8") as f:
                    dd = json.load(f)
                dd["ts"] = now                   # 刷 ts 避免刚还原又被归档
                with open(dst, "w", encoding="utf-8") as f:
                    json.dump(dd, f)
                os.remove(src)
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
    snippets_win = [None]      # 快捷指令窗口(单例;重复打开只保留一个)
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
        hist_btn = tk.Label(bar, text="☰", fg="#9a9a9a", bg=BORDER, font=BTN_FONT, padx=8)   # 历史记录
        hist_btn.pack(side="right")
        for _b in (close_btn, min_btn, refresh_btn, hist_btn):
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
        hist_btn.bind("<Button-1>", lambda e: show_history())   # ☰ → 打开历史记录
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
        # 高度按条目数自适应(少则矮、多则封顶 460 配滚动条);位置跟着便签走(便签右侧、顶部对齐)
        nw, nh = 360, max(110, min(56 + len(items) * 30, 460))
        nx, ny = root.winfo_rootx() + W + 6, max(0, root.winfo_rooty())   # 兜底:灯窗口旁
        if note_win[0] is not None and note_win[0].winfo_exists():
            try:
                nx = note_win[0].winfo_rootx() + note_win[0].winfo_width() + 6
                ny = note_win[0].winfo_rooty()
            except Exception:
                pass
        if nx + nw > win.winfo_screenwidth() - 6:   # 超右屏 → 改放便签左侧
            try:
                nx = note_win[0].winfo_rootx() - nw - 6
            except Exception:
                nx = max(0, win.winfo_screenwidth() - nw - 6)
        win.geometry("%dx%d+%d+%d" % (nw, nh, max(0, nx), max(0, ny)))
        win.update_idletasks()

        rows = []                # [(it, row_frame, ptxt), ...]
        ph = {"on": True}        # 搜索框 placeholder 状态
        entry = None
        title_lbl = None
        if items:
            top = tk.Frame(win, bg=BG)
            top.pack(side="top", fill="x", padx=6, pady=(6, 4))
            title_lbl = tk.Label(top, text=" 快捷记录历史(共 %d 条)" % len(items),
                                 fg="#9a9a9a", bg=BG, font=("Microsoft YaHei", 9))
            title_lbl.pack(side="top", anchor="w")
            entry = tk.Entry(win, bg="#2a2a2a", fg="#777777", insertbackground="#e8e8e8",
                             relief="flat", bd=0, font=("Microsoft YaHei", 9))
            entry.insert(0, "搜索…")
            entry.pack(side="top", fill="x", padx=6, pady=(0, 4))

        body = tk.Frame(win, bg=BG)
        body.pack(side="top", fill="both", expand=True, padx=6, pady=(0, 6))
        cv = tk.Canvas(body, bg=BG, bd=0, highlightthickness=0)
        sb = tk.Scrollbar(body, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)   # sb 按需 pack(_sync_sb):内容少时不显示滚动条
        inner = tk.Frame(cv, bg=BG)
        inner_win = cv.create_window((0, 0), window=inner, anchor="nw")

        def _sync_sb(vis_h):
            """内容高度超出可视区才显示滚动条,否则隐藏。"""
            cv.configure(scrollregion=cv.bbox("all"))
            bbox = cv.bbox("all")
            content_h = (bbox[3] - bbox[1]) if bbox else 0
            need = content_h > vis_h + 2
            try:
                if need and not sb.winfo_ismapped():
                    sb.pack(side="right", fill="y")
                elif not need and sb.winfo_ismapped():
                    sb.pack_forget()
            except Exception:
                pass
        def _resize_text(t):
            try:
                t.update_idletasks()
                r = t.count("1.0", "end-1c", "displaylines")
                t.config(height=max(1, r[0] if r else 1))
            except Exception:
                pass
        def _on_inner_cfg(_e):
            _sync_sb(cv.winfo_height())
        def _on_cv_cfg(e):
            cv.itemconfig(inner_win, width=e.width)
            for _it, _row, _ptxt in rows:
                if _row.winfo_ismapped():
                    _resize_text(_ptxt)
            _sync_sb(e.height)
        inner.bind("<Configure>", _on_inner_cfg)
        cv.bind("<Configure>", _on_cv_cfg)
        win.after(120, lambda: _sync_sb(cv.winfo_height()))   # 初次布局后再核一次(防 Configure 时机漏判)

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

        nomatch_lbl = tk.Label(inner, text="(无匹配记录)", fg="#888888", bg=BG,
                               font=("Microsoft YaHei", 9))

        if not items:
            tk.Label(inner, text="(无记录)", fg="#888888", bg=BG,
                     font=("Microsoft YaHei", 9)).pack(anchor="w", padx=4, pady=8)
        else:
            for it in items:
                ts = it.get("ts", 0)
                text = it.get("text", "")
                row = tk.Frame(inner, bg=BG)
                row.pack(side="top", fill="x", padx=2, pady=2)
                tk.Label(row, text=human_ago(ts), fg="#777777", bg=BG,
                         font=("Consolas", 7)).pack(side="left", padx=(2, 6))
                ptxt = tk.Text(row, bg=BG, fg="#dddddd", bd=0, highlightthickness=0,
                               wrap="word", font=("Microsoft YaHei", 9), height=1,
                               padx=0, pady=0, cursor="hand2")
                ptxt.pack(side="left", fill="x", expand=True)
                ptxt.tag_configure("hl", background="#6a5a1a", foreground="#ffffff")
                ptxt.insert("1.0", preview_snippet(text, "")[0])
                ptxt.config(state="disabled")
                row.configure(cursor="hand2")

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
                ptxt.bind("<Button-1>", _click)
                row.bind("<Button-3>", _right)
                ptxt.bind("<Button-3>", _right)
                rows.append((it, row, ptxt))

            def _apply_filter(query):
                q = (query or "").strip()
                if ph["on"]:                 # placeholder 态(「搜索…」)→ 视为空 query
                    q = ""
                matched = 0
                for it, row, ptxt in rows:
                    text = it.get("text", "")
                    if match_record(text, q):
                        if not row.winfo_ismapped():
                            row.pack(side="top", fill="x", padx=2, pady=2)
                        matched += 1
                        snippet, spans = preview_snippet(text, q)
                        ptxt.config(state="normal")
                        ptxt.delete("1.0", "end")
                        ptxt.insert("1.0", snippet or "")
                        ptxt.tag_remove("hl", "1.0", "end")
                        for a, b in spans:
                            ptxt.tag_add("hl", "1.0+%dc" % a, "1.0+%dc" % b)
                        ptxt.config(state="disabled")
                        _resize_text(ptxt)
                    else:
                        if row.winfo_ismapped():
                            row.pack_forget()
                if title_lbl is not None:
                    if q:
                        title_lbl.config(text=" 快捷记录历史 · 匹配 %d / 共 %d 条" % (matched, len(rows)))
                    else:
                        title_lbl.config(text=" 快捷记录历史(共 %d 条)" % len(rows))
                if q and matched == 0:
                    nomatch_lbl.pack(side="top", anchor="w", padx=4, pady=8)
                else:
                    nomatch_lbl.pack_forget()
                _sync_sb(cv.winfo_height())
            def _on_focusin(_e):
                if ph["on"]:
                    entry.delete(0, "end")
                    entry.config(fg="#e8e8e8")
                    ph["on"] = False
            def _on_key(_e):
                ph["on"] = False
                _apply_filter(entry.get())
            def _on_esc(_e):
                entry.delete(0, "end")
                ph["on"] = True
                entry.insert(0, "搜索…")
                entry.config(fg="#777777")
                _apply_filter("")
            entry.bind("<FocusIn>", _on_focusin)
            entry.bind("<KeyRelease>", _on_key)
            entry.bind("<Escape>", _on_esc)
            win.after(150, lambda: _apply_filter(""))   # 布局后初始化预览高度 + 计数

    def show_snippets():
        """快捷指令:列表(左键复制内容到剪切板,右键编辑/删除)。
        一条都没有 → 直接显示添加表单;有记录 → 显示列表(顶部「＋ 新增」入口)。
        单窗口双状态,单例,重复打开只前置。"""
        nw = 380

        def _lamp_pos():
            """弹框贴交通灯右侧(超右屏则改灯左侧),垂直顶部对齐灯窗口。"""
            nx = root.winfo_rootx() + W + 6
            ny = max(0, root.winfo_rooty())
            if nx + nw > root.winfo_screenwidth() - 6:
                nx = max(0, root.winfo_rootx() - nw - 6)
            return max(0, nx), max(0, ny)

        if snippets_win[0] is not None and snippets_win[0].winfo_exists():
            w = snippets_win[0]
            try:
                if w.wm_state() == "iconic":        # 最小化态 → 复原
                    w.deiconify()
                nx, ny = _lamp_pos()                 # 重新贴灯定位(灯可能已被拖动)
                w.geometry("+%d+%d" % (nx, ny))
                w.lift()
                w.attributes("-topmost", True)
                w.focus_force()
            except Exception:
                pass
            return
        win = tk.Toplevel(root)
        win.title("快捷指令")
        win.attributes("-topmost", True)
        win.configure(bg=BG)
        _set_note_icon(win)
        snippets_win[0] = win
        items = load_snippets()
        nx, ny = _lamp_pos()
        win.geometry("%dx%d+%d+%d" % (nw, 220, nx, ny))
        win.update_idletasks()

        # 顶部:标题(左) + ＋新增(右)。系统标题栏提供 ✕/最小化。
        top = tk.Frame(win, bg=BG)
        top.pack(side="top", fill="x", padx=6, pady=(6, 4))
        title_lbl = tk.Label(top, text=" 快捷指令", fg="#9a9a9a", bg=BG,
                             font=("Microsoft YaHei", 9))
        title_lbl.pack(side="left")
        add_btn = tk.Label(top, text="＋ 新增", fg="#9a9a9a", bg=BG,
                           font=("Microsoft YaHei", 9), padx=6, cursor="hand2")
        add_btn.pack(side="right")
        add_btn.bind("<Enter>", lambda e: add_btn.configure(fg="#ffffff"))
        add_btn.bind("<Leave>", lambda e: add_btn.configure(fg="#9a9a9a"))

        body = tk.Frame(win, bg=BG)
        body.pack(side="top", fill="both", expand=True, padx=6, pady=(0, 6))

        def _clear_body():
            for _c in body.winfo_children():
                try:
                    _c.destroy()
                except Exception:
                    pass

        def _apply_size(h):
            """切换列表/编辑态时重设窗口高度,并校正 y 使整窗留在工作区内
            (灯窗口常贴屏底,旁边弹出的高窗口会溢出屏幕底部)。"""
            try:
                wx, wy = win.winfo_x(), win.winfo_y()
                wa = work_area()
                bot = wa[3] if wa else win.winfo_screenheight()
                top = wa[1] if wa else 0
                if wy + h > bot:
                    wy = max(top, bot - h)
                win.geometry("%dx%d+%d+%d" % (nw, h, max(0, wx), max(0, wy)))
            except Exception:
                pass

        def _set_title(t):
            try:
                title_lbl.config(text=" " + t)
            except Exception:
                pass

        def _close():
            snippets_win[0] = None
            try:
                win.destroy()
            except Exception:
                pass

        def _copy(tx, label):
            """把内容写进系统剪切板 → body 换成醒目'已复制'提示 → 短暂停留后关窗(用完即走)。"""
            try:
                root.clipboard_clear()
                root.clipboard_append(tx)
                root.update()          # 立即让剪切板生效
            except Exception as ex:
                log_error(ex, "复制快捷指令")
                _set_title("复制失败")
                return
            _clear_body()
            try:
                add_btn.pack_forget()              # 提示态:收起新增入口
            except Exception:
                pass
            _apply_size(150)
            _set_title("已复制")
            box = tk.Frame(body, bg=BG)
            box.pack(expand=True)
            tk.Label(box, text="✓", fg="#34d058", bg=BG,
                     font=("Segoe UI Symbol", 30)).pack(pady=(16, 0))
            tk.Label(box, text="已复制到剪切板", fg="#cfcfcf", bg=BG,
                     font=("Microsoft YaHei", 11)).pack(pady=(2, 0))
            if label:
                tk.Label(box, text=label, fg="#777777", bg=BG,
                         font=("Microsoft YaHei", 9)).pack(pady=(1, 12))
            win.after(700, _close)

        # ---------- 列表态(有记录) ----------
        def _render_list():
            _clear_body()
            add_btn.pack(side="right")            # 列表态:显出新增入口
            items = load_snippets()
            _apply_size(max(160, min(96 + len(items) * 34, 480)))
            _set_title("快捷指令(共 %d 条)" % len(items))

            cv = tk.Canvas(body, bg=BG, bd=0, highlightthickness=0)
            sb = tk.Scrollbar(body, orient="vertical", command=cv.yview)
            cv.configure(yscrollcommand=sb.set)
            cv.pack(side="left", fill="both", expand=True)
            inner = tk.Frame(cv, bg=BG)
            inner_win = cv.create_window((0, 0), window=inner, anchor="nw")
            rows = []

            def _sync_sb(vis_h):
                cv.configure(scrollregion=cv.bbox("all"))
                bbox = cv.bbox("all")
                content_h = (bbox[3] - bbox[1]) if bbox else 0
                need = content_h > vis_h + 2
                try:
                    if need and not sb.winfo_ismapped():
                        sb.pack(side="right", fill="y")
                    elif not need and sb.winfo_ismapped():
                        sb.pack_forget()
                except Exception:
                    pass

            def _resize_text(t):
                try:
                    t.update_idletasks()
                    r = t.count("1.0", "end-1c", "displaylines")
                    t.config(height=max(1, r[0] if r else 1))
                except Exception:
                    pass

            def _on_cv_cfg(e):
                cv.itemconfig(inner_win, width=e.width)
                for _it, _row, _ptxt in rows:
                    if _row.winfo_ismapped():
                        _resize_text(_ptxt)
                _sync_sb(e.height)

            inner.bind("<Configure>", lambda e: _sync_sb(cv.winfo_height()))
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

            if not items:
                tk.Label(inner, text="还没有快捷指令", fg="#cfcfcf", bg=BG,
                         font=("Microsoft YaHei", 10)).pack(anchor="w", padx=4, pady=(8, 2))
                tk.Label(inner, text="点右上「＋ 新增」或下面按钮添加第一条", fg="#777777", bg=BG,
                         font=("Microsoft YaHei", 9)).pack(anchor="w", padx=4)
                tk.Button(inner, text="＋ 新增快捷指令", command=lambda: _render_edit(None),
                          bg="#3a3a3a", fg="#e8e8e8", activebackground="#4a4a4a", relief="flat",
                          bd=0, font=("Microsoft YaHei", 9), padx=12, pady=4
                          ).pack(anchor="w", padx=4, pady=(8, 4))
            else:
                for it in items:
                    text = it.get("text", "")
                    label = it.get("title") or text.splitlines()[0][:24] if text else "(空)"
                    row = tk.Frame(inner, bg=BG)
                    row.pack(side="top", fill="x", padx=2, pady=2)
                    name_lbl = tk.Label(row, text=label, fg="#cfcfcf", bg=BG,
                                        font=("Microsoft YaHei", 9, "bold"), cursor="hand2")
                    name_lbl.pack(side="left", padx=(2, 6))
                    ptxt = tk.Text(row, bg=BG, fg="#a0a0a0", bd=0, highlightthickness=0,
                                   wrap="word", font=("Microsoft YaHei", 9), height=1,
                                   padx=0, pady=0, cursor="hand2")
                    ptxt.pack(side="left", fill="x", expand=True)
                    ptxt.insert("1.0", preview_snippet(text, "")[0])
                    ptxt.config(state="disabled")
                    row.configure(cursor="hand2")

                    def _click(e, tx=text, lb=label):
                        _copy(tx, lb)

                    def _right(e, _it=it):
                        m = tk.Menu(root, tearoff=0, bg=BG, fg="#dddddd",
                                    activebackground="#3a3a3a", activeforeground="#ffffff")
                        m.add_command(label="编辑", command=lambda: _render_edit(_it))

                        def _del():
                            save_snippets([x for x in load_snippets()
                                           if x.get("id") != _it.get("id")])
                            _render_list()

                        m.add_command(label="删除", command=_del)
                        try:
                            m.tk_popup(e.x_root, e.y_root)
                        finally:
                            m.grab_release()

                    name_lbl.bind("<Button-1>", _click)
                    name_lbl.bind("<Button-3>", _right)
                    row.bind("<Button-1>", _click)
                    ptxt.bind("<Button-1>", _click)
                    row.bind("<Button-3>", _right)
                    ptxt.bind("<Button-3>", _right)
                    rows.append((it, row, ptxt))
                win.after(120, lambda: _sync_sb(cv.winfo_height()))

        # ---------- 编辑/新增态(共用一张表单) ----------
        def _render_edit(existing):
            _clear_body()
            add_btn.pack_forget()                 # 编辑态:收起顶部新增入口,避免误触
            is_new = existing is None
            _apply_size(320)
            _set_title("新增快捷指令" if is_new else "编辑快捷指令")

            form = tk.Frame(body, bg=BG)
            form.pack(side="top", fill="both", expand=True, padx=2, pady=2)
            tk.Label(form, text="名称(可选,留空取内容首行)", fg="#777777", bg=BG,
                    font=("Microsoft YaHei", 8)).pack(anchor="w")
            title_e = tk.Entry(form, bg="#2a2a2a", fg="#e8e8e8", insertbackground="#e8e8e8",
                               relief="flat", bd=0, font=("Microsoft YaHei", 10))
            title_e.pack(fill="x", pady=(2, 8))
            if not is_new:
                title_e.insert(0, existing.get("title", ""))
            tk.Label(form, text="内容(列表里左键点击即复制此项)", fg="#777777", bg=BG,
                    font=("Microsoft YaHei", 8)).pack(anchor="w")
            text_t = tk.Text(form, bg="#2a2a2a", fg="#e8e8e8", insertbackground="#e8e8e8",
                             bd=0, highlightthickness=0, wrap="word", relief="flat",
                             font=("Microsoft YaHei", 10), padx=6, pady=5, height=6)
            text_t.pack(fill="both", expand=True, pady=(2, 8))
            if not is_new:
                text_t.insert("1.0", existing.get("text", ""))

            def _flash(msg):
                _set_title(msg)
                win.after(1200, lambda: _set_title("新增快捷指令" if is_new else "编辑快捷指令"))

            def _save():
                title = title_e.get().strip()
                text = text_t.get("1.0", "end-1c").strip()
                if not text:                      # 内容必填(空内容没意义)
                    _flash("内容不能为空")
                    text_t.focus_set()
                    return
                if not title:
                    title = text.splitlines()[0][:24]
                items = load_snippets()
                if is_new:
                    items.append({"id": int(time.time() * 1000), "title": title, "text": text})
                else:
                    _id = existing.get("id")
                    for x in items:
                        if x.get("id") == _id:
                            x["title"] = title
                            x["text"] = text
                            break
                save_snippets(items)
                _render_list()

            def _cancel():
                if is_new and not load_snippets():
                    _close()                      # 新增态 + 本来就空 → 直接关窗
                else:
                    _render_list()

            btns = tk.Frame(form, bg=BG)
            btns.pack(fill="x")
            tk.Button(btns, text="取消", command=_cancel, bg="#2a2a2a", fg="#9a9a9a",
                      activebackground="#3a3a3a", relief="flat", bd=0,
                      font=("Microsoft YaHei", 9), padx=12).pack(side="right", padx=(0, 6))
            tk.Button(btns, text="保存", command=_save, bg="#3a3a3a", fg="#e8e8e8",
                      activebackground="#4a4a4a", relief="flat", bd=0,
                      font=("Microsoft YaHei", 9), padx=14).pack(side="right")
            win.bind("<Control-Return>", lambda e: (_save(), "break")[1])   # Ctrl+Enter 保存
            title_e.focus_set()

        add_btn.bind("<Button-1>", lambda e: _render_edit(None))
        win.bind("<Escape>", lambda e: _close())
        win.bind("<Destroy>", lambda e: snippets_win.__setitem__(0, None), add="+")

        # 首次打开:空 → 直接添加表单;非空 → 列表
        if items:
            _render_list()
        else:
            _render_edit(None)

    def show_error_log():
        """弹出错误日志窗口:最近异常(最新在上),可一键复制全文丢给排查。"""
        w = tk.Toplevel(root)
        w.title("错误日志 — 可复制丢给排查")
        w.geometry("780x560")
        w.configure(bg="#1e1e1e")
        entries = read_error_log_entries()

        def _all_text():
            if not entries:
                return "(无错误日志)"
            lines = []
            for e in reversed(entries):          # 最新在上
                lines.append("[%s] %s" % (e.get("ts", ""), e.get("ctx", "")))
                lines.append(e.get("tb", "").rstrip())
                lines.append("-" * 60)
            return "\n".join(lines)

        bar = tk.Frame(w, bg="#2a2a2a")
        bar.pack(side="top", fill="x")
        tk.Label(bar, text=" %d 条 · 路径: %s" % (len(entries), ERROR_LOG),
                 fg="#9a9a9a", bg="#2a2a2a", font=("Microsoft YaHei", 8)
                 ).pack(side="left", padx=6, pady=5)

        def _copy():
            try:
                root.clipboard_clear()
                root.clipboard_append(_all_text())
                btn_copy.config(text="已复制 ✓")
                w.after(1200, lambda: btn_copy.config(text="复制全部"))
            except Exception as ex:
                log_error(ex, "复制错误日志")

        btn_copy = tk.Button(bar, text="复制全部", command=_copy, bg="#3a3a3a", fg="#e8e8e8",
                             activebackground="#4a4a4a", relief="flat", bd=0,
                             font=("Microsoft YaHei", 8), padx=10)
        btn_copy.pack(side="right", padx=4, pady=4)

        def _clear():
            clear_error_log()
            w.destroy()
        tk.Button(bar, text="清空", command=_clear, bg="#3a3a3a", fg="#e8e8e8",
                  activebackground="#4a4a4a", relief="flat", bd=0,
                  font=("Microsoft YaHei", 8), padx=10).pack(side="right", padx=2, pady=4)

        txt = tk.Text(w, wrap="word", bg="#1e1e1e", fg="#dcdcdc", insertbackground="#dcdcdc",
                      font=("Consolas", 9), padx=10, pady=8)
        sb = tk.Scrollbar(w, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", _all_text())
        txt.configure(state="disabled")

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="清理不活跃会话", command=cleanup_stale)
    menu.add_command(label="检测所有会话", command=restore_sessions)   # 还原被清理/删除的会话
    menu.add_command(label="错误日志", command=show_error_log)   # 显示异常日志,可复制丢给排查
    menu.add_separator()
    menu.add_command(label="快捷记录", command=toggle_note)
    menu.add_command(label="快捷指令", command=show_snippets)
    menu.add_separator()
    menu.add_checkbutton(label="超时兜底", variable=fallback_var, command=on_toggle_fallback)
    menu.add_cascade(label="超时时间", menu=timeout_menu)
    def restart():
        import subprocess
        try:
            subprocess.Popen([sys.executable, os.path.abspath(__file__)])
        except Exception:
            pass
        root.destroy()

    def restore_statusline():
        """把 claude-hud 的 statusLine 写回 ~/.claude/settings.json —— 一键恢复上下文栏。
        根因:CC Switch 切模型重写 settings.json 时常把 statusLine 字段丢掉,Claude Code
        无 statusLine 可调 → 整条上下文栏消失(切回带 statusLine 的配置才显示)。本函数
        优先从 ~/.claude 下最近一份含 claude-hud 的备份取 statusLine,都没有则用内置标准值;
        保留 settings.json 其他字段不动(原子写)。"""
        import glob
        import tkinter.messagebox as mb
        home = os.path.expanduser("~")
        sf = os.path.join(home, ".claude", "settings.json")
        STD_CMD = (
            'cols=${COLUMNS:-}; case "$cols" in ""|*[!0-9]*) cols=$(stty size </dev/tty 2>/dev/null | awk \'{print $2}\');; esac; '
            'case "$cols" in ""|*[!0-9]*) cols=120;; esac; export COLUMNS=$(( cols > 4 ? cols - 4 : 1 )); '
            'plugin_dir=$(ls -1d "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/cache/*/claude-hud/*/ 2>/dev/null | sort -V | tail -1); '
            'exec "C:/Program Files/nodejs/node.exe" "${plugin_dir}dist/index.js"'
        )
        try:
            with open(sf, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            log_error(e, "restore_statusline read")
            try: mb.showerror("恢复上下文栏", "读不到 settings.json:\n" + sf)
            except Exception: pass
            return
        if "claude-hud" in (d.get("statusLine") or {}).get("command", ""):
            try: mb.showinfo("恢复上下文栏", "statusLine 已经在(指向 claude-hud),无需恢复。\n若状态栏仍不显示,重开一下会话。")
            except Exception: pass
            return
        sl, src = None, "内置标准值"
        try:
            for b in sorted(glob.glob(os.path.join(home, ".claude", "settings.json*")), key=os.path.getmtime, reverse=True):
                if os.path.abspath(b) == os.path.abspath(sf): continue
                try:
                    with open(b, encoding="utf-8") as f: bd = json.load(f)
                    if "claude-hud" in (bd.get("statusLine") or {}).get("command", ""):
                        sl, src = bd["statusLine"], os.path.basename(b); break
                except Exception: pass
        except Exception: pass
        if not sl:
            sl = {"type": "command", "command": STD_CMD}
        try:
            d["statusLine"] = sl
            tmp = sf + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            os.replace(tmp, sf)
        except Exception as e:
            log_error(e, "restore_statusline write")
            try: mb.showerror("恢复上下文栏", "写入失败:" + str(e))
            except Exception: pass
            return
        try: mb.showinfo("恢复上下文栏", "已把 statusLine 写回 settings.json(取自 " + src + ")。\n上下文栏应在 ~1 秒内恢复;若没动静,重开当前会话即可。")
        except Exception: pass

    def fix_term_window():
        """手动修正会话跳转的终端窗口。hook 记 hwnd 用「首次 GetForegroundWindow + 旧值存活
        守卫」:hook 触发那一刻前台是谁就记谁,claude 启动时用户已切走/后台启动就记错;记错后
        只要窗口活着就一直保留(跳错窗口)。本功能:选会话 → 5 秒内切到正确终端窗口 → 到点抓
        前台写回 session 文件,下次点击即跳对。局限:WT 多 tab 共享 hwnd,只能 focus 到窗口级,
        显示哪个 tab 由 WT 决定(Trae 有 helper 能切终端,WT 无等价机制)。"""
        import tkinter.messagebox as mb
        try:
            files = sorted(f for f in os.listdir(SESSIONS_DIR) if f.endswith(".json"))
        except OSError:
            files = []
        items = []
        for fn in files:
            try:
                with open(os.path.join(SESSIONS_DIR, fn), encoding="utf-8") as f:
                    d = json.load(f)
                items.append((fn[:-5], d))
            except Exception:
                continue
        if not items:
            try: mb.showinfo("修正终端窗口", "当前没有会话记录。")
            except Exception: pass
            return

        pw = tk.Toplevel(root)
        pw.title("修正终端窗口")
        pw.attributes("-topmost", True)
        pw.transient(root)
        pw.configure(bg="#2a2a2a")
        pw.geometry("460x300")
        tk.Label(pw, text="选中跳错位置的会话 → 开始抓取 → 5 秒内切到它真正的终端窗口",
                 fg="#9a9a9a", bg="#2a2a2a", font=("Microsoft YaHei", 8)).pack(padx=10, pady=6)
        lb = tk.Listbox(pw, bg="#1e1e1e", fg="#dcdcdc", selectbackground="#4a6fa5",
                        selectforeground="#ffffff", font=("Microsoft YaHei", 9),
                        relief="flat", highlightthickness=0)
        lb.pack(fill="both", expand=True, padx=10, pady=4)
        for _sid, _d in items:
            cur = _window_title(_d.get("hwnd") or 0) or "(hwnd 失效)"
            lb.insert("end", " %s  →  %s" % (_d.get("name") or _sid[:8], cur[:64]))

        def _grab(sid, d):
            pw.destroy()
            gw = tk.Toplevel(root)
            gw.title("抓取前台窗口")
            gw.attributes("-topmost", True)
            gw.transient(root)
            gw.configure(bg="#2a2a2a")
            lbl = tk.Label(gw, text="", fg="#e8e8e8", bg="#2a2a2a",
                           font=("Microsoft YaHei", 11, "bold"), padx=26, pady=16)
            lbl.pack()
            state = {"n": 5}

            def _tick():
                if state["n"] > 0:
                    lbl.config(text="请在 %d 秒内点击切换到\n「%s」所在的终端窗口" % (state["n"], d.get("name") or sid[:8]))
                    state["n"] -= 1
                    gw.after(1000, _tick)
                    return
                hwnd = 0
                try:
                    import ctypes
                    from ctypes import wintypes
                    u = ctypes.windll.user32
                    u.GetForegroundWindow.argtypes = []; u.GetForegroundWindow.restype = wintypes.HWND
                    u.IsWindowVisible.argtypes = [wintypes.HWND]; u.IsWindowVisible.restype = wintypes.BOOL
                    h = u.GetForegroundWindow()
                    if h and u.IsWindowVisible(h):
                        hwnd = int(h)
                except Exception as ex:
                    log_error(ex, "fix_term_window grab")
                try: gw.destroy()
                except Exception: pass
                if not hwnd or "python" in _window_process_name(hwnd).lower():
                    try: mb.showwarning("修正终端窗口", "没抓到目标窗口(5 秒内没切换,前台还是本工具或已不可见),请重试。")
                    except Exception: pass
                    return
                d["hwnd"] = hwnd
                _write_session_file(sid, d)
                try: mb.showinfo("修正终端窗口", "已把「%s」的跳转窗口改为:\n%s  (%s)\n下次点击该会话即跳这个窗口。"
                                 % (d.get("name") or sid[:8], _window_title(hwnd), _window_process_name(hwnd)))
                except Exception: pass

            _tick()

        def _start():
            sel = lb.curselection()
            if not sel:
                try: mb.showinfo("修正终端窗口", "先选中一个会话。")
                except Exception: pass
                return
            _grab(*items[sel[0]])

        bar = tk.Frame(pw, bg="#2a2a2a")
        bar.pack(fill="x", padx=10, pady=6)
        tk.Button(bar, text="开始抓取", command=_start, bg="#3a3a3a", fg="#e8e8e8",
                  activebackground="#4a4a4a", relief="flat", bd=0,
                  font=("Microsoft YaHei", 9), padx=14).pack(side="right")

    menu.add_separator()
    menu.add_checkbutton(label="窗口置顶", variable=topmost_var, command=on_toggle_topmost)
    menu.add_command(label="复位位置", command=lambda: root.geometry(default_geo()))
    menu.add_command(label="修正终端窗口", command=fix_term_window)
    menu.add_separator()
    menu.add_command(label="恢复上下文栏", command=restore_statusline)
    menu.add_command(label="重启", command=restart)
    menu.add_command(label="退出", command=root.destroy)
    cv.bind("<ButtonPress-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    # Scanned sessions have no SessionEnd hook. Reconcile them before the first
    # render so a restart cannot resurrect a terminal that was closed.
    restore_sessions()
    poll()
    root.mainloop()


if __name__ == "__main__":
    sys.exit(cli())
