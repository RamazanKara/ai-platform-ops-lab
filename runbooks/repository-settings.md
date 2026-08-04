# GitHub repository settings

Repository-host controls are declared in `.github/repository-settings.json`. Audit them with an
authenticated GitHub CLI session:

```bash
python3 scripts/github-settings.py
```

The command is read-only by default and exits non-zero on drift. A repository owner can apply the
declared settings explicitly:

```bash
python3 scripts/github-settings.py --apply
```

The apply mode enables private vulnerability reporting, vulnerability alerts, automated security
updates, secret scanning and push protection; enables Discussions; normalizes merge behavior; and
protects `main` with required CI checks and review/conversation rules, publishes project topics and
the docs homepage, and creates a reviewer-gated `pypi` environment restricted to `v*` tags. GitHub plan or organization
policies can reject features unavailable to the repository; do not weaken the declaration to hide
that failure. Record the platform limitation and remediate it at the account level.

Run the audit after renaming CI jobs because required-check context names are exact. Never run
`--apply` against a fork or mirror without first reviewing the resolved repository printed by
`gh repo view`.

## Why `enforce_admins` is false

The declaration sets `enforce_admins: false` deliberately. GitHub does not allow approving your
own pull request, so `required_approving_review_count: 1` cannot be satisfied by a sole
maintainer. With admin enforcement on, cutting a release meant temporarily deleting a
branch-protection control and restoring it afterwards, which turns a bypass into routine
procedure and is worse than not claiming the control.

What still holds for everyone, maintainer included: all ten required status checks must pass,
conversations must be resolved, force pushes and deletions stay blocked, and a contributor's
pull request still needs a review. What changed is only that the maintainer can merge their own
reviewed work without a second account.

Revisit this the moment the project has more than one maintainer with write access; at that
point the review requirement becomes satisfiable and `enforce_admins` should go back to true.
