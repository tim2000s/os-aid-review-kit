"""End-to-end tests: each builds a throwaway git repository, installs the kit and runs the real tools."""
import json
import os
import shutil
import subprocess
import sys

import pytest

# git and the tools print UTF-8; Windows would otherwise decode their output as cp1252
UTF8 = {"text": True, "encoding": "utf-8", "errors": "replace"}

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def sh(cwd, *cmd, check=True, env=None):
    # resolving the program finds Windows .cmd shims (npm's lefthook and biome); output is read as UTF-8
    cmd = (shutil.which(cmd[0]) or cmd[0], *cmd[1:])
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, **UTF8,
                       env={**os.environ, **(env or {})})
    if check and p.returncode != 0:
        raise AssertionError(f"{cmd} failed ({p.returncode}):\n{p.stdout}\n{p.stderr}")
    return p


NO_SYMLINKS = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra privilege on Windows")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Keep the machine's global git config (which may point at the kit's global hooks) and its review
    ledger out of every test."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("REVIEW_LEDGER", str(tmp_path / "ledger"))
    (tmp_path / "gitconfig").write_text("[user]\n\temail = t@example.org\n\tname = T\n")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    sh(r, "git", "init", "-q", "-b", "main")
    sh(r, "git", "config", "user.email", "t@example.org")
    sh(r, "git", "config", "user.name", "T")
    (r / "requirements.txt").write_text("")
    (r / "app.py").write_text("import os\n\n\ndef f():\n    return 1\n")  # one inherited finding (F401)
    sh(r, "git", "add", ".")
    sh(r, "git", "commit", "-qm", "base")
    sh(r, PY, os.path.join(KIT, "kit", "install.py"), str(r))
    sh(r, "git", "add", ".")
    sh(r, "git", "commit", "-qm", "kit", "--no-verify")
    return r


def lintgate(r, *args):
    return sh(r, PY, ".review/bin/lintgate.py", "--staged", *args, check=False)


def review(r, *args, env=None):
    return sh(r, PY, ".review/bin/review.py", *args, check=False, env=env)


def test_inherited_finding_does_not_fail(repo):
    (repo / "app.py").write_text("import os\n\n\ndef f():\n    return 2\n")
    sh(repo, "git", "add", "app.py")
    p = lintgate(repo)
    assert p.returncode == 0, p.stdout + p.stderr


def test_introduced_finding_fails(repo):
    (repo / "app.py").write_text("import os\nimport sys\n\n\ndef f():\n    return 2\n")
    sh(repo, "git", "add", "app.py")
    p = lintgate(repo)
    assert p.returncode == 1
    assert "F401" in p.stdout and "sys" in p.stdout


def test_added_file_counts_every_finding(repo):
    (repo / "new.py").write_text("import json\n")
    sh(repo, "git", "add", "new.py")
    assert lintgate(repo).returncode == 1


def test_staged_content_is_linted_not_working_tree(repo):
    (repo / "app.py").write_text("import os\nimport sys\n\n\ndef f():\n    return 2\n")
    sh(repo, "git", "add", "app.py")
    (repo / "app.py").write_text("import os\n\n\ndef f():\n    return 2\n")  # fixed on disk, not staged
    assert lintgate(repo).returncode == 1


@pytest.mark.skipif(shutil.which("biome") is None, reason="biome not installed")
def test_javascript_new_findings(repo):
    (repo / "package.json").write_text("{}\n")
    sh(repo, PY, os.path.join(KIT, "kit", "install.py"), str(repo))
    (repo / "a.js").write_text("const used = 1;\nconsole.log(used);\n")
    sh(repo, "git", "add", ".")
    sh(repo, "git", "commit", "-qm", "js", "--no-verify")
    (repo / "a.js").write_text("const used = 1;\nconst unused = 2;\nconsole.log(used);\n")
    sh(repo, "git", "add", "a.js")
    p = lintgate(repo)
    assert p.returncode == 1 and "noUnusedVariables" in p.stdout


def record(r, findings=None, way_back=None, author="claude-opus-5-5"):
    (r / ".review" / "exp.txt").write_text("Changes f to return 2. Could break callers that expect 1.\n")
    (r / ".review" / "findings.json").write_text(json.dumps(findings or []))
    args = ["record", "--author", author, "--reviewer", "claude-opus-5-5, fresh context",
            "--explanation", ".review/exp.txt", "--findings", ".review/findings.json"]
    if way_back:
        args += ["--way-back", way_back]
    return review(r, *args)


def stage_change(r):
    (r / "app.py").write_text("import os\n\n\ndef f():\n    return 2\n")
    sh(r, "git", "add", "app.py")


def test_unreviewed_change_blocks_commit_check(repo):
    stage_change(repo)
    p = review(repo, "check")
    assert p.returncode == 1 and "unreviewed: app.py" in p.stdout


def test_record_covers_exact_content(repo):
    stage_change(repo)
    assert review(repo, "checks").returncode in (0, 1)
    p = record(repo)
    assert p.returncode == 0, p.stderr
    assert review(repo, "check").returncode == 0
    rec_path = p.stdout.strip()
    rec = (repo / rec_path).read_text()
    blob = sh(repo, "git", "rev-parse", ":app.py").stdout.strip()
    assert blob in rec and "Human review" in rec and "Pending" in rec
    # editing the file after the record makes it unreviewed again
    (repo / "app.py").write_text("import os\n\n\ndef f():\n    return 3\n")
    sh(repo, "git", "add", "app.py")
    assert review(repo, "check").returncode == 1


def test_record_refuses_stale_checks(repo):
    stage_change(repo)
    review(repo, "checks")
    (repo / "app.py").write_text("import os\n\n\ndef f():\n    return 9\n")
    sh(repo, "git", "add", "app.py")
    p = record(repo)
    assert p.returncode != 0 and "changed after" in p.stderr


def test_unfixed_important_finding_refused(repo):
    stage_change(repo)
    review(repo, "checks")
    p = record(repo, findings=[{"label": "important", "summary": "x", "disposition": "accepted"}])
    assert p.returncode != 0 and "must be fixed" in p.stderr


def test_high_tier_needs_way_back_and_passing_checks(repo):
    tiers = repo / ".review" / "tiers.toml"
    tiers.write_text(tiers.read_text().replace("[high]\npaths = []", '[high]\npaths = ["app.py"]'))
    stage_change(repo)
    review(repo, "checks")
    p = record(repo)
    assert p.returncode != 0 and ("way-back" in p.stderr or "every check" in p.stderr)


def test_trailers_and_commit_hooks(repo):
    if shutil.which("lefthook") is None:
        pytest.skip("lefthook not installed")
    sh(repo, "lefthook", "install")
    stage_change(repo)
    blocked = sh(repo, "git", "commit", "-qm", "change", check=False)
    assert blocked.returncode != 0, "commit without a record must be refused"
    review(repo, "checks")
    assert record(repo).returncode == 0
    sh(repo, "git", "commit", "-qm", "change")
    msg = sh(repo, "git", "log", "-1", "--format=%B").stdout
    assert "Review-Record:" in msg and "Human-Review: pending" in msg and "Written-By: claude-opus-5-5" in msg


def test_skip_is_flagged(repo):
    stage_change(repo)
    p = review(repo, "check", env={"REVIEW_SKIP": "work in progress"})
    assert p.returncode == 0 and "flagged" in p.stdout
    msg = repo / "msg.txt"
    msg.write_text("wip\n")
    review(repo, "trailers", str(msg), env={"REVIEW_SKIP": "work in progress"})
    assert "Review-Skipped: work in progress" in msg.read_text()


def test_status_and_signoff(repo):
    stage_change(repo)
    review(repo, "checks")
    rid = os.path.basename(record(repo).stdout.strip())[:-3]
    sh(repo, "git", "commit", "-qm", "change", "--no-verify")
    assert f"pending  medium {rid}" in review(repo, "status").stdout
    (repo / "app.py").write_text("changed on disk\n")
    refused = review(repo, "signoff", rid, "--by", "A Person")
    assert refused.returncode != 0 and "differ" in refused.stderr
    assert "changed  app.py" in review(repo, "status").stdout
    sh(repo, "git", "checkout", "app.py")
    assert review(repo, "signoff", rid, "--by", "A Person", "--notes", "Read it.").returncode == 0
    text = (repo / ".review" / "records" / f"{rid}.md").read_text()
    assert "approved by A Person" in text
    assert "no records await" in review(repo, "status").stdout


def test_install_never_overwrites(repo):
    (repo / "trivy.yaml").write_text("# the team's own\n")
    sh(repo, PY, os.path.join(KIT, "kit", "install.py"), str(repo))
    assert (repo / "trivy.yaml").read_text() == "# the team's own\n"
    assert (repo / "trivy.yaml.kit-proposed").exists()


def test_install_tailors_languages(repo):
    wf = (repo / ".github" / "workflows" / "quality-gate.yml").read_text()
    # CI installs every non-Swift toolchain, so a file in an undetected language is linted, not skipped
    assert "ruff==" in wf and "ktlint" in wf and "biome" in wf and "macos" not in wf
    assert "--languages python,javascript,kotlin,swift" in wf
    assert not (repo / "detekt.yml").exists() and (repo / "ruff.toml").exists()
    assert (repo / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    assert "review-kit:start" in (repo / "AGENTS.md").read_text()


def test_record_committed_range(repo):
    stage_change(repo)
    sh(repo, "git", "commit", "-qm", "unreviewed", "--no-verify")
    base = sh(repo, "git", "rev-parse", "HEAD~1").stdout.strip()
    assert review(repo, "check", "--base", base).returncode == 1
    review(repo, "checks", "--base", base)
    (repo / ".review" / "exp.txt").write_text("Retrospective record of f returning 2.\n")
    p = review(repo, "record", "--base", base, "--author", "claude-opus-5-5", "--reviewer", "r, fresh context",
               "--explanation", ".review/exp.txt")
    assert p.returncode == 0, p.stderr
    sh(repo, "git", "commit", "-qm", "record", "--no-verify")
    assert review(repo, "check", "--base", base).returncode == 0
    assert "commits since" in (repo / p.stdout.strip()).read_text()


# Each test below reproduces a defect found by the fresh-context review of 20 November 2026.

def test_checks_refuse_unstaged_edits_to_staged_files(repo):
    (repo / "app.py").write_text("KEY = 'AKIA" + "IOSFODNN7EXAMPLE'\n")
    sh(repo, "git", "add", "app.py")
    (repo / "app.py").write_text("KEY = None\n")  # the secret is still in the index
    p = review(repo, "checks")
    assert p.returncode != 0 and "unstaged edits" in p.stderr
    assert review(repo, "check").returncode == 1


def test_gate_files_are_high_tier_and_cannot_exempt_themselves(repo):
    tiers = repo / ".review" / "tiers.toml"
    tiers.write_text(tiers.read_text().replace('[exempt]\npaths = [', '[exempt]\npaths = ["*", '))
    stage_change(repo)
    sh(repo, "git", "add", ".review/tiers.toml")
    p = review(repo, "check")
    assert p.returncode == 1 and "unreviewed: .review/tiers.toml" in p.stdout
    assert "high" in review(repo, "tier").stdout.split(".review/tiers.toml")[0].splitlines()[-1]


def test_hand_written_record_does_not_count(repo):
    stage_change(repo)
    b = sh(repo, "git", "rev-parse", ":app.py").stdout.strip()
    fake = repo / ".review" / "records" / "fake.md"
    fake.write_text("```json\n" + json.dumps({"id": "x", "files": [{"path": "app.py", "blob": b}]}) + "\n```\n")
    sh(repo, "git", "add", str(fake))
    assert review(repo, "check").returncode == 1
    assert "invalid" in review(repo, "status").stdout


def test_changes_requested_record_does_not_count(repo):
    base = sh(repo, "git", "rev-parse", "HEAD").stdout.strip()
    stage_change(repo)
    review(repo, "checks")
    rid = os.path.basename(record(repo).stdout.strip())[:-3]
    sh(repo, "git", "commit", "-qm", "change", "--no-verify")
    assert review(repo, "check", "--base", base).returncode == 0
    review(repo, "signoff", rid, "--by", "A Person", "--verdict", "changes-requested")
    sh(repo, "git", "commit", "-qam", "verdict", "--no-verify")
    assert review(repo, "check", "--base", base).returncode == 1


def test_deletion_needs_a_record(repo):
    sh(repo, "git", "rm", "-q", "app.py")
    assert review(repo, "check").returncode == 1
    review(repo, "checks")
    assert record(repo).returncode == 0
    assert review(repo, "check").returncode == 0


def test_check_push_checks_the_pushed_ref_not_head(repo):
    main = sh(repo, "git", "rev-parse", "HEAD").stdout.strip()
    sh(repo, "git", "checkout", "-qb", "evil")
    stage_change(repo)
    sh(repo, "git", "commit", "-qm", "unreviewed", "--no-verify")
    evil = sh(repo, "git", "rev-parse", "HEAD").stdout.strip()
    sh(repo, "git", "checkout", "-q", "main")
    line = f"refs/heads/evil {evil} refs/heads/evil {main}\n"
    p = subprocess.run([PY, ".review/bin/review.py", "check-push"], cwd=repo, input=line,
                       capture_output=True, **UTF8)
    assert p.returncode == 1 and "app.py (in refs/heads/evil)" in p.stdout
    ok = f"refs/heads/main {main} refs/heads/main {main}\n"
    p = subprocess.run([PY, ".review/bin/review.py", "check-push"], cwd=repo, input=ok, capture_output=True, **UTF8)
    assert p.returncode == 0


@pytest.mark.skipif(shutil.which("biome") is None, reason="biome not installed")
def test_lintgate_fails_closed_on_broken_linter_config(repo):
    (repo / "package.json").write_text("{}\n")
    sh(repo, PY, os.path.join(KIT, "kit", "install.py"), str(repo))
    (repo / "biome.json").write_text("{ not json")
    (repo / "a.js").write_text("debugger;\neval('1');\n")
    sh(repo, "git", "add", "biome.json", "a.js", "package.json")
    p = lintgate(repo)
    assert p.returncode == 2 and "biome failed" in p.stderr


def test_unicode_path_can_be_recorded(repo):
    (repo / "café.py").write_text("X = 1\n")
    sh(repo, "git", "add", "café.py")
    p = lintgate(repo)
    assert "1 file(s) checked" in p.stdout, p.stdout + p.stderr
    review(repo, "checks")
    assert record(repo).returncode == 0
    assert review(repo, "check").returncode == 0


# Round two of the review, 20 November 2026.

def test_tests_refuse_a_dirty_working_tree(repo):
    tiers = repo / ".review" / "tiers.toml"
    tiers.write_text(tiers.read_text() + 'unit = "true"\n')
    sh(repo, "git", "add", ".review/tiers.toml")
    sh(repo, "git", "commit", "-qm", "tests", "--no-verify")
    stage_change(repo)
    (repo / "other.py").write_text("B = 1\n")  # untracked, would feed the tests
    p = review(repo, "checks")
    assert p.returncode != 0 and "other.py" in p.stderr


def test_linter_config_files_are_gate_files(repo):
    (repo / ".ruff.toml").write_text("[lint]\nselect = []\n")
    sh(repo, "git", "add", ".ruff.toml")
    out = review(repo, "tier").stdout
    assert any(line.startswith("high") and ".ruff.toml" in line for line in out.splitlines())


def test_deleting_a_linter_config_does_not_switch_it_off(repo):
    if shutil.which("biome") is None:
        pytest.skip("biome not installed")
    (repo / "package.json").write_text("{}\n")
    sh(repo, PY, os.path.join(KIT, "kit", "install.py"), str(repo))
    sh(repo, "git", "add", ".")
    sh(repo, "git", "commit", "-qm", "js", "--no-verify")
    sh(repo, "git", "rm", "-q", "biome.json")
    (repo / "a.js").write_text("const unused = 1;\n")
    sh(repo, "git", "add", "a.js")
    assert lintgate(repo).returncode == 1


def test_pre_push_hook_checks_a_branch_other_than_head(repo, tmp_path):
    if shutil.which("lefthook") is None:
        pytest.skip("lefthook not installed")
    remote = tmp_path / "remote.git"
    sh(tmp_path, "git", "init", "-q", "--bare", str(remote))
    sh(repo, "git", "remote", "add", "origin", str(remote))
    sh(repo, "git", "push", "-q", "--no-verify", "origin", "main")
    sh(repo, "lefthook", "install")
    sh(repo, "git", "checkout", "-qb", "evil")
    stage_change(repo)
    sh(repo, "git", "commit", "-qm", "unreviewed", "--no-verify")
    sh(repo, "git", "checkout", "-q", "main")
    p = sh(repo, "git", "push", "origin", "evil", check=False)
    out = p.stdout + p.stderr
    assert p.returncode != 0 and "unreviewed: app.py (in refs/heads/evil)" in out, out
    assert "evil" not in sh(repo, "git", "ls-remote", "origin").stdout


def test_first_push_measures_from_nothing(repo):
    # from nothing every finding is new, so the fixture's deliberate unused import has to go first
    (repo / "app.py").write_text("def f():\n    return 1\n")
    sh(repo, "git", "commit", "-qam", "clean", "--no-verify")
    tip = sh(repo, "git", "rev-parse", "HEAD").stdout.strip()
    line = f"refs/heads/main {tip} refs/heads/main {'0' * 40}\n"
    p = subprocess.run([PY, ".review/bin/review.py", "check-push"], cwd=repo, input=line,
                       capture_output=True, **UTF8)
    assert p.returncode == 1 and "unreviewed: app.py" in p.stdout
    review(repo, "checks", "--base", ":root")
    (repo / ".review" / "exp.txt").write_text("The repository as first committed.\n")
    r = review(repo, "record", "--base", ":root", "--author", "a person", "--reviewer", "r, fresh context",
               "--explanation", ".review/exp.txt", "--way-back", "delete the repository")
    assert r.returncode == 0, r.stderr
    sh(repo, "git", "commit", "-qm", "record", "--no-verify")
    tip = sh(repo, "git", "rev-parse", "HEAD").stdout.strip()
    line = f"refs/heads/main {tip} refs/heads/main {'0' * 40}\n"
    p = subprocess.run([PY, ".review/bin/review.py", "check-push"], cwd=repo, input=line,
                       capture_output=True, **UTF8)
    assert p.returncode == 0, p.stdout + p.stderr


def test_config_added_in_the_change_lints_the_before_side_too(repo):
    if shutil.which("biome") is None:
        pytest.skip("biome not installed")
    (repo / "a.js").write_text("const unused = 1;\ndebugger;\n")  # inherited findings
    sh(repo, "git", "add", "a.js")
    sh(repo, "git", "commit", "-qm", "js", "--no-verify")
    (repo / "package.json").write_text("{}\n")
    sh(repo, PY, os.path.join(KIT, "kit", "install.py"), str(repo))  # biome.json arrives now
    (repo / "a.js").write_text("const unused = 1;\ndebugger;\n// touched\n")
    sh(repo, "git", "add", "a.js", "biome.json", "package.json")
    p = lintgate(repo)
    assert p.returncode == 0, p.stdout + p.stderr


def test_biome_defers_house_style_to_an_existing_eslint(repo):
    (repo / "package.json").write_text("{}\n")
    (repo / ".eslintrc.js").write_text("module.exports = {};\n")
    sh(repo, PY, os.path.join(KIT, "kit", "install.py"), str(repo))
    rules = json.loads((repo / "biome.json").read_text())["linter"]["rules"]
    assert rules["style"] == "off" and rules["complexity"] == "off"
    assert rules["suspicious"]["noExplicitAny"] == "error"  # correctness and security rules stay on


# Ambient mode: a repository without the kit, gated through the global hooks.

@pytest.fixture
def plain(tmp_path):
    r = tmp_path / "plain"
    r.mkdir()
    sh(r, "git", "init", "-q", "-b", "main")
    (r / "app.py").write_text("import os\n\n\ndef f():\n    return 1\n")
    (r / "notes.md").write_text("notes\n")
    sh(r, "git", "add", ".")
    sh(r, "git", "commit", "-qm", "base")
    sh(tmp_path, "git", "config", "--global", "core.hooksPath", os.path.join(KIT, "global-hooks"))
    return r


def ambient(r, *args, env=None, stdin=None):
    p = subprocess.run([PY, os.path.join(KIT, "kit", "review.py"), *args], cwd=r, capture_output=True, **UTF8,
                       input=stdin, env={**os.environ, **(env or {})})
    return p


def ambient_record(r, tmp_path):
    (tmp_path / "exp.txt").write_text("Changes f to return 2.\n")
    return ambient(r, "record", "--author", "claude-opus-5-5", "--reviewer", "claude-opus-5-5, fresh context",
                   "--explanation", str(tmp_path / "exp.txt"))


def test_ambient_gate_refuses_then_records_outside_the_tree(plain, tmp_path):
    stage_change(plain)
    blocked = sh(plain, "git", "commit", "-qm", "change", check=False)
    assert blocked.returncode != 0 and "unreviewed: app.py" in blocked.stdout + blocked.stderr
    ambient(plain, "checks")
    p = ambient_record(plain, tmp_path)
    assert p.returncode == 0, p.stderr
    assert str(tmp_path / "ledger") in p.stdout and not (plain / ".review").exists()
    sh(plain, "git", "commit", "-qm", "change")
    msg = sh(plain, "git", "log", "-1", "--format=%B").stdout
    assert "Review-Record:" in msg and "Human-Review: pending" in msg and "Review-Ledger:" in msg, msg
    assert "pending" in sh(tmp_path / "ledger", "git", "log", "--format=%s").stdout
    listing = ambient(tmp_path, "status", "--all")
    assert listing.returncode == 0 and str(plain) in listing.stdout and "1 record(s)" in listing.stdout


def test_ambient_secret_scan_runs_at_commit(plain):
    if shutil.which("trufflehog") is None:
        pytest.skip("trufflehog not installed")
    sh(plain, "git", "config", "review.ambient", "true")
    (plain / "conf.txt").write_text("token=demo1234-" + "0123456789abcdef\n")  # split so this file holds no token
    sh(plain, "git", "add", "conf.txt")
    p = sh(plain, "git", "commit", "-qm", "c", check=False, env={"REVIEW_SKIP": "test"})
    assert p.returncode != 0 and "Nightscout" in p.stdout + p.stderr


def test_global_hooks_run_the_repositorys_own_hook_and_honour_opt_out(plain):
    own = plain / ".git" / "hooks" / "pre-commit"
    own.write_text("#!/bin/sh\necho own-hook-ran; exit 1\n", newline="\n")
    own.chmod(0o755)
    sh(plain, "git", "config", "review.ambient", "false")
    stage_change(plain)
    p = sh(plain, "git", "commit", "-qm", "c", check=False)
    assert p.returncode != 0 and "own-hook-ran" in p.stdout + p.stderr
    own.unlink()
    assert sh(plain, "git", "commit", "-qm", "c", check=False).returncode == 0  # opted out of review


def test_content_already_upstream_needs_no_record(plain, tmp_path):
    up = tmp_path / "up"
    sh(tmp_path, "git", "clone", "-q", str(plain), str(up))
    (up / "app.py").write_text("def f():\n    return 5\n")
    sh(up, "git", "rm", "-q", "notes.md")
    sh(up, "git", "commit", "-qam", "upstream work", "--no-verify")
    sh(plain, "git", "remote", "add", "upstream", str(up))
    sh(plain, "git", "fetch", "-q", "upstream")
    sh(plain, "git", "merge", "-q", "--no-commit", "--no-ff", "upstream/main")
    assert ambient(plain, "check").returncode == 0, "upstream's own changes must not need a record"
    (plain / "mine.py").write_text("x = 1\n")
    sh(plain, "git", "add", "mine.py")
    p = ambient(plain, "check")
    assert p.returncode == 1 and "unreviewed: mine.py" in p.stdout and "app.py" not in p.stdout


@pytest.mark.skipif(shutil.which("biome") is None, reason="biome not installed")
def test_ambient_lints_with_the_kits_config_where_the_repo_has_none(plain):
    (plain / "new.js").write_text("const unused = 1;\ndebugger;\n")
    sh(plain, "git", "add", "new.js")
    p = ambient(plain, "checks")
    assert "fail     lint (new findings)" in p.stdout, p.stdout


@NO_SYMLINKS
@pytest.mark.skipif(not (shutil.which("semgrep") and shutil.which("trufflehog")), reason="scanners not installed")
def test_checks_skip_symlinks(repo):
    (repo / "link.py").symlink_to("app.py")
    sh(repo, "git", "add", "link.py")
    p = review(repo, "checks")
    assert "fail     semgrep" not in p.stdout and "fail     secrets" not in p.stdout, p.stdout


def test_own_pushed_branch_is_not_upstream(plain, tmp_path):
    remote = tmp_path / "remote.git"
    sh(tmp_path, "git", "init", "-q", "--bare", str(remote))
    sh(plain, "git", "remote", "add", "origin", str(remote))
    sh(plain, "git", "push", "-q", "--no-verify", "origin", "main")
    sh(plain, "git", "remote", "set-head", "origin", "main")
    sh(plain, "git", "checkout", "-qb", "feat")
    stage_change(plain)
    sh(plain, "git", "commit", "-qm", "unreviewed", "--no-verify")
    sh(plain, "git", "push", "-q", "--no-verify", "origin", "feat")
    sh(plain, "git", "checkout", "-q", "main")
    sh(plain, "git", "merge", "-q", "--no-commit", "--no-ff", "feat")
    assert ambient(plain, "check").returncode == 1, "a branch the developer pushed is not upstream"


def test_old_upstream_content_does_not_excuse_a_revert_or_deletion(plain, tmp_path):
    old = sh(plain, "git", "rev-parse", "HEAD").stdout.strip()
    up = tmp_path / "up"
    sh(tmp_path, "git", "clone", "-q", str(plain), str(up))
    sh(plain, "git", "remote", "add", "upstream", str(up))
    sh(plain, "git", "fetch", "-q", "upstream")
    (plain / "app.py").write_text("def f():\n    return 2\n")  # a fix, after upstream's last commit
    (plain / "later.py").write_text("x = 1\n")
    sh(plain, "git", "add", ".")
    sh(plain, "git", "commit", "-qm", "fix", "--no-verify")
    fix = sh(plain, "git", "rev-parse", "HEAD").stdout.strip()
    sh(plain, "git", "checkout", "-q", old, "--", "app.py")  # back to upstream's old content
    sh(plain, "git", "commit", "-qm", "revert", "--no-verify")
    p = ambient(plain, "check", "--base", fix)
    assert p.returncode == 1 and "app.py" in p.stdout, "a revert to old upstream content is new work"
    # upstream moves on without ever having later.py; deleting it in the merge is the fork's own act
    (up / "other.txt").write_text("u\n")
    sh(up, "git", "add", ".")
    sh(up, "git", "commit", "-qm", "upstream", "--no-verify")
    sh(plain, "git", "fetch", "-q", "upstream")
    sh(plain, "git", "merge", "-q", "--no-commit", "--no-ff", "upstream/main")
    sh(plain, "git", "rm", "-q", "later.py")
    p = ambient(plain, "check")
    assert p.returncode == 1 and "later.py" in p.stdout and "other.txt" not in p.stdout, p.stdout


def test_kit_repo_with_another_hook_framework_still_runs_the_gate(repo, tmp_path):
    if shutil.which("lefthook") is None:
        pytest.skip("lefthook not installed")
    sh(tmp_path, "git", "config", "--global", "core.hooksPath", os.path.join(KIT, "global-hooks"))
    own = repo / ".git" / "hooks" / "pre-commit"
    own.write_text("#!/bin/sh\nexit 0\n", newline="\n")  # as the pre-commit framework would leave it
    own.chmod(0o755)
    stage_change(repo)
    p = sh(repo, "git", "commit", "-qm", "c", check=False)
    assert p.returncode != 0 and "unreviewed: app.py" in p.stdout + p.stderr


def test_other_hooks_and_push_input_reach_the_repositorys_own_hooks(plain, tmp_path):
    hooks = plain / ".git" / "hooks"
    (hooks / "post-checkout").write_text("#!/bin/sh\ntouch \"$(git rev-parse --git-dir)/checkout-ran\"\n",
                                         newline="\n")
    (hooks / "pre-push").write_text("#!/bin/sh\ncat > \"$(git rev-parse --git-dir)/push-input\"\n", newline="\n")
    for h in ("post-checkout", "pre-push"):
        (hooks / h).chmod(0o755)
    sh(plain, "git", "checkout", "-qb", "other")
    assert (plain / ".git" / "checkout-ran").exists()
    remote = tmp_path / "remote.git"
    sh(tmp_path, "git", "init", "-q", "--bare", str(remote))
    sh(plain, "git", "remote", "add", "origin", str(remote))
    sh(plain, "git", "config", "review.ambient", "false")
    sh(plain, "git", "push", "-q", "origin", "other")
    assert "refs/heads/other" in (plain / ".git" / "push-input").read_text()


def test_command_line_opt_out_does_not_switch_the_gate_off(plain):
    stage_change(plain)
    p = sh(plain, "git", "-c", "review.ambient=false", "commit", "-qm", "c", check=False)
    assert p.returncode != 0 and "unreviewed: app.py" in p.stdout + p.stderr


def test_merge_that_drops_the_forks_own_change_needs_a_record(plain, tmp_path):
    up = tmp_path / "up"
    sh(tmp_path, "git", "clone", "-q", str(plain), str(up))
    (plain / "app.py").write_text("def f():\n    return 3  # fork safety fix\n")
    sh(plain, "git", "commit", "-qam", "fork fix", "--no-verify")
    (up / "other.txt").write_text("u\n")
    sh(up, "git", "add", ".")
    sh(up, "git", "commit", "-qm", "upstream", "--no-verify")
    sh(plain, "git", "remote", "add", "upstream", str(up))
    sh(plain, "git", "fetch", "-q", "upstream")
    sh(plain, "git", "merge", "-q", "--no-commit", "--no-ff", "upstream/main")
    sh(plain, "git", "checkout", "-q", "upstream/main", "--", "app.py")  # as `-X theirs` would
    sh(plain, "git", "rm", "-q", "notes.md")  # upstream kept it; the fork drops it in the merge
    p = ambient(plain, "check")
    assert p.returncode == 1 and "app.py" in p.stdout and "notes.md" in p.stdout, p.stdout
    assert "other.txt" not in p.stdout


def test_kit_repo_whose_lefthook_config_does_not_run_the_kit_is_refused(repo, tmp_path):
    sh(tmp_path, "git", "config", "--global", "core.hooksPath", os.path.join(KIT, "global-hooks"))
    (repo / "lefthook.yml").write_text("pre-commit:\n  commands:\n    own:\n      run: 'true'\n")
    own = repo / ".git" / "hooks" / "pre-commit"
    own.write_text("#!/bin/sh\n# lefthook\nexit 0\n", newline="\n")
    own.chmod(0o755)
    stage_change(repo)
    p = sh(repo, "git", "commit", "-qm", "c", check=False)
    assert p.returncode != 0 and "kit-proposed" in p.stdout + p.stderr


def test_origin_is_not_upstream_unless_named(plain, tmp_path):
    remote = tmp_path / "remote.git"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(remote))
    sh(plain, "git", "remote", "add", "origin", str(remote))
    sh(plain, "git", "push", "-q", "--no-verify", "origin", "main")
    other = tmp_path / "other"
    sh(tmp_path, "git", "clone", "-q", str(remote), str(other))
    (other / "app.py").write_text("def f():\n    return 7\n")  # reached origin outside the gate
    sh(other, "git", "commit", "-qam", "web edit", "--no-verify")
    sh(other, "git", "push", "-q", "--no-verify", "origin", "main")
    sh(plain, "git", "fetch", "-q", "origin")
    sh(plain, "git", "remote", "set-head", "origin", "main")
    (plain / "notes.md").write_text("local\n")
    sh(plain, "git", "commit", "-qam", "local", "--no-verify")
    sh(plain, "git", "merge", "-q", "--no-commit", "--no-ff", "origin/main")
    assert "app.py" in ambient(plain, "check").stdout
    sh(plain, "git", "config", "review.upstream", "refs/remotes/origin/main")
    assert "app.py" not in ambient(plain, "check").stdout


def test_upstream_setting_cannot_name_a_local_branch_or_come_from_the_command_line(plain):
    stage_change(plain)
    sh(plain, "git", "commit", "-qm", "unreviewed", "--no-verify")
    sh(plain, "git", "config", "review.upstream", "refs/heads/main")
    assert ambient(plain, "check", "--base", "HEAD~1").returncode == 1
    sh(plain, "git", "config", "--unset", "review.upstream")
    env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "review.upstream", "GIT_CONFIG_VALUE_0": "HEAD"}
    assert ambient(plain, "check", "--base", "HEAD~1", env=env).returncode == 1


def test_command_line_upstream_is_ignored(plain, tmp_path):
    remote = tmp_path / "remote.git"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(remote))
    sh(plain, "git", "remote", "add", "origin", str(remote))
    sh(plain, "git", "push", "-q", "--no-verify", "origin", "main")
    sh(plain, "git", "checkout", "-qb", "feat")
    stage_change(plain)
    sh(plain, "git", "commit", "-qm", "unreviewed", "--no-verify")
    sh(plain, "git", "push", "-q", "--no-verify", "origin", "feat")
    sh(plain, "git", "checkout", "-q", "main")
    sh(plain, "git", "merge", "-q", "--no-commit", "--no-ff", "feat")
    env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "review.upstream",
           "GIT_CONFIG_VALUE_0": "refs/remotes/origin/feat"}
    assert ambient(plain, "check", env=env).returncode == 1


@NO_SYMLINKS
def test_status_and_signoff_read_a_symlink_as_git_stores_it(repo):
    (repo / "link.py").symlink_to("app.py")
    sh(repo, "git", "add", "link.py")
    review(repo, "checks")
    rid = os.path.basename(record(repo).stdout.strip())[:-3]
    assert "changed  link.py" not in review(repo, "status").stdout
    assert review(repo, "signoff", rid, "--by", "T").returncode == 0
    (repo / "link.py").unlink()
    (repo / "link.py").symlink_to("missing.py")  # repointed at nothing after the review
    assert "changed  link.py" in review(repo, "status").stdout


# The assistant guard, cross-platform details and the complexity rules.

GUARD = os.path.join(KIT, "kit", "guard.py")


def guard(tool, **tool_input):
    event = json.dumps({"tool_name": tool, "tool_input": tool_input})
    return subprocess.run([PY, GUARD], input=event, capture_output=True, **UTF8).returncode


@pytest.mark.parametrize("command", [
    'git commit -m "x" --no-verify', "git commit -nm wip", "git push --no-ver origin main",
    "LEFTHOOK=0 git commit -m x", "LEFTHOOK=false git commit -m x", "HUSKY=0 git commit -m x",
    "HOME=/tmp/x git commit -m x", "env -i git commit -m x", "export HOME=/tmp/x",
    "git config core.hooksPath /tmp", "git -c core.hooksPath=/dev/null commit -m x",
    'git -c core.hooksPath=/dev/null commit -m "config --get"', "git config review.ambient false",
    "git config --get review.ambient\ngit config review.ambient false",
    "python3 ~/os-aid-review-kit/kit/global.py off", "REVIEW_TIERS=/tmp/t.toml git commit -m x",
    "python3 .review/bin/review.py signoff 20261120-1 --by Tim", "rm .git/hooks/pre-commit",
    "printf '[review]\\nambient = false\\n' >> .git/config", "sed -i '' 's/pending/approved/' .review/records/a.md",
    "echo 'pre-commit: {skip: true}' > lefthook-local.yml", "cp /tmp/s.json ~/.claude/settings.json",
    "cd repo && git commit --no-verify -m x", "(git commit --no-verify -m x)", "{ git commit -n -m x; }",
    "bash -c 'git commit --no-verify -m x'", "sh -c \"LEFTHOOK=0 git commit -m x\"", "eval git commit --no-verify",
    'git commit -m "Fix X (closes #3)" --no-verify', 'git commit -m "a; b" --no-verify', 'git commit -m "a & b" -n',
    'git commit -m "x|y" --no-verify', 'git commit -m "$(printf x)" --no-verify',
    "git -c alias.c='commit --no-verify' c -m x", "git config alias.ci 'commit -n'", "unset HOME; git commit -m x",
])
def test_guard_refuses_ways_round_the_gate(command):
    assert guard("Bash", command=command) == 2


@pytest.mark.parametrize("command", [
    'git commit -am "fix n"', "git commit -m ok", "git status && git commit -m ok",
    "git config --global --get core.hooksPath", "python3 .review/bin/review.py status", "git push origin main",
    'git commit -m "Refuse --no-verify in the guard"', "wget --no-verbose https://example.org/x",
    "cat .git/hooks/pre-commit", "ls .git/hooks", 'git add kit/review.py && git commit -m "fix signoff"',
    "git add .review/records/a.md", 'git commit -m "Set core.hooksPath from global.py"',
    "git commit -F - <<'EOF'\nBlock core.hooksPath and --no-verify\nEOF",
])
def test_guard_allows_ordinary_work(command):
    assert guard("Bash", command=command) == 0


def test_guard_reads_powershell_variables():
    assert guard("PowerShell", command="$env:LEFTHOOK = '0'; git commit -m x") == 2
    assert guard("PowerShell", command="git commit -m x") == 0


def test_guard_protects_records_from_hand_edits():
    assert guard("Edit", file_path="/r/.review/records/20261120-1.md") == 2
    assert guard("Write", file_path="C:\\Users\\t\\.review-ledger\\repos\\x\\records\\a.md") == 2
    assert guard("Edit", file_path="/r/app.py") == 0
    assert subprocess.run([PY, GUARD], input="not json", capture_output=True, **UTF8).returncode == 2
    assert guard("Bash", command=None) == 0 and guard("Edit", file_path="/r/.git/config") == 2
    bad = json.dumps({"tool_name": "Bash", "tool_input": "git commit --no-verify"})
    assert subprocess.run([PY, GUARD], input=bad, capture_output=True, **UTF8).returncode == 2


def test_global_on_adds_the_guard_to_claude_settings_and_off_removes_it(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"model": "x", "hooks": {"Stop": [1]}}))
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    run = [PY, os.path.join(KIT, "kit", "global.py")]
    for _ in range(2):  # a second run must not add a second entry
        subprocess.run([*run, "on", "--claude"], env=env, check=True, capture_output=True)
    s = json.loads((home / ".claude" / "settings.json").read_text())
    assert s["model"] == "x" and s["hooks"]["Stop"] == [1]
    assert len(s["hooks"]["PreToolUse"]) == 1 and "guard.py" in json.dumps(s["hooks"]["PreToolUse"])
    own = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 ~/my/guard.py"}]}
    s["hooks"]["PreToolUse"].append(own)  # a guard.py of the user's own must survive
    (home / ".claude" / "settings.json").write_text(json.dumps(s))
    subprocess.run([*run, "off"], env=env, check=False, capture_output=True)
    subprocess.run([*run, "on", "--claude"], env=env, check=True, capture_output=True)
    subprocess.run([*run, "off"], env=env, check=True, capture_output=True)
    s = json.loads((home / ".claude" / "settings.json").read_text())
    assert s["hooks"]["PreToolUse"] == [own] and s["model"] == "x"


@pytest.mark.skipif(sys.platform == "win32", reason="no executable bit on Windows")
def test_global_on_restores_the_executable_bit(tmp_path):
    kit = tmp_path / "kit"
    shutil.copytree(KIT, kit, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    for f in (kit / "global-hooks").iterdir():
        f.chmod(0o644)  # as an unzip can leave them
    env = {**os.environ, "HOME": str(tmp_path / "home")}
    subprocess.run([PY, str(kit / "kit" / "global.py"), "on"], env=env, check=True, capture_output=True)
    assert all(os.access(f, os.X_OK) for f in (kit / "global-hooks").iterdir())


def test_global_hooks_are_plain_files_not_symlinks():
    hooks = os.path.join(KIT, "global-hooks")
    names = os.listdir(hooks)
    assert "pre-commit" in names and "pre-push" in names and "post-checkout" in names
    assert not any(os.path.islink(os.path.join(hooks, n)) for n in names)
    for n in names:
        with open(os.path.join(hooks, n), "rb") as f:
            assert b"\r\n" not in f.read(), f"{n} has CRLF endings"


def test_ledger_directory_stays_inside_the_ledger_for_any_path(tmp_path):
    sys.path.insert(0, os.path.join(KIT, "kit"))
    import review
    review.LEDGER = str(tmp_path / "ledger")
    outside = os.path.abspath("/opt/src/x")
    drive = os.path.splitdrive(outside)[0].rstrip(":") or "root"
    # the one place inside the home folder that would collide if the two kinds were not kept apart
    inside = os.path.join(os.path.expanduser("~"), "abs", drive, "opt", "src", "x")
    dirs = [review.ambient_dir(t) for t in (outside, inside)]
    assert all(d.startswith(str(tmp_path / "ledger")) for d in dirs)
    assert dirs[0] != dirs[1], "a path outside the home folder must not share a ledger with one inside it"


def test_sarif_paths_are_cut_at_the_root_whatever_the_separator():
    sys.path.insert(0, os.path.join(KIT, "kit"))
    import lintgate
    root = "C:\\Users\\runner\\AppData\\Local\\Temp\\tmpab12"
    doc = {"runs": [{"results": [{"ruleId": "F401", "message": {"text": "unused"}, "locations": [
        {"physicalLocation": {"artifactLocation": {"uri": "file:///C:/Users/runner/AppData/Local/Temp/tmpab12/src/a%20b.py"},
                              "region": {"startLine": 3}}}]}]}]}
    raw = {"ruleId": "F401", "message": {"text": "unused"}, "locations": [{"physicalLocation": {
        "artifactLocation": {"uri": root + "\\src\\c.py"}, "region": {"startLine": 1}}}]}
    doc["runs"][0]["results"].append(raw)  # some Windows tools print a bare path with backslashes
    f, g = lintgate.parse_sarif(json.dumps(doc), root)
    assert f["path"] == "src/a b.py" and f["line"] == 3 and g["path"] == "src/c.py"


def test_ambient_python_gets_complexity_rules_only_without_its_own_ruff_config(plain):
    branchy = "def f(x):\n" + "".join(f"    if x == {i}:\n        return {i}\n" for i in range(12)) + "    return -1\n"
    (plain / "branchy.py").write_text(branchy)
    sh(plain, "git", "add", "branchy.py")
    p = ambient(plain, "checks")
    assert "fail     lint (new findings)" in p.stdout, p.stdout
    (plain / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["F"]\n')
    sh(plain, "git", "add", "pyproject.toml")
    p = ambient(plain, "checks")
    assert "pass     lint (new findings)" in p.stdout, p.stdout


def test_removing_a_ruff_config_does_not_make_inherited_complexity_new(plain):
    branchy = "def f(x):\n" + "".join(f"    if x == {i}:\n        return {i}\n" for i in range(12)) + "    return -1\n"
    (plain / "branchy.py").write_text(branchy)
    (plain / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["F"]\n')
    sh(plain, "git", "add", ".")
    sh(plain, "git", "commit", "-qm", "own ruff config", "--no-verify")
    (plain / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (plain / "branchy.py").write_text(branchy.replace("return -1", "return -2"))
    sh(plain, "git", "add", ".")
    p = ambient(plain, "checks")
    assert "pass     lint (new findings)" in p.stdout, p.stdout


def test_trailers_keep_a_non_ascii_commit_message_intact(repo):
    stage_change(repo)
    review(repo, "checks")
    record(repo)
    msg = repo / "msg.txt"
    msg.write_bytes("Café ≥ 3.9 mmol/L\n".encode())
    review(repo, "trailers", str(msg))
    text = msg.read_bytes().decode("utf-8")
    assert text.startswith("Café ≥ 3.9 mmol/L") and "Review-Record:" in text
