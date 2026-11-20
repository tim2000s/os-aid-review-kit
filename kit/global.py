#!/usr/bin/env python3
"""Turn the review gate on for every repository on this machine, or off again.

    global.py on [--opt-out REPO ...]   point git's global core.hooksPath at the kit's dispatcher
    global.py off                       remove it, if it is the kit's
    global.py status                    say what is set

A repository with the kit committed runs its own gate through the dispatcher; every other one is
reviewed in ambient mode, with records in ~/.review-ledger. A repository that commits on a timer
(the memory repositories) or should not be gated at all is opted out with
`git -C REPO config review.ambient false`, which --opt-out sets. A repository that sets its own
core.hooksPath (husky does) overrides the global one and is not covered; `on` lists any it finds
under the directories given by --scan.
"""

import argparse
import os
import subprocess
import sys

HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "global-hooks")


def current():
    p = subprocess.run(["git", "config", "--global", "--get", "core.hooksPath"], capture_output=True, text=True)
    return p.stdout.strip() or None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=["on", "off", "status"])
    ap.add_argument("--opt-out", nargs="*", default=[])
    ap.add_argument("--scan", nargs="*", default=[], help="directories to search for repos with their own hooksPath")
    a = ap.parse_args(argv)
    now = current()
    if a.action == "status":
        print(f"core.hooksPath (global): {now or 'unset'}" + (" (the kit)" if now == HOOKS else ""))
        return 0
    if a.action == "off":
        if now != HOOKS:
            raise SystemExit(f"global: core.hooksPath is {now or 'unset'}, not the kit's; leaving it")
        subprocess.run(["git", "config", "--global", "--unset", "core.hooksPath"], check=True)
        print("global: removed; repositories run their own .git/hooks again")
        return 0
    if now and now != HOOKS:
        raise SystemExit(f"global: core.hooksPath is already {now}; merge it with {HOOKS} by hand")
    subprocess.run(["git", "config", "--global", "core.hooksPath", HOOKS], check=True)
    print(f"global: core.hooksPath = {HOOKS}")
    for repo in a.opt_out:
        subprocess.run(["git", "-C", repo, "config", "review.ambient", "false"], check=True)
        print(f"global: opted out {repo}")
    for top in a.scan:
        for d, dirs, _ in os.walk(top):
            if ".git" in dirs or os.path.isfile(os.path.join(d, ".git")):
                p = subprocess.run(
                    ["git", "-C", d, "config", "--local", "--get", "core.hooksPath"], capture_output=True, text=True
                )
                if p.stdout.strip():
                    print(f"global: not covered, sets its own core.hooksPath ({p.stdout.strip()}): {d}")
            dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("node_modules", "build", "Library")]
    return 0


if __name__ == "__main__":
    sys.exit(main())
