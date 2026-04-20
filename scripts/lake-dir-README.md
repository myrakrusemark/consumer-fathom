# Fathom lake — `~/.fathom/fathom/`

This is where your instance's memory lives on disk. One instance per subdirectory
of `~/.fathom/` (named by `COMPOSE_PROJECT_NAME`, default `fathom`).

## What's here

| Path | What |
|---|---|
| `api/drift-history.json` | Stats ECG "drift" track — semantic drift between identity crystal and current lake state |
| `api/mood-state.json` | Current carrier wave (how "loud" things feel) |
| `api/tokens.json` | Bearer tokens minted by the dashboard for API clients |
| `deltas/media/` | Image blobs referenced by delta `media_hash` fields |
| `deltas/resonance.json` | Activation thresholds for facet hooks |
| `deltas/retrievals-history.json` | Per-minute recall counts for the Stats panel |
| `backups/deltas-YYYYMMDDTHHMMSSZ.sql.gz` | Rolling `pg_dump` snapshots (3 most recent kept) |
| `backups/quarantine/` | Suspicious dumps held for manual acknowledgment |
| `source-runner/` | Per-source poll cursors so restarts don't re-ingest history |

## Where the live database actually is

Not here. Postgres runs in a container and stores its data in a **named podman
volume** called `fathom-pg` (or `<COMPOSE_PROJECT_NAME>-pg` for other instances):

```
~/.local/share/containers/storage/volumes/fathom-pg/_data/
```

Two reasons it's not bind-mounted into this directory:

1. **Rootless UID mapping.** Postgres inside the container runs as a nonroot UID
   that doesn't match your host UID. Named volumes let podman handle ownership
   transparently; a bind mount needs manual chown / `:Z,U` flags.
2. **SELinux labeling.** On Fedora, named volumes get the right `container_file_t`
   label automatically.

You can list / inspect the volume with:

```bash
podman volume ls
podman volume inspect fathom-pg
```

The `backups/` directory you see in this lake dir *does* contain real,
restorable SQL dumps of the live DB — the delta-store writes one per hour,
keeping the 3 most recent. That's your disaster-recovery path if the volume
is ever corrupt.

## Full teardown

```bash
cd path/to/consumer-fathom
docker compose down -v          # stop containers + drop the named pg volume
rm -rf ~/.fathom/fathom/        # drop everything you see here
```
