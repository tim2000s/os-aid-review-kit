#!/usr/bin/env python3
"""Turn the review gate on for every repository on this machine, or off again.

    global.py on [--opt-out REPO ...] [--claude]   point git's global core.hooksPath at the kit's
                                        dispatcher; --claude also adds kit/guard.py to Claude Code
    global.py off                       remove both, if they are the kit's
    global.py status                    say what is set

A repository with the kit committed runs its own gate through the dispatcher; every other one is
reviewed in ambient mode, with records in ~/.review-ledger. A repository that commits on a timer
(the memory repositories) or should not be gated at all is opted out with
`git -C REPO config review.ambient false`, which --opt-out sets. A repository that sets its own
core.hooksPath (husky does) overrides the global one and is not covered; `on` lists any it finds
under the directories given by --scan.
"""

import argparse
import json
import os
import subprocess
import sys

# git and the tools print UTF-8; Windows would otherwise decode their output as cp1252
UTF8 = {"text": True, "encoding": "utf-8", "errors": "replace"}

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(KIT, "global-hooks")
SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
_K = KIT.replace("\\", "/")  # forward slashes, which sh accepts on every platform
GUARD = {"type": "command", "command": f'sh "{_K}/kit/py" "{_K}/kit/guard.py"'}
MATCHER = "Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit"


def claude_guard(add):
    """Add the guard to Claude Code's user settings, or remove it, leaving every other setting and hook
    alone: only a hook entry running exactly the kit's guard command is touched."""
    settings = {}
    if os.path.exists(SETTINGS):
        with open(SETTINGS, encoding="utf-8") as f:
            settings = json.load(f)
    pre = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    for entry in pre:
        entry["hooks"] = [h for h in entry.get("hooks", []) if h.get("command") != GUARD["command"]]
    pre[:] = [e for e in pre if e.get("hooks")]
    if add:
        pre.append({"matcher": MATCHER, "hooks": [GUARD]})
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + ".kit-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.replace(tmp, SETTINGS)  # a crash mid-write cannot leave Claude Code with a half-written file
    print(f"global: Claude Code guard {'added to' if add else 'removed from'} {SETTINGS}")


def current():
    p = subprocess.run(["git", "config", "--global", "--get", "core.hooksPath"], capture_output=True, **UTF8)
    return p.stdout.strip() or None


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # a pipe on Windows otherwise takes the code page
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=["on", "off", "status"])
    ap.add_argument("--opt-out", nargs="*", default=[])
    ap.add_argument("--claude", action="store_true", help="also add the guard hook to Claude Code")
    ap.add_argument("--scan", nargs="*", default=[], help="directories to search for repos with their own hooksPath")
    a = ap.parse_args(argv)
    now = current()
    if a.action == "status":
        ours = now and os.path.normcase(os.path.abspath(now)) == os.path.normcase(HOOKS)
        print(f"core.hooksPath (global): {now or 'unset'}" + (" (the kit)" if ours else ""))
        return 0
    if a.action == "off":
        if not now or os.path.normcase(os.path.abspath(now)) != os.path.normcase(HOOKS):
            raise SystemExit(f"global: core.hooksPath is {now or 'unset'}, not the kit's; leaving it")
        subprocess.run(["git", "config", "--global", "--unset", "core.hooksPath"], check=True)
        print("global: removed; repositories run their own .git/hooks again")
        if os.path.exists(SETTINGS):
            claude_guard(False)
        return 0
    same = now and os.path.normcase(os.path.abspath(now)) == os.path.normcase(HOOKS)
    if now and not same:
        raise SystemExit(f"global: core.hooksPath is already {now}; merge it with {HOOKS} by hand")
    subprocess.run(["git", "config", "--global", "core.hooksPath", HOOKS], check=True)
    print(f"global: core.hooksPath = {HOOKS}")
    for name in os.listdir(HOOKS):  # a copied or unzipped kit can lose the executable bit
        os.chmod(os.path.join(HOOKS, name), 0o755)  # noqa: S103 nosemgrep
    if a.claude:
        claude_guard(True)
    for repo in a.opt_out:
        subprocess.run(["git", "-C", repo, "config", "review.ambient", "false"], check=True)
        print(f"global: opted out {repo}")
    for top in a.scan:
        for d, dirs, _ in os.walk(top):
            if ".git" in dirs or os.path.isfile(os.path.join(d, ".git")):
                p = subprocess.run(
                    ["git", "-C", d, "config", "--local", "--get", "core.hooksPath"], capture_output=True, **UTF8
                )
                if p.stdout.strip():
                    print(f"global: not covered, sets its own core.hooksPath ({p.stdout.strip()}): {d}")
            dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("node_modules", "build", "Library")]
    return 0


if __name__ == "__main__":
    sys.exit(main())
