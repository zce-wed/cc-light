#!/usr/bin/env node
// cc-light hook: read CC hook JSON from stdin, maintain sessions/<sid>.json
// Node (not pythonw) for reliable stdin on Windows.
// Maintains a `subs` counter per session so green isn't written while subagents run.
const fs = require('fs');
const path = require('path');

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
    writeSession(sid, { state: 'yellow', msg: '', ts: now, name: old.name || name, subs: (old.subs || 0) + 1 });
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

// normal write (yellow/red/green), preserve old name + subs
const old = readSession(sid);
writeSession(sid, {
    state: action,
    msg: '',
    ts: now,
    name: (old && old.name) || name,
    subs: (old && old.subs) || 0,
});
