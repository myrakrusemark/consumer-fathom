#!/usr/bin/env bash
# Migrate an existing consumer-fathom install from the old in-repo layout
# (./data/ + auto-scoped postgres volume) to the canonical layout
# (~/.fathom/<instance>/ + named postgres volume <instance>-pg).
#
# Safe to rerun — checks for each step before acting.
#
# Usage:
#   ./scripts/migrate-to-canonical-paths.sh [instance_name]
#   (defaults to "fathom")

set -euo pipefail

INSTANCE="${1:-fathom}"
NEW_LAKE_DIR="${HOME}/.fathom/${INSTANCE}"
NEW_PG_VOLUME="${INSTANCE}-pg"

# Old layout — what we're migrating FROM.
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OLD_DATA_DIR="${REPO_DIR}/data"
# The auto-scoped postgres volume podman/docker created previously. Project
# name is derived from the directory basename unless COMPOSE_PROJECT_NAME was
# set. We probe both common patterns.
OLD_PG_VOLUME_CANDIDATES=(
  "$(basename "$REPO_DIR")_pgdata"
  "consumer-fathom_pgdata"
)

echo "→ target instance: ${INSTANCE}"
echo "→ new lake dir:    ${NEW_LAKE_DIR}"
echo "→ new pg volume:   ${NEW_PG_VOLUME}"
echo

if [[ ! -d "$OLD_DATA_DIR" ]] && ! podman volume exists "${OLD_PG_VOLUME_CANDIDATES[0]}" 2>/dev/null; then
  echo "Nothing to migrate — no old ./data/ dir and no old volume found."
  echo "If this is a fresh install, just run: docker compose up -d"
  exit 0
fi

# ── Stop the stack so nothing mutates mid-migration ─────────────────────────
cd "$REPO_DIR"
echo "→ stopping stack…"
docker compose down 2>/dev/null || podman compose down 2>/dev/null || true

# ── State files: move ./data/ → ~/.fathom/<instance>/ ───────────────────────
if [[ -d "$OLD_DATA_DIR" ]]; then
  echo "→ moving ${OLD_DATA_DIR} → ${NEW_LAKE_DIR}"
  mkdir -p "${NEW_LAKE_DIR}"
  # Copy first, then remove — safer than mv across volume boundaries.
  cp -a "$OLD_DATA_DIR"/. "$NEW_LAKE_DIR"/
  # Leave old dir in place for the user to delete manually once they're happy.
  echo "  ✓ copied. Review ${NEW_LAKE_DIR} and then delete ${OLD_DATA_DIR} manually."
else
  echo "→ no ${OLD_DATA_DIR} to migrate (skipping)"
  mkdir -p "${NEW_LAKE_DIR}"
fi

# ── Drop a README into LAKE_DIR so the split between this dir and the
#    named postgres volume is discoverable (stumbling into ~/.fathom/fathom/
#    should answer "where's the live DB?" without reading code).
if [[ -f "${REPO_DIR}/scripts/lake-dir-README.md" && ! -f "${NEW_LAKE_DIR}/README.md" ]]; then
  cp "${REPO_DIR}/scripts/lake-dir-README.md" "${NEW_LAKE_DIR}/README.md"
  echo "→ wrote ${NEW_LAKE_DIR}/README.md"
fi

# ── Postgres: rename the named volume ───────────────────────────────────────
OLD_PG_VOLUME=""
for candidate in "${OLD_PG_VOLUME_CANDIDATES[@]}"; do
  if podman volume exists "$candidate" 2>/dev/null || docker volume inspect "$candidate" &>/dev/null; then
    OLD_PG_VOLUME="$candidate"
    break
  fi
done

if [[ -n "$OLD_PG_VOLUME" ]]; then
  # Runtime (docker or podman). Prefer whichever we found the volume in.
  if command -v podman &>/dev/null && podman volume exists "$OLD_PG_VOLUME" 2>/dev/null; then
    RUNTIME=podman
  else
    RUNTIME=docker
  fi

  if $RUNTIME volume inspect "$NEW_PG_VOLUME" &>/dev/null; then
    echo "→ ${NEW_PG_VOLUME} already exists — skipping copy to avoid clobbering"
  else
    echo "→ copying volume ${OLD_PG_VOLUME} → ${NEW_PG_VOLUME} (${RUNTIME})"
    # Neither docker nor older podman have `volume rename` reliably, so
    # copy through a throwaway alpine container. Works across both runtimes.
    $RUNTIME volume create "$NEW_PG_VOLUME" >/dev/null
    $RUNTIME run --rm \
      -v "$OLD_PG_VOLUME:/src:ro" \
      -v "$NEW_PG_VOLUME:/dst" \
      alpine sh -c 'cp -a /src/. /dst/'
    echo "  ✓ copied. You can delete the old volume with: ${RUNTIME} volume rm ${OLD_PG_VOLUME}"
  fi
else
  echo "→ no old postgres volume found (skipping)"
fi

# ── .env: make sure COMPOSE_PROJECT_NAME is set ─────────────────────────────
if [[ -f "${REPO_DIR}/.env" ]]; then
  if ! grep -q '^COMPOSE_PROJECT_NAME=' "${REPO_DIR}/.env"; then
    echo "→ adding COMPOSE_PROJECT_NAME=${INSTANCE} to .env"
    echo "" >> "${REPO_DIR}/.env"
    echo "COMPOSE_PROJECT_NAME=${INSTANCE}" >> "${REPO_DIR}/.env"
  else
    echo "→ .env already sets COMPOSE_PROJECT_NAME (leaving as-is)"
  fi
  # LAKE_DIR — replace the CHANGE-ME placeholder or append if missing.
  if grep -q '^LAKE_DIR=.*CHANGE-ME' "${REPO_DIR}/.env"; then
    sed -i "s|^LAKE_DIR=.*|LAKE_DIR=${NEW_LAKE_DIR}|" "${REPO_DIR}/.env"
    echo "→ set LAKE_DIR=${NEW_LAKE_DIR} in .env"
  elif ! grep -q '^LAKE_DIR=' "${REPO_DIR}/.env"; then
    echo "LAKE_DIR=${NEW_LAKE_DIR}" >> "${REPO_DIR}/.env"
    echo "→ added LAKE_DIR=${NEW_LAKE_DIR} to .env"
  else
    echo "→ .env already sets LAKE_DIR (leaving as-is)"
  fi
fi

echo
echo "Done. Next step: docker compose up -d"
echo "If everything looks right, you can delete ${OLD_DATA_DIR}."
