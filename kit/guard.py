#!/usr/bin/env python3
"""Pre-tool hook for Claude Code, Codex and Gemini CLI: refuse the assistant's attempts to get round
the review gate.

Git cannot stop a commit made with --no-verify, so a person can still skip the hooks. Most of the
code here is written by an assistant, and the assistant's commands pass through this hook first:
it refuses a command that would skip or disable the hooks, a change made by hand to a review record
or to the settings the gate depends on, and a sign-off, which only the person who did the reading
may enter. `global.py on --claude --codex --gemini` adds it to each assistant's user settings, and
`--agent` tells it which one is calling, since each names its tools and reads a refusal differently:

    Claude Code  PreToolUse; Bash, PowerShell, Edit, Write ...; refuses with exit 2 and stderr
    Codex        PreToolUse; Bash, apply_patch; refuses with a permissionDecision "deny" on stdout
    Gemini CLI   BeforeTool; run_shell_command, write_file, replace; refuses with exit 2 and stderr

`enable.py` installs it too. It refuses in every repository, gated or not: an attempt to read where a
shell command will run, so as to relax the checks elsewhere, was given up on 25 November 2026 after
three review rounds each found a new way into a gated repository past it.

Claude Code and Gemini block a call only on exit 2, and Codex only on a deny, so every failure here,
from bad input to an internal error, is turned into a refusal. The matching is best-effort: a command is split at shell
separators and read word by word, commit messages and heredoc bodies are left out so that text
about the gate does not trip it, but a script written to disk and then run is not inspected.
"""

import json
import os
import re
import shlex
import sys

# variables that skip the hook runner, or point git or the gate at other settings
ENV_BLOCK = {
    "LEFTHOOK_EXCLUDE",
    "HOME",
    "XDG_CONFIG_HOME",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_NOSYSTEM",
    "REVIEW_LEDGER",
    "REVIEW_TIERS",
}
OFF_VALUES = {"0", "false", "no", "off"}
GATE_SETTINGS = re.compile(r"core\.hookspath|review\.ambient|review\.upstream", re.I)
PROTECTED = re.compile(
    r"(^|/)(\.review/records(/|$)|\.review-ledger(/|$)|\.git/hooks/|\.git/config$|\.gitconfig$|"
    r"\.review-kit(\.new|\.old)?(/|$)|lefthook-local\.ya?ml$|\.claude/settings(\.local)?\.json$|"
    r"\.codex/(hooks\.json|config\.toml|requirements\.toml)$|\.gemini/settings\.json$)"
)
SHELL_TOOLS = {"Bash", "PowerShell", "exec_command", "shell", "local_shell", "run_shell_command"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "write_file", "replace"}
PATCH_FILE = re.compile(r"^\s*\*\*\* (?:Add|Update|Delete) File: (.+)$|^\s*\*\*\* Move to: (.+)$", re.M)


def protected(path):
    path = path.strip().strip("'\"").replace("\\", "/")
    codex_home = os.environ.get("CODEX_HOME", "").replace("\\", "/").rstrip("/")
    if codex_home and re.search(rf"^{re.escape(codex_home)}/(hooks\.json|config\.toml|requirements\.toml)$", path):
        return True
    return bool(PROTECTED.search(path))
# commands that only read or stage a file, so that naming disable.py in them is ordinary kit work
READERS = {"cat", "less", "head", "tail", "grep", "rg", "ruff", "diff", "wc", "ls", "nl", "stat", "file", "semgrep",
           "mypy", "cp"}
GIT_READS = {"add", "rm", "mv", "diff", "show", "log", "status", "blame", "restore", "grep", "commit"}
WRITERS = {"rm", "mv", "cp", "tee", "truncate", "chmod", "ln", "dd", "install", "unlink", "touch", "mkdir",
           "rmdir", "trash", "gio"}


def segments(command):
    """The simple commands in a shell line, heredoc bodies removed. Separators inside quotes do not
    split, so a commit message or a quoted argument is read as one word, but a command substitution
    ($(...) or backticks) is split out even inside double quotes, since the shell runs it there."""
    command = re.sub(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(\n|$)", "\n", command, flags=re.S)
    out, cur, quote, i = [], [], None, 0

    def cut():
        out.append("".join(cur))
        cur.clear()

    while i < len(command):
        c = command[i]
        if quote == "'":
            quote = None if c == "'" else quote
            cur.append(c)
        elif c == "\\" and i + 1 < len(command):
            cur.append(command[i:i + 2])
            i += 2
            continue
        elif command.startswith("$(", i) or c == "`":
            cut()
            i += 2 if c == "$" else 1
            continue
        elif quote == '"':
            quote = None if c == '"' else quote
            cur.append(c)
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif c in ";&|\n(){}":
            cut()
        else:
            cur.append(c)
        i += 1
    cut()
    return [x for x in out if x.strip()]


def words(segment):
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def check_segment(seg):
    w = words(seg)
    if not w:
        return None
    env, i = {}, 0
    while i < len(w) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w[i]):
        k, v = w[i].split("=", 1)
        env[k] = v
        i += 1
    if w[0] == "export":
        env.update(x.split("=", 1) for x in w[1:] if "=" in x)
    # PowerShell sets variables with $env:NAME = value
    env.update((m[0], m[1]) for m in re.findall(r"\$env:(\w+)\s*=\s*['\"]?([^'\";\s]*)", seg, re.I))
    if w[0] == "unset" and any(x in ENV_BLOCK for x in w[1:]):
        return "unsets a variable git needs to find the gate's settings"
    rest = w[i:]
    if not rest and not env:
        return None
    if rest and os.path.basename(rest[0]) in ("sh", "bash", "zsh", "dash", "ksh", "eval"):
        # a command handed to another shell is checked as a command of its own
        inner = (
            " ".join(rest[1:])
            if os.path.basename(rest[0]) == "eval"
            else rest[rest.index("-c") + 1]
            if "-c" in rest[:-1]
            else ""
        )
        for part in segments(inner):
            why = check_segment(part)
            if why:
                return why
    if w[0] in ("env", "export") or rest[:1] == ["env"]:
        if "-i" in rest or "--ignore-environment" in rest or w[:2] == ["env", "-i"]:
            return "clears the environment, so git would run without the gate's settings"
        env.update(x.split("=", 1) for x in rest if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", x))
    for k, v in env.items():
        if k in ENV_BLOCK or (k in ("LEFTHOOK", "HUSKY") and v.lower() in OFF_VALUES):
            return f"sets {k}, which skips the hooks or points git or the gate at other settings"
    names = [os.path.basename(x) for x in rest]
    # writing to a protected file with a shell command
    targets = re.findall(r">>?\s*(\S+)", seg)
    if names and names[0] in ("cp", "install", "ln", "rsync", "ditto"):
        # only the destination is written: the last operand, or the directory given with -t
        targets += [x for x in rest[1:] if not x.startswith("-")][-1:]
        targets += [rest[k + 1] for k, x in enumerate(rest[:-1]) if x in ("-t", "--target-directory")]
        targets += [x.split("=", 1)[1] for x in rest if x.startswith("--target-directory=")]
    elif names and (names[0] == "unzip" or (names[0] == "tar" and any(
            re.fullmatch(r"-?[A-Za-z]*x[A-Za-z]*", x) or x == "--extract" for x in rest[1:]))):
        targets += rest[1:]  # an extraction writes where -C or -d points, in whichever flag form
    elif names and (names[0] in WRITERS or (names[0] in ("sed", "perl") and any(
            re.match(r"-\w*i|--in-place", x) for x in rest)) or (names[0] == "find" and any(
            x in ("-delete", "-exec", "-execdir", "-ok") for x in rest))):
        targets += rest[1:]
    if any(protected(t) for t in targets):
        return "writes to a review record, a hook or a setting the gate depends on"
    if "review.py" in names and "signoff" in rest:
        return "enters a human sign-off, which only the person who read the code may do"
    g = rest[names.index("git") + 1 :] if "git" in names else []
    words_ = iter(g)
    sub = ""
    for x in words_:
        if x in ("-C", "-c", "--git-dir", "--work-tree"):
            next(words_, None)  # their argument is not the subcommand
        elif not x.startswith("-") and "=" not in x:
            sub = x
            break
    first = names[0] if names else ""
    reads = first in READERS or (first == "sed" and not any(x.startswith("-i") for x in rest)) or (
        first == "git" and sub in GIT_READS)
    if "disable.py" in names and not reads:
        return "switches the review gate off for this repository"
    module = any(x == "-m" and y == "global" for x, y in zip(rest, rest[1:], strict=False))
    if ("global.py" in names or module) and not reads:
        # only the two actions that add or report are the assistant's; off and update are the person's,
        # and an argument that is not literally one of the two (a variable, $(...), xargs) counts as either
        at = names.index("global.py") if "global.py" in names else rest.index("-m") + 1
        after = [x for x in rest[at + 1:] if not x.startswith("-")]
        if after[:1] not in (["on"], ["status"]):
            return "runs global.py other than as `on` or `status`; off and update are the person's to run"
    if "git" not in names:
        return None
    if sub == "commit":
        after = g[g.index("commit") + 1 :]
        kept, skip = [], False
        for x in after:  # leave out message text, which may well mention the gate
            if skip:
                skip = False
                continue
            if x in ("-m", "--message", "-F", "--file", "-C", "-c", "--reuse-message", "--reedit-message"):
                skip = True
                continue
            if not x.startswith(("--message=", "--file=")):
                kept.append(x)
        g = g[: g.index("commit") + 1] + kept
        if any(re.fullmatch(r"-[a-zA-Z]*n[a-zA-Z]*", x) and not x.startswith("-m") for x in kept):
            return "skips git's hooks (-n is --no-verify for git commit)"
    if any("--no-v" in x for x in g):
        return "skips git's hooks (git accepts any abbreviation of --no-verify)"
    joined = " ".join(g)  # an alias's definition may be one word (-c alias.c=...) or the next one
    if re.search(r"alias\.", joined, re.I) and re.search(r"commit\b.*?(^|[\s='])-[a-zA-Z]*n[a-zA-Z]*\b", joined):
        return "defines a git alias that skips the hooks"
    if any(GATE_SETTINGS.search(x) for x in g):
        read_only = sub == "config" and any(x in ("--get", "--get-all", "--get-regexp", "--list", "-l") for x in g)
        if not read_only:
            return "changes a setting the review gate depends on"
    return None


def commit_flags(command):
    """A second, quote-aware pass over the whole command: a separator inside a commit message, as in
    -m "Fix X (closes #3)", splits the line wrongly for segments(), and the flags after it are then
    read here instead."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    for i, t in enumerate(tokens):
        if os.path.basename(t) != "git":
            continue
        g = []
        for x in tokens[i + 1 :]:
            if x and all(c in "();<>|&" for c in x):
                break
            g.append(x)
        if "commit" not in g:
            continue
        kept, skip = [], False
        for x in g[g.index("commit") + 1 :]:
            if skip:
                skip = False
            elif x in ("-m", "--message", "-F", "--file", "-C", "-c", "--reuse-message", "--reedit-message"):
                skip = True
            elif not x.startswith(("--message=", "--file=")):
                kept.append(x)
        if any("--no-v" in x for x in kept) or any(re.fullmatch(r"-[a-zA-Z]*n[a-zA-Z]*", x) for x in kept):
            return "skips git's hooks (--no-verify or -n on git commit)"
    return None


def reason(tool, data):
    if not isinstance(data, dict):
        raise ValueError("tool_input is not an object")
    if tool == "apply_patch":  # Codex: the patch text names every file it touches
        patch = data.get("command") or data.get("cmd") or data.get("input") or ""
        if not isinstance(patch, str):
            raise ValueError("patch is not a string")
        paths = [a or b for a, b in PATCH_FILE.findall(patch)]
        if any(protected(p) for p in paths):
            return "edits a review record, a hook or a setting the gate depends on by hand"
        return None
    if tool in SHELL_TOOLS:
        command = data.get("command") or data.get("cmd") or ""  # Codex's exec_command calls it cmd
        if not isinstance(command, str):
            raise ValueError("command is not a string")
        # a patch handed to the shell (apply_patch <<'EOF' ... EOF) names its files in the heredoc
        if any(protected(a or b) for a, b in PATCH_FILE.findall(command)):
            return "edits a review record, a hook or a setting the gate depends on by hand"
        for seg in segments(command):
            why = check_segment(seg)
            if why:
                return why
        heredocs_removed = re.sub(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(\n|$)", "\n", command, flags=re.S)
        why = commit_flags(heredocs_removed)
        if why:
            return why
    elif tool in EDIT_TOOLS:
        path = str(data.get("file_path") or data.get("notebook_path") or data.get("path") or "").replace("\\", "/")
        if protected(path):
            return "edits a review record, a hook or a setting the gate depends on by hand"
    return None


def refuse(agent, message):
    if agent == "codex":
        # Codex reads a deny from stdout; its exit-2 path has been reported to fail open on Windows
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": message}}))
        return 0
    print(message, file=sys.stderr)
    return 2


def main():
    for stream in (sys.stdout, sys.stderr):  # a pipe on Windows otherwise takes the code page
        stream.reconfigure(encoding="utf-8", errors="replace")
    agent = sys.argv[sys.argv.index("--agent") + 1] if "--agent" in sys.argv[:-1] else "claude"
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))  # not the Windows code page
        why = reason(event.get("tool_name", ""), event.get("tool_input") or {})
    except Exception as e:  # noqa: BLE001 - any failure must refuse, or the call goes ahead
        return refuse(agent, f"review guard: could not check this call ({e}), so it is refused")
    if why:
        return refuse(agent, f"review guard: refused because this {why}. Run the review steps instead "
                             "(review.py checks, a fresh-context review, review.py record); REVIEW_SKIP=\"<reason>\" "
                             "marks a work-in-progress commit. A person, not the assistant, decides any exception.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
