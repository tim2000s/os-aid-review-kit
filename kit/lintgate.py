#!/usr/bin/env python3
"""Lint gate that fails on the findings a change introduces, not the ones it inherits.

An open-source AID repository carries years of code written before any of these linters ran on
it, so linting a touched file whole would block every change on findings its author did not make.
The gate therefore lints two copies of each changed file, the version before the change and the
version after it, and reports a finding only where a rule's count went up. A file the change adds
has no earlier version, so every finding in it counts.

Both copies, and the linter configuration beside each, are written from git (the index for a
commit, a commit for a range) into temporary directories. What is linted is what will be committed,
and a change that loosens a linter's configuration does not loosen the check of the code before it.
A linter that is missing, crashes or prints nothing parseable fails the gate rather than passing it.

    lintgate.py --staged [--fix]       before a commit: HEAD against the index
    lintgate.py --base REF             in CI: merge-base(REF, HEAD) against HEAD
    lintgate.py ... --mode greenfield  every finding in a changed file counts
    lintgate.py ... --languages python,javascript   lint only these; other files are named and skipped
    lintgate.py ... --json OUT         also write the findings as JSON for the review record
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
DETEKT_CONFIGS = ("detekt.yml", "config/detekt/detekt.yml")


class LinterFailed(Exception):
    pass


def ruff(files, root):
    return run_sarif(["ruff", "check", "--no-cache", "--exit-zero", "--output-format", "sarif", *files], root)


def biome(files, root):
    cmd = ["biome", "lint", "--vcs-enabled=false", "--reporter=sarif", "--max-diagnostics=none"]
    return run_sarif(cmd + files, root, findings_rc=(1,))


def ktlint(files, root):
    return run_sarif(["ktlint", "--reporter=sarif", *files], root, findings_rc=(1,))


def detekt(files, root):
    cfg = next((c for c in DETEKT_CONFIGS if os.path.exists(os.path.join(root, c))), None)
    out = os.path.join(root, ".detekt.sarif")
    cmd = ["detekt", "--input", ",".join(files), "--report", f"sarif:{out}"]
    if cfg:
        cmd += ["--config", cfg, "--build-upon-default-config"]
    need(cmd[0])
    p = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if p.returncode not in (0, 2) or not os.path.exists(out):  # detekt exits 2 when it found issues
        raise LinterFailed(f"detekt failed ({p.returncode}): {(p.stderr or p.stdout).strip()[:400]}")
    with open(out) as f:
        return parse_sarif(f.read(), root)


def swiftlint(files, root):
    cmd = ["swiftlint", "lint", "--quiet", "--reporter", "sarif"]
    if os.path.exists(os.path.join(root, ".swiftlint.yml")):
        cmd += ["--config", ".swiftlint.yml"]
    return run_sarif(cmd + files, root, findings_rc=(2,))


# language -> (extensions, [(linter name, lint function, config needed in the tree or None)], formatter)
LANGUAGES = {
    "python": ((".py",), [("ruff", ruff, None)], ["ruff", "format"]),
    "javascript": ((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"), [("biome", biome, "biome.json")],
                   ["biome", "format", "--write"]),
    "kotlin": ((".kt", ".kts"), [("ktlint", ktlint, None), ("detekt", detekt, DETEKT_CONFIGS)], ["ktlint", "--format"]),
    "swift": ((".swift",), [("swiftlint", swiftlint, ".swiftlint.yml")], ["swiftformat"]),
}


def language_of(path):
    ext = os.path.splitext(path)[1]
    return next((lang for lang, (exts, _, _) in LANGUAGES.items() if ext in exts), None)


def need(tool):
    if shutil.which(tool) is None:
        raise LinterFailed(f"{tool} is not installed; see the kit README for the install line")


def run_sarif(cmd, root, findings_rc=()):
    """Run a linter that prints SARIF. Exit 0, or a code the tool uses for 'found issues', with
    parseable output counts; anything else (including silence with a failing code) is a failure."""
    need(cmd[0])
    p = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if p.returncode not in (0, *findings_rc) or (p.returncode != 0 and not p.stdout.strip()):
        raise LinterFailed(f"{cmd[0]} failed ({p.returncode}): {(p.stderr or p.stdout).strip()[:400]}")
    if not p.stdout.strip():
        return []
    try:
        return parse_sarif(p.stdout, root)
    except (ValueError, KeyError) as e:
        raise LinterFailed(f"{cmd[0]} printed output that is not SARIF: {e}") from None


def parse_sarif(text, root):
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object")
    doc = json.loads(text[start:])
    real_root = os.path.realpath(root)
    found = []
    for run in doc["runs"]:
        for r in run.get("results", []):
            loc = (r.get("locations") or [{}])[0].get("physicalLocation", {})
            path = loc.get("artifactLocation", {}).get("uri", "").removeprefix("file://")
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
    p = subprocess.run(["git", "-c", "core.quotepath=off", *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"lintgate: git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def changed(staged, old, new):
    """[(status, path)] for added, modified, renamed-to and type-changed files."""
    args = ["diff", "--name-status", "-z", "--no-renames", "--diff-filter=ACMT"]
    args += (["--cached", old] if staged else [old, new])
    parts = git(*args).split("\0")
    return [(parts[i][0], parts[i + 1]) for i in range(0, len(parts) - 1, 2) if parts[i]]


def show(spec, path):
    p = subprocess.run(["git", "show", f"{spec}:{path}"], capture_output=True)
    return p.stdout if p.returncode == 0 else None


def materialise(root, spec, paths, fallback=None):
    """Write the files, and the linter configuration, as git holds them at spec ('' = the index). A
    configuration file the change deletes is taken from `fallback`, so deleting it cannot switch the
    linter off for the change that deletes it."""
    for path in [*CONFIG_FILES, *paths]:
        content = show(spec, path)
        if content is None and fallback and path in CONFIG_FILES:
            content = show(fallback, path)
        if content is None:
            if path in paths:
                raise SystemExit(f"lintgate: {path} is not in {spec or 'the index'}")
            continue
        dest = os.path.join(root, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(content)


def lint_tree(root, paths):
    by_linter = collections.defaultdict(list)
    for p in paths:
        for name, fn, needs in LANGUAGES[language_of(p)][1]:
            needs = (needs,) if isinstance(needs, str) else needs
            if needs and not any(os.path.exists(os.path.join(root, n)) for n in needs):
                continue
            by_linter[(name, fn)].append(p)
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
    for status, path in files:
        if status != "A":
            continue
        if subprocess.run(["git", "diff", "--quiet", "--", path]).returncode != 0:
            print(f"lintgate: {path} has unstaged edits, not formatting it; run the formatter by hand")
            continue
        fmt = LANGUAGES[language_of(path)][2]
        if shutil.which(fmt[0]):
            subprocess.run([*fmt, path], capture_output=True)
            subprocess.run(["git", "add", "--", path], check=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--staged", action="store_true")
    g.add_argument("--base")
    ap.add_argument("--mode", choices=["adopt", "greenfield"], default="adopt")
    ap.add_argument("--fix", action="store_true", help="format added files (staged mode only)")
    ap.add_argument("--languages", help="comma-separated subset of: " + ", ".join(LANGUAGES))
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args(argv)

    os.chdir(git("rev-parse", "--show-toplevel").strip())
    has_head = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], capture_output=True).returncode == 0
    if a.staged:
        new_spec = ""
        old_spec = "HEAD" if has_head else git("hash-object", "-t", "tree", "/dev/null").strip()
    else:
        new_spec, old_spec = "HEAD", git("merge-base", a.base, "HEAD").strip()
    wanted = set(a.languages.split(",")) if a.languages else set(LANGUAGES)
    files, skipped = [], []
    for status, path in changed(a.staged, old_spec, new_spec):
        lang = language_of(path)
        if lang in wanted:
            files.append((status, path))
        elif lang:
            skipped.append(path)
    for p in skipped:
        print(f"lintgate: not linted here ({language_of(p)} is not in --languages): {p}")
    if a.fix and a.staged:
        fix_added(files)
    added = {p for s, p in files if s == "A" or a.mode == "greenfield" or not has_head}
    paths = [p for _, p in files]
    olds = [p for p in paths if p not in added]

    try:
        with tempfile.TemporaryDirectory() as after_root, tempfile.TemporaryDirectory() as before_root:
            materialise(after_root, new_spec, paths, fallback=old_spec)
            after = lint_tree(after_root, paths)
            materialise(before_root, old_spec, olds)
            before = lint_tree(before_root, olds)
    except LinterFailed as e:
        print(f"lintgate: {e}", file=sys.stderr)
        return 2
    found = new_findings(before, after, added)

    if not a.quiet:
        for f in found:
            print(f"{f['path']}:{f['line']}: [{f['tool']} {f['rule']}] {f['message']}")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump({"files": paths, "skipped": skipped, "mode": a.mode, "new_findings": found}, fh, indent=1)
    if found:
        print(f"lintgate: {len(found)} finding(s) introduced by this change", file=sys.stderr)
        return 1
    print(f"lintgate: {len(files)} file(s) checked, no new findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
