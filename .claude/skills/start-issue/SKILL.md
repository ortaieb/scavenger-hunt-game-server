---
name: start-issue
description: Start work on a GitHub issue — fetch latest main, create an issue-numbered branch, implement the work, and open a PR back to main. Use when asked to work on, start, pick up or implement a GitHub issue (e.g. "work on issue 3", "/start-issue 12").
argument-hint: <issue-number>
allowed-tools: Bash(git:*), Bash(gh:*)
---

# Start issue

Work on GitHub issue **#$ARGUMENTS** from a fresh branch off `main`, and finish with a pull request.

If no issue number was given, ask for one before doing anything else.

## 1. Pre-flight checks

1. Confirm this is a git repo with a GitHub remote: `git remote get-url origin`.
2. Confirm `gh` is authenticated: `gh auth status`. If not, stop and tell me to run `gh auth login`.
3. Check the working tree: `git status --porcelain`. If there are uncommitted changes, **stop and ask** whether to stash, commit or discard them. Never discard changes without asking.

## 2. Read the issue

```bash
gh issue view <number> --json number,title,body,state,labels
```

- If the issue doesn't exist, stop and say so.
- If it is closed, ask before continuing.
- Read the body carefully: it is the specification for the work.

## 3. Create the branch from the latest main

Build the branch name as `<number>-<summary>`:
- `<summary>` is 3–6 words in lower-case kebab-case taken from the issue title.
- Drop filler words ("a", "the", "and") and punctuation.
- Example: #1 "Create a TypeScript / NodeJS web application project" → `1-create-web-app-project`

Then:

```bash
git fetch origin
git switch -c "<branch-name>" origin/main
```

Always branch from `origin/main` after fetching, never from the current branch or a stale local `main`.

If a branch for this issue already exists locally or on the remote (`git branch -a --list "*<number>-*"`), **stop and ask** whether to reuse it or create a new one.

Tell me the branch name before starting the work.

## 4. Do the work

- Implement what the issue asks for, following the project's CLAUDE.md conventions.
- If the issue is ambiguous in a way that changes the design, ask before building.
- Commit in small, logical steps. Reference the issue in each message, e.g. `Add Express server skeleton (#1)`.
- Never commit to `main`. Never force-push.

## 5. Verify

Run whatever the project uses to verify changes: build, lint, type-check, tests (check `package.json` scripts, Makefile, Gradle tasks etc.). Fix any failures before opening the PR. If something can't be fixed, say so in the PR description.

## 6. Open the pull request

```bash
git push -u origin HEAD
gh pr create --base main \
  --title "<issue title>" \
  --body "Closes #<number>

## Summary
<what changed and why>

## Testing
<what was run and the result>"
```

## 7. Report back

Give me:
- the PR URL
- a short summary of what was done
- anything left open or worth reviewing closely

Do **not** merge the PR.
