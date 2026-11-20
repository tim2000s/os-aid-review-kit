<!-- review-kit:start (managed by os-aid-review-kit; edit the kit, not this block) -->
## Code review and quality gate

Every change to a file outside `.review/tiers.toml`'s exempt list is reviewed and recorded before it
is committed. The record names each file with the hash of the exact content reviewed, which is what
lets a human reviewer later see what was checked and whether it has changed since.

1. Make the change, then run the formatter and linter for each language touched. `lefthook` runs
   them on commit; running them first saves a failed commit.
2. Stage the change and run `python3 .review/bin/review.py checks`. It runs the lint gate (new
   findings only), the secret scan, the kit's Semgrep rules and the tests named in
   `.review/tiers.toml`, and remembers the results against the staged content.
3. Get a fresh-context review of the staged diff: a reviewer that did not write the change, given the
   diff and this repository's `REVIEW.md`, not the reasoning that produced the change. Label each
   finding important (fix before commit), nit or pre-existing. Fix every important finding, re-stage,
   and run step 2 again.
4. Write, in plain words, what the change does, why, and what could go wrong, then record it:
   `python3 .review/bin/review.py record --author <who or what wrote it> --reviewer "<reviewer>, fresh
   context" --explanation <file> --findings <json file> [--way-back "<how to undo>"]`. A high-tier
   change needs every check passing and a way back.
5. Commit. The record file is staged with the code, and the commit message gets `Review-Record`,
   `Review-Tier`, `Written-By`, `Reviewed-By` and `Human-Review: pending` trailers.

`python3 .review/bin/review.py status` lists what awaits a human, highest tier first. A human
reviewer signs off with `review.py signoff <id> --by <name>`.

Rules that hold whatever the tool says:

- Never disable a linter rule (`// noinspection`, `# noqa`, `@Suppress`, `// swiftlint:disable`,
  `// biome-ignore`) without a comment on the same line saying why.
- Never add a third-party dependency without checking that the version is current and maintained,
  and updating the lockfile in the same change.
- Never bypass the hooks (`--no-verify`, `LEFTHOOK=0`). `REVIEW_SKIP="<reason>"` exists for a
  work-in-progress commit; it is written into the commit trailers, and `git push` still refuses until
  the files are recorded.
- A change's tier comes from what it touches (`review.py tier`), never from who wrote it. Where it is
  unclear, it is the higher tier.
<!-- review-kit:end -->
