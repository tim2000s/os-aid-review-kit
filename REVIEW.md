# Review checklist

Concerns that recur in this kit. Label findings important, nit or pre-existing.

- Fail-open. Any path where a tool is missing, crashes, prints nothing or prints something
  unparseable must end in a failure or an explicit "not run", never in a pass.
- A record must describe exactly the content that is committed or pushed. Check every place a blob
  hash is taken (index, HEAD, working tree) against what the hook or CI step is guarding.
- The installer must not overwrite a file the kit does not own. Owned files are `.review/bin/*`,
  `.review/README.md` and the marked block in `AGENTS.md`.
- A test written for the kit must fail against a deliberately broken implementation before it is
  trusted.
- Test fixtures and examples use fake credentials. On 20 November 2026 a real Nightscout read token
  went into `tests/semgrep_targets/` and was removed from history before the first push; the kit's
  TruffleHog detector would have caught it at commit, had the kit been installed on itself.
- A linter runs only where the repository has chosen it, since a default style counts code written to
  match its neighbours as new findings. On 25 November 2026 ktlint ran with its own default style on
  Boost-autoconfig-rerun, which has no ktlint setup: 703 findings, about 80 of them counted as new, and
  `review.py checks` formatted two added files away from the repository's style. The test
  `test_ktlint_runs_only_where_the_repository_set_ktlint_up` now catches the first. The kit no longer
  formats anything, since rewriting a file nobody asked to have rewritten changes what was reviewed;
  `test_checks_leave_the_staged_files_as_they_were` catches that returning.
