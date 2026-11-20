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

`pytest -q tests/` builds throwaway repositories, installs the kit and runs the real tools (28
tests, twelve of them written from two rounds of fresh-context review on 20 November 2026; each of
those failed against the version it was written for). The Semgrep rules have annotated targets in `tests/semgrep_targets/`; run them with
`semgrep --test` on a directory holding the rule file and the targets together. Each test was
checked against a deliberately broken implementation and fails on it.

## Requirements

Python 3.11 or newer for the scripts, which use only the standard library. The tools themselves:
`brew install lefthook ktlint detekt swiftlint swiftformat trufflehog semgrep trivy biome mypy ruff`,
and `uv tool install --python 3.12 codegraphcontext==0.6.13` for the code graph.

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
- The records live in the repository. A pull request to an upstream that does not want `.review/`
  carries the `pr-body` summary in its description instead.
