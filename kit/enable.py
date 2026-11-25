#!/usr/bin/env python3
"""Turn the review gate on for one repository. Written so that an assistant can do it when asked.

    enable.py [REPO]            point REPO's hooks at the kit (default: the repository here), add the
                                assistant guard and review steps for each assistant installed, and
                                print what was done and what is still needed, for the assistant to act on
    enable.py --status [REPO]   say whether the gate runs for commits in REPO; exits 1 where it does not

Nothing is committed into the repository and no other repository changes: the hooks path and
`review.ambient` are set in REPO's own git config. The instructions apply only where the gate runs;
the guard refuses ways round the gate in every repository. Records go to the ledger, as in ambient
mode. Turning the gate on only adds checks, so an assistant may run this; turning it off is
`disable.py`, which the guard leaves to a person.
"""

import argparse
import importlib
import os
import shutil
import subprocess
import sys
import tomllib

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KIT, "kit"))
import install  # noqa: E402
import review  # noqa: E402

machine = importlib.import_module("global")  # global.py; `global` is a keyword

UTF8 = {"text": True, "encoding": "utf-8", "errors": "replace"}


HOOKS = os.path.join(KIT, "global-hooks")


def same_path(a, b):
    return os.path.normcase(os.path.abspath(os.path.expanduser(a))) == os.path.normcase(os.path.abspath(b))


def git(top, *args):
    return subprocess.run(["git", "-C", top, *args], capture_output=True, **UTF8).stdout.strip()


def gate_mode(directory):
    """How the review gate runs for commits made in `directory`, or None where it does not."""
    top = git(directory, "rev-parse", "--show-toplevel")
    machine = git(directory, "config", "--global", "--get", "core.hooksPath")
    machine = "the hooks for every repository on this machine" if machine and same_path(machine, HOOKS) else None
    if not top:
        return machine
    if os.path.exists(os.path.join(top, ".review", "bin", "review.py")):
        return "the kit committed in this repository"
    if git(top, "config", "--local", "--bool", "review.ambient") == "false":
        return None
    local = git(top, "config", "--local", "--get", "core.hooksPath")
    if local:  # a repository's own hooks path replaces the machine's
        return "this repository's own hooks" if same_path(local, HOOKS) else None
    return machine


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # a pipe on Windows otherwise takes the code page
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)
    top = git(a.repo, "rev-parse", "--show-toplevel")
    if not top:
        raise SystemExit(f"enable: {os.path.abspath(a.repo)} is not inside a git repository")
    if a.status:
        mode = gate_mode(top)
        print(f"review gate: on in {top}, through {mode}" if mode else f"review gate: off in {top}")
        return 0 if mode else 1

    own = git(top, "config", "--local", "--get", "core.hooksPath")
    if own and not same_path(own, HOOKS):
        raise SystemExit(
            f"enable: {top} sets its own core.hooksPath ({own}), as husky does, and the kit would "
            "replace it; set the gate up by hand there (see the kit README)"
        )
    subprocess.run(["git", "-C", top, "config", "--local", "core.hooksPath", HOOKS], check=True)
    subprocess.run(["git", "-C", top, "config", "--local", "review.ambient", "true"], check=True)
    for name in os.listdir(HOOKS):  # a copied or unzipped kit can lose the executable bit
        os.chmod(os.path.join(HOOKS, name), 0o755)  # noqa: S103 nosemgrep

    installed = os.path.exists(os.path.join(top, ".review", "bin", "review.py"))
    if installed:
        tiers, script = os.path.join(top, ".review", "tiers.toml"), ".review/bin/review.py"
    else:
        review.enter_ambient(top)  # copies the matching preset into the ledger the first time
        tiers, script = os.environ["REVIEW_TIERS"], os.path.join(KIT, "kit", "review.py")
    with open(tiers, "rb") as f:
        cfg = tomllib.load(f)
    langs = install.audit(top)["languages"]
    need = [
        b for k in [*langs, "all"] for b in install.BINARIES[k] if installed or b not in ("lefthook", "trivy", "mypy")
    ]
    missing = [t for t in need if not shutil.which(t) and not (t == "detekt" and shutil.which("detekt-cli"))]
    added = []
    for agent, (settings, *_) in machine.AGENTS.items():
        if os.path.isdir(os.path.dirname(os.path.realpath(settings))):
            machine.install_guard(agent, True)
            machine.instructions(agent, True)
            added.append(machine.NAMES[agent])

    run = f'sh "{os.path.join(KIT, "kit", "py")}" "{script}"' if not installed else f"python3 {script}"
    print(f"\nThe review gate is on for {top}, set in this repository's own git config; nothing was committed.")
    print(f"Tiers: the {install.detect_preset(top)} settings, in {tiers}.")
    if not cfg.get("tests"):
        print(
            "Tests: none configured, and every record will say so. A change to a high-tier path (the dose "
            "path) cannot be recorded until a test command is named under [tests] in that file. Only the "
            "person may edit it, so ask them to add their usual test command."
        )
    if missing:
        how = (
            f"brew install {' '.join(missing)}"
            if shutil.which("brew")
            else f"the Requirements section of {os.path.join(KIT, 'README.md')}"
        )
        print(f"Missing tools: {', '.join(missing)}. Ask the person before installing them, with {how}.")
    print(
        f"Guard and review steps added for: {', '.join(added)}; they take effect in the next session."
        if added
        else "No assistant settings were found, so no guard was added; run this again after "
        "installing Claude Code, Codex or Gemini CLI."
    )
    print(f"""
For every change in this repository from now on:
1. Stage it and run: {run} checks
2. Have the staged diff reviewed by a fresh session or subagent that did not write it, given the diff
   and REVIEW.md where there is one. Fix every important finding, re-stage and run the checks again.
3. Write what the change does, why and what could go wrong to a file outside the repository, then run:
   {run} record --author <model> --reviewer "<model>, fresh context" --explanation <file> \\
       --findings <json file> [--way-back "<how to undo it>"]
4. Commit. The hooks refuse a commit whose files are in no record.
5. For a pull request: {run} pr-body --base <upstream branch> | gh pr create --body-file -
Only the person may turn the gate off (disable.py) or sign off a record.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
