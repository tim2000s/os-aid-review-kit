#!/usr/bin/env python3
"""Lint gate that fails on the findings a change introduces, not the ones it inherits.

An open-source AID repository carries years of code written before any of these linters ran on
it, so linting a touched file whole would block every change on findings its author did not make.
The gate therefore lints two copies of each changed file, the version before the change and the
version after it, and reports a finding only where a rule's count went up. A file the change adds
has no earlier version, so every finding in it counts.

Both copies are written from git (the index for a commit, a commit for a range) into temporary
directories with the repository's linter configuration alongside, so what is linted is what will
be committed rather than whatever happens to be in the working tree.

    lintgate.py --staged [--fix]     before a commit: HEAD against the index
    lintgate.py --base REF           in CI: merge-base(REF, HEAD) against HEAD
    lintgate.py ... --mode greenfield  every finding in a changed file counts
    lintgate.py ... --json OUT       also write the findings as JSON for the review record
"""
import argparse
import collections
import json
import os
import shutil
import subprocess
import sys
import tempfile

CONFIG_FILES = ["pyproject.toml", "ruff.toml", ".ruff.toml", "biome.json", "biome.jsonc",
                "detekt.yml", "config/detekt/detekt.yml", ".editorconfig", ".swiftlint.yml", ".gitignore"]


def ruff(files, root):
    return run_sarif(["ruff", "check", "--no-cache", "--exit-zero", "--output-format", "sarif", *files], root)


def biome(files, root):
    cmd = ["biome", "lint", "--vcs-enabled=false", "--reporter=sarif", "--max-diagnostics=none"]
    return run_sarif(cmd + files, root)


def ktlint(files, root):
    return run_sarif(["ktlint", "--reporter=sarif", *files], root)


def detekt(files, root):
    cfg = next((c for c in ("detekt.yml", "config/detekt/detekt.yml") if os.path.exists(os.path.join(root, c))), None)
    out = os.path.join(root, ".detekt.sarif")
    cmd = ["detekt", "--input", ",".join(files), "--report", f"sarif:{out}"]
    if cfg:
        cmd += ["--config", cfg, "--build-upon-default-config"]
    subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    return parse_sarif(open(out).read(), root) if os.path.exists(out) else []


def swiftlint(files, root):
    cmd = ["swiftlint", "lint", "--quiet", "--reporter", "sarif"]
    if os.path.exists(os.path.join(root, ".swiftlint.yml")):
        cmd += ["--config", ".swiftlint.yml"]
    return run_sarif(cmd + files, root)


# extension -> (linter name, lint function, formatter command or None, config that switches it on)
LANGUAGES = {
    ".py": ("ruff", ruff, ["ruff", "format"], None),
    ".js": ("biome", biome, ["biome", "format", "--write"], "biome.json"),
    ".jsx": ("biome", biome, ["biome", "format", "--write"], "biome.json"),
    ".mjs": ("biome", biome, ["biome", "format", "--write"], "biome.json"),
    ".cjs": ("biome", biome, ["biome", "format", "--write"], "biome.json"),
    ".ts": ("biome", biome, ["biome", "format", "--write"], "biome.json"),
    ".tsx": ("biome", biome, ["biome", "format", "--write"], "biome.json"),
    ".kt": ("ktlint", ktlint, ["ktlint", "--format"], None),
    ".kts": ("ktlint", ktlint, ["ktlint", "--format"], None),
    ".swift": ("swiftlint", swiftlint, ["swiftformat"], ".swiftlint.yml"),
}
EXTRA = {".kt": [("detekt", detekt)]}


def run_sarif(cmd, root):
    if shutil.which(cmd[0]) is None:
        raise SystemExit(f"lintgate: {cmd[0]} is not installed; see the kit README for the install line")
    p = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if not p.stdout.strip():
        if p.returncode not in (0, 1):
            raise SystemExit(f"lintgate: {cmd[0]} failed ({p.returncode}): {p.stderr.strip()[:400]}")
        return []
    return parse_sarif(p.stdout, root)


def parse_sarif(text, root):
    start = text.find("{")
    doc = json.loads(text[start:]) if start >= 0 else {"runs": []}
    real_root = os.path.realpath(root)
    found = []
    for run in doc.get("runs", []):
        for r in run.get("results", []):
            loc = (r.get("locations") or [{}])[0].get("physicalLocation", {})
            uri = loc.get("artifactLocation", {}).get("uri", "")
            path = uri.removeprefix("file://")
            # tools disagree on the form (absolute, file://, or relative to some other base such as
            # ktlint's), so cut at the temporary root wherever it appears
            for prefix in (real_root.lstrip("/") + "/", root.lstrip("/") + "/"):
                if prefix in path:
                    path = path.split(prefix, 1)[1]
                    break
            found.append({"rule": r.get("ruleId", "?"), "message": r.get("message", {}).get("text", ""),
                          "path": path, "line": loc.get("region", {}).get("startLine", 0)})
    return found


def git(*args, check=True):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check).stdout


def changed(staged, base):
    if staged:
        out = git("diff", "--cached", "--name-status", "--diff-filter=ACMR")
    else:
        out = git("diff", "--name-status", "--diff-filter=ACMR", f"{base}...HEAD")
    files = []
    for line in out.splitlines():
        parts = line.split("\t")
        files.append((parts[0][0], parts[-1], parts[1] if parts[0][0] == "R" else parts[-1]))
    return files  # (status, new path, old path)


def materialise(root, spec_by_path, top):
    """Write each file's content from git spec ('' = index, 'REF' = commit) under root."""
    for c in CONFIG_FILES:
        src = os.path.join(top, c)
        if os.path.exists(src):
            os.makedirs(os.path.dirname(os.path.join(root, c)), exist_ok=True)
            shutil.copy(src, os.path.join(root, c))
    for path, spec in spec_by_path.items():
        dest = os.path.join(root, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        blob = subprocess.run(["git", "show", f"{spec}:{path}"], capture_output=True, check=True).stdout
        with open(dest, "wb") as f:
            f.write(blob)


def lint_tree(root, paths, top):
    by_linter = collections.defaultdict(list)
    for p in paths:
        ext = os.path.splitext(p)[1]
        if ext not in LANGUAGES:
            continue
        name, fn, _, needs = LANGUAGES[ext]
        if needs and not os.path.exists(os.path.join(top, needs)):
            continue
        by_linter[(name, fn)].append(p)
        for extra in EXTRA.get(ext, []):
            if extra[0] != "detekt" or os.path.exists(os.path.join(top, "detekt.yml")):
                by_linter[extra].append(p)
    out = []
    for (name, fn), files in by_linter.items():
        for f in fn(files, root):
            f["tool"] = name
            out.append(f)
    return out


def new_findings(before, after, added):
    """Findings in `after` whose (tool, rule, message) count rose against `before`, per file."""
    def key(f):
        return (f["path"], f["tool"], f["rule"], f["message"])

    old = collections.Counter(key(f) for f in before)
    new = collections.Counter(key(f) for f in after)
    return [f for f in after if f["path"] in added or new[key(f)] > old[key(f)]]


def fix_added(files):
    """Format files the change adds, unless they carry unstaged edits that `git add` would sweep in."""
    for status, path, _ in files:
        ext = os.path.splitext(path)[1]
        if status != "A" or ext not in LANGUAGES or LANGUAGES[ext][2] is None:
            continue
        if subprocess.run(["git", "diff", "--quiet", "--", path]).returncode != 0:
            print(f"lintgate: {path} has unstaged edits, not formatting it; run the formatter by hand")
            continue
        fmt = LANGUAGES[ext][2]
        if shutil.which(fmt[0]):
            subprocess.run([*fmt, path], capture_output=True)
            subprocess.run(["git", "add", path], check=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--staged", action="store_true")
    g.add_argument("--base")
    ap.add_argument("--mode", choices=["adopt", "greenfield"], default="adopt")
    ap.add_argument("--fix", action="store_true", help="format added files (staged mode only)")
    ap.add_argument("--json")
    a = ap.parse_args(argv)

    top = git("rev-parse", "--show-toplevel").strip()
    os.chdir(top)
    files = [f for f in changed(a.staged, a.base) if os.path.splitext(f[1])[1] in LANGUAGES]
    if a.fix and a.staged:
        fix_added(files)
    if a.staged:
        new_spec, old_spec = "", "HEAD"
        has_head = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], capture_output=True).returncode == 0
    else:
        new_spec, old_spec, has_head = "HEAD", git("merge-base", a.base, "HEAD").strip(), True
    added = {p for s, p, _ in files if s == "A" or a.mode == "greenfield" or not has_head}

    with tempfile.TemporaryDirectory() as after_root, tempfile.TemporaryDirectory() as before_root:
        materialise(after_root, {p: new_spec for _, p, _ in files}, top)
        after = lint_tree(after_root, [p for _, p, _ in files], top)
        olds = {old: new for s, new, old in files if new not in added}
        materialise(before_root, {old: old_spec for old in olds}, top)
        before = lint_tree(before_root, list(olds), top)
        for f in before:
            f["path"] = olds.get(f["path"], f["path"])
    found = new_findings(before, after, added)

    for f in found:
        print(f"{f['path']}:{f['line']}: [{f['tool']} {f['rule']}] {f['message']}")
    summary = {"files": [p for _, p, _ in files], "mode": a.mode, "new_findings": found}
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(summary, fh, indent=1)
    if found:
        print(f"lintgate: {len(found)} finding(s) introduced by this change", file=sys.stderr)
        return 1
    print(f"lintgate: {len(files)} file(s) checked, no new findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
