#!/usr/bin/env python3
"""Claude Code PreToolUse hook: refuse the assistant's attempts to get round the review gate.

Git cannot stop a commit made with --no-verify, so a person can still skip the hooks. Most of the
code here is written by an assistant, and the assistant's commands pass through this hook first:
it refuses a command that would skip or disable the hooks, a change made by hand to a review record
or to the settings the gate depends on, and a sign-off, which only the person who did the reading
may enter. `global.py on --claude` adds it to ~/.claude/settings.json.

Claude Code blocks a tool call only when a hook exits with status 2, so every failure here, from bad
input to an internal error, exits 2. The matching is best-effort: a command is split at shell
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
    r"(^|/)(\.review/records/|\.review-ledger/|\.git/hooks/|\.git/config$|\.gitconfig$|"
    r"lefthook-local\.ya?ml$|\.claude/settings(\.local)?\.json$)"
)
WRITERS = {"rm", "mv", "cp", "tee", "truncate", "chmod", "ln", "dd", "install", "unlink"}


def segments(command):
    """The simple commands in a shell line, heredoc bodies removed."""
    command = re.sub(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(\n|$)", "\n", command, flags=re.S)
    return [s for s in re.split(r"\|\||&&|[;&|\n`(){}]|\$\(", command) if s.strip()]


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
    if names and (names[0] in WRITERS or (names[0] == "sed" and any(x.startswith("-i") for x in rest))):
        targets += rest[1:]
    if any(PROTECTED.search(t.replace("\\", "/")) for t in targets):
        return "writes to a review record, a hook or a setting the gate depends on"
    if "review.py" in names and "signoff" in rest:
        return "enters a human sign-off, which only the person who read the code may do"
    if "global.py" in names and "off" in rest:
        return "switches the review gate off for every repository"
    if "git" not in names:
        return None
    g = rest[names.index("git") + 1 :]
    sub = next((x for x in g if not x.startswith("-") and "=" not in x), "")
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
    if tool in ("Bash", "PowerShell"):
        command = data.get("command") or ""
        if not isinstance(command, str):
            raise ValueError("command is not a string")
        for seg in segments(command):
            why = check_segment(seg)
            if why:
                return why
        heredocs_removed = re.sub(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(\n|$)", "\n", command, flags=re.S)
        why = commit_flags(heredocs_removed)
        if why:
            return why
    elif tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = str(data.get("file_path") or data.get("notebook_path") or "").replace("\\", "/")
        if PROTECTED.search(path):
            return "edits a review record, a hook or a setting the gate depends on by hand"
    return None


def main():
    try:
        event = json.load(sys.stdin)
        why = reason(event.get("tool_name", ""), event.get("tool_input") or {})
    except Exception as e:  # noqa: BLE001 - any failure must block, since only exit 2 blocks
        print(f"review guard: could not check this call ({e}), so it is refused", file=sys.stderr)
        return 2
    if why:
        print(
            f"review guard: refused because this {why}. Run the review steps instead "
            '(review.py checks, a fresh-context review, review.py record); REVIEW_SKIP="<reason>" '
            "marks a work-in-progress commit. A person, not the assistant, decides any exception.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
