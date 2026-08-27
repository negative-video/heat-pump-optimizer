#!/usr/bin/env bash
# Pre-tool-use hook for Claude Code.
# Intercepts Bash tool calls to gate git commit/tag and block bare "python".
#
# Reads JSON from stdin with the tool call details.
# Exit 0 = allow, Exit 2 = block.

set -euo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null || echo "")

# ── Guard: block bare "python " (should be python3) ───────────────────
# Match "python " at start of command or after pipe/&&/; but NOT "python3"
if echo "$COMMAND" | grep -qE '(^|[|;&]\s*)python\s' && ! echo "$COMMAND" | grep -qE '(^|[|;&]\s*)python3\s'; then
    echo "Use python3, not python — macOS may not have python on PATH" >&2
    exit 2
fi

# ── Gate: git commit → run validate.py ─────────────────────────────────
if echo "$COMMAND" | grep -q "git commit"; then
    echo "Pre-commit gate: running validate.py..." >&2
    if ! python3 tools/validate.py >&2; then
        echo "" >&2
        echo "Commit blocked — fix validation errors above before committing." >&2
        exit 2
    fi
    exit 0
fi

# ── Gate: git tag → run validate.py + full test suite ──────────────────
if echo "$COMMAND" | grep -q "git tag"; then
    echo "Pre-tag gate: running validate.py + pytest..." >&2
    if ! python3 tools/validate.py >&2; then
        echo "" >&2
        echo "Tag blocked — fix validation errors above." >&2
        exit 2
    fi
    if ! python3 -m pytest tests/ -x -q --tb=short >&2; then
        echo "" >&2
        echo "Tag blocked — fix test failures above." >&2
        exit 2
    fi
    exit 0
fi

# ── Everything else: pass through ──────────────────────────────────────
exit 0
