#!/usr/bin/env python3
"""Install the review kit into a repository: audit it, then add only the configuration that fits.

A file that already exists is not overwritten. Where one is present and differs from what the kit
would write, the kit's version is written beside it as `<name>.kit-proposed` and listed in the
report, so a team's own rules are merged by a person rather than replaced by a script. The
exceptions are the kit's own files (`.review/bin/*`, `.review/README.md`), the marked block in
`AGENTS.md` and `CLAUDE.md`, and the `codegraphcontext` entry in `.mcp.json`, which are added or
refreshed in place and reported as updated.

    install.py TARGET [--preset auto|default|androidaps|trio|nightscout] [--hooks] [--dry-run]

--hooks runs `lefthook install` afterwards, unless the repository already uses husky or pre-commit,
in which case the report says so and leaves the hooks alone.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

# git and the tools print UTF-8; Windows would otherwise decode their output as cp1252
UTF8 = {"text": True, "encoding": "utf-8", "errors": "replace"}

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(KIT, "templates")

MARKERS = {
    "kotlin": ["build.gradle.kts", "build.gradle", "pom.xml", "settings.gradle.kts", "settings.gradle"],
    "swift": ["Package.swift"],
    "python": ["pyproject.toml", "requirements.txt", "Pipfile", "uv.lock", "setup.py"],
    "javascript": ["package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json"],
}
EXTENSIONS = {"kotlin": (".kt", ".kts"), "swift": (".swift",), "python": (".py",),
              "javascript": (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")}
BINARIES = {"kotlin": ["ktlint", "detekt"], "swift": ["swiftlint"], "python": ["ruff", "mypy"],
            "javascript": ["biome"], "all": ["lefthook", "trufflehog", "semgrep", "trivy", "git"]}
GITIGNORE = [".env", ".env.*", "*.pem", "google-services.json", "GoogleService-Info.plist",
             ".review/.last-checks.json", ".review/.test-output.txt", ".review/.test-integration.txt",
             ".detekt.sarif"]


def audit(root):
    files = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, **UTF8,
                           check=True).stdout.split("\0")
    counts = {lang: sum(f.endswith(ext) for f in files) for lang, ext in EXTENSIONS.items()}
    markers = {lang: [m for m in ms if os.path.exists(os.path.join(root, m))] for lang, ms in MARKERS.items()}
    if any(f.endswith((".xcodeproj/project.pbxproj", ".xcworkspace/contents.xcworkspacedata")) for f in files):
        markers["swift"].append("*.xcodeproj")
    # a language is present when it has a build marker or a meaningful share of tracked source files
    langs = [lang for lang in EXTENSIONS if markers[lang] or counts[lang] >= 5]
    ci = [p for p in (".github/workflows", ".gitlab-ci.yml", ".circleci", "azure-pipelines.yml")
          if os.path.exists(os.path.join(root, p))]
    hooks = [p for p in (".husky", ".pre-commit-config.yaml", "lefthook.yml", ".lefthook.yml")
             if os.path.exists(os.path.join(root, p))]
    linters = [p for p in (".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml", "eslint.config.js",
                           "eslint.config.mjs", ".prettierrc", ".prettierrc.json", "prettier.config.js",
                           "detekt.yml", "config/detekt/detekt.yml", ".swiftlint.yml", ".swiftformat",
                           "ruff.toml", ".ruff.toml", "biome.json", ".editorconfig")
               if os.path.exists(os.path.join(root, p))]
    pyproject = os.path.join(root, "pyproject.toml")
    if os.path.exists(pyproject) and "[tool.ruff" in open(pyproject, encoding="utf-8").read():
        linters.append("pyproject.toml [tool.ruff]")
    return {"languages": langs, "counts": counts, "markers": markers, "ci": ci, "hooks": hooks, "linters": linters}


def detect_preset(root):
    if os.path.isdir(os.path.join(root, "plugins", "aps")):
        return "androidaps"
    if os.path.isdir(os.path.join(root, "Trio.xcworkspace")) or os.path.isdir(os.path.join(root, "trio-oref")):
        return "trio"
    if os.path.exists(os.path.join(root, "lib", "server", "entries.js")):
        return "nightscout"
    return "default"


def strip_languages(text, langs):
    """Drop `# kit:lang=X` ... `# kit:end` blocks for languages the repository does not use."""
    out, keep = [], True
    for line in text.splitlines(keepends=True):
        m = re.match(r"\s*# kit:lang=(\w+)", line)
        if m:
            keep = m.group(1) in langs
            continue
        if line.strip() == "# kit:end":
            keep = True
            continue
        if keep:
            out.append(line)
    return "".join(out)


def lefthook_yml(langs, mode):
    # every extension the gate knows, whatever was detected; lintgate itself decides what it lints
    globs = ",".join(e.lstrip(".") for exts in EXTENSIONS.values() for e in exts)
    return f"""# Generated by os-aid-review-kit. Commit runs the fast checks on what is staged, in order, stopping
# at the first failure; push checks every ref being pushed. Install with `lefthook install`.
pre-commit:
  piped: true
  commands:
    1_review-record:
      run: sh .review/bin/py .review/bin/review.py check
    2_lint-new-findings:
      glob: "*.{{{globs}}}"
      run: sh .review/bin/py .review/bin/lintgate.py --staged --mode {mode}
    3_secret-scan:
      # TruffleHog applies --exclude-paths only while walking directories, not to files named on its
      # command line, so the hook filters the records out itself; CI's history scan uses the file.
      # review.py secrets does not fail on a value the file already holds on a remote-tracking branch
      exclude:
        - ".review/records/*.md"
      run: sh .review/bin/py .review/bin/review.py secrets {{staged_files}}
    4_guardrails:
      glob: "*.{{{globs}}}"
      exclude:
        - ".review/records/*.md"
      run: semgrep scan --metrics=off --error --quiet --config .semgrep/ {{staged_files}}

commit-msg:
  commands:
    review-trailers:
      run: sh .review/bin/py .review/bin/review.py trailers {{1}}

# A script, not a command: lefthook skips a pre-push command when it finds no files in the push
# measured from HEAD, which is the case when a branch other than the checked-out one is pushed.
pre-push:
  scripts:
    "review-gate.sh":
      runner: sh
      use_stdin: true
"""


PRE_PUSH = """#!/bin/sh
# Generated by os-aid-review-kit. Receives git's pre-push lines on stdin.
set -e
sh .review/bin/py .review/bin/review.py check-push
# measured from the upstream when there is one; a first push has none, so everything counts
if BASE=$(sh .review/bin/py .review/bin/review.py base 2>/dev/null); then
  semgrep scan --metrics=off --config p/default --config .semgrep/ --error --quiet --baseline-commit "$BASE"
else
  semgrep scan --metrics=off --config p/default --config .semgrep/ --error --quiet
fi
trivy fs --config trivy.yaml .
"""


def sonar_properties(root, langs, audit_result):
    key = os.path.basename(os.path.abspath(root))
    lines = [f"sonar.projectKey={key}", f"sonar.projectName={key}", "sonar.sources=.",
             "sonar.exclusions=**/build/**,**/node_modules/**,**/Pods/**,**/.build/**,**/*.min.js,"
             "**/test*/**,**/*Test*/**,.review/**",
             "sonar.tests=.", "sonar.test.inclusions=**/test*/**,**/*Test*/**,**/*.test.js,**/test_*.py"]
    if "kotlin" in langs:
        lines.append("sonar.coverage.jacoco.xmlReportPaths=**/build/reports/jacoco/**/*.xml")
    if "python" in langs:
        lines.append("sonar.python.coverage.reportPaths=coverage.xml")
    if "javascript" in langs:
        lines.append("sonar.javascript.lcov.reportPaths=coverage/lcov.info")
    return "\n".join(lines) + "\n"


def accepted_cves(root):
    """Pre-existing findings are recorded, not fixed here and not allowed to block unrelated work."""
    p = subprocess.run([shutil.which("trivy") or "trivy", "fs", "--quiet", "--format", "json", "--exit-code", "0",
                        "--severity", "HIGH,CRITICAL", "--scanners", "vuln", "--ignorefile", "", "."],
                       cwd=root, capture_output=True, **UTF8)
    if p.returncode != 0 or not p.stdout.strip():
        raise SystemExit(f"install: trivy failed, so no CVE list was written: {p.stderr.strip()[:400]}")
    vulns = sorted({(v["VulnerabilityID"], v["PkgName"], v["Severity"])
                    for r in json.loads(p.stdout or "{}").get("Results", []) for v in r.get("Vulnerabilities") or []})
    today = datetime.date.today().isoformat()
    lines = [f"# Pre-existing when the review kit was installed on {today}: {len(vulns)} HIGH or CRITICAL findings.",
             "# Accepted so that only newly introduced ones fail; each is still to be fixed upstream. Remove a",
             "# line when the dependency is updated, and never add one without a reason and a date."]
    lines += [f"{vid}  # {pkg} {sev}, pre-existing {today}" for vid, pkg, sev in vulns]
    return "\n".join(lines) + "\n"


class Writer:
    def __init__(self, root, dry):
        self.root, self.dry = root, dry
        self.created, self.proposed, self.unchanged, self.updated = [], [], [], []

    def put(self, rel, content, owned=False):
        """Write a file; an existing one is left alone unless the kit owns it (owned=True)."""
        path = os.path.join(self.root, rel)
        if os.path.exists(path):
            if open(path, encoding="utf-8").read() == content:
                self.unchanged.append(rel)
                return
            if owned:
                self.updated.append(rel)
                if not self.dry:
                    with open(path, "w", newline="\n", encoding="utf-8") as f:
                        f.write(content)
                return
            rel_p = rel + ".kit-proposed"
            self.proposed.append(rel_p)
            path = os.path.join(self.root, rel_p)
        else:
            self.created.append(rel)
        if not self.dry:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", newline="\n", encoding="utf-8") as f:
                f.write(content)

    def copy(self, rel, src, owned=False):
        self.put(rel, open(src, encoding="utf-8").read(), owned)

    def append_lines(self, rel, lines):
        path = os.path.join(self.root, rel)
        have = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
        add = [x for x in lines if x not in have]
        if add:
            self.updated.append(f"{rel} (+{len(add)} lines)")
            if not self.dry:
                with open(path, "a", newline="\n", encoding="utf-8") as f:
                    f.write(("\n" if have and have[-1] != "" else "") + "# review kit\n" + "\n".join(add) + "\n")

    def block(self, rel, block):
        """Insert or refresh the kit's marked block in an instructions file."""
        path = os.path.join(self.root, rel)
        text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        start, end = "<!-- review-kit:start", "<!-- review-kit:end -->"
        if start in text and end not in text:
            raise SystemExit(f"install: {rel} has the kit's start marker but no end marker; fix it by hand")
        if start in text:
            new = text[:text.index(start)] + block.strip() + text[text.index(end) + len(end):]
        else:
            new = text.rstrip() + ("\n\n" if text else "") + block.strip() + "\n"
        if new != text:
            (self.updated if text else self.created).append(rel)
            if not self.dry:
                with open(path, "w", newline="\n", encoding="utf-8") as f:
                    f.write(new)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # a pipe on Windows otherwise takes the code page
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("target")
    ap.add_argument("--preset", default="auto", choices=["auto", "default", "androidaps", "trio", "nightscout"])
    ap.add_argument("--hooks", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--accept-existing-cves", action="store_true",
                    help="list the HIGH and CRITICAL CVEs present now in .trivyignore, so only new ones fail")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.target)
    if not os.path.isdir(os.path.join(root, ".git")):
        raise SystemExit(f"install: {root} is not the root of a git repository")

    au = audit(root)
    langs = au["languages"]
    preset = detect_preset(root) if a.preset == "auto" else a.preset
    tiers = open(os.path.join(T, "tiers", f"{preset}.toml"), encoding="utf-8").read()
    mode = re.search(r'^mode = "(\w+)"', tiers, re.M).group(1)
    w = Writer(root, a.dry_run)

    # Phase 2: linters, only for languages present and only where none is configured; no formatter, since
    # the kit rewrites nothing and its own formatter settings would read as the repository having one
    if "kotlin" in langs:
        if not any("detekt" in x for x in au["linters"]):
            w.copy("detekt.yml", os.path.join(T, "detekt.yml"))
    if "swift" in langs:
        w.copy(".swiftlint.yml", os.path.join(T, ".swiftlint.yml"))
    if "python" in langs:
        if not any("ruff" in x for x in au["linters"]):
            w.copy("ruff.toml", os.path.join(T, "ruff.toml"))
        w.copy("mypy.ini", os.path.join(T, "mypy.ini"))
    if "javascript" in langs:
        biome = json.load(open(os.path.join(T, "biome.json"), encoding="utf-8"))
        if any("eslint" in x or "prettier" in x for x in au["linters"]):
            # house style belongs to the repository's own ESLint config: Biome keeps correctness,
            # suspicious-pattern and security rules, and drops the groups and rules that contradict an
            # ES5/CommonJS codebase (function expressions, string concatenation, 'use strict', block var)
            rules = biome["linter"]["rules"]
            rules["style"] = "off"
            rules["complexity"] = "off"
            rules.setdefault("suspicious", {})["noRedundantUseStrict"] = "off"
            rules.setdefault("correctness", {})["noInnerDeclarations"] = "off"
        w.put("biome.json", json.dumps(biome, indent=2) + "\n")

    # Phase 3: hooks
    w.put("lefthook.yml", lefthook_yml(langs, mode))
    w.put(".lefthook/pre-push/review-gate.sh", PRE_PUSH)
    if not a.dry_run:
        # a hook script has to be executable; group and other read is git's norm for tracked scripts
        os.chmod(os.path.join(root, ".lefthook/pre-push/review-gate.sh"), 0o755)  # noqa: S103 nosemgrep

    # Phase 4: SAST, SCA, secrets
    w.copy(".semgrep/custom-ai-guardrails.yml", os.path.join(T, ".semgrep", "custom-ai-guardrails.yml"))
    w.copy("trivy.yaml", os.path.join(T, "trivy.yaml"))
    w.copy("trufflehog.yml", os.path.join(T, "trufflehog.yml"))
    w.append_lines(".gitignore", GITIGNORE)
    if a.accept_existing_cves:
        w.put(".trivyignore", accepted_cves(root))
    elif not os.path.exists(os.path.join(root, ".trivyignore")):
        w.put(".trivyignore", "# Accepted pre-existing findings: one CVE id per line, with the reason and a date.\n")

    # Phase 5: CI
    if ".gitlab-ci.yml" in au["ci"] and ".github/workflows" not in au["ci"]:
        print("install: GitLab CI found; the kit ships a GitHub workflow only, so CI is left unchanged")
    else:
        w.put(".github/workflows/quality-gate.yml",
              strip_languages(open(os.path.join(T, ".github", "workflows", "quality-gate.yml"),
                                   encoding="utf-8").read(), langs)
              .replace("__KIT_LANGS_LINUX__", "python,javascript,kotlin" + ("" if "swift" in langs else ",swift")))
    w.copy(".pr_agent.toml", os.path.join(T, ".pr_agent.toml"))
    w.put("sonar-project.properties", sonar_properties(root, langs, au))

    # Phase 6: MCP and agent instructions
    mcp_path = os.path.join(root, ".mcp.json")
    mcp = json.load(open(mcp_path, encoding="utf-8")) if os.path.exists(mcp_path) else {"mcpServers": {}}
    if "codegraphcontext" not in mcp.setdefault("mcpServers", {}):
        template = json.load(open(os.path.join(T, "mcp.json"), encoding="utf-8"))
        mcp["mcpServers"]["codegraphcontext"] = template["mcpServers"]["codegraphcontext"]
        (w.updated if os.path.exists(mcp_path) else w.created).append(".mcp.json")
        if not a.dry_run:
            with open(mcp_path, "w", encoding="utf-8") as f:
                json.dump(mcp, f, indent=2)
                f.write("\n")
    block = open(os.path.join(T, "AGENTS-block.md"), encoding="utf-8").read()
    w.block("AGENTS.md", block)
    claude = os.path.join(root, "CLAUDE.md")
    if not os.path.exists(claude):
        w.put("CLAUDE.md", "@AGENTS.md\n")
    elif not os.path.islink(claude) and "@AGENTS.md" not in open(claude, encoding="utf-8").read():
        w.block("CLAUDE.md", block)

    # The review records and their tools
    w.put(".review/tiers.toml", tiers)
    w.copy(".review/README.md", os.path.join(T, "review-README.md"), owned=True)
    # records hold generated blob hashes, which TruffleHog's SonarCloud detector reads as tokens
    w.put(".review/trufflehog-exclude.txt", "^\\.review/records/[^/]+\\.md$\n", owned=True)
    w.copy(".review/bin/review.py", os.path.join(KIT, "kit", "review.py"), owned=True)
    w.copy(".review/bin/lintgate.py", os.path.join(KIT, "kit", "lintgate.py"), owned=True)
    w.copy(".review/bin/py", os.path.join(KIT, "kit", "py"), owned=True)
    # shell scripts must keep LF endings on a Windows checkout with core.autocrlf
    w.append_lines(".gitattributes", [".review/bin/py text eol=lf", ".lefthook/** text eol=lf"])
    if not a.dry_run:
        os.chmod(os.path.join(root, ".review/bin/py"), 0o755)  # noqa: S103 nosemgrep
    if not os.path.exists(os.path.join(root, ".review", "records")):
        w.put(".review/records/.gitkeep", "")
    if not os.path.exists(os.path.join(root, "REVIEW.md")):
        w.put("REVIEW.md", "# Review checklist\n\nThe ways this repository has gone wrong, each as a check a reviewer "
              "can test a diff against. Label findings important, nit or pre-existing.\n\n"
              "- The change is no larger than the task needs. An abstraction, option or wrapper nothing asked "
              "for, a reimplementation of an existing helper, dead or speculative code, or handling of cases "
              "that cannot occur is important.\n")

    alt = {"detekt": "detekt-cli"}
    missing = sorted({b for k in langs + ["all"] for b in BINARIES[k]
                      if shutil.which(b) is None and not (b in alt and shutil.which(alt[b]))})
    hooks_note = ""
    if a.hooks and not a.dry_run:
        other = [h for h in au["hooks"] if h in (".husky", ".pre-commit-config.yaml")]
        if w.proposed and "lefthook.yml.kit-proposed" in w.proposed:
            hooks_note = ("not installed: lefthook.yml already existed; merge lefthook.yml.kit-proposed, "
                          "then run lefthook install")
        elif other:
            hooks_note = f"not installed: the repository already uses {', '.join(other)}; merge lefthook.yml by hand"
        elif os.path.normcase(os.path.abspath(subprocess.run(
                ["git", "config", "--global", "--get", "core.hooksPath"], capture_output=True, **UTF8
                ).stdout.strip() or "-")) in (os.path.normcase(os.path.join(KIT, "global-hooks")),
                                              os.path.normcase(os.path.expanduser("~/.review-kit/global-hooks"))):
            # lefthook refuses a global hooks path; the kit's dispatcher runs lefthook itself
            hooks_note = "covered by the kit's global hooks (kit/global.py on)"
        elif lefthook := shutil.which("lefthook"):
            p = subprocess.run([lefthook, "install"], cwd=root, capture_output=True, **UTF8)
            hooks_note = "installed" if p.returncode == 0 else f"lefthook install failed: {p.stderr.strip()}"
        else:
            hooks_note = "not installed: lefthook is missing"

    report = {"target": root, "preset": preset, "mode": mode, "audit": au, "created": w.created,
              "proposed_beside_existing": w.proposed, "appended_or_updated": w.updated,
              "unchanged": w.unchanged, "missing_binaries": missing, "hooks": hooks_note or "not requested",
              "ci_secrets": ["ANTHROPIC_API_KEY (pr-agent, optional)",
                             "SONAR_TOKEN and SONAR_HOST_URL, plus variable SONAR_ENABLED=true (optional)"]}
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
