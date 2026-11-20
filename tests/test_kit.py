"""End-to-end tests: each builds a throwaway git repository, installs the kit and runs the real tools."""
import json
import os
import shutil
import subprocess
import sys

import pytest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def sh(cwd, *cmd, check=True, env=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env={**os.environ, **(env or {})})
    if check and p.returncode != 0:
        raise AssertionError(f"{cmd} failed ({p.returncode}):\n{p.stdout}\n{p.stderr}")
    return p


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
                       capture_output=True, text=True)
    assert p.returncode == 1 and "app.py (in refs/heads/evil)" in p.stdout
    ok = f"refs/heads/main {main} refs/heads/main {main}\n"
    p = subprocess.run([PY, ".review/bin/review.py", "check-push"], cwd=repo, input=ok, capture_output=True, text=True)
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
