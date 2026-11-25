#!/usr/bin/env python3
"""Turn the review gate on for every repository on this machine, or off again.

    global.py on [--opt-out REPO ...] [--claude] [--codex] [--gemini]
                                        point git's global core.hooksPath at the kit's dispatcher, and
                                        add kit/guard.py to each assistant named
    global.py update                    put the checkout's last commit live (a person's step, run from
                                        the live copy: python3 ~/.review-kit/kit/global.py update)
    global.py off                       remove the hooks path and every guard, if they are the kit's
    global.py status                    say what is set and which commit is live

The hooks and the guard run from ~/.review-kit, a copy of the checkout's last commit, so that an
edit to the checkout, made by hand or by an assistant, changes nothing live until it is committed
through the kit's own gate and a person runs `update`. `on` and `enable.py` make the copy only where
there is none; the guard refuses `update` and shell writes to the copy from an assistant.

A repository with the kit committed runs its own gate through the dispatcher; every other one is
reviewed in ambient mode, with records in ~/.review-ledger. A repository that commits on a timer
(the memory repositories) or should not be gated at all is opted out with
`git -C REPO config review.ambient false`, which --opt-out sets. A repository that sets its own
core.hooksPath (husky does) overrides the global one and is not covered; `on` lists any it finds
under the directories given by --scan.
"""

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

# git and the tools print UTF-8; Windows would otherwise decode their output as cp1252
UTF8 = {"text": True, "encoding": "utf-8", "errors": "replace"}

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")
RUNTIME = os.path.join(HOME, ".review-kit")
HOOKS = os.path.join(RUNTIME, "global-hooks")
# where the hooks ran before 25 November 2026: this kit's checkout, or the default one when run from the copy
LEGACY_HOOKS = (os.path.join(KIT, "global-hooks"), os.path.join(HOME, "os-aid-review-kit", "global-hooks"))
PARTS = ("kit", "global-hooks", "templates")
_K = RUNTIME.replace("\\", "/")  # forward slashes, which sh accepts on every platform
GUARD = f'sh "{_K}/kit/py" "{_K}/kit/guard.py"'
CLOSED = " || exit 2"  # a missing live copy must refuse every call, not let it through
SOURCE = KIT  # the checkout a copy is made from; update reads it from the live copy's DEPLOYED instead
KIT_GUARD = re.compile(r'^sh "[^"]*/kit/py" "[^"]*/kit/guard\.py"')  # this kit's guard, from any location
# each assistant's user settings file, the event its pre-tool hooks hang from, the tools to match, and
# the hook entry; a timeout is given because Gemini's is in milliseconds and Codex's in seconds
AGENTS = {
    "claude": (os.path.join(HOME, ".claude", "settings.json"), "PreToolUse",
               "Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit", {"type": "command", "command": GUARD + CLOSED}),
    "codex": (os.path.join(os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex"), "hooks.json"),
              "PreToolUse", "Bash|apply_patch|Edit|Write",
              {"type": "command", "command": GUARD + " --agent codex" + CLOSED, "timeout": 30}),
    "gemini": (os.path.join(HOME, ".gemini", "settings.json"), "BeforeTool", "run_shell_command|write_file|replace",
               {"type": "command", "command": GUARD + " --agent gemini" + CLOSED, "name": "review-guard",
                "timeout": 30000}),
}
NAMES = {"claude": "Claude Code", "codex": "Codex", "gemini": "Gemini CLI"}
# the instructions file each assistant reads in every project, which gets the kit's marked block
INSTRUCTIONS = {"claude": os.path.join(HOME, ".claude", "CLAUDE.md"),
                "codex": os.path.join(os.path.dirname(AGENTS["codex"][0]), "AGENTS.md"),
                "gemini": os.path.join(HOME, ".gemini", "GEMINI.md")}
START, END = "<!-- review-kit:start", "<!-- review-kit:end -->"


def ours(path, live_only=False):
    """Whether a hooks path is the kit's: the live copy's, or (unless live_only) a checkout's from before."""
    return bool(path) and any(os.path.normcase(os.path.abspath(os.path.expanduser(path))) == os.path.normcase(h)
                              for h in ((HOOKS,) if live_only else (HOOKS, *LEGACY_HOOKS)))


def kit_git(*args):
    p = subprocess.run(["git", "-C", SOURCE, *args], capture_output=True, **UTF8)
    if p.returncode:
        raise SystemExit(f"global: git {' '.join(args)} failed in {SOURCE}: {p.stderr.strip()}")
    return p.stdout.strip()


def deployed(part=1):
    """The commit the live copy holds (part 2: the checkout it came from), or None."""
    try:
        with open(os.path.join(RUNTIME, "DEPLOYED"), encoding="utf-8") as f:
            m = re.match(r"commit ([0-9a-f]{40}) of (.+?): ", f.read())
    except FileNotFoundError:
        return None
    return m and m.group(part)


def check_reviewed(base):
    """Refuse unless every file changed since base has a valid review record, judged by the review.py and
    the tier settings that base itself holds, so that neither the working tree nor the commit being
    deployed can loosen the check."""
    with tempfile.TemporaryDirectory() as tmp:
        for path in ("kit/review.py", ".review/tiers.toml"):
            got = subprocess.run(["git", "-C", SOURCE, "show", f"{base}:{path}"], capture_output=True)
            if got.returncode:
                raise SystemExit(f"global: {base} holds no {path}, so its review records cannot be checked")
            os.makedirs(os.path.join(tmp, os.path.dirname(path)), exist_ok=True)
            with open(os.path.join(tmp, path), "wb") as f:
                f.write(got.stdout)
        env = {**os.environ, "REVIEW_TIERS": os.path.join(tmp, ".review", "tiers.toml")}
        p = subprocess.run([sys.executable, os.path.join(tmp, "kit", "review.py"), "check", "--base", base],
                           cwd=SOURCE, capture_output=True, env=env, **UTF8)
    if p.returncode:
        raise SystemExit(f"global: not deployed; files changed since {base[:10]} lack a valid review record:\n"
                         f"{(p.stdout + p.stderr).strip()}")


def deploy(update=False):
    """Copy the checkout's last commit to RUNTIME and say what was copied. The first copy, where there is
    none, takes HEAD as it stands, since nothing earlier is there to judge it by. Every later one is an
    update, a person's step, and goes live only when each file changed since the live commit has a valid
    review record, so a commit made with REVIEW_SKIP cannot go live. A kit that is not a git checkout
    (an unzipped download) is copied as it stands, the first time only."""
    if update and not deployed():
        raise SystemExit(f"global: {RUNTIME} does not say which commit it holds, so there is nothing to check an "
                         f"update against; delete {RUNTIME} and run global.py on to make a new first copy")
    if not update and os.path.exists(RUNTIME):
        raise SystemExit(f"global: {RUNTIME} exists but does not say which commit it holds; a person runs "
                         "`global.py update`, or deletes it and runs `global.py on`")
    if os.path.normcase(os.path.realpath(SOURCE)) == os.path.normcase(os.path.realpath(RUNTIME)):
        raise SystemExit("global: the live copy cannot be copied onto itself; it names no checkout to copy from")
    new, old = RUNTIME + ".new", RUNTIME + ".old"
    shutil.rmtree(new, ignore_errors=True)
    os.makedirs(new)
    if os.path.exists(os.path.join(SOURCE, ".git")):
        head = kit_git("rev-parse", "HEAD")
        tar = subprocess.run(["git", "-C", SOURCE, "archive", "--format=tar", head, *PARTS], capture_output=True,
                             check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(tar)) as t:
            try:
                t.extractall(new, filter="data")
            except TypeError:  # Python 3.11 before 3.11.4 has no filter; the archive comes from git itself
                t.extractall(new)  # noqa: S202 nosemgrep
        if update:
            try:
                if subprocess.run(["git", "-C", SOURCE, "merge-base", "--is-ancestor", deployed(), head]).returncode:
                    raise SystemExit(f"global: not deployed; {head[:10]} does not descend from the live commit "
                                     f"{deployed()[:10]}, and going back is a revert to commit and review")
                check_reviewed(deployed())
            except SystemExit:
                shutil.rmtree(new, ignore_errors=True)
                raise
        what = f"commit {head} of {SOURCE}: {kit_git('log', '-1', '--format=%s', head)}"
    elif update:
        raise SystemExit(f"global: {SOURCE} is not a git checkout, so there is no reviewed commit to update to")
    else:
        for part in PARTS:
            shutil.copytree(os.path.join(SOURCE, part), os.path.join(new, part),
                            ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache"))
        what = f"{SOURCE} as it stands, which is not a git checkout"
    with open(os.path.join(new, "DEPLOYED"), "w", encoding="utf-8") as f:
        f.write(what + "\n")
    hooks = os.path.join(new, "global-hooks")
    for path in [*(os.path.join(hooks, n) for n in os.listdir(hooks)), os.path.join(new, "kit", "py")]:
        os.chmod(path, 0o755)  # noqa: S103 nosemgrep - an unzip or a copy can lose the executable bit
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(RUNTIME):
        os.rename(RUNTIME, old)
    try:
        os.rename(new, RUNTIME)
    except OSError:
        if os.path.exists(old):
            os.rename(old, RUNTIME)  # put the previous copy back rather than leave the gate with none
        raise
    shutil.rmtree(old, ignore_errors=True)
    return what


def ensure_live():
    """Make the live copy where there is none, or where its record of what it holds is missing."""
    if not os.path.exists(os.path.join(RUNTIME, "DEPLOYED")):
        print(f"global: {RUNTIME} now holds {deploy()}")


def has_guard(agent):
    path = os.path.realpath(AGENTS[agent][0])
    hooks = load_settings(path).get("hooks", {}).get(AGENTS[agent][1], []) if os.path.exists(path) else []
    return any(KIT_GUARD.match(str(h.get("command", ""))) for g in hooks for h in g.get("hooks", []))


def instructions(agent, add):
    """Insert or refresh the kit's marked block in the assistant's global instructions, or remove it."""
    path = os.path.realpath(INSTRUCTIONS[agent])
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    if not add and START not in text:
        return
    if START in text and END not in text:
        raise SystemExit(f"global: {path} has the kit's start marker but no end marker; fix it by hand")
    if START in text:
        before, after = text[:text.index(START)].rstrip("\n"), text[text.index(END) + len(END):].lstrip("\n")
        text = (before + "\n" + after).strip("\n")
        text = text + "\n" if text else ""
    if add:
        with open(os.path.join(RUNTIME, "templates", "assistant-instructions.md"), encoding="utf-8") as f:
            block = f.read().strip()
        text = text.rstrip() + ("\n\n" if text.strip() else "") + block + "\n"
    elif not os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"global: review instructions {'added to' if add else 'removed from'} {path}")


def install_guard(agent, add):
    """Add the guard to one assistant's user settings, or remove it, leaving every other setting and
    hook alone: only a hook entry running the kit's guard command is touched."""
    path, event, matcher, entry = AGENTS[agent]
    path = os.path.realpath(path)  # write through a symlink into a dotfiles folder, not over it
    if not add and not os.path.exists(path):
        return
    settings = load_settings(path)
    if not add and event not in settings.get("hooks", {}):
        return
    pre = settings.setdefault("hooks", {}).setdefault(event, [])
    for group in pre:
        group["hooks"] = [h for h in group.get("hooks", []) if not KIT_GUARD.match(str(h.get("command", "")))]
    pre[:] = [g for g in pre if g.get("hooks")]
    if add:
        pre.append({"matcher": matcher, "hooks": [entry]})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".kit-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    if os.path.exists(path):
        shutil.copymode(path, tmp)  # keep a settings file that holds tokens as private as it was
    os.replace(tmp, path)  # a crash mid-write cannot leave the assistant with a half-written file
    print(f"global: {NAMES[agent]} guard {'added to' if add else 'removed from'} {path}")
    if add and agent == "codex":
        print("global: Codex runs a hook only once it is trusted; open Codex, run /hooks and trust the review guard")
    hooks_config = settings.get("hooksConfig", {})
    if add and agent == "gemini" and (hooks_config.get("enabled") is False or "review-guard" in hooks_config.get(
            "disabled", [])):
        print("global: Gemini CLI has hooks, or this one, switched off in hooksConfig, so the guard will not run")


def load_settings(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except ValueError as e:
        raise SystemExit(f"global: {path} is not plain JSON ({e}); it may hold comments, which the kit "
                         "cannot preserve, so add the guard there by hand or remove the comments") from None


def current():
    p = subprocess.run(["git", "config", "--global", "--get", "core.hooksPath"], capture_output=True, **UTF8)
    return p.stdout.strip() or None


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # a pipe on Windows otherwise takes the code page
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=["on", "off", "status", "update"])
    ap.add_argument("--opt-out", nargs="*", default=[])
    for agent in AGENTS:
        ap.add_argument(f"--{agent}", action="store_true", help=f"also add the guard hook to {NAMES[agent]}")
    ap.add_argument("--scan", nargs="*", default=[], help="directories to search for repos with their own hooksPath")
    a = ap.parse_args(argv)
    now = current()
    if a.action == "status":
        label = " (the kit's live copy)" if ours(now, live_only=True) else (
            " (the kit's checkout, from before the live copy; run global.py on)" if ours(now) else "")
        print(f"core.hooksPath (global): {now or 'unset'}{label}")
        live = os.path.join(RUNTIME, "DEPLOYED")
        print(f"live copy: {open(live, encoding='utf-8').read().strip() if os.path.exists(live) else 'none'}")
        return 0
    if a.action == "update":
        # from the live copy, whose code no uncommitted edit can reach, so the check below is the reviewed one
        if os.path.normcase(os.path.realpath(KIT)) != os.path.normcase(os.path.realpath(RUNTIME)):
            raise SystemExit(f"global: run the update from the live copy: python3 {RUNTIME}/kit/global.py update")
        global SOURCE
        SOURCE = deployed(2) or SOURCE
        print(f"global: {RUNTIME} now holds {deploy(update=True)}")
        return 0
    if a.action == "off":
        for agent in AGENTS:
            install_guard(agent, False)
            instructions(agent, False)
        if not ours(now):
            print(f"global: core.hooksPath is {now or 'unset'}, not the kit's; leaving it")
            return 0
        subprocess.run(["git", "config", "--global", "--unset", "core.hooksPath"], check=True)
        print("global: removed; repositories run their own .git/hooks again")
        return 0
    for agent in AGENTS:
        if getattr(a, agent):
            load_settings(os.path.realpath(AGENTS[agent][0]))  # fail before anything is changed
    if now and not ours(now):
        raise SystemExit(f"global: core.hooksPath is already {now}; merge it with {HOOKS} by hand")
    ensure_live()
    subprocess.run(["git", "config", "--global", "core.hooksPath", HOOKS], check=True)
    print(f"global: core.hooksPath = {HOOKS}")
    for agent in AGENTS:
        if getattr(a, agent):
            install_guard(agent, True)
            instructions(agent, True)
        elif has_guard(agent):
            install_guard(agent, True)  # an older guard entry is pointed at the live copy
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
