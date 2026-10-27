"""Install passive cc-light Codex hooks; preserve all unrelated hooks and trust."""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

MARKER = "cc-light Codex indicator"


def configure(path, install=True):
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    before = json.dumps(data, sort_keys=True)
    script = str(Path(__file__).resolve().with_name("codex_wait.py")).replace("\\", "/")
    python = str(Path(sys.executable).resolve()).replace("\\", "/")
    for event in ("PermissionRequest", "PostToolUse"):
        groups = hooks.setdefault(event, [])
        for group in groups:
            group["hooks"] = [h for h in group.get("hooks", []) if h.get("statusMessage") != MARKER]
        groups[:] = [g for g in groups if g.get("hooks")]
        if install:
            groups.append({"matcher": "*", "hooks": [{"type": "command",
                "command": '"%s" "%s"' % (python, script),
                "statusMessage": MARKER, "timeout": 3}]})
    if json.dumps(data, sort_keys=True) == before:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(str(path), str(path) + ".bak-cc-light-" + str(time.time_ns()))
    temporary = path.with_name(path.name + ".tmp-cc-light")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "uninstall"])
    parser.add_argument("--path", default=str(Path.home() / ".codex/hooks.json"))
    args = parser.parse_args()
    changed = configure(args.path, args.action == "install")
    print("Updated" if changed else "Already configured", args.path)
    if args.action == "install":
        print("Review and trust the two cc-light Codex indicator hooks in Codex /hooks.")
        print("These hooks only record indicator state and never approve or deny requests.")
