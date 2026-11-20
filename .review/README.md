# Review records

Each file in `records/` describes one reviewed change. It lists every file the review covered with
the git blob hash of the content that was reviewed, the tier, who or what wrote the change, who or
what reviewed it, the checks that ran and their results, the review findings with what was done
about each, the author's account of the change, and the human review.

For a human reviewer, the useful commands are these, run from the repository root:

    python3 .review/bin/review.py status            # records awaiting a human, highest tier first,
                                                    # and files that changed after their review
    python3 .review/bin/review.py pr-body --base main   # summary of the records behind a branch
    python3 .review/bin/review.py signoff <id> --by "Your Name" [--verdict changes-requested] [--notes "..."]

`git hash-object <file>` gives a file's current blob hash. If it matches the hash in a record, the
file on disk is the file that was reviewed. If it does not, `status` says so, and a sign-off is
refused unless forced, since it would otherwise describe content nobody reviewed.

`tiers.toml` decides each file's tier. `bin/` holds the two scripts; they need Python 3.11 or newer
and nothing outside the standard library.
