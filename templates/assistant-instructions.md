<!-- review-kit:start (managed by os-aid-review-kit; edit the kit, not this block) -->
## Reviewing code before it is committed

Every commit and push on this machine goes through the review gate in `~/os-aid-review-kit`. Run its
script as `sh ~/os-aid-review-kit/kit/py ~/os-aid-review-kit/kit/review.py <command>`, which works on
every system; in a repository with the kit committed, `.review/bin/review.py` is the same script.

1. Make the smallest change that does the job. Stage it and run `review.py checks`.
2. Have the staged diff reviewed by a fresh session that did not write it, given the diff and the
   repository's REVIEW.md where there is one. Label each finding important, nit or pre-existing, fix
   every important one, re-stage and run the checks again. An oversized change is an important
   finding: an abstraction nobody asked for, a reimplemented helper, dead or speculative code, or
   handling for cases that cannot occur.
3. Write what the change does, why, and what could go wrong to a file, then run `review.py record
   --author <model> --reviewer "<model>, fresh context" --explanation <file> --findings <json file>`,
   adding `--way-back "<how to undo it>"` for high-tier code.
4. Commit. The hooks refuse a commit whose files are in no record, and add the review details to
   the commit message.
5. For a pull request, write its description with
   `review.py pr-body --base <branch> | gh pr create --body-file -`. In a repository without the kit committed the
   records stay on this machine, so the description is the only evidence the maintainer sees.

Never skip or switch off the hooks (`--no-verify`, `git commit -n`, `core.hooksPath`, `LEFTHOOK=0`),
never edit a review record by hand, and never run `review.py signoff`, which is for the person who
reads the code. A guard refuses these; when it does, follow the steps above or tell the user what
was refused and why. `REVIEW_SKIP="<reason>"` marks a work-in-progress commit, and the push then
waits until the files have a record.
<!-- review-kit:end -->
