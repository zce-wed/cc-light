#!/usr/bin/env node
// cc-light hook: read CC hook JSON from stdin, maintain sessions/<sid>.json
// Node (not pythonw) for reliable stdin on Windows.
// Maintains a `subs` counter per session so green isn't written while subagents run.
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

// 沿父进程链向上找第一个有窗口的终端(WindowsTerminal/VSCode/cmd)的 HWND,
// 写进 session 文件,供灯窗口点击会话名时跳转过去。
function getWindowHwnd() {
    const script = `$term='WindowsTerminal|conhost|cmd|Code|Trae|Cursor|Windsurf|wezterm|alacritty|Hyper|conemu64|HBuilderX'; $tu=Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow(); [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h,out uint p);' -Name U -Namespace CC -PassThru; $fg=$tu::GetForegroundWindow(); if($fg -and $fg -ne 0){ $pv=0; [void]$tu::GetWindowThreadProcessId($fg,[ref]$pv); $fp=Get-Process -Id $pv -EA SilentlyContinue; if($fp -and $fp.Name -match $term){ [Console]::Write($fg.ToInt64()); exit } }; $p=$PID;$first=0; while($p){ $pr=Get-CimInstance Win32_Process -Filter ('ProcessId='+$p) -EA SilentlyContinue; if(-not $pr){break}; $proc=Get-Process -Id $p -EA SilentlyContinue; if($proc -and $proc.MainWindowHandle -and $proc.MainWindowHandle -ne 0){ if($first -eq 0){$first=$proc.MainWindowHandle}; if($proc.Name -match $term){[Console]::Write($proc.MainWindowHandle);exit} }; $p=$pr.ParentProcessId }; if($first -ne 0){[Console]::Write($first)}`;
    try {
        const out = execFileSync('powershell', ['-NoProfile', '-Command', script],
                                 { encoding: 'utf8', timeout: 10000, windowsHide: true });
        const h = parseInt(String(out).trim(), 10);
        return (isNaN(h) || h === 0) ? 0 : h;
    } catch (e) { return 0; }
}

// terminal.processId = claude.exe 的父进程(VS Code 报告那一层,如 2132)。
// hook 进程链: hook → hook-runner → claude.exe → <term.processId>。
// 故从 process.ppid 向上找 name 含 claude 的进程,返回其父 pid。
function getTerminalPid() {
    const startPid = process.ppid;
    if (!startPid) return 0;
    const script = `$p=[int]$env:START_PID; while($p){ $pr=Get-CimInstance Win32_Process -Filter ('ProcessId='+$p) -EA SilentlyContinue; if(-not $pr){break}; $proc=Get-Process -Id $p -EA SilentlyContinue; if($proc -and $proc.Name -match 'claude'){ [Console]::Write($pr.ParentProcessId); break }; $p=$pr.ParentProcessId }`;
    try {
        const out = execFileSync('powershell', ['-NoProfile', '-Command', script],
                                 { encoding: 'utf8', timeout: 10000, windowsHide: true,
                                   env: Object.assign({}, process.env, { START_PID: String(startPid) }) });
        const pid = parseInt(String(out).trim(), 10);
        return (isNaN(pid) || pid === 0) ? 0 : pid;
    } catch (e) { return 0; }
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
    writeSession(sid, { state: 'yellow', msg: '', ts: now, name: old.name || name, subs: (old.subs || 0) + 1, hwnd: old.hwnd || getWindowHwnd(), termpid: old.termpid || getTerminalPid() });
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

// normal write (yellow/red/green), preserve old name + subs + hwnd
const old = readSession(sid);
writeSession(sid, {
    state: action,
    msg: '',
    ts: now,
    name: (old && old.name) || name,
    subs: (old && old.subs) || 0,
    hwnd: (old && old.hwnd) || getWindowHwnd(),
    termpid: (old && old.termpid) || getTerminalPid(),
});
