#!/usr/bin/env python3
"""Review records: which files were reviewed, by what, against which checks, and by whom since.

A record is one markdown file under .review/records/, written when a change is reviewed and
committed with it. It names every file the review covered together with the git blob hash of the
exact content reviewed (or "deleted" for a removal), so anyone can tell whether the code in front of
them is the code that was reviewed: a file whose current hash appears in no valid record has not
been reviewed in that form. The record carries the tier, who or what wrote the change, the checks
run and their results, the findings of a fresh-context review and what was done about each, the
author's own account of the change, and the human review, which starts as pending and is filled in
by `signoff`.

A record is an attestation and not a proof: anyone able to run this script can write one. The
coverage check therefore accepts a record only when it is internally consistent with the rules
(its tiers are no lower than the files' tiers now, a high-tier record shows every check passing and
a way back, a person has not asked for changes), and CI reruns the checks independently.

The machine-readable part is a JSON block at the top of the file; the prose below it is rendered
from that block, so the two cannot disagree.

    review.py tier [--base REF]                tier of each changed file (staged when no --base)
    review.py checks [--base REF]              run the gate's checks on the staged change (or commits since REF)
    review.py record [--base REF] --author A --reviewer R --explanation FILE [--findings FILE] [--way-back TEXT]
    review.py check [--base REF]               fail if a changed file's content is in no valid record
    review.py check-push                       pre-push hook: check every ref being pushed (refs on stdin)
    review.py status                           what awaits human review, and what changed since
    review.py signoff ID --by NAME [--verdict approved|changes-requested] [--notes TEXT]
    review.py pr-body --base REF               markdown summary for a pull request description
    review.py trailers MSGFILE                 commit-msg hook: append the record's trailers
    review.py base                             merge-base with the upstream (or default) branch
    review.py status --all                     pending records across every repository in the ledger
    REF may be :root, meaning everything the tip holds (a repository's first commits).

A repository without the kit installed is reviewed in ambient mode, through the global hooks that
`global.py` points git at. Its records live outside the tree, in a ledger under ~/.review-ledger
with one directory per checkout, so that a fork of somebody else's project gains no `.review/`
directory; its tiers come from the preset that matches its layout, copied into the ledger the first
time and editable there, and the commit trailers name the ledger directory.
"""
import argparse
import datetime
import fnmatch
import json
import os
import re
import subprocess
import sys
import tomllib

DIR = ".review"
RECORDS = os.path.join(DIR, "records")
LAST_CHECKS = os.path.join(DIR, ".last-checks.json")
KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.environ.get("REVIEW_LEDGER") or os.path.expanduser("~/.review-ledger")
AMBIENT = False
TIERS = ["low", "medium", "high"]
DELETED = "deleted"
ZERO = "0" * 40
# The gate's own configuration is reviewed at the highest tier, so a change cannot loosen the gate
# and pass through it in the same commit.
GATE = [".review/tiers.toml", ".review/bin/*", ".review/trufflehog-exclude.txt", "lefthook.yml", ".lefthook.yml",
        ".lefthook/*", ".semgrep/*",
        "trufflehog.yml", "trivy.yaml", ".trivyignore", ".github/workflows/*", "biome.json", "biome.jsonc",
        "ruff.toml", ".ruff.toml", "pyproject.toml", ".editorconfig", "detekt.yml", "config/detekt/detekt.yml",
        ".swiftlint.yml", ".pr_agent.toml"]
EXEMPT = [".review/records/*.md", ".review/records/.gitkeep", ".review/README.md", ".review/.last-checks.json",
          ".review/.test-output.txt", ".review/.test-integration.txt"]


def git(*args, check=True):
    p = subprocess.run(["git", "-c", "core.quotepath=off", *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"review: git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.strip()


def ambient_dir(toplevel):
    """The ledger directory for a checkout: its path under the home directory, or its absolute path."""
    home = os.path.expanduser("~")
    rel = os.path.relpath(toplevel, home) if toplevel.startswith(home + os.sep) else toplevel.lstrip(os.sep)
    return os.path.join(LEDGER, "repos", rel)


def enter_ambient(toplevel):
    global DIR, RECORDS, LAST_CHECKS, AMBIENT
    AMBIENT = True
    DIR = ambient_dir(toplevel)
    RECORDS = os.path.join(DIR, "records")
    LAST_CHECKS = os.path.join(DIR, ".last-checks.json")
    tiers = os.path.join(DIR, "tiers.toml")
    if not os.path.exists(tiers):
        sys.path.insert(0, os.path.join(KIT, "kit"))
        from install import detect_preset
        os.makedirs(DIR, exist_ok=True)
        with open(os.path.join(KIT, "templates", "tiers", f"{detect_preset(toplevel)}.toml")) as src, \
                open(tiers, "w") as dst:
            dst.write(f"# Ambient-mode tiers for {toplevel}, copied from the kit's preset; edit freely.\n" + src.read())
    # lintgate reads the exempt list from here, and lints with the kit's configs where the repo has none
    os.environ["REVIEW_TIERS"] = tiers
    os.environ["REVIEW_CONFIG_FALLBACK"] = os.path.join(KIT, "templates")


def ledger_commit(path, message):
    """The ledger is its own git repository, so a record's history (a sign-off included) is kept."""
    if not os.path.isdir(os.path.join(LEDGER, ".git")):
        git("init", "-q", LEDGER)
        git("-C", LEDGER, "config", "review.ambient", "false")
        with open(os.path.join(LEDGER, ".gitignore"), "w") as f:
            f.write(".last-checks.json\n")
    git("-C", LEDGER, "add", "-A", "--", ".gitignore", os.path.relpath(path, LEDGER))
    git("-C", LEDGER, "commit", "-q", "--no-verify", "-m", message)


def config():
    path = os.environ.get("REVIEW_TIERS") or os.path.join(DIR, "tiers.toml")
    if not os.path.exists(path):
        raise SystemExit(f"review: {path} is missing; run the kit's install.py on this repository")
    with open(path, "rb") as f:
        return tomllib.load(f)


def match(path, patterns):
    return next((p for p in patterns if fnmatch.fnmatch(path, p)), None)


def tier_of(path, cfg):
    """Highest tier whose patterns match; unmatched code takes the configured default (medium)."""
    if match(path, GATE):
        return "high", "part of the review gate"
    if match(path, EXEMPT) or match(path, cfg.get("exempt", {}).get("paths", [])):
        return None, "exempt"
    for t in reversed(TIERS):
        p = match(path, cfg.get(t, {}).get("paths", []))
        if p:
            return t, f"matches {p}"
    return cfg.get("default_tier", "medium"), "no pattern matched; default tier"


def has_commit(ref="HEAD"):
    return subprocess.run(["git", "rev-parse", "--verify", "-q", ref], capture_output=True).returncode == 0


def span_of(base=None, tip=None):
    """(old, new): new is '' for the index, else a commit; old is what the change is measured from."""
    if base is None:
        return ("HEAD" if has_commit() else None), ""
    tip = tip or "HEAD"
    if base == ":root":  # everything the tip holds, as for a first push
        return git("hash-object", "-t", "tree", "/dev/null"), git("rev-parse", tip)
    return git("merge-base", base, tip), git("rev-parse", tip)


def changed(span):
    """{path: status} for every file the span adds, modifies, deletes or changes the type of."""
    old, new = span
    args = ["diff", "--name-status", "-z", "--no-renames"]
    if new == "":
        args.append("--cached")
        if old is None:
            args.append(git("hash-object", "-t", "tree", "/dev/null"))
    else:
        args += [old, new]
    parts = git(*args).split("\0")
    return {parts[i + 1]: parts[i][0] for i in range(0, len(parts) - 1, 2) if parts[i]}


def blob(path, spec, status="M"):
    return DELETED if status == "D" else git("rev-parse", f"{spec}:{path}")


def reviewable(span, cfg):
    return {p: s for p, s in changed(span).items() if tier_of(p, cfg)[0]}


def parse_record(text):
    m = re.search(r"```json\n(.*?)\n```", text, re.S)
    return json.loads(m.group(1)) if m else None


def records_at(spec):
    """Records as git holds them: '' for the index (what a commit will contain), or a commit. In
    ambient mode the records are not in the tree, and every record in the ledger is read instead."""
    if AMBIENT:
        return load_records()
    if spec is None:
        return []
    listing = (git("ls-files", "--cached", "--", RECORDS) if spec == "" else
               git("ls-tree", "-r", "--name-only", spec, "--", RECORDS, check=False))
    recs = [parse_record(git("show", f"{spec}:{p}")) for p in listing.splitlines() if p.endswith(".md")]
    return [r for r in recs if r]


def load_records():
    recs = []
    if os.path.isdir(RECORDS):
        for name in sorted(os.listdir(RECORDS)):
            if name.endswith(".md"):
                with open(os.path.join(RECORDS, name)) as f:
                    r = parse_record(f.read())
                if r:
                    recs.append(r)
    return recs


def invalid(rec, cfg):
    """Why a record cannot count as coverage, or None. Catches hand-made and inconsistent records."""
    need = {"id", "tier", "author", "reviewer", "files", "checks", "explanation", "human_review"}
    if not isinstance(rec, dict) or need - rec.keys():
        return f"missing {sorted(need - rec.keys()) if isinstance(rec, dict) else 'everything'}"
    if rec["human_review"].get("status") == "changes-requested":
        return "a human reviewer asked for changes"
    if not rec["checks"] or not any(c.get("name", "").startswith("lint") for c in rec["checks"]):
        return "no checks recorded"
    for f in rec["files"]:
        now = tier_of(f["path"], cfg)[0]
        if now and TIERS.index(now) > TIERS.index(f.get("tier", "low")):
            return f"{f['path']} is {now} tier now but was recorded as {f.get('tier')}"
    if rec["tier"] == "high" or any(f.get("tier") == "high" for f in rec["files"]):
        if any(c.get("result") != "pass" for c in rec["checks"]):
            return "high tier with a check that did not pass"
        if not rec.get("way_back"):
            return "high tier without a way back"
    return None


def coverage(recs, cfg):
    out = {}
    for r in recs:
        if not invalid(r, cfg):
            for f in r["files"]:
                out[(f["path"], f["blob"])] = r["id"]
    return out


def render(rec):
    lines = [f"# Review record {rec['id']}", "",
             "Generated by review.py from the JSON block; edit through `review.py`, not by hand.", "",
             "```json", json.dumps(rec, indent=1), "```", "",
             f"Tier: {rec['tier']}. Written by {rec['author']}. Reviewed by {rec['reviewer']}.",
             f"Recorded {rec['created']} on branch {rec['branch']}, parent commit {rec['parent'][:10]}; "
             f"covers the {rec.get('scope', 'staged change')}.", "",
             "## Files reviewed", "", "| File | Blob | Tier | Why |", "|---|---|---|---|"]
    lines += [f"| `{f['path']}` | `{f['blob'][:10]}` | {f['tier']} | {f['tier_reason']} |" for f in rec["files"]]
    lines += ["", "## The change, in the author's words", "", rec["explanation"].strip(), "",
              "## Checks", "", "| Check | Result | Detail |", "|---|---|---|"]
    lines += [f"| {c['name']} | {c['result']} | {c.get('detail', '')} |" for c in rec["checks"]]
    lines += ["", "## Review findings", ""]
    if rec["findings"]:
        lines += ["| Label | Finding | Disposition |", "|---|---|---|"]
        lines += [f"| {f['label']} | {f['summary']} | {f['disposition']} |" for f in rec["findings"]]
    else:
        lines += ["The reviewer reported no findings."]
    lines += ["", "## Way back", "", rec.get("way_back") or "Not stated.", "", "## Human review", ""]
    h = rec["human_review"]
    if h["status"] == "pending":
        script = os.path.join(KIT, "kit", "review.py") if AMBIENT else ".review/bin/review.py"
        where = rec.get("repo", "the repository")
        lines += [f"Pending. Run `python3 {script} signoff {rec['id']} --by NAME` in {where} "
                  "after reading the files listed above."]
    else:
        lines += [f"{h['status']} by {h['by']} on {h['date']}.", "", h.get("notes") or ""]
    return "\n".join(lines) + "\n"


def write(rec):
    os.makedirs(RECORDS, exist_ok=True)
    path = os.path.join(RECORDS, f"{rec['id']}.md")
    with open(path, "w") as f:
        f.write(render(rec))
    if AMBIENT:
        ledger_commit(path, f"{rec['id']} {rec['human_review']['status']}: {rec.get('repo', '')}")
    return path


def unstaged(paths):
    """Staged files whose working copy differs from the index. The scanners and tests read the
    working copy, so they would be checking something other than what gets committed."""
    present = [p for p in paths if os.path.exists(p)]
    if not present:
        return []
    return [p for p in git("diff", "--name-only", "-z", "--", *present).split("\0") if p]


def tree_dirty(against):
    """Tracked edits or untracked files outside what is being checked. The configured tests run on the
    working tree as a whole, so any of these would feed them content that is not being committed."""
    lines = git("status", "--porcelain=v1", "-z", "--untracked-files=all").split("\0")
    out = []
    for line in lines:
        if not line:
            continue
        x, y, path = line[0], line[1], line[3:]
        if path.startswith(DIR + "/"):
            continue
        if line.startswith("??") or y != " " or (against == "HEAD" and x != " "):
            out.append(path)
    return out


def scope(a, cfg):
    """(span, {path: status}, {path: blob}) for the staged change or the commits since --base."""
    span = span_of(a.base)
    files = reviewable(span, cfg)
    spec = span[1]
    if spec == "":
        dirty = unstaged(list(files))
        if dirty:
            raise SystemExit("review: these staged files also have unstaged edits, so the checks would "
                             f"read different content from what is committed: {', '.join(dirty)}. "
                             "Stage or stash the edits (git stash --keep-index) first.")
    elif files:
        live = [p for p, s in files.items() if s != "D"]
        if live and subprocess.run(["git", "diff", "--quiet", "HEAD", "--", *live]).returncode:
            raise SystemExit("review: with --base the files must match HEAD on disk; commit or stash the edits")
    return span, files, {p: blob(p, spec, s) for p, s in files.items()}


def cmd_base(a):
    """The commit a push or pull request is measured from: the upstream, else the default branch."""
    for ref in ("@{u}", "origin/HEAD", "origin/main", "origin/master", "main", "master"):
        if has_commit(ref):
            print(git("merge-base", ref, "HEAD"))
            return 0
    raise SystemExit("review: no upstream or default branch to compare against")


def cmd_tier(a):
    cfg = config()
    for p, s in changed(span_of(a.base)).items():
        t, why = tier_of(p, cfg)
        print(f"{t or '-':7} {s} {p}  ({why})")


def cmd_checks(a):
    """Run the configured checks on the change and remember the results against its blobs."""
    cfg = config()
    here = os.path.dirname(os.path.abspath(__file__))
    lint = [sys.executable, os.path.join(here, "lintgate.py"), "--mode", cfg.get("mode", "adopt")]
    tpl = os.path.join(KIT, "templates")
    hog_cfg, rules = ((os.path.join(tpl, "trufflehog.yml"), os.path.join(tpl, ".semgrep")) if AMBIENT
                      else ("trufflehog.yml", ".semgrep/"))
    if cfg.get("tests"):
        dirty = tree_dirty("index" if a.base is None else "HEAD")
        if dirty:
            raise SystemExit("review: the tests run on the whole working tree, and it holds changes that are not "
                             f"part of this {'commit' if a.base is None else 'range'}: {', '.join(dirty[:8])}"
                             f"{' ...' if len(dirty) > 8 else ''}. Run `git stash push --keep-index "
                             "--include-untracked`, rerun, then `git stash pop`.")
    if a.base is None:
        # formatting added files happens here, before the blobs are taken, so the record matches the commit
        subprocess.run([*lint, "--staged", "--fix", "--quiet"], capture_output=True)
    span, files, blobs = scope(a, cfg)
    # a symlink's content is its target's path; semgrep refuses one as a scan root
    live = [p for p, s in files.items() if s != "D" and not os.path.islink(p)]
    checks = [("lint (new findings)", [*lint, *(["--staged"] if a.base is None else ["--base", a.base])])]
    if live:
        checks.append(("secrets (trufflehog)", ["trufflehog", "filesystem", "--no-update", "--no-verification",
                                                "--fail", "--config", hog_cfg, *live]))
        checks.append(("semgrep (kit rules)", ["semgrep", "scan", "--metrics=off", "--error", "--quiet",
                                               "--config", rules, *live]))
    for name, command in cfg.get("tests", {}).items():
        checks.append((f"tests: {name}", ["sh", "-c", command]))
    results = []
    for name, command in checks:
        try:
            p = subprocess.run(command, capture_output=True, text=True)
            result = "pass" if p.returncode == 0 else "fail"
            tail = (p.stdout + p.stderr).strip().splitlines()[-1:] or [""]
            results.append({"name": name, "result": result, "detail": tail[0][:200].replace("|", "/")})
        except FileNotFoundError:
            results.append({"name": name, "result": "not run", "detail": f"{command[0]} is not installed"})
        print(f"{results[-1]['result']:8} {name}  {results[-1]['detail']}")
    with open(LAST_CHECKS, "w") as f:
        json.dump({"base": a.base, "blobs": blobs, "checks": results}, f, indent=1)
    return 1 if any(r["result"] != "pass" for r in results) else 0


def cmd_record(a):
    cfg = config()
    span, files, blobs = scope(a, cfg)
    if not files:
        raise SystemExit("review: nothing in this change needs a record")
    try:
        with open(LAST_CHECKS) as f:
            last = json.load(f)
    except FileNotFoundError:
        raise SystemExit("review: run `review.py checks` on this change first") from None
    if last["blobs"] != blobs or last.get("base") != a.base:
        raise SystemExit("review: the files changed after `review.py checks` ran, or it ran on a different "
                         "range; run it again with the same arguments")
    with open(a.findings) if a.findings else open(os.devnull) as fh:
        findings = json.loads(fh.read() or "[]")
    for f in findings:
        if f.get("label") not in ("important", "nit", "pre-existing") or not f.get("disposition"):
            raise SystemExit(f"review: finding needs a label (important/nit/pre-existing) and a disposition: {f}")
        if f["label"] == "important" and f["disposition"] != "fixed":
            raise SystemExit(f"review: an important finding must be fixed before commit: {f['summary']}")
    tiers = {p: tier_of(p, cfg) for p in files}
    tier = max((t for t, _ in tiers.values()), key=TIERS.index)
    failed = [c["name"] for c in last["checks"] if c["result"] != "pass"]
    if tier == "high" and failed:
        raise SystemExit(f"review: a high-tier change needs every check to pass; failing: {', '.join(failed)}")
    if tier == "high" and not a.way_back:
        raise SystemExit("review: a high-tier change needs --way-back (how to undo it once used)")
    now = datetime.datetime.now(datetime.UTC)
    with open(a.explanation) as fh:
        explanation = fh.read()
    rec = {
        "id": now.strftime("%Y%m%d-%H%M%S") + "-" + git("write-tree")[:7],
        "created": now.isoformat(timespec="seconds"),
        "branch": git("branch", "--show-current", check=False) or "(detached)",
        "parent": git("rev-parse", "HEAD") if has_commit() else "(first commit)",
        "scope": "staged change" if a.base is None else f"commits since {span[0][:10]}",
        "tier": tier,
        "author": a.author,
        "reviewer": a.reviewer,
        "files": [{"path": p, "blob": blobs[p], "tier": tiers[p][0], "tier_reason": tiers[p][1]} for p in files],
        "explanation": explanation,
        "checks": last["checks"],
        "findings": findings,
        "way_back": a.way_back,
        "human_review": {"status": "pending"},
    }
    if AMBIENT:
        # a committed record stays free of local paths; a ledger record needs them to be found again
        rec["repo"] = os.getcwd()
        rec["origin"] = re.sub(r"//[^/@]+@", "//", git("remote", "get-url", "origin", check=False))  # no tokens
    path = write(rec)
    if not AMBIENT:
        git("add", path)
    print(path)


def upstream_refs():
    """The branches whose content counts as somebody else's: `review.upstream` (repeatable) where set,
    else the default branch of each remote other than origin. Never every remote-tracking ref, since
    the developer's own pushed branches are remote-tracking refs too, and never origin by default,
    since in a repository that is not a fork origin's default branch is the developer's own."""
    # --local, so `git -c review.upstream=HEAD push` cannot exempt everything; and only remote-tracking
    # refs, since naming a local branch would make the tip its own upstream
    named = git("config", "--local", "--get-all", "review.upstream", check=False).split()
    if named:
        full = [git("rev-parse", "--symbolic-full-name", n, check=False) for n in named]
        return [f for f in full if f.startswith("refs/remotes/")]
    refs = []
    for remote in git("remote", check=False).split():
        if remote == "origin":
            continue
        head = git("symbolic-ref", "-q", f"refs/remotes/{remote}/HEAD", check=False)
        refs += [head] if head else [r for r in (f"refs/remotes/{remote}/{b}" for b in ("main", "master", "dev"))
                                     if has_commit(r)][:1]
    return refs


def blobs_at(spec, paths):
    out = {}
    for entry in git("ls-tree", "-z", spec, "--", *paths).split("\0"):
        if "\t" in entry:
            meta, path = entry.split("\t", 1)
            out[path] = meta.split()[2]
    return out


def published(pairs, base, tips):
    """Paths whose state in this change is upstream's doing, through a merge. For each upstream branch,
    m = merge-base(ref, tip) is what the tip has taken in from it; a point the base already contained
    brought nothing new. A path counts only when the fork never changed it (identical, or absent, at
    the base and where fork and upstream diverged) and the tip now matches m, so upstream made the
    change and taking it overrides no work of the fork's: a revert of the fork's own change, or a
    deletion of a file the fork modified, still needs a record."""
    left = dict(pairs)
    found = set()
    if not base:
        return found
    for r in upstream_refs():
        for t in tips:
            m = git("merge-base", r, t, check=False)
            if not left or not m or subprocess.run(["git", "merge-base", "--is-ancestor", m, base]).returncode == 0:
                continue
            fork = git("merge-base", m, base)
            at_m, at_fork, at_base = (blobs_at(c, list(left)) for c in (m, fork, base))
            for path, b in list(left.items()):
                ours = None if b == DELETED else b
                if at_base.get(path) == at_fork.get(path) and at_m.get(path) == ours:
                    found.add(path)
                    del left[path]
    return found


def on_disk(path):
    """The blob hash git would store for the file as it is now; for a symlink that is the link
    itself, not the file it points to, which is what `git hash-object PATH` would read."""
    if os.path.islink(path):
        return subprocess.run(["git", "hash-object", "--stdin"], input=os.fsencode(os.readlink(path)),
                              capture_output=True, check=True).stdout.decode().strip()
    return git("hash-object", path)


def uncovered(span, cfg):
    """Files in the span whose content is in no valid record held at the span's tip, leaving out
    content already published on a remote-tracking branch."""
    files = reviewable(span, cfg)
    cov = coverage(records_at(span[1]), cfg)
    missing = {p: blob(p, span[1], s) for p, s in files.items()}
    missing = {p: b for p, b in missing.items() if (p, b) not in cov}
    if not missing:
        return []
    tips = [span[1]] if span[1] else [t for t in ("MERGE_HEAD",) if has_commit(t)]
    done = published(missing, span[0], tips) if tips else set()
    return [p for p in missing if p not in done]


def report(missing):
    for p in missing:
        print(f"unreviewed: {p}")
    if missing:
        print("review: these files are in no valid review record in their current form. Run "
              "`review.py checks`, a fresh-context review, then `review.py record`.", file=sys.stderr)
        return 1
    return 0


def cmd_check(a):
    cfg = config()
    span = span_of(a.base)
    if a.base is None:
        dirty = unstaged(list(reviewable(span, cfg)))
        if dirty:
            print("review: staged files with unstaged edits (the commit hooks would scan the wrong content): "
                  + ", ".join(dirty), file=sys.stderr)
            return 1
    missing = uncovered(span, cfg)
    if os.environ.get("REVIEW_SKIP") and a.base is None:
        print(f"review: REVIEW_SKIP set ({os.environ['REVIEW_SKIP']}); {len(missing)} unreviewed file(s) "
              "will be flagged in the commit trailers and must be recorded before push")
        return 0
    return report(missing)


def cmd_check_push(a):
    """Lines on stdin are '<local ref> <local sha> <remote ref> <remote sha>', one per ref pushed."""
    cfg = config()
    missing = []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO:
            continue  # malformed, or a deletion of the remote ref
        local, remote = parts[1], parts[3]
        if remote != ZERO and has_commit(remote):
            base = remote
        else:
            # a new branch: measure from the remote's default branch, or from nothing on a first push
            base = next((r for r in ("origin/HEAD", "origin/main", "origin/master") if has_commit(r)), ":root")
            if base == ":root":
                print(f"review: no remote sha or origin default branch for {parts[0]}, so it is measured from "
                      "the empty tree and every file it holds needs a record", file=sys.stderr)
        missing += [f"{p} (in {parts[0]})" for p in uncovered(span_of(base, local), cfg)]
    return report(missing)


def status_all():
    """Records awaiting a person across every checkout in the ledger. Repositories with the kit
    installed keep their records in their own tree, so they are not listed here."""
    root = os.path.join(LEDGER, "repos")
    n = 0
    for d, _, names in sorted(os.walk(root)):
        for name in sorted(names):
            if d.endswith(os.sep + "records") and name.endswith(".md"):
                with open(os.path.join(d, name)) as f:
                    r = parse_record(f.read())
                if r and r.get("human_review", {}).get("status") == "pending":
                    n += 1
                    print(f"pending  {r['tier']:6} {r['id']}  {r.get('repo', d)}  {len(r['files'])} file(s), "
                          f"written by {r['author']}")
    print(f"review: {n} record(s) in {root} await human review")
    return 0


def cmd_status(a):
    cfg = config()
    recs = load_records()
    pending = [r for r in recs if r.get("human_review", {}).get("status") == "pending"]
    for r in sorted(pending, key=lambda r: (-TIERS.index(r["tier"]), r["id"])):
        print(f"pending  {r['tier']:6} {r['id']}  {len(r['files'])} file(s), written by {r['author']}")
    for r in recs:
        why = invalid(r, cfg)
        if why:
            print(f"invalid  {r.get('id', '?')}: {why}; it does not count as review")
    latest = {}
    for r in recs:
        for f in r["files"]:
            latest[f["path"]] = (f["blob"], r["id"])
    for path, (b, rid) in sorted(latest.items()):
        if b != DELETED and os.path.lexists(path) and on_disk(path) != b:
            print(f"changed  {path}: differs from the content reviewed in {rid}")
    if not pending:
        print("review: no records await human review")


def cmd_signoff(a):
    recs = {r["id"]: r for r in load_records()}
    rec = recs.get(a.id) or sys.exit(f"review: no record {a.id}")
    drift = [f["path"] for f in rec["files"]
             if f["blob"] != DELETED and os.path.lexists(f["path"]) and on_disk(f["path"]) != f["blob"]]
    if drift and not a.force:
        raise SystemExit("review: these files differ from the reviewed content, so a sign-off would not "
                         f"describe what is on disk: {', '.join(drift)} (use --force to sign the record anyway)")
    rec["human_review"] = {"status": a.verdict, "by": a.by, "notes": a.notes or "",
                           "date": datetime.date.today().isoformat(), "files_differ_at_signoff": drift}
    print(write(rec))


def cmd_pr_body(a):
    cfg = config()
    span = span_of(a.base)
    recs = records_at(span[1])
    cov = coverage(recs, cfg)
    files = reviewable(span, cfg)
    keyed = {p: (p, blob(p, span[1], s)) for p, s in files.items()}
    ids = sorted({cov[k] for k in keyed.values() if k in cov})
    print("## Review records\n")
    for r in (r for r in recs if r["id"] in ids):
        h = r["human_review"]
        print(f"- `{r['id']}`: {r['tier']} tier, written by {r['author']}, reviewed by {r['reviewer']}; "
              f"{sum(c['result'] == 'pass' for c in r['checks'])}/{len(r['checks'])} checks passed; "
              f"{len(r['findings'])} finding(s); human review {h['status']}")
    unrev = [p for p, k in keyed.items() if k not in cov]
    if unrev:
        print("\nNot covered by any valid record in their current form: " + ", ".join(f"`{p}`" for p in unrev))


def cmd_trailers(a):
    if AMBIENT:
        # the record is not staged with the commit, so it is found by the content it covers
        cfg = config()
        span = span_of()
        pairs = {(p, blob(p, "", s)) for p, s in reviewable(span, cfg).items()}
        new = [r for r in load_records() if not invalid(r, cfg)
               and pairs & {(f["path"], f["blob"]) for f in r["files"]}]
    else:
        staged = set(git("diff", "--cached", "--name-only", "-z").split("\0"))
        new = [r for r in records_at("") if os.path.join(RECORDS, f"{r['id']}.md") in staged]
    lines = []
    for r in new:
        lines += [f"Review-Record: {r['id']}", f"Review-Tier: {r['tier']}", f"Written-By: {r['author']}",
                  f"Reviewed-By: {r['reviewer']}", f"Human-Review: {r['human_review']['status']}"]
    if new and AMBIENT:
        lines.append("Review-Ledger: ~/.review-ledger")  # where, without this machine's directory layout
    if os.environ.get("REVIEW_SKIP"):
        lines.append(f"Review-Skipped: {os.environ['REVIEW_SKIP']}")
    if lines:
        with open(a.msgfile) as f:
            msg = f.read()
        lines = [line for line in lines if line not in msg]
        if lines:
            with open(a.msgfile, "w") as f:
                f.write(msg.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("tier"); s.add_argument("--base")
    s = sub.add_parser("checks"); s.add_argument("--base", help="review commits since REF instead of the staged change")
    sub.add_parser("base")
    s = sub.add_parser("record")
    s.add_argument("--base", help="record commits since REF instead of the staged change")
    s.add_argument("--author", required=True, help="who or what wrote the change, e.g. claude-opus-5-5 or a name")
    s.add_argument("--reviewer", required=True, help="who or what reviewed it, e.g. 'claude-opus-5-5, fresh context'")
    s.add_argument("--explanation", required=True, help="file: what the change does, why, and what could go wrong")
    s.add_argument("--findings", help="file: JSON list of {label, summary, disposition}")
    s.add_argument("--way-back")
    s = sub.add_parser("check"); s.add_argument("--base")
    sub.add_parser("check-push")
    s = sub.add_parser("status"); s.add_argument("--all", action="store_true")
    s = sub.add_parser("signoff"); s.add_argument("id"); s.add_argument("--by", required=True)
    s.add_argument("--verdict", choices=["approved", "changes-requested"], default="approved")
    s.add_argument("--notes"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("pr-body"); s.add_argument("--base", required=True)
    s = sub.add_parser("trailers"); s.add_argument("msgfile")
    a = ap.parse_args(argv)
    if a.cmd == "status" and a.all:
        return status_all()
    os.chdir(git("rev-parse", "--show-toplevel"))
    # ambient only where nothing of an installed kit is present, so deleting tiers.toml fails closed
    if not os.environ.get("REVIEW_TIERS") and not os.path.exists(DIR):
        enter_ambient(os.getcwd())
    return {"tier": cmd_tier, "checks": cmd_checks, "record": cmd_record, "check": cmd_check,
            "check-push": cmd_check_push, "status": cmd_status, "signoff": cmd_signoff,
            "pr-body": cmd_pr_body, "trailers": cmd_trailers, "base": cmd_base}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())
