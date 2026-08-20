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

A change to a `.qleverignore` file triggers a rebuild, the same as a change
to `.qlever/converters.json` — it changes what gets indexed even though its
own content never is.

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

Once a file is fixed and saved, the next rebuild replaces the error quad with
the actual triples.

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

A change to a file in `/data` triggers a rebuild according to these rules:

- **No rebuild currently running**: the first change schedules a rebuild
  exactly `REBUILD_DELAY` seconds in the future. Further changes during that
  window are noted but do **not** push back the deadline. So when the system
  is idle, a rebuild starts at most `REBUILD_DELAY` seconds after the change.
- **A rebuild is currently running** (which can take seconds, minutes, or
  hours depending on data volume): additional changes set a `change_pending`
  flag. Multiple changes collapse to a single queued rebuild — never more
  than one is queued at a time.
- **When the current rebuild finishes** and `change_pending` is set, the
  next rebuild starts immediately, without re-debouncing.

So the overall guarantee is: a change triggers a rebuild **either** at most
`REBUILD_DELAY` seconds later (if the system was idle), **or** immediately
after the currently running rebuild finishes. Two rebuilds never run in
parallel. On a directory under continuous write load, you'll see a
back-to-back rebuild loop with no idle gaps.

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
- `build_index.sh` — converts triple files (and non-RDF files via declared
  converters) to N-Quads and feeds them to `qlever-index`.
- `examples/projects/` — a worked converter example: project Markdown files with
  a `.qlever/converters.json` and a dependency-free Markdown→Turtle converter.
- `nginx.conf` — proxies port 7001 to the active QLever slot. Includes the
  dynamic upstream file `/run/nginx-upstream.conf`.
- `docker-compose.yml` — minimal usage example.
