# qlever-dir — Generic SPARQL store from a directory of RDF triples

A self-contained Docker container that turns any directory of RDF triple files
into a live SPARQL 1.1 endpoint backed by [QLever](https://github.com/ad-freiburg/qlever).
Files are watched on the filesystem — an ordinary content change is applied
straight to the running store via SPARQL Update and is visible in seconds.
Structural changes and accumulated update volume instead trigger a full
rebuild in the background, using a blue-green strategy so the endpoint stays
available the whole time.

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
4. Watches `/data` with `inotifywait`. A change to one file's content is
   applied to the active slot via SPARQL Update after `INCREMENTAL_DELAY`
   seconds of quiet — no rebuild. A structural change (new/removed
   directory, `.qlever/converters.json`, `.qleverignore`) or enough
   accumulated incremental deltas (`COMPACTION_DELTA_TRIPLES`) instead
   schedules a full rebuild, debounced by `REBUILD_DELAY`. See
   [Incremental updates](#incremental-updates).
5. `nginx` on port 7001 proxies to whichever QLever instance is currently
   active, and refuses any request carrying write credentials, so the
   published endpoint is structurally read-only. The upstream swap during a
   rebuild is atomic — clients see no downtime, only a brief reconnect at the
   moment of swap.

## Quad-only formats are not loaded

`.nq` and other quad formats are intentionally **not** processed. This container
synthesizes graph names from file paths; quad files already carry their own
graph names and would conflict with that model.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `BASE_URI` | `https://example.org/data/` | URI prefix prepended to relative file paths to form graph IRIs. Should end with `/`. |
| `INCREMENTAL_DELAY` | `2` | Seconds of quiet on a single changed file before its content is applied to the active slot via SPARQL Update. Coalesces a burst of writes (e.g. one save) into one update. |
| `REBUILD_DELAY` | `15` | Seconds to wait after the last *structural* filesystem event (or after crossing `COMPACTION_DELTA_TRIPLES`) before triggering a full rebuild. Debounces rapid structural changes; it is no longer the latency for ordinary content changes — see `INCREMENTAL_DELAY`. |
| `COMPACTION_DELTA_TRIPLES` | `100000` | Cumulative delta triples (inserted, or 1 per dropped graph) applied incrementally since the last full rebuild before a compaction rebuild is scheduled. Keeps QLever's unmerged-delta query overhead bounded. |
| `RECONCILE_INTERVAL` | `3600` | Seconds between reconciliation sweeps that diff the active slot's build-time manifest against the live filesystem and re-apply any drift. `0` disables the periodic sweep (a sweep still runs once after every slot swap). |
| `HEALTH_CHECK_TIMEOUT` | `300` | Seconds a newly started `qlever-server` may take to answer its first query before its slot is rejected. Applies to freshly built slots and to a slot resumed after a restart alike; a server that exits fails immediately rather than waiting out the timeout. Raise it for very large indexes. |

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
      # INCREMENTAL_DELAY: "2"             # seconds before a content change is applied
      # COMPACTION_DELTA_TRIPLES: "100000" # delta size that triggers a compaction rebuild
      # RECONCILE_INTERVAL: "3600"         # seconds between reconciliation sweeps (0 disables)
      # HEALTH_CHECK_TIMEOUT: "300"        # seconds a new qlever-server may take to answer
    restart: unless-stopped
```

## Supported input formats

- `.nt` (N-Triples)
- `.ttl` (Turtle)
- `.n3` (Notation3, triples subset — parsed as Turtle)

All formats are normalized through `rapper` (from `raptor2-utils`) so prefix
declarations, escaped strings, etc. are handled correctly.

## Converters for non-RDF files

Files that are not RDF (Markdown, CSV, …) can also be indexed by declaring a
**converter** for their extension. The container itself stays domain-agnostic:
converters live in the mounted data directory, not in the image.

Place a `.qlever/converters.json` next to the files it should handle:

```
projects/
  .qlever/
    converters.json      ← { "md": "md2ttl.py" }
    md2ttl.py            ← executable converter
  rollstuhl-bluetooth.md
  solar-panel-quote.md
```

`converters.json` maps a file extension to an executable command. The command is
resolved relative to the `.qlever/` directory (an absolute path is used as-is)
and invoked as:

```
<command> <input-file>
```

It must **emit Turtle on stdout**. The output is then normalized through
`rapper` and tagged with the source file's own path-derived graph IRI — so a
converted `projects/foo.md` yields triples in graph
`<{BASE_URI}projects/foo.md>`, keeping provenance on the source file. A
converter that **exits non-zero** produces a queryable `parsingError` quad
(see below) instead of aborting the build, exactly like an RDF parse failure.

**Nearest config wins (cascading).** For each file, the converter is taken from
the closest `.qlever/converters.json` found walking up to `/data`. A `.qlever/`
therefore configures its own directory *and* everything beneath it, and a deeper
`.qlever/` overrides a shallower one. No separate recursive/non-recursive
setting is needed — placing the config controls the scope.

A worked example (the project schema above, with a dependency-free stdlib
converter) is in [`examples/projects/`](examples/projects).

> **Trust note:** when a `.qlever/converters.json` is present, qlever-dir
> executes the referenced program from the mounted data directory on every
> rebuild. Only mount data you trust to also run its converters. Without any
> `converters.json`, behaviour is unchanged — only `.nt`/`.ttl`/`.n3` are read.

## Excluding files with `.qleverignore`

A data directory ("chamber") can exclude its own files from the index scan by
dropping a `.qleverignore` file next to them — without the container needing
to know anything about it, and without any other chamber being affected.
This is the mechanism for a store that legitimately owns a file (e.g. a large
dump that belongs in a dedicated store) but doesn't want it picked up here.

```
genetics/
  .qleverignore        ← genetics.nt
  genetics.nt           excluded, matched by the pattern above
  summary.ttl            still indexed
```

**File format.** One glob pattern per line, relative to the directory
containing the `.qleverignore`. Blank lines and lines starting with `#` are
ignored; trailing whitespace is stripped.

```
# .qleverignore in the "genetics" directory
genetics.nt
dumps/*.nt
**/*.big.ttl
```

**Pattern semantics:**

- A pattern is matched against the candidate file's path *relative to the
  directory holding the `.qleverignore`* (glob syntax, e.g. `*`, `?`, `[...]`
  — Python's `fnmatch`).
- A pattern containing no `/` (e.g. `genetics.nt`) also matches by basename
  at any depth under that directory, the same as plain gitignore patterns
  do.
- A pattern containing `/` (e.g. `dumps/*.nt`) is anchored to that
  directory: it matches `dumps/x.nt` but not `other/dumps/x.nt` or a
  `dumps/` one level further down.
- Every ancestor directory of a candidate file, up to `/data`, that holds a
  `.qleverignore` is consulted, and a match from **any** of them excludes
  the file — a pattern in one chamber's `.qleverignore` never reaches into
  another chamber's subtree, but within one chamber's own subtree, nearer
  and farther `.qleverignore` files are simply pooled together.

**Not supported: negation.** Gitignore's `!pattern` re-inclusion syntax is
intentionally not implemented, to keep the semantics simple and unambiguous
(no "nearest wins" resolution to reason about). A `!`-prefixed line is
skipped with a warning logged to stderr rather than applied.

A change to a `.qleverignore` file triggers a full rebuild, the same as a
change to `.qlever/converters.json` — it changes what gets indexed even
though its own content never is.

## Parse errors are visible through the SPARQL endpoint

If a file fails to parse, the build does not abort. Instead of that file's
triples, the index gets a single diagnostic quad in the file's named graph:

```
<{graph_iri}> <urn:qlever-dir:parsingError> "{stderr message}" <{graph_iri}> .
```

You can list all currently broken files via SPARQL:

```sparql
SELECT ?file ?error WHERE {
  ?file <urn:qlever-dir:parsingError> ?error
}
```

Once a file is fixed and saved, the next incremental update or rebuild
replaces the error quad with the actual triples.

## Incremental updates

Most file changes never trigger a rebuild. A single file's content changing
(create, edit, delete, move) is applied straight to the active slot:

1. `inotifywait` reports the change. If it's an ordinary content change
   (native RDF extension or a declared converter extension), it's queued for
   the incremental path, not the rebuild debouncer.
2. After `INCREMENTAL_DELAY` seconds of quiet on that path, the orchestrator
   sends one SPARQL 1.1 Update request to the active slot:
   `DROP SILENT GRAPH <g>` followed by
   `INSERT DATA { GRAPH <g> { ...current triples... } }` — a whole-graph
   replace, not a diff. If the file was deleted, moved away, or is now
   `.qleverignore`'d, only the `DROP SILENT` runs. Both halves are one
   request, so there is no window where the graph reads empty.
3. The update is idempotent and visible to queries as soon as it commits —
   typically a few seconds after the file changed, not `REBUILD_DELAY` plus
   a full build.
4. `emit_file.sh` computes the graph IRI and triples for both this path and
   the full build (see [Files in this project](#files-in-this-project)), so
   the two can never disagree about how a file maps to RDF.

**What still forces a full rebuild.** A single-file diff can't express
everything:

- A **structural** change — a new/removed directory, or an edit to
  `.qlever/converters.json` or `.qleverignore` — can change *which* files
  are indexed at all, so it schedules a full rebuild, debounced by
  `REBUILD_DELAY`.
- Enough **accumulated incremental deltas** — `COMPACTION_DELTA_TRIPLES`
  triples inserted or graphs dropped since the last rebuild — also schedules
  one, because QLever's query performance degrades as unmerged delta triples
  pile up on top of the base index.

A rebuild in this world is a **compaction pass**, not the visibility
mechanism: it folds the base index and every applied delta into one clean
index, built into the idle slot as before (see
[Blue-green rebuild details](#blue-green-rebuild-details)).

**Writes are gated; reads are not.** The orchestrator generates a fresh
access token at container start and passes it to both `qlever-server` slots;
only the orchestrator, talking to the slots directly on 7101/7102, ever
presents it, and it is redacted from every log line (including
`qlever-server`'s own startup banner, which prints it in cleartext). Reads
stay anonymous. `nginx` on the published port 7001 returns 403 to any
request carrying an `access-token` query parameter or an `Authorization`
header, so the public endpoint is structurally read-only — even a leaked
token is useless there. One gap this doesn't close: a token embedded in a
form-encoded POST body isn't visible to nginx's request-line/header check,
so for that channel the token's secrecy, not nginx, is the control.

**Reconciliation backstop.** After every slot swap, and every
`RECONCILE_INTERVAL` seconds (`0` disables the periodic sweep; the post-swap
sweep still runs), the orchestrator diffs the active slot's build-time
`manifest.tsv` (one md5 per source file, per graph) against the current
filesystem and re-applies or drops whatever no longer matches. This is the
backstop for a coalesced or lost inotify event, an incremental update that
exhausted its retries, or a change that landed during the initial build
before the watcher was running.

**Persistence.** `qlever-server` runs without `--persist-updates`: the
filesystem under `/data` stays the single source of truth, and a container
restart rebuilds the index from it rather than replaying a SPARQL Update
journal — at the cost of redoing a full build on every restart.

> **Deploy note:** two things the code assumes should be confirmed once
> against your actual `qlever-server` build, then the redundant path
> trimmed: (1) that `DROP SILENT GRAPH` on a graph that
> doesn't exist yet is a no-op, not an error — the fallback if it isn't is
> `DELETE WHERE { GRAPH <g> { ?s ?p ?o } }`; (2) which of the
> `Authorization: Bearer` header or the `access-token` query parameter your
> build's write/admin auth actually honours — the orchestrator currently
> sends both.

## Blue-green rebuild details

- **Slot A**: index at `/index-a`, QLever on port 7101
- **Slot B**: index at `/index-b`, QLever on port 7102
- **nginx** on port 7001 with a single `upstream qlever_active { ... }` block
  pointing at whichever slot's port is currently live.

Each rebuild cycle (also the compaction pass — see
[Incremental updates](#incremental-updates)):

1. Build a fresh index into the inactive slot's directory from a full scan
   of `/data`, writing `manifest.tsv` (md5 per source file per graph)
   alongside it.
2. Start a new QLever server on the inactive slot's port.
3. Health-check it (`ASK {}` query) until it returns 200.
4. Replay every path changed since the scan into the new slot, via the same
   SPARQL Update the incremental path uses, so the slot about to go live is
   never staler than the one it replaces.
5. Atomically swap the nginx upstream to the new port and reload nginx.
6. Stop the old QLever server.
7. Run a reconciliation sweep against the newly active slot.

## Resuming across a restart

On startup the orchestrator looks for the slot holding the most recently
**completed** build — one that finished *and* proved servable, never a build
a crash interrupted mid-way (see `find_resumable_slot()` in
`orchestrator.py`). If it finds one, it starts serving that index immediately
while a full rebuild runs in the background against the other slot to catch
up on anything that changed while the container was down. Once that
rebuild's health check passes, traffic swaps to it exactly like an ordinary
blue-green rebuild — the resumed slot is never left stale on purpose, only
used to avoid a needless gap in service.

Whether there is anything to resume from depends on how `/index-a` and
`/index-b` are stored:

- **`docker restart` / `docker compose restart` of the same container**, or a
  crash under `restart: unless-stopped`: the container's writable layer
  survives, so the index dirs come back with the previous run's build in
  them and the resume happens even with no volumes configured.
- **The container is recreated** — an image update, `docker compose up`
  after changing the service, `docker rm` + `docker run`: a fresh writable
  layer means both index dirs come up empty. Unless `/index-a` and
  `/index-b` are on named volumes or bind mounts, there is nothing to resume
  from and the endpoint serves 502s for the whole first rebuild, as it did
  before this feature.

So mount them on volumes if you want the resume to survive image updates too:

```yaml
services:
  sparql:
    build: .
    volumes:
      - ./my-data:/data:ro
      - index-a:/index-a
      - index-b:/index-b
    # ...

volumes:
  index-a:
  index-b:
```

A rebuild still always runs on startup, resumed or not: the point of
persisting the index isn't to skip that (the container genuinely cannot know
what changed in `/data` while it was down), only to keep answering queries
from the last known-good build while it does.

If that startup rebuild fails, the resumed slot keeps serving — a stale
index answers queries, an exited container doesn't — and the rebuild is
retried every `REBUILD_DELAY` seconds until one succeeds. Without a slot to
resume from there is nothing to fall back on, so a failed initial build still
exits and lets the container's restart policy retry.

The resumed slot stays supervised while that rebuild runs: a rebuild blocks
for as long as it takes to build and health-check the other slot, so the
orchestrator polls the serving `qlever-server` and the nginx master
throughout it and exits non-zero the moment either dies, rather than leaving
port 7001 down until the build happens to finish. Restarting is cheap now —
the container comes back serving the completed index on disk.

A resumed slot gets the same health check as any freshly built one:
`HEALTH_CHECK_TIMEOUT` seconds (300 by default) for its `qlever-server` to
answer a query, cut short the moment that server exits — so an index an
incompatible `qlever-server` version cannot load after an image update is
rejected within seconds rather than at the deadline, while a large index that
simply takes a while to load still gets the full allowance. A slot that fails
this check is discarded, its completed-build marker is removed so the next
restart doesn't try it again, and startup falls back to a fresh build exactly
as if nothing had been persisted.

## Full rebuild scheduling

A **structural** change (new/removed directory, `.qlever/converters.json`,
`.qleverignore`) or crossing `COMPACTION_DELTA_TRIPLES` schedules a full
rebuild according to these rules. Ordinary content changes never reach this
path — see [Incremental updates](#incremental-updates):

- **No rebuild currently running**: the first qualifying change schedules a
  rebuild exactly `REBUILD_DELAY` seconds in the future. Further qualifying
  changes during that window are noted but do **not** push back the
  deadline. So when the system is idle, a rebuild starts at most
  `REBUILD_DELAY` seconds after the change.
- **A rebuild is currently running** (which can take seconds, minutes, or
  hours depending on data volume): additional qualifying changes set a
  `change_pending` flag. Multiple changes collapse to a single queued
  rebuild — never more than one is queued at a time. Incremental updates to
  the still-active slot are unaffected by a compaction build running against
  the idle slot.
- **When the current rebuild finishes** and `change_pending` is set, the
  next rebuild starts immediately, without re-debouncing.
- **When a rebuild fails** (the build itself, or the new slot never becoming
  healthy): the active slot keeps serving unchanged, and another rebuild is
  scheduled `REBUILD_DELAY` seconds later — no filesystem event is needed to
  retry, since the change that triggered the rebuild has already been
  consumed. Retries continue on that cadence until one succeeds.

Two rebuilds never run in parallel. On a directory under continuous
structural churn, you'll see a back-to-back rebuild loop with no idle gaps;
under ordinary content-only churn, rebuilds are infrequent and driven mostly
by `COMPACTION_DELTA_TRIPLES`.

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
- `orchestrator.py` — entrypoint. Manages the inotify watcher, the
  incremental SPARQL Update path, the full-rebuild/compaction state machine,
  blue-green swap, the reconciliation sweep, and nginx reloads.
- `build_index.sh` — scans `/data` for indexable files (native and converter
  extensions, filtered by `.qleverignore`), feeds each through `emit_file.sh`
  into `qlever-index`, and writes `manifest.tsv` alongside the index.
- `emit_file.sh` — the single file→triples/quads authority: converter lookup,
  `rapper` invocation, blank-node prefixing, error-quad emission. Shared by
  `build_index.sh` and the incremental update path so the two can never
  disagree about how a file maps to RDF.
- `examples/projects/` — a worked converter example: project Markdown files with
  a `.qlever/converters.json` and a dependency-free Markdown→Turtle converter.
- `nginx.conf` — proxies port 7001 to the active QLever slot and rejects any
  request carrying write credentials, keeping the published endpoint
  read-only. Includes the dynamic upstream file `/run/nginx-upstream.conf`.
- `docker-compose.yml` — minimal usage example.
