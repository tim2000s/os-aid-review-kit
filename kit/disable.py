#!/usr/bin/env python3
"""Turn the review gate off for one repository: for a person to run, since the guard refuses it from
an assistant.

    disable.py [REPO]

Undoes `enable.py` for REPO: removes its hooks path where that is the kit's and sets `review.ambient`
false, which also opts the repository out of the machine-wide hooks. Records in the ledger are kept.
The guard and the instructions stay where they are; `global.py off` removes them. A repository
with the kit committed is still gated by its own lefthook.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enable  # noqa: E402


def main(argv=None):
    repo = (argv if argv is not None else sys.argv[1:])[:1] or ["."]
    top = subprocess.run(
        ["git", "-C", repo[0], "rev-parse", "--show-toplevel"], capture_output=True, text=True
    ).stdout.strip()
    if not top:
        raise SystemExit(f"disable: {os.path.abspath(repo[0])} is not inside a git repository")
    own = subprocess.run(
        ["git", "-C", top, "config", "--local", "--get", "core.hooksPath"], capture_output=True, text=True
    ).stdout.strip()
    if enable.hooks.ours(own):
        subprocess.run(["git", "-C", top, "config", "--local", "--unset", "core.hooksPath"], check=True)
    subprocess.run(["git", "-C", top, "config", "--local", "review.ambient", "false"], check=True)
    mode = enable.gate_mode(top)
    print(f"review gate: still on in {top}, through {mode}" if mode else f"review gate: off in {top}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
