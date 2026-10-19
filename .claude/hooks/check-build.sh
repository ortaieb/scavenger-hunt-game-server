#!/usr/bin/env bash
# .claude/hooks/check-build.sh
#
# Stop hook: stops Claude from finishing its turn while mypy or pytest fail.
# Registered in .claude/settings.json (committed, so every contributor gets it).
#
# Contract with Claude Code:
#   exit 0 -> Claude may stop
#   exit 2 -> Claude must keep working; stderr is the feedback Claude reads
set -uo pipefail

MAX_BLOCKS="${CLAUDE_CHECKS_MAX_BLOCKS:-3}"      # consecutive blocks before giving up
OUTPUT_LINES="${CLAUDE_CHECKS_OUTPUT_LINES:-80}" # failure output passed back to Claude

input="$(cat)"
field() {
  printf '%s' "$input" |
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" | head -n 1
}

# Run in the repo Claude is working in. `cwd` follows Claude into worktrees and
# subdirectories; CLAUDE_PROJECT_DIR is the fallback.
work_dir="$(field cwd)"
root="$(git -C "${work_dir:-.}" rev-parse --show-toplevel 2>/dev/null || true)"
cd "${root:-${CLAUDE_PROJECT_DIR:-.}}" || exit 0

# A missing toolchain is not something Claude can fix, so warn instead of blocking.
if ! command -v uv >/dev/null 2>&1; then
  echo '{"systemMessage": "check-build hook skipped: uv is not on PATH."}'
  exit 0
fi

state="${TMPDIR:-/tmp}/claude-check-build-$(field session_id)"

if out="$( { uv run mypy . && uv run pytest -x -q; } 2>&1 )"; then
  rm -f "$state"
  exit 0
fi

# Loop guard: a Stop hook that always exits 2 can keep Claude working forever.
blocks=$(( $(cat "$state" 2>/dev/null || echo 0) + 1 ))
if (( blocks > MAX_BLOCKS )); then
  rm -f "$state"
  echo "{\"systemMessage\": \"check-build: mypy/pytest still failing after ${MAX_BLOCKS} attempts. Letting Claude stop; please review.\"}"
  exit 0
fi
echo "$blocks" > "$state"

# Something on stdout stops Claude Code reading a stderr that mentions a missing
# file (e.g. a FileNotFoundError in a test) as a broken hook rather than a block.
echo "check-build: checks failed (attempt ${blocks}/${MAX_BLOCKS})"
{
  echo "Checks failing - fix before finishing (attempt ${blocks} of ${MAX_BLOCKS})."
  echo "Last ${OUTPUT_LINES} lines of mypy/pytest output:"
  printf '%s\n' "$out" | tail -n "$OUTPUT_LINES"
} >&2
exit 2
