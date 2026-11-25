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

    lintgate.py --staged               before a commit: HEAD against the index
    lintgate.py --base REF             in CI: merge-base(REF, HEAD) against HEAD
    lintgate.py ... --mode greenfield  every finding in a changed file counts
    lintgate.py ... --languages python,javascript   lint only these; other files are named and skipped
    lintgate.py ... --json OUT         also write the findings as JSON for the review record
"""
import argparse
import collections
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import unquote

# git and the tools print UTF-8; Windows would otherwise decode their output as cp1252
UTF8 = {"text": True, "encoding": "utf-8", "errors": "replace"}

CONFIG_FILES = ["pyproject.toml", "ruff.toml", ".ruff.toml", "biome.json", "biome.jsonc",
                "detekt.yml", "config/detekt/detekt.yml", ".editorconfig", ".swiftlint.yml", ".gitignore"]
DETEKT_CONFIGS = ("detekt.yml", "config/detekt/detekt.yml")
# linters that are skipped without a config; ambient mode supplies the kit's, with defect rules only
FALLBACK_CONFIGS = {"biome.json": ("biome.json", "biome.jsonc"), "detekt.yml": DETEKT_CONFIGS,
                    ".swiftlint.yml": (".swiftlint.yml",)}  # each with every file the linter would read instead


class LinterFailed(Exception):
    pass


def ruff(files, root):
    return run_sarif(["ruff", "check", "--no-cache", "--exit-zero", "--output-format", "sarif", *files], root)


def biome(files, root):
    cmd = ["biome", "lint", "--vcs-enabled=false", "--reporter=sarif", "--max-diagnostics=none"]
    return run_sarif(cmd + files, root, findings_rc=(1,))


def ktlint_configured(*specs):
    """ktlint's default style is nobody's in particular, so it runs only where the repository set it up:
    ktlint in an .editorconfig, a Gradle build or a version catalogue. Asked of git once for both sides
    of the change, so that adding or removing the setup cannot make one side's findings look new."""
    for spec in specs:
        where = [spec] if spec else []  # '' is the index, which git grep reads with --cached
        p = subprocess.run(["git", "grep", "-q", "-i", *(["--cached"] if not spec else []), "ktlint", *where, "--",
                            "*.editorconfig", "*.gradle", "*.gradle.kts", "*.versions.toml"],
                           capture_output=True, **UTF8)
        if p.returncode not in (0, 1):
            raise LinterFailed(f"could not tell whether ktlint is set up: {p.stderr.strip()[:400]}")
        if p.returncode == 0:
            return True
    return False


def ktlint(files, root):
    return run_sarif(["ktlint", "--reporter=sarif", *files], root, findings_rc=(1,))


def detekt(files, root):
    cfg = next((c for c in DETEKT_CONFIGS if os.path.exists(os.path.join(root, c))), None)
    out = os.path.join(root, ".detekt.sarif")
    cmd = ["detekt", "--input", ",".join(files), "--report", f"sarif:{out}"]
    if cfg:
        cmd += ["--config", cfg, "--build-upon-default-config"]
    cmd[0] = need(cmd[0])
    p = subprocess.run(cmd, cwd=root, capture_output=True, **UTF8)
    if p.returncode not in (0, 2) or not os.path.exists(out):  # detekt exits 2 when it found issues
        raise LinterFailed(f"detekt failed ({p.returncode}): {(p.stderr or p.stdout).strip()[:400]}")
    with open(out, encoding="utf-8") as f:
        return parse_sarif(f.read(), root)


def swiftlint(files, root):
    cmd = ["swiftlint", "lint", "--quiet", "--reporter", "sarif"]
    if os.path.exists(os.path.join(root, ".swiftlint.yml")):
        cmd += ["--config", ".swiftlint.yml"]
    return run_sarif(cmd + files, root, findings_rc=(2,))


# language -> (extensions, [(linter name, lint function, config needed in the tree or None)])
LANGUAGES = {
    "python": ((".py",), [("ruff", ruff, None)]),
    "javascript": ((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"), [("biome", biome, ("biome.json", "biome.jsonc"))]),
    "kotlin": ((".kt", ".kts"), [("ktlint", ktlint, None), ("detekt", detekt, DETEKT_CONFIGS)]),
    "swift": ((".swift",), [("swiftlint", swiftlint, ".swiftlint.yml")]),
}


def language_of(path):
    ext = os.path.splitext(path)[1]
    return next((lang for lang, (exts, _) in LANGUAGES.items() if ext in exts), None)


ALTERNATIVE_NAMES = {"detekt": ("detekt-cli",)}  # the release zip installs detekt as detekt-cli


def need(tool):
    """The tool's full path. Resolving it here, rather than leaving it to subprocess, is what finds a
    Windows .cmd or .bat shim (npm's biome, ktlint.bat), which CreateProcess does not search for."""
    for name in (tool, *ALTERNATIVE_NAMES.get(tool, ())):
        path = shutil.which(name)
        if path:
            return path
    raise LinterFailed(f"{tool} is not installed; see the kit README for the install line")


def run_sarif(cmd, root, findings_rc=()):
    """Run a linter that prints SARIF. Exit 0, or a code the tool uses for 'found issues', with
    parseable output counts; anything else, silence included, is a failure."""
    name, cmd = cmd[0], [need(cmd[0]), *cmd[1:]]  # messages name the tool, not its resolved path
    p = subprocess.run(cmd, cwd=root, capture_output=True, **UTF8)
    if p.returncode not in (0, *findings_rc) or (p.returncode != 0 and not p.stdout.strip()):
        raise LinterFailed(f"{name} failed ({p.returncode}): {(p.stderr or p.stdout).strip()[:400]}")
    if not p.stdout.strip():
        # every supported linter prints a SARIF document even when it finds nothing
        raise LinterFailed(f"{name} printed nothing, so what it found is unknown")
    try:
        return parse_sarif(p.stdout, root)
    except (ValueError, KeyError) as e:
        raise LinterFailed(f"{name} printed output that is not SARIF: {e}") from None


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
            path = unquote(loc.get("artifactLocation", {}).get("uri", "")).removeprefix("file://").replace("\\", "/")
            # tools disagree on the form (absolute, file://, or relative to some other base such as
            # ktlint's), so cut at the temporary root wherever it appears; Windows paths differ in
            # separators and the case of the drive letter
            fold = str.lower if os.name == "nt" else str
            for base in (real_root, root):
                prefix = base.replace("\\", "/").lstrip("/") + "/"
                at = fold(path).find(fold(prefix))
                if at >= 0:
                    path = path[at + len(prefix):]
                    break
            found.append({"rule": r.get("ruleId", "?"), "message": r.get("message", {}).get("text", ""),
                          "path": path, "line": loc.get("region", {}).get("startLine", 0)})
    return found


def git(*args, check=True):
    p = subprocess.run(["git", "-c", "core.quotepath=off", *args], capture_output=True, **UTF8)
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
    configuration file missing at spec is taken from `fallback` (the other side of the change), so
    deleting a configuration cannot switch its linter off for that change, and adding one lints the
    code before the change the same way, rather than counting every inherited finding as new."""
    for path in [*CONFIG_FILES, *paths]:
        content = show(spec, path)
        if content is None and fallback is not None and path in CONFIG_FILES:
            content = show(fallback, path)
        if content is None and path in FALLBACK_CONFIGS and os.environ.get("REVIEW_CONFIG_FALLBACK") and not any(
                show(s, own) is not None for s in (spec, fallback) if s is not None for own in FALLBACK_CONFIGS[path]):
            # ambient mode: a repository with no config for this linter gets the kit's defect rules only
            src = os.path.join(os.environ["REVIEW_CONFIG_FALLBACK"], path)
            content = open(src, "rb").read() if os.path.exists(src) else None
        if content is None:
            if path in paths:
                raise SystemExit(f"lintgate: {path} is not in {spec or 'the index'}")
            continue
        dest = os.path.join(root, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(content)


def lint_tree(root, paths, skip=()):
    by_linter = collections.defaultdict(list)
    for p in paths:
        for name, fn, needs in LANGUAGES[language_of(p)][1]:
            if name in skip:
                continue
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


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # a pipe on Windows otherwise takes the code page
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--staged", action="store_true")
    g.add_argument("--base")
    ap.add_argument("--mode", choices=["adopt", "greenfield"], default="adopt")
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
        new_spec = "HEAD"
        old_spec = (git("hash-object", "-t", "tree", "/dev/null").strip() if a.base == ":root"
                    else git("merge-base", a.base, "HEAD").strip())
    wanted = set(a.languages.split(",")) if a.languages else set(LANGUAGES)
    exempt = []
    tiers = os.environ.get("REVIEW_TIERS") or ".review/tiers.toml"
    if os.path.exists(tiers):
        with open(tiers, "rb") as fh:
            exempt = tomllib.load(fh).get("exempt", {}).get("paths", [])
    files, skipped = [], []
    for status, path in changed(a.staged, old_spec, new_spec):
        if path.startswith(".review/") or any(fnmatch.fnmatch(path, p) for p in exempt):
            continue  # the kit's own scripts (linted in the kit's repository) and files the tiers exempt
        lang = language_of(path)
        if lang in wanted:
            files.append((status, path))
        elif lang:
            skipped.append(path)
    for p in skipped:
        print(f"lintgate: not linted here ({language_of(p)} is not in --languages): {p}")
    try:
        skip = () if not any(language_of(p) == "kotlin" for _, p in files) or ktlint_configured(
            old_spec, new_spec) else ("ktlint",)
    except LinterFailed as e:
        print(f"lintgate: {e}", file=sys.stderr)
        return 2
    added = {p for s, p in files if s == "A" or a.mode == "greenfield" or not has_head}
    paths = [p for _, p in files]
    olds = [p for p in paths if p not in added]

    try:
        with tempfile.TemporaryDirectory() as after_root, tempfile.TemporaryDirectory() as before_root:
            materialise(after_root, new_spec, paths, fallback=old_spec)
            materialise(before_root, old_spec, olds, fallback=new_spec)
            after = lint_tree(after_root, paths, skip)
            before = lint_tree(before_root, olds, skip)
    except LinterFailed as e:
        print(f"lintgate: {e}", file=sys.stderr)
        return 2
    found = new_findings(before, after, added)

    if not a.quiet:
        # where an existing rule's count rose, every instance is a candidate, so say how many are new
        before_n = collections.Counter((f["path"], f["tool"], f["rule"]) for f in before)
        after_n = collections.Counter((f["path"], f["tool"], f["rule"]) for f in after)
        for f in found:
            k = (f["path"], f["tool"], f["rule"])
            extra = "" if f["path"] in added else f" ({before_n[k]} before, {after_n[k]} after)"
            print(f"{f['path']}:{f['line']}: [{f['tool']} {f['rule']}] {f['message']}{extra}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"files": paths, "skipped": skipped, "mode": a.mode, "new_findings": found}, fh, indent=1)
    if found:
        print(f"lintgate: {len(found)} finding(s) introduced by this change", file=sys.stderr)
        return 1
    print(f"lintgate: {len(files)} file(s) checked, no new findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
