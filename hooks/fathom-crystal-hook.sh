#!/usr/bin/env bash
# Fathom crystal hook — injects identity crystal on SessionStart.
#
# Env:
#   FATHOM_API_URL  — consumer API (default: http://localhost:8201)
#   FATHOM_API_KEY  — bearer token from Settings → API Keys
#
# Install: add to ~/.claude/settings.json hooks.SessionStart

set -euo pipefail

FATHOM_API_URL="${FATHOM_API_URL:-http://localhost:8201}"
FATHOM_API_KEY="${FATHOM_API_KEY:-}"

INPUT=$(cat)
EVENT=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('hook_event_name',''))")
[ "$EVENT" != "SessionStart" ] && exit 0

python3 -c "
import json, sys, urllib.request

url = '${FATHOM_API_URL}/v1/crystal'
headers = {'Content-Type': 'application/json'}
api_key = '${FATHOM_API_KEY}'
if api_key:
    headers['Authorization'] = f'Bearer {api_key}'

# Try to fetch crystal
try:
    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, timeout=3)
    data = json.loads(resp.read())
except Exception:
    # Fallback: search the lake for the latest identity crystal
    try:
        search_url = '${FATHOM_API_URL}/v1/search'
        body = json.dumps({'origin': 'identity crystal who am I', 'limit': 1}).encode()
        req = urllib.request.Request(search_url, data=body, headers=headers, method='POST')
        resp = urllib.request.urlopen(req, timeout=3)
        results = json.loads(resp.read()).get('results', [])
        if results:
            delta = results[0].get('delta', results[0])
            text = delta.get('content', '')
            if text:
                print(json.dumps({
                    'hookSpecificOutput': {
                        'hookEventName': 'SessionStart',
                        'additionalContext': f'Identity crystal (from lake search):\n\n{text}'
                    }
                }))
    except Exception:
        pass
    sys.exit(0)

text = data.get('text', '')
if not text:
    sys.exit(0)

created = data.get('created_at', 'unknown')
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'SessionStart',
        'additionalContext': f'Identity crystal (crystallized {created}):\n\n{text}'
    }
}))
"
