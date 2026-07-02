#!/usr/bin/env node
// cc-light hook: read Claude Code hook JSON from stdin, write sessions/<sid>.json
// Replaces the pythonw-based hook for reliability (node reads stdin stably on Windows).
const fs = require('fs');
const path = require('path');

const DIR = __dirname;
const SESSIONS_DIR = path.join(DIR, 'sessions');
const MISS_LOG = path.join(DIR, 'hook-miss.log');
const COLORS = { red: 1, yellow: 1, green: 1 };

const action = process.argv[2];

let raw = '';
try { raw = fs.readFileSync(0, 'utf8'); } catch (e) {}
let d = {};
try { d = raw.trim() ? JSON.parse(raw) : {}; } catch (e) {}

function safe(s) { return String(s).replace(/[^A-Za-z0-9_-]/g, '') || 'unknown'; }

if (action === 'end') {
    const sid = d.session_id;
    try { fs.appendFileSync(MISS_LOG, (Date.now() / 1000) + ' end sid=' + sid + ' raw=' + raw + '\n'); } catch (e) {}
    if (sid) {
        try { fs.unlinkSync(path.join(SESSIONS_DIR, safe(sid) + '.json')); } catch (e) {}
    }
    process.exit(0);
}

if (!COLORS[action]) process.exit(2);

const sid = d.session_id;
if (!sid) {
    // missing session_id: log and don't write (avoid unknown pollution)
    try { fs.appendFileSync(MISS_LOG, (Date.now() / 1000) + ' miss-sid raw=' + raw + '\n'); } catch (e) {}
    process.exit(0);
}

const cwd = d.cwd || '';
const base = cwd.replace(/\\/g, '/').replace(/\/$/, '').split('/').pop();
const name = base || String(sid).slice(0, 8);
const data = { state: action, msg: '', ts: Date.now() / 1000, name: name };

try { fs.mkdirSync(SESSIONS_DIR, { recursive: true }); } catch (e) {}
const file = path.join(SESSIONS_DIR, safe(sid) + '.json');
const tmp = file + '.tmp';
try {
    fs.writeFileSync(tmp, JSON.stringify(data));
    fs.renameSync(tmp, file);   // atomic
} catch (e) {}
