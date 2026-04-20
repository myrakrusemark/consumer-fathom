#!/usr/bin/env bash
# Fathom recall hook — searches the lake and injects memories as context.
#
# Triggered on UserPromptSubmit. Hits the canonical /v1/search endpoint
# with depth=shallow (one similarity search is fine per-prompt; the agent
# can call search_lake for a deeper plan when it wants one) and injects
# the server-rendered hierarchical prompt text directly. No formatting
# happens in the hook — the API is the single source of truth.
#
# Env:
#   FATHOM_API_URL       — consumer API (default: http://localhost:8201)
#   FATHOM_API_KEY       — bearer token
#   RECALL_DEPTH         — "shallow" (default) or "deep"
#   RECALL_LIMIT         — results per step (default: 30)
#   RECALL_THRESHOLD     — shallow-mode distance cutoff (default: 0.35)
#   RECALL_MIN_PROMPT_LEN — skip prompts shorter than this (default: 10)
#
# Install: add to ~/.claude/settings.json hooks.UserPromptSubmit
#          with "timeout": 8000

set -euo pipefail

FATHOM_API_URL="${FATHOM_API_URL:-http://localhost:8201}"
FATHOM_API_KEY="${FATHOM_API_KEY:-}"
DEPTH="${RECALL_DEPTH:-shallow}"
LIMIT="${RECALL_LIMIT:-30}"
THRESHOLD="${RECALL_THRESHOLD:-0.35}"
MIN_LEN="${RECALL_MIN_PROMPT_LEN:-10}"

INPUT=$(cat)

eval "$(echo "$INPUT" | python3 -c "
import sys, json, shlex
d = json.load(sys.stdin)
print(f'EVENT={shlex.quote(d.get(\"hook_event_name\", \"\"))}')
print(f'PROMPT={shlex.quote(d.get(\"prompt\", \"\"))}')
print(f'ASSISTANT={shlex.quote(d.get(\"last_assistant_message\", \"\"))}')
")"

[ "$EVENT" != "UserPromptSubmit" ] && exit 0
[ -z "$PROMPT" ] && exit 0
if [ "${#PROMPT}" -lt "$MIN_LEN" ]; then
    exit 0
fi

# Build query: recent assistant context + current prompt (richer embeddings)
QUERY=""
if [ -n "$ASSISTANT" ]; then
    QUERY="$(echo "$ASSISTANT" | head -c 500)

"
fi
QUERY="${QUERY}$(echo "$PROMPT" | head -c 1000)"

export DEPTH LIMIT THRESHOLD
SEARCH_BODY=$(python3 -c "
import json, os, sys
print(json.dumps({
    'text': sys.stdin.read().strip(),
    'depth': os.environ['DEPTH'],
    'limit': int(os.environ['LIMIT']),
    'threshold': float(os.environ['THRESHOLD']),
}))
" <<< "$QUERY")

AUTH_HEADER=""
[ -n "$FATHOM_API_KEY" ] && AUTH_HEADER="-H \"Authorization: Bearer ${FATHOM_API_KEY}\""

RESULT=$(eval curl -sf -X POST "\"${FATHOM_API_URL}/v1/search\"" \
    -H "'Content-Type: application/json'" \
    ${AUTH_HEADER} \
    -d "'${SEARCH_BODY}'" 2>/dev/null) || exit 0

python3 -c "
import json, sys

r = json.loads(sys.stdin.read())
as_prompt = r.get('as_prompt') or ''
total = int(r.get('total_count') or 0)
if not as_prompt or total == 0:
    sys.exit(0)

header = f'--- You remember {total} things ---'
context = f'{header}\n\n{as_prompt}'

print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'UserPromptSubmit',
        'systemMessage': f'You remember {total} things',
        'additionalContext': context,
    }
}))
" <<< "$RESULT"
