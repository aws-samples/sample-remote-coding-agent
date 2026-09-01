---
name: review-rigor
description: Use when verifying or reviewing an uncommitted change, to judge whether a green test run is evidence for that change rather than for the baseline it was already passing.
---

# Review rigor

A test suite that passes tells you the suite passed. It does not tell you the change
was exercised. Establishing that is the whole job.

## 1. Diff against the base branch

Read the change as a base-branch diff — `git diff <baseBranch>...HEAD` plus
`git status --porcelain` for what is still uncommitted. Never review `git diff HEAD~1`:
a branch is usually more than one commit, an amended or squashed commit moves
`HEAD~1` under you, and the last commit is not the change under review.

## 2. Name the test that executes each hunk

Walk the changed hunks one at a time. For each one, name the specific test — file and
test function — whose execution reaches those lines. Write the pairing down. A hunk
you cannot pair with a test is either untested or dead, and both are findings.

## 3. Run the suite's own declared command

Run what the repository declares — `make test` here — and never a hand-rolled
`pytest` invocation. The declared command exists because the suite may only run from
a prepared virtualenv, with its own `addopts`, plugins and environment; a bare
`pytest` can pass while the real gate fails, or fail for reasons that are yours and
not the change's.

## 4. Judge the evidence, not the exit code

A green suite that never exercised the change is `VERDICT: FAIL`. So is a change
whose only new test asserts nothing, or one whose test would still pass with the
change reverted. If you doubt a test covers a hunk, revert that hunk in a scratch
copy, or break it deliberately, and confirm the named test goes red. A test that
cannot fail is not coverage.

## 5. End with exactly one verdict line

The last line of your reply is exactly one of

```
VERDICT: PASS
VERDICT: FAIL
```

followed by the reason on the same line. Nothing after it. The verdict lives in the
message and never in a file in the tree — a file is writable by the role being
judged, and the point of the verdict is that it is not.
