#!/usr/bin/env node
// cc-light hook: read CC hook JSON from stdin, maintain sessions/<sid>.json
// Node (not pythonw) for reliable stdin on Windows.
// Maintains a `subs` counter per session so green isn't written while subagents run.
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

// 一次 powershell 同时刷新 hwnd + termpid(hook 每次工具调用都触发,合并避免两次进程启动)。
// 关键:hwnd/termpid 必须在窗口/终端重建后自动刷新 —— 旧实现 `old.hwnd || get()` 是旧值优先,
// 一旦首次记下就永不更新,窗口重建后留下死 hwnd,点击跳转失效。这里用"新值优先 + 存活守卫":
// 旧 hwnd 还活着(IsWindow)/旧 termpid 进程还在 → 保留(廉价,不遍历父链);否则沿父链重取。
function getMeta(prevHwnd, prevPid) {
    const script = [
        '$ErrorActionPreference="SilentlyContinue"',
        'Add-Type -MemberDefinition \'[DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h); [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow(); [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h,out uint p);\' -Name U -Namespace CCL -PassThru | Out-Null',
        // ---- hwnd:旧值存活则保留,否则重取(GetForegroundWindow 快速路径 → 父链兜底) ----
        '$hwnd=0; $prev=[int64]$env:PREV_HWND',
        'if($prev -and [CCL.U]::IsWindow([IntPtr]$prev)){ $hwnd=$prev }',
        'else{',
        '  $term="WindowsTerminal|conhost|cmd|Code|Trae|Cursor|Windsurf|wezterm|alacritty|Hyper|conemu64|HBuilderX"',
        '  $fg=[CCL.U]::GetForegroundWindow()',
        '  if($fg -and $fg -ne 0){ $pv=0; [void][CCL.U]::GetWindowThreadProcessId($fg,[ref]$pv); $fp=Get-Process -Id $pv; if($fp -and $fp.Name -match $term){ $hwnd=[int64]$fg } }',
        '  if(-not $hwnd){ $p=$PID; $first=0; while($p){ $pr=Get-CimInstance Win32_Process -Filter ("ProcessId="+$p); if(-not $pr){break}; $proc=Get-Process -Id $p; if($proc -and $proc.MainWindowHandle -and $proc.MainWindowHandle -ne 0){ if($first -eq 0){$first=[int64]$proc.MainWindowHandle}; if($proc.Name -match $term){$hwnd=[int64]$proc.MainWindowHandle;break} }; $p=$pr.ParentProcessId }; if(-not $hwnd -and $first){ $hwnd=$first } }',
        '}',
        // ---- termpid:旧进程还在则保留,否则沿父链找 name 含 claude 的进程,取其父 ----
        '$tpid=0; $prevT=[int64]$env:PREV_TPID',
        'if($prevT -and (Get-Process -Id $prevT)){ $tpid=$prevT }',
        'else{ $p=$PID; while($p){ $pr=Get-CimInstance Win32_Process -Filter ("ProcessId="+$p); if(-not $pr){break}; $proc=Get-Process -Id $p; if($proc -and $proc.Name -match "claude"){ $tpid=[int64]$pr.ParentProcessId; break }; $p=$pr.ParentProcessId } }',
        '[Console]::Write($hwnd.ToString()+"`t"+$tpid.ToString())'
    ].join('\n');
    try {
        const out = execFileSync('powershell', ['-NoProfile', '-Command', script],
            { encoding: 'utf8', timeout: 10000, windowsHide: true,
              env: Object.assign({}, process.env, { PREV_HWND: String(prevHwnd || 0), PREV_TPID: String(prevPid || 0) }) });
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

const action = process.argv[2];

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
const now = Date.now() / 1000;

// subagent start: subs+1, write yellow (a subagent running => the session is busy)
if (action === 'sub_start') {
    const old = readSession(sid) || {};
    const meta = getMeta(old.hwnd, old.termpid);
    writeSession(sid, { state: 'yellow', msg: '', ts: now, name: old.name || name, subs: (old.subs || 0) + 1, hwnd: meta.hwnd || old.hwnd || 0, termpid: meta.termpid || old.termpid || 0 });
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
const meta = getMeta(old && old.hwnd, old && old.termpid);
writeSession(sid, {
    state: action,
    msg: '',
    ts: now,
    name: (old && old.name) || name,
    subs: (old && old.subs) || 0,
    hwnd: meta.hwnd || (old && old.hwnd) || 0,
    termpid: meta.termpid || (old && old.termpid) || 0,
});
