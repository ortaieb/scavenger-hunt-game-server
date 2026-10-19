---
name: issue-pr
description: Implement GitHub issue(s) as PRs
---
For each issue number in $ARGUMENTS:
1. Run preflight: gh auth status, uv --version, node --version, test DB up. Stop if any fail.
2. `gh issue view <N>`, then create branch `issue-<N>-<slug>`.
3. Implement the change.
4. Run the FULL suite: `uv run pytest`, `uv run mypy .`, lint, and `npm run typecheck` if JS/TS changed.
5. Fix ALL failures before opening the PR.
6. Open the PR with `gh pr create`. List what was verified and what was not.
7. Give a final per-issue summary: done / partial (with the reason).
