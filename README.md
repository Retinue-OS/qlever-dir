# qlever-dir — Generic SPARQL store from a directory of RDF triples

A self-contained Docker container that turns any directory of RDF triple files
into a live SPARQL 1.1 endpoint backed by [QLever](https://github.com/ad-freiburg/qlever).
Files are watched on the filesystem — when they change, the index is rebuilt
in the background using a blue-green strategy so the endpoint stays available
the whole time.

The container is generic: it knows nothing about the domain of the data. Mount
any directory of triples and you get a queryable store.

## What it does

1. On startup, scans `/data` recursively for `.nt`, `.ttl`, and `.n3` files.
2. Parses each file (via `rapper`) and emits N-Quads, synthesizing the named
   graph from the file path:

   `<{BASE_URI}{path relative to /data}>`

   e.g. `/data/health/obs.nt` becomes graph
   `<https://example.org/data/health/obs.nt>`.
3. Builds a QLever index and starts a SPARQL server.
4. Watches `/data` with `inotifywait`. After `REBUILD_DELAY` seconds of quiet
   following a change, triggers a blue-green rebuild.
5. `nginx` on port 7001 proxies to whichever QLever instance is currently
   active. The upstream swap is atomic — clients see no downtime, only a brief
   reconnect at the moment of swap.

## Quad-only formats are not loaded

`.nq` and other quad formats are intentionally **not** processed. This container
synthesizes graph names from file paths; quad files already carry their own
graph names and would conflict with that model.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `BASE_URI` | `https://example.org/data/` | URI prefix prepended to relative file paths to form graph IRIs. Should end with `/`. |
| `REBUILD_DELAY` | `15` | Seconds to wait after the last filesystem event before triggering a rebuild. Acts as a debounce so rapid file copies don't cause repeated rebuilds. |

## Quick start

```bash
mkdir -p my-data
cp /path/to/your/data.nt my-data/

docker compose up --build
```

Query the endpoint:

```bash
curl 'http://localhost:7001/?query=SELECT+*+WHERE+%7B+%3Fs+%3Fp+%3Fo+%7D+LIMIT+10'
```

## docker-compose.yml example

```yaml
services:
  sparql:
    build: .
    ports:
      - "7001:7001"
    volumes:
      - ./my-data:/data:ro
    environment:
      BASE_URI: https://example.org/data/
      REBUILD_DELAY: "15"
    restart: unless-stopped
```

## Supported input formats

- `.nt` (N-Triples)
- `.ttl` (Turtle)
- `.n3` (Notation3, triples subset — parsed as Turtle)

All formats are normalized through `rapper` (from `raptor2-utils`) so prefix
declarations, escaped strings, etc. are handled correctly.

## Blue-green rebuild details

- **Slot A**: index at `/index-a`, QLever on port 7101
- **Slot B**: index at `/index-b`, QLever on port 7102
- **nginx** on port 7001 with a single `upstream qlever_active { ... }` block
  pointing at whichever slot's port is currently live.

Each rebuild cycle:

1. Build a fresh index into the inactive slot's directory.
2. Start a new QLever server on the inactive slot's port.
3. Health-check it (`ASK {}` query) until it returns 200.
4. Atomically swap the nginx upstream to the new port and reload nginx.
5. Stop the old QLever server.

## Rebuild scheduling

- **Debounce**: a filesystem change starts a `REBUILD_DELAY`-second timer.
  Further changes during that window reset the timer. Only after the window
  is quiet does the rebuild start. This coalesces bursts of changes (large
  `cp -r`, `git pull`, batch ingestion).
- **No parallel rebuilds**: while a rebuild is running, additional changes
  set a `change_pending` flag. Only **one** rebuild is queued — multiple
  changes during a rebuild collapse to a single follow-up rebuild.
- **Back-to-back**: when a rebuild finishes and `change_pending` is set, the
  next rebuild starts immediately (without re-debouncing). On a directory
  that's constantly being written to, you'll see a continuous rebuild loop,
  but never two rebuilds in parallel.

## Filesystem watching limitations

`inotify` is used to detect changes. This is reliable on Linux bind-mount
volumes (host writes propagate into the container). It is **unreliable** on:

- NFS mounts
- Docker Desktop on macOS/Windows (VirtioFS/osxfs)

On those systems, a polling fallback would be needed (not currently
implemented).

## Files in this project

- `Dockerfile` — based on `adfreiburg/qlever:latest`, adds nginx, inotify-tools,
  rapper, Python.
- `orchestrator.py` — entrypoint. Manages the state machine, inotify watcher,
  blue-green swap, and nginx reloads.
- `build_index.sh` — converts triple files to N-Quads and feeds them to
  `qlever-index`.
- `nginx.conf` — proxies port 7001 to the active QLever slot. Includes the
  dynamic upstream file `/run/nginx-upstream.conf`.
- `docker-compose.yml` — minimal usage example.
