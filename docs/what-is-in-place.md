# The review gate on this machine: what is in place

20 November 2026. Kit repository `tim2000s/os-aid-review-kit` (private).

## Purpose

AndroidAPS stopped accepting AI-generated pull requests on 15 June 2026, on the grounds that
reviewing such code costs maintainers more than it cost to write and that unverified code is a risk
the project will not take. The gate described here moves the checking to the person submitting the
code and writes down what was checked, so that a maintainer can confirm it quickly instead of
redoing it. It was built from the handover document "AI Agent Handover: Open-Source Code Quality &
Security Stack Setup" and has been extended so that it applies to every repository on this machine
rather than only to those it was installed in.

## What happens when code is committed

A change is staged, and then:

1. `review.py checks` runs the lint gate, the secret scan, the kit's Semgrep rules and any tests the
   tier file names, and stores the results against the git blob hash of each staged file.
2. A model that did not write the change reviews the staged diff against the repository's REVIEW.md.
   Each finding is labelled important, nit or pre-existing; every important one is fixed and the
   checks are run again.
3. `review.py record` writes the review record. It refuses when the files changed after the checks
   ran, when an important finding was not fixed, or when a high-tier change has a failing check or
   no stated way back.
4. `git commit` runs the hooks. The commit is refused unless every file it changes, in exactly the
   form being committed, appears in a valid record. The commit message then gains trailers naming
   the record, the tier, the author, the reviewer and `Human-Review: pending`. A work-in-progress
   commit can set `REVIEW_SKIP="<reason>"`, which lets it through with a `Review-Skipped` trailer;
   the push then refuses until the files have a record.
5. `git push` checks every ref being pushed, not only the checked-out branch, in the same way.

The lint gate lints each file as it was before the change and as it is after, and fails only where
a rule's count rose. Existing repositories carry years of findings from before any linter ran on
them; this way, those do not block new work, and new findings cannot be passed off as old.

## Where the gate applies

Git's global `core.hooksPath` points at `~/os-aid-review-kit/global-hooks/`, so every commit and
push in every repository passes through the kit's dispatcher. The dispatcher first runs whatever
hook the repository had of its own, and then one of the following.

| Repository | What runs | Where the records are |
|---|---|---|
| Kit committed (os-aid-review-kit; cgm-remote-monitor branch `review-kit`) | the repository's own lefthook configuration | `.review/records/`, committed with the code |
| Any other repository | ambient mode: record check and TruffleHog at commit, record check at push | `~/.review-ledger/repos/<path>/records/` |
| Opted out: timsim-claude-memory, trio-claude-memory, boost-aaps-memory | the repository's own hooks only | none |

Ambient mode exists because most repositories here are forks of other people's projects
(AndroidAPS, Trio, Nightscout), where a `.review/` directory would travel into every pull request
upstream. The records are kept outside the tree instead, in a ledger that is its own git repository
and is committed each time a record is written or signed off. Each repository's tiers come from the
preset that matches its layout (AndroidAPS, Trio, Nightscout or a default), copied into the ledger
the first time and editable there. The three memory repositories are opted out because they commit
on a timer and hold memory files rather than code.

## What a reviewer is given

Each record lists every file it covers with the blob hash of the content that was reviewed, so
whether the code in front of a person is the code that was reviewed is a lookup. The record also
holds:

- the tier of each file and the reason for it
- who or what wrote the change, and who or what reviewed it
- each check and its result
- each review finding with its label and what was done about it
- the author's account of what the change does and what could go wrong
- the way back, and the human review, which starts as pending

The commands a person uses:

| Command | What it does |
|---|---|
| `review.py status` | lists the records awaiting a person, highest tier first, and any file changed since its review |
| `review.py status --all` | the same across every ambient repository on the machine |
| `review.py signoff <id> --by <name>` | records the verdict; refused while the file on disk differs from the reviewed one, unless `--force` is given, which writes the difference into the record |
| `review.py pr-body --base <branch>` | a summary of the records for a pull request description |

In a repository with the kit committed these are run as `python3 .review/bin/review.py`, and
elsewhere as `python3 ~/os-aid-review-kit/kit/review.py`.

## Tiers

A file's tier is set by what it can affect; who wrote it plays no part. High tier covers anything that
can change a dose or a limit, or whether the loop runs, together with the files that configure the
gate itself, so a change cannot loosen the gate and pass through it in the same commit. High-tier
records must show every check passing and state a way back. Exempt files (images, lockfiles,
licences) need no record.

## Code that came from upstream

Syncing a fork with upstream would otherwise demand a record for every file upstream changed.
Content a merge takes from upstream is therefore left out, under a deliberately narrow rule.
Upstream means the default branch of each remote other than `origin`, or refs named in the
repository's own `review.upstream` setting, and only remote-tracking refs are accepted. A file is
left out only when the fork never changed it and the merge result is identical to upstream's
version. A merge that drops one of the fork's own changes, a revert to old upstream content and a
plain commit all still need a record.

## Tools

| Purpose | Tool and version |
|---|---|
| Kotlin | ktlint 1.8.0, detekt 1.23.8 |
| Swift | SwiftLint 0.65.1, SwiftFormat 0.64.7 |
| Python | Ruff 0.17.3, mypy 2.4.1 |
| JavaScript | Biome 2.6.8 |
| Hooks | lefthook 2.2.2 and the kit's dispatcher |
| Static analysis | Semgrep 1.186.0, with eleven kit rules (insecure URLs, hard-coded addresses, SQL interpolation, empty catch blocks, unfinished stubs, Nightscout secrets, inline mmol/L conversion) |
| Dependencies | Trivy 0.76.2, with a CycloneDX SBOM in CI |
| Secrets | TruffleHog 3.100.0, with detectors for Nightscout tokens and API secrets |
| Code graph for agents | CodeGraphContext 0.6.13 over MCP |

A repository with the kit committed also gets a GitHub Actions workflow running the same checks,
an advisory PR-Agent review and an optional SonarQube scan.

## How it was verified

The kit's test suite builds throwaway repositories and runs the real tools against them. It holds
48 tests. Each one was run against a deliberately broken version of the code it covers and failed
there, because a test that cannot fail checks nothing.

Six rounds of fresh-context review were run in all. Three were on the original build and found 8,
4 and 1 important defects. Three were on the machine-wide extension and found 6, 2 and 1. Every one
was fixed and has a regression test. Most of the second set were ways the upstream rule could have
let the developer's own unreviewed code through: a pushed branch counted as upstream, a stale
branch excused a deletion, and a merge could silently undo a fork's own dosing change.

On 20 November, after the global hooks were switched on, an unreviewed commit was attempted in
three real repositories, each in a throwaway worktree: oref-investigations (ambient), Boost-AAPS-core
(ambient, a fork) and cgm-remote-monitor (kit committed). All three were refused, and a commit in an
opted-out memory repository went through. Records written by other sessions were already present in
the ledger that evening, so ambient mode was in use.

## What it does not cover

- A record is an attestation, not a proof. Anyone who can run `review.py` can write one, so the
  coverage check rejects records that break the rules, and CI reruns the checks, but the review a
  record describes is only as good as the reviewer.
- A commit made with `--no-verify` skips the commit hooks, and the push check still catches the
  unrecorded file unless the push uses `--no-verify` too. CLAUDE.md forbids both; nothing on the
  machine can prevent them.
- A repository that sets its own `core.hooksPath` (husky does) is outside the gate. None did when
  this was written.
- A clean `git merge` is checked at push, not at commit.
- On a pull request, GitHub runs the pull request's own copy of the workflow, so a pull request can
  edit its own CI checks. Branch protection, with a CODEOWNERS entry for the gate's files, is the
  control that holds.
- The ledger has no remote, so ambient records exist only on this machine. The commit trailers are
  the part that travels with the code.

## State on 20 November 2026

- Every record awaits human sign-off: those in the kit's own repository, three on the Nightscout
  branch, and those in the ledger.
- The Nightscout `review-kit` branch is local and unpushed.
- The kit's CI workflow has never run on GitHub. PR-Agent needs `ANTHROPIC_API_KEY`, and SonarQube
  needs `SONAR_TOKEN`, `SONAR_HOST_URL` and the variable `SONAR_ENABLED=true`.
- Work committed before the gate was switched on needs a retrospective record before it can be
  pushed (`review.py checks --base <ref>`, then `review.py record --base <ref>`).
- The weekly Trio upstream sync may stop at its push when a merge produces files that match neither
  side. It reports the failure and loses nothing.
- During the build, a real Nightscout read token was put into a test file. It was removed from the
  history before the first push and appears in no other repository, and the kit's TruffleHog
  detector now catches that pattern at commit.

## Way back

`python3 ~/os-aid-review-kit/kit/global.py off` removes the global hooks path, after which every
repository runs its own `.git/hooks` as before. `git config --local review.ambient false` opts a
single ambient-mode repository out. The instructions that require the process are in the "Recording reviews,
in every project" section of `~/.claude/CLAUDE.md`.
