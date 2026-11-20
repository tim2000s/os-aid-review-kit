# os-aid-review-kit

A quality gate and review ledger for open-source automated insulin delivery repositories, written for
people who use AI models to write code and want to show that what they submit was checked.

AndroidAPS stopped accepting AI-generated pull requests on 15 June 2026 (`CONTRIBUTING.md`,
d7208d8837) for two stated reasons. Reviewing such code takes maintainers longer than the model
took to write it, and unverified code is a risk the project will not accept. The kit is built
against both. It moves the checking to the contributor, and it records what was checked in a form a
maintainer can confirm in minutes: every reviewed file is named by the git blob hash of the content
that was reviewed, so whether the code in a pull request is the code that was reviewed is a lookup
rather than a judgement.

## What it installs

`python3 kit/install.py <repo> [--hooks] [--accept-existing-cves] [--dry-run]` audits the repository
for Kotlin, Swift, Python and JavaScript and adds configuration only for what it finds:

| Layer | Tools | Files |
|---|---|---|
| Lint and format | ktlint and detekt, SwiftLint and SwiftFormat, Ruff and mypy, Biome | `detekt.yml`, `.swiftlint.yml`, `.swiftformat`, `ruff.toml`, `mypy.ini`, `biome.json` |
| Hooks | Lefthook | `lefthook.yml` |
| Static analysis | Semgrep (registry rules plus the kit's own) | `.semgrep/custom-ai-guardrails.yml` |
| Dependencies | Trivy, with a CycloneDX SBOM in CI | `trivy.yaml`, `.trivyignore` |
| Secrets | TruffleHog, with detectors for Nightscout tokens and API secrets | `trufflehog.yml`, `.gitignore` lines |
| CI | GitHub Actions; PR-Agent advisory review; optional SonarQube | `.github/workflows/quality-gate.yml`, `.pr_agent.toml`, `sonar-project.properties` |
| Code graph for agents | CodeGraphContext over MCP | `.mcp.json` |
| Agent instructions | | a marked block in `AGENTS.md`, with `CLAUDE.md` importing it |
| Review records | `review.py`, `lintgate.py` | `.review/` |

It never overwrites a file. Where one exists and differs, the kit's version is written beside it as
`<name>.kit-proposed`. The kit's own scripts in `.review/bin/` and the marked block in `AGENTS.md`
are the exceptions, and are refreshed on each run.

## Every repository on a machine

`python3 kit/global.py on` points git's global `core.hooksPath` at `global-hooks/`, so the gate runs
in every repository on the machine without anything being added to them. The dispatcher there runs a
repository's own hook first (a lefthook or pre-commit install in `.git/hooks`, which git stops
running once a global hooks path is set), then the gate:

| Repository | What runs | Where records live |
|---|---|---|
| Kit committed (`.review/bin/review.py`) | its own lefthook configuration | `.review/records/`, committed with the code |
| Anything else | ambient mode: record check and TruffleHog at commit, record check at push | `~/.review-ledger/repos/home/<path>/records/` (or `abs/<drive>/<path>` outside the home folder) |
| Opted out (`git config review.ambient false`) | the repository's own hooks only | none |

Ambient mode exists for forks of other people's projects, where a `.review/` directory would travel
into every pull request, and for the many small repositories that would otherwise each need an
install. In ambient mode, run `python3 ~/os-aid-review-kit/kit/review.py` in place of
`.review/bin/review.py`. The first time it runs in a repository, it copies the matching tier preset to
`tiers.toml` in that repository's ledger directory, to be edited there. Linters without a configuration
in the repository use the kit's templates. Each record also notes the checkout path and the origin. The
ledger is a git repository of its own, committed on every record and sign-off; it has no remote until
one is added. `review.py status --all` lists every pending ambient record on the machine, and commits
carry a `Review-Ledger` trailer naming the directory.

Both modes leave out content that came from upstream through a merge, so syncing a fork does not
demand a record for code the change did not write. Upstream is `git config review.upstream <ref>`
(repeatable) where set, else the default branch of each remote other than `origin`; the
developer's own pushed branches never count, and nor does origin unless named, since in a repository
that is not a fork origin is the developer's own. For each upstream branch the gate takes the point
the tip has merged from it and ignores that point when the base already contained it, so old
upstream content cannot excuse a revert. A path is left out only when the fork never changed it
(the same at the base as where fork and upstream diverged) and the tip now matches upstream, so
taking upstream's side overrides none of the fork's own work. A plain commit is never exempt, and a
file a merge produces that matches neither side still needs a record.

The memory repositories, which commit on a timer, are opted out. A repository that sets its own
`core.hooksPath` (husky does) overrides the global one and is not covered; `global.py on --scan DIR`
lists any. lefthook refuses to install under a global hooks path, so `install.py --hooks` reports
the repository as covered by the dispatcher, which runs `lefthook run` itself.

## How the gate treats existing code

An established repository carries findings from before any of these tools ran on it. Linting a
touched file whole would block every change on problems its author did not make, and contributors
would learn to bypass the hooks. So in `adopt` mode (the default) `lintgate.py` lints the version of
each changed file before the change and after it, and fails only where a rule's count rose. Semgrep
runs with `--baseline-commit` for the same reason, and `--accept-existing-cves` lists the CVEs
already in the lockfile in `.trivyignore`, dated, so that only new ones fail. A file the change adds
has no earlier version, and every finding in it counts. `greenfield` mode in `.review/tiers.toml`
makes every finding in a changed file count.

## Review records

Each change to a file that `tiers.toml` does not exempt needs a record before it can be committed.
The sequence is `review.py checks`, a review of the staged diff by a model or person who did not
write it, then `review.py record`. The record holds:

- each file with its blob hash, its tier and why it has that tier
- who or what wrote the change, and who or what reviewed it
- the checks that ran and their results
- each review finding with its label (important, nit or pre-existing) and what was done about it
- the author's own account of what the change does and what could go wrong
- the way back, required for high tier, and the human review, which starts as pending

An important finding that was not fixed, a high-tier change with a failing check, and a high-tier
change without a way back are all refused. Commits carry `Review-Record`, `Review-Tier`,
`Written-By`, `Reviewed-By` and `Human-Review` trailers. `review.py status` shows what awaits a
person; `review.py signoff` records their verdict and refuses when the file on disk has changed
since the review.

Tiers come from what a file can affect, never from who wrote it. The presets in `templates/tiers/`
put the dose path, limits, pump and sensor drivers, treatment and glucose data handling, and the
wiring between them in the high tier for AndroidAPS, Trio and Nightscout.

## Tests

`pytest -q tests/` builds throwaway repositories, installs the kit and runs the real tools (108
tests; the tests for each behaviour were run against a deliberately broken version of it and failed
there). The Semgrep rules have annotated targets in `tests/semgrep_targets/`; run them with
`semgrep --test` on a directory holding the rule file and the targets together.

## Requirements

Python 3.11 or newer for the scripts, which use only the standard library, and Git. `kit/py` finds
the interpreter (`python3`, `python` or `py -3`), so the hooks work where Windows provides no
`python3`. The tools, by system:

| System | Install |
|---|---|
| macOS | `brew install lefthook ktlint detekt swiftlint swiftformat trufflehog semgrep trivy biome mypy ruff` |
| Linux | Homebrew from brew.sh, then the macOS line without `swiftlint swiftformat` |
| Windows | Git for Windows (its `sh` runs the hooks), Python and Node.js; `py -m pip install ruff mypy semgrep`; `npm install -g @biomejs/biome lefthook`; TruffleHog, and for Kotlin ktlint and detekt-cli, from their release pages |

`uv tool install --python 3.12 codegraphcontext==0.6.13` adds the code graph. `kit-tests.yml` runs
the suite on all three systems with the real tools.

## The assistant guard

`global.py on --claude` adds `kit/guard.py` to Claude Code as a PreToolUse hook. It refuses a Bash
command that would skip or switch off the gate (`--no-verify` in any abbreviation, `git commit -n`,
`core.hooksPath`, `LEFTHOOK=0`, the opt-out and upstream settings, `global.py off`, environment
overrides), a `review.py signoff`, which only the person who read the code may enter, and an edit
made by hand to a record or the ledger. It also refuses shell writes to those files, to `.git/config`,
`~/.gitconfig`, `lefthook-local.yml` and Claude Code's settings, and variables such as `HOME=` and
`LEFTHOOK=false` that run git without the gate. Reading settings with `git config --get` and reading hooks are allowed, and commit
messages and heredoc bodies are not inspected, so text about the gate does not trip it. Every
failure inside the guard exits 2, the only status Claude Code treats as a refusal. The matching is
best-effort. A script written to disk and then run is not inspected, and nor are a few deliberate
evasions found in review: `bash -lc`, `env bash -c`, `sudo sh -c`, a command piped or fed by heredoc
into a shell, `pwsh -c`, `env -u HOME` and PowerShell's `Remove-Item Env:`. Git has no way to refuse
`--no-verify` itself, so a person can still skip the hooks; the guard binds the assistant, which
writes most of the code.

## Size and complexity

Model-written code tends to run long. detekt and SwiftLint check method length and complexity from
their templates, and the Ruff template adds C90 (complexity above 10) and PLR0915 (too many
statements). In ambient mode a Python repository with no Ruff configuration of its own is linted with
`templates/ruff-ambient.toml`, Ruff's defaults plus those two rules. Linters measure size, not need,
so the review instructions in the AGENTS block and the default REVIEW.md make an oversized change an
important finding: an unrequested abstraction, a reimplemented helper, dead or speculative code, or
handling for cases that cannot occur.

## Limits

- PR-Agent and SonarQube need repository secrets (`ANTHROPIC_API_KEY`; `SONAR_TOKEN`,
  `SONAR_HOST_URL` and the variable `SONAR_ENABLED=true`). GitHub withholds secrets from workflows
  triggered by forks, so on a fork's pull request the PR-Agent job skips. Switching it to
  `pull_request_target` would hand the key to the fork's code, and the kit does not.
- SwiftLint in CI runs on a macOS runner from Homebrew and is not version-pinned.
- Semgrep's `--baseline-commit` refuses to run with unstaged changes, so the push hook needs a clean
  working tree.
- A record is an attestation, not a proof. Anyone who can run `review.py` can write one, so the
  coverage check rejects records that break the rules (missing checks, a tier lower than the file's
  tier now, a high tier without passing checks or a way back, a person's request for changes), and
  CI reruns the checks itself.
- On `pull_request`, GitHub runs the pull request's own copy of the workflow, so a pull request can
  edit the CI jobs and the lint, Semgrep and Trivy configuration they read. The review-records job
  reads `review.py` and `tiers.toml` from the base branch, and every gate file is high tier, but the
  protection that holds is branch protection plus a person reading any change to a gate file. A
  CODEOWNERS entry for `.review/`, `.lefthook/`, `lefthook.yml`, `.semgrep/` and `.github/` does that.
- Checks with tests configured refuse to run while the working tree holds anything not being
  committed, untracked files included, because the tests read the whole tree. `git stash push
  --keep-index --include-untracked` clears it for the run.
- The pre-push Semgrep scan measures from HEAD, not from each pushed ref; the review-coverage check
  before it does handle each ref.
- Ambient records live only on this machine until the ledger is given a remote, and the coverage
  check reads whatever the ledger holds, so a record there is not tied to the commit the way a
  committed one is. The commit trailers are the part that travels.
- The upstream exemption trusts the default branch of every remote other than origin. A remote that
  points at the developer's own second fork should be left out by naming the real upstream in
  `review.upstream`. An octopus merge is checked against its first parent only.
- With a global hooks path git runs no hooks from `.git/hooks`. The dispatcher is linked under every
  hook name and runs the repository's own hook first, but `reference-transaction` is
  not linked, and `git lfs install` cannot write its hooks into the kit's directory; a repository
  using LFS sets its own `core.hooksPath` and is then outside the gate.
- A clean `git merge` runs `pre-merge-commit`, not `pre-commit`, so merges are gated at push only.
- The records live in the repository. A pull request to an upstream that does not want `.review/`
  carries the `pr-body` summary in its description instead.
