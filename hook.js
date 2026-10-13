#!/usr/bin/env node
// cc-light hook: read CC hook JSON from stdin, maintain sessions/<sid>.json
// Node (not pythonw) for reliable stdin on Windows.
// Maintains a `subs` counter per session so green isn't written while subagents run.
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

// 一次 powershell 同时刷新 hwnd + termpid(hook 每次工具调用都触发,合并避免两次进程启动)。
// 关键:hwnd/termpid 必须在窗口/终端重建后自动刷新 —— 旧实现 `old.hwnd || get()` 是旧值优先,
// 一旦首次记下就永不更新,窗口重建后留下死 hwnd,点击跳转失效。这里用"新值优先 + 存活守卫"。
// 实现挪到 hook_meta.ps1(PS5.1 读 ps1 要 BOM),逻辑升级:
//  1) 归属校验:旧 hwnd 活着还要其所属进程在当前父链上才算数 —— resume/迁移场景旧值指向
//     别的窗口(如 Trae)时判失效强制重取,错值可自愈;
//  2) WT 单进程多窗口:MainWindowHandle 只是其中一个窗口 → EnumWindows 枚举该 pid 全部
//     可见顶层窗口,标题含项目名(name)者优先(TAPD-exec <项目名> 命中)。
function getMeta(prevHwnd, prevPid, name) {
    // ProjectManager 后台隐藏任务(CC_LIGHT_BG=1):无终端窗口,直接置 0 ——
    // termpid 会误指到父进程(3001 后端 node)导致点击误聚焦它的宿主窗口
    if (process.env.CC_LIGHT_BG === "1") return { hwnd: 0, termpid: 0 };
    try {
        const out = execFileSync('powershell',
            ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', path.join(__dirname, 'hook_meta.ps1')],
            { encoding: 'utf8', timeout: 10000, windowsHide: true,
              env: Object.assign({}, process.env, {
                  PREV_HWND: String(prevHwnd || 0),
                  PREV_TPID: String(prevPid || 0),
                  NAME: String(name || '') }) });
        const parts = String(out).trim().split(/\s+/);
        const h = parseInt(parts[0], 10), t = parseInt(parts[1], 10);
        return { hwnd: (isNaN(h) ? 0 : h), termpid: (isNaN(t) ? 0 : t) };
    } catch (e) { return { hwnd: 0, termpid: 0 }; }
}

const DIR = __dirname;
// 运行数据放 %APPDATA%\cc-light —— ~/.claude 被坚果云同步会清空 sessions,必须移出
const DATA_DIR = path.join(process.env.APPDATA || DIR, 'cc-light');
const SESSIONS_DIR = path.join(DATA_DIR, 'sessions');
const MISS_LOG = path.join(DATA_DIR, 'hook-miss.log');
const COLORS = { red: 1, yellow: 1, green: 1 };

const rawAction = process.argv[2];
const SOFT = rawAction === 'yellow_soft';   // PostToolUse 心跳:只刷 state/ts,跳过 getMeta 避免每个工具都起 powershell
const action = SOFT ? 'yellow' : rawAction;

// ProjectManager 后台隐藏任务(CC_LIGHT_BG=1):状态由后端代写(scanned 会话),
// hook 一律让位 —— hook 写的非 scanned 版会覆盖后端版本、被 read_sessions 就地归档,
// 造成「亮一两秒→灭→20s 心跳救回」的闪烁
if (process.env.CC_LIGHT_BG === "1") process.exit(0);

let raw = '';
try { raw = fs.readFileSync(0, 'utf8'); } catch (e) {}
let d = {};
try { d = raw.trim() ? JSON.parse(raw) : {}; } catch (e) {}

function safe(s) { return String(s).replace(/[^A-Za-z0-9_-]/g, '') || 'unknown'; }
function sessionFile(sid) { return path.join(SESSIONS_DIR, safe(sid) + '.json'); }
function readSession(sid) {
    try { return JSON.parse(fs.readFileSync(sessionFile(sid), 'utf8')); }
    catch (e) { return null; }
}
function writeSession(sid, data) {
    try {
        fs.mkdirSync(SESSIONS_DIR, { recursive: true });
        const tmp = sessionFile(sid) + '.tmp';
        fs.writeFileSync(tmp, JSON.stringify(data));
        fs.renameSync(tmp, sessionFile(sid));   // atomic
    } catch (e) {}
}

// SessionEnd: delete the session file (+ log for diagnostics)
if (action === 'end') {
    const sid = d.session_id;
    try { fs.appendFileSync(MISS_LOG, (Date.now() / 1000) + ' end sid=' + sid + '\n'); } catch (e) {}
    if (sid) { try { fs.unlinkSync(sessionFile(sid)); } catch (e) {} }
    process.exit(0);
}

const sid = d.session_id;
if (!sid) {
    try { fs.appendFileSync(MISS_LOG, (Date.now() / 1000) + ' miss-sid action=' + action + '\n'); } catch (e) {}
    process.exit(0);
}

const cwd = d.cwd || '';
const base = cwd.replace(/\\/g, '/').replace(/\/$/, '').split('/').pop();
const name = base || String(sid).slice(0, 8);
// name 始终用当前 cwd 的 basename(每次重算,不继承 old.name):「检测所有会话」补建的 scanned 会话
// 曾用 proj_name(含父目录)写 name,hook 接管若继承会固化这个污染值,而窗口标题只含 basename →
// hwnd 失效后 _jump 兜底 find_window_by_name 匹配不到标题,跳不动。见 cc_light.py scan_active_jsonl。
const now = Date.now() / 1000;
// 后台任务(CC_LIGHT_BG=1):hwnd/termpid 强制 0(不用旧值——旧值误指父进程 3001 的宿主窗口)
const BG = process.env.CC_LIGHT_BG === "1";

// subagent start: subs+1, write yellow (a subagent running => the session is busy)
if (action === 'sub_start') {
    const old = readSession(sid) || {};
    const meta = getMeta(old.hwnd, old.termpid, name);
    writeSession(sid, { state: 'yellow', msg: '', ts: now, name: name, subs: (old.subs || 0) + 1, hwnd: BG ? 0 : (meta.hwnd || old.hwnd || 0), termpid: BG ? 0 : (meta.termpid || old.termpid || 0) });
    process.exit(0);
}
// subagent stop: subs-1, do NOT change state (the main agent may spawn another subagent)
if (action === 'sub_stop') {
    const old = readSession(sid);
    if (old) writeSession(sid, Object.assign({}, old, { subs: Math.max(0, (old.subs || 0) - 1) }));
    process.exit(0);
}

if (!COLORS[action]) process.exit(2);

// green: if there are active subagents (subs>0), don't write green (keep yellow)
if (action === 'green') {
    const old = readSession(sid);
    if (old && (old.subs || 0) > 0) process.exit(0);
}

// normal write (yellow/red/green), preserve old name + subs, refresh hwnd/termpid
const old = readSession(sid);
// SOFT(工具心跳)跳过 getMeta:hwnd/termpid 沿用旧值,避免 Claude 每个工具都起 powershell 拖慢响应
const meta = SOFT
    ? { hwnd: (old && old.hwnd) || 0, termpid: (old && old.termpid) || 0 }
    : getMeta(old && old.hwnd, old && old.termpid, name);
writeSession(sid, {
    state: action,
    msg: '',
    ts: now,
    name: name,
    subs: (old && old.subs) || 0,
    hwnd: BG ? 0 : (meta.hwnd || (old && old.hwnd) || 0),
    termpid: BG ? 0 : (meta.termpid || (old && old.termpid) || 0),
});
