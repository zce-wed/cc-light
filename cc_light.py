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
SESSIONS_DIR = os.path.join(DIR, "sessions")
POS_FILE = os.path.join(DIR, "pos.json")
MISS_LOG = os.path.join(DIR, "hook-miss.log")
STALE = 1 * 3600        # 1 小时无更新视为僵尸,清理
CONFIG_FILE = os.path.join(DIR, "config.json")
DEFAULT_CONFIG = {"yellow_timeout": 180, "timeout_fallback": True}   # 默认 3 分钟 + 开兜底
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
            if now - d.get("ts", 0) > STALE:
                try:
                    os.remove(path)
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
        r = min(3.5, max(2.0, (avail - (n - 1) * gap) / (2 * n)))   # 点多则自动缩小
        total = n * 2 * r + (n - 1) * gap
        x = (W - total) / 2 + r
        y = 126
        for (sid, d) in items:
            st = effective_state(d, now)
            color = COLORS.get(st, {}).get("on", "#666666")
            dots.append(cv.create_oval(x - r, y - r, x + r, y + r, fill=color, outline=""))
            x += 2 * r + gap

    def poll():
        sessions = read_sessions()
        now = time.time()
        c, n = aggregate(sessions)
        apply(c)
        render_dots(sessions, now)
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
    cv.bind("<Double-Button-1>", lambda e: root.attributes("-topmost", not root.attributes("-topmost")))

    # ---- 会话明细 ----
    def show_details():
        sessions = read_sessions()
        now = time.time()
        win = tk.Toplevel(root)
        win.title("cc-light 会话明细")
        win.attributes("-topmost", True)
        win.configure(bg=BG)
        order = sorted(sessions.items(),
                       key=lambda kv: (PRIO.get(effective_state(kv[1], now), 99), -kv[1].get("ts", 0)))
        names = [d.get("name", "") for _, d in sessions.items()]
        tk.Label(win, text="  会话明细(按优先级)", fg="#bbbbbb", bg=BG,
                 font=("Microsoft YaHei", 10, "bold")).grid(row=0, column=0, sticky="w", padx=6, pady=(6, 2))
        if not order:
            tk.Label(win, text="  (无活跃会话)", fg="#888888", bg=BG,
                     font=("Microsoft YaHei", 9)).grid(row=1, column=0, sticky="w", padx=6, pady=2)
        for i, (sid, d) in enumerate(order, start=1):
            raw = d.get("state", "gray")
            st = effective_state(d, now)
            stale = (raw == "yellow" and st == "green")   # 超时降级的,标注
            color = COLORS.get(st, {}).get("on", "#666666")
            name = d.get("name") or sid[:8]
            if names.count(d.get("name", "")) > 1:
                name = name + " " + sid[:4]
            row = tk.Frame(win, bg=BG)
            row.grid(row=i, column=0, sticky="w", padx=6, pady=1)
            tk.Label(row, text="●", fg=color, bg=BG, font=("Consolas", 11)).pack(side="left")
            word = STATE_WORD.get(st, "") + (" ·超时" if stale else "")
            tk.Label(row, text=" " + word, fg=color, bg=BG,
                     font=("Microsoft YaHei", 9, "bold")).pack(side="left")
            tk.Label(row, text="  " + name, fg="#dddddd", bg=BG,
                     font=("Microsoft YaHei", 9)).pack(side="left")
            tk.Label(row, text="  " + human_ago(d.get("ts", 0)), fg="#777777", bg=BG,
                     font=("Consolas", 8)).pack(side="left")

    def cleanup_stale():
        """删超过超时阈值无更新的会话(关窗口没触发 SessionEnd 的残留)"""
        thr = _config["yellow_timeout"]
        now = time.time()
        for sid, d in list(read_sessions().items()):
            if now - d.get("ts", 0) > thr:
                delete_session(sid)
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
    for sec in (60, 120, 180, 300, 600):
        timeout_menu.add_radiobutton(label="%d 秒" % sec, value=sec, variable=timeout_var, command=on_set_timeout)

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="会话明细…", command=show_details)
    menu.add_command(label="清理不活跃会话", command=cleanup_stale)
    menu.add_separator()
    menu.add_checkbutton(label="超时兜底", variable=fallback_var, command=on_toggle_fallback)
    menu.add_cascade(label="超时时间", menu=timeout_menu)
    menu.add_separator()
    menu.add_command(label="手动 · 红(等确认)", command=lambda: write_session("_manual", "red", "", "手动"))
    menu.add_command(label="手动 · 黄(运行中)", command=lambda: write_session("_manual", "yellow", "", "手动"))
    menu.add_command(label="手动 · 绿(完成)",   command=lambda: write_session("_manual", "green", "", "手动"))
    menu.add_command(label="手动 · 清除",       command=lambda: delete_session("_manual"))
    menu.add_separator()
    menu.add_command(label="复位位置", command=lambda: root.geometry(default_geo()))
    menu.add_command(label="退出", command=root.destroy)
    cv.bind("<ButtonPress-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    poll()
    root.mainloop()


if __name__ == "__main__":
    sys.exit(cli())
