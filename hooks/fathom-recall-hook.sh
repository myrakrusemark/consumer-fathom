#!/usr/bin/env bash
# Fathom recall hook — searches the lake and injects context.
#
# Triggered on UserPromptSubmit. Searches using the user's prompt
# (plus recent assistant context for richer embeddings), injects
# relevant memories as additionalContext before the LLM responds.
#
# Env:
#   FATHOM_API_URL        — consumer API (default: http://localhost:8201)
#   FATHOM_API_KEY        — bearer token
#   RECALL_THRESHOLD      — max distance to consider relevant (default: 0.35)
#   RECALL_INJECT_MAX     — how many results to inject (default: 15)
#   RECALL_CONTENT_MAX    — max chars per delta (default: 400)
#   RECALL_MIN_PROMPT_LEN — skip prompts shorter than this (default: 10)
#
# Install: add to ~/.claude/settings.json hooks.UserPromptSubmit
#          with "timeout": 8000

set -euo pipefail

FATHOM_API_URL="${FATHOM_API_URL:-http://localhost:8201}"
FATHOM_API_KEY="${FATHOM_API_KEY:-}"

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

MIN_LEN="${RECALL_MIN_PROMPT_LEN:-10}"
if [ "${#PROMPT}" -lt "$MIN_LEN" ]; then
    exit 0
fi

# Build query: assistant context + current prompt
QUERY=""
if [ -n "$ASSISTANT" ]; then
    QUERY="$(echo "$ASSISTANT" | head -c 500)

"
fi
QUERY="${QUERY}$(echo "$PROMPT" | head -c 1000)"

# Search
RECALL_LIMIT="${RECALL_LIMIT:-30}"
SEARCH_BODY=$(python3 -c "
import json, sys
print(json.dumps({'origin': sys.stdin.read().strip(), 'limit': int(sys.argv[1])}))
" "$RECALL_LIMIT" <<< "$QUERY")

AUTH_HEADER=""
[ -n "$FATHOM_API_KEY" ] && AUTH_HEADER="-H \"Authorization: Bearer ${FATHOM_API_KEY}\""

RESULT=$(eval curl -sf -X POST "\"${FATHOM_API_URL}/v1/search\"" \
    -H "'Content-Type: application/json'" \
    ${AUTH_HEADER} \
    -d "'${SEARCH_BODY}'" 2>/dev/null) || exit 0

# Format and inject
python3 -c "
import json, sys, os
from collections import Counter

THRESHOLD = float(os.environ.get('RECALL_THRESHOLD', '0.35'))
INJECT_MAX = int(os.environ.get('RECALL_INJECT_MAX', '15'))
CONTENT_MAX = int(os.environ.get('RECALL_CONTENT_MAX', '400'))

raw = json.loads(sys.stdin.read())
results = raw.get('results', []) or raw.get('added', [])
if not results:
    sys.exit(0)

relevant = [s for s in results if s.get('distance', 1.0) <= THRESHOLD]
if not relevant:
    sys.exit(0)

total = len(relevant)
injected = relevant[:INJECT_MAX]
overflow = relevant[INJECT_MAX:]

lines = []
if total > len(injected):
    lines.append(f'--- Delta Recall: {total} relevant (showing {len(injected)}) ---')
else:
    lines.append(f'--- Delta Recall: {total} relevant ---')
lines.append('')

for s in injected:
    d = s.get('delta', s)
    dist = s.get('distance', 0)
    did = d.get('id', '?')[:10]
    tags = ', '.join(d.get('tags', []))
    content = d.get('content', '')[:CONTENT_MAX]
    lines.append(f'[{did}] d={dist:.3f} [{tags}]')
    lines.append(content)
    lines.append('')

if overflow:
    tag_counts = Counter()
    for s in overflow:
        d = s.get('delta', s)
        for t in d.get('tags', []):
            if t.startswith('session:') or t.startswith('project:'):
                continue
            tag_counts[t] += 1

    lines.append(f'--- {len(overflow)} more nearby ---')
    if tag_counts:
        top_tags = tag_counts.most_common(8)
        tag_str = ' \u00b7 '.join(f'{tag} ({n})' for tag, n in top_tags)
        lines.append(f'  {tag_str}')
    lines.append('  Use \`fathom delta search \"...\" --subset\` to explore the full set.')
    lines.append('---')

context = '\n'.join(lines)
sys_msg = f'Delta recall: {total} relevant'
if overflow:
    sys_msg += f' ({len(injected)} shown, {len(overflow)} in horizon)'

print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'UserPromptSubmit',
        'systemMessage': sys_msg,
        'additionalContext': context,
    }
}))
" <<< "$RESULT"
