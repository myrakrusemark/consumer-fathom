#!/usr/bin/env bash
# Fathom delta hook — writes conversation turns to the lake.
#
# Fires on UserPromptSubmit (user message) and Stop (assistant response).
# Images from ~/.claude/image-cache/ are uploaded as media deltas.
# Run async — fire and forget, don't block the conversation.
#
# Env:
#   FATHOM_API_URL  — consumer API (default: http://localhost:8201)
#   FATHOM_API_KEY  — bearer token
#
# Install: add to ~/.claude/settings.json hooks (UserPromptSubmit + Stop)
#          with "async": true

set -euo pipefail

FATHOM_API_URL="${FATHOM_API_URL:-http://localhost:8201}"
FATHOM_API_KEY="${FATHOM_API_KEY:-}"
MAX_CHARS="${FATHOM_MAX_CHARS:-4000}"
IMAGE_CACHE_DIR="${HOME}/.claude/image-cache"
STATE_DIR="${HOME}/.claude/.fathom-hook-state"

INPUT=$(cat)

eval "$(echo "$INPUT" | python3 -c "
import sys, json, shlex
d = json.load(sys.stdin)
print(f'export EVENT={shlex.quote(d.get(\"hook_event_name\", \"\"))}')
print(f'export SESSION_ID={shlex.quote(d.get(\"session_id\", \"unknown\"))}')
print(f'export CWD={shlex.quote(d.get(\"cwd\", \"\"))}')
print(f'export PROMPT={shlex.quote(d.get(\"prompt\", \"\"))}')
print(f'export ASSISTANT={shlex.quote(d.get(\"last_assistant_message\", \"\"))}')
")"

AUTH_HEADER=""
[ -n "$FATHOM_API_KEY" ] && AUTH_HEADER="Authorization: Bearer ${FATHOM_API_KEY}"

# ── Image capture (UserPromptSubmit only) ─────────────────────────────────

if [ "$EVENT" = "UserPromptSubmit" ]; then
    SESSION_IMAGE_DIR="${IMAGE_CACHE_DIR}/${SESSION_ID}"
    if [ -d "$SESSION_IMAGE_DIR" ]; then
        mkdir -p "$STATE_DIR"
        STATE_FILE="${STATE_DIR}/${SESSION_ID}.last"
        LAST_SEEN=0
        [ -f "$STATE_FILE" ] && LAST_SEEN=$(cat "$STATE_FILE")

        for img in "$SESSION_IMAGE_DIR"/*.png; do
            [ -f "$img" ] || continue
            NUM=$(basename "$img" .png)
            [[ "$NUM" =~ ^[0-9]+$ ]] || continue
            [ "$NUM" -le "$LAST_SEEN" ] && continue

            curl -sf -X POST "${FATHOM_API_URL}/v1/media/upload" \
                ${AUTH_HEADER:+-H "${AUTH_HEADER}"} \
                -F "file=@${img}" \
                -F "content=${PROMPT:-(pasted image)}" \
                -F "tags=image,chat,claude-code,session:${SESSION_ID},project:${CWD}" \
                -F "source=claude-code" \
                -o /dev/null 2>/dev/null &

            LAST_SEEN="$NUM"
        done

        echo "$LAST_SEEN" > "$STATE_FILE"
    fi
fi

# ── Text capture ──────────────────────────────────────────────────────────

if [ "$EVENT" = "UserPromptSubmit" ]; then
    CONTENT="$PROMPT"
    export ROLE="user"
elif [ "$EVENT" = "Stop" ]; then
    CONTENT="$ASSISTANT"
    export ROLE="assistant"
else
    exit 0
fi

[ -z "$CONTENT" ] && exit 0

python3 -c "
import json, sys, os, urllib.request

content = sys.stdin.read().strip()
max_chars = int(os.environ.get('MAX_CHARS', '4000'))
url = os.environ['FATHOM_API_URL'] + '/v1/deltas'
api_key = os.environ.get('FATHOM_API_KEY', '')
role = os.environ['ROLE']
session_id = os.environ['SESSION_ID']
cwd = os.environ['CWD']

def split(text, limit):
    if len(text) <= limit:
        return [text]
    mid = len(text) // 2
    for sep in ['\n\n', '\n', '. ']:
        left = text.rfind(sep, 0, mid + limit // 2)
        if left > limit // 4:
            offset = len(sep) if sep != '. ' else 2
            return split(text[:left + offset].rstrip(), limit) + split(text[left + offset:].lstrip(), limit)
    return split(text[:limit], limit) + split(text[limit:], limit)

chunks = split(content, max_chars)
tags = [role, 'chat', 'claude-code', f'session:{session_id}', f'project:{cwd}']

for chunk in chunks:
    if not chunk.strip():
        continue
    body = json.dumps({
        'content': chunk,
        'tags': tags,
        'source': 'claude-code',
    }).encode()
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    if api_key:
        req.add_header('Authorization', f'Bearer {api_key}')
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass
" <<< "$CONTENT"
