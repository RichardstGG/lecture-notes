# Project Agent Instructions

These instructions apply to every agent working anywhere in this repository.

## Source of truth and scope

- Treat the current repository source, tests, and Git history as the implementation source of truth.
- Preserve unrelated user or agent changes. Do not reset, stash, delete, or overwrite work you do not own.
- Keep each branch and pull request limited to one coherent task. Do not mix unrelated cleanup, formatting, generated files, recordings, models, outputs, logs, caches, local configuration, or machine-specific paths into a change.
- Treat changes to CLI arguments, exit codes, JSON or TOML schemas, API endpoints, lifecycle phases, filesystem layout, runtime state, configuration keys, and other public interfaces as shared contract changes. Document and test them explicitly.

## Autonomous Pull Request workflow

Use an autonomous Pull Request workflow. During development, continuously evaluate whether the current work has reached a coherent and independently reviewable milestone.

Use this principle:

> One PR = one coherent, reviewable change.

Do not create Pull Requests for trivial, incomplete, or temporary intermediate checkpoints.

When a meaningful milestone is complete, automatically:

1. Review the complete diff and confirm it contains only the intended change.
2. Run all applicable automated tests, lint checks, type checks, builds, and targeted manual validation.
3. Create or use an appropriately named `codex/<task-name>` branch based on the current default branch. Do not build an unnecessary dependency on an unmerged branch.
4. Create focused commits containing only the relevant files and changes.
5. Push the working branch to the remote.
6. Create a GitHub Pull Request targeting the repository's default branch.
7. Report the Pull Request URL, commits, behavior changes, verification results, compatibility impact, and known limitations to the user.

The following restrictions are mandatory:

- Never push directly to the default branch.
- Never approve or merge your own Pull Request.
- Never bypass branch protection or required checks.
- Never commit secrets, credentials, private local configuration, personal recordings, generated outputs, models, caches, logs, or unrelated machine-specific data.
- Final Pull Request review, approval, and merge are always performed by the user.

If subsequent work depends on the Pull Request being merged, stop after reporting the Pull Request and wait for the user. If the next work is genuinely independent, it may continue on a separate branch based on the default branch; do not create unnecessary stacked Pull Request dependencies.

## Completion and reporting

- Do not claim a milestone is complete until the diff has been reviewed and applicable verification has passed.
- Clearly distinguish automated tests, mock tests, static validation, manual tests, hardware tests, and untested behavior.
- If a check cannot run, state why, what was still verified, and the exact environment or command needed to finish verification.
- Keep the working tree status and any unrelated pre-existing changes explicit in the final report.
