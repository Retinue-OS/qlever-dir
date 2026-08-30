#!/usr/bin/env python3
"""
orchestrator.py — Blue-green QLever index manager with incremental SPARQL
Update and inotify-based rebuild.

Slots:
  a -> index dir /index-a, port 7101
  b -> index dir /index-b, port 7102

nginx on port 7001 proxies to the active slot.

Two update paths now exist:
  - INCREMENTAL (the common case): a single file's rdf-extension or
    converter-extension change is applied straight to the ACTIVE slot via a
    whole-graph SPARQL Update (DROP + INSERT DATA for that file's graph, or
    DROP alone if the file went away or became ignored) — visible in
    seconds, no rebuild, no swap.
  - FULL REBUILD: still the blue-green build-into-idle-slot/health-check/
    swap cycle below, but it is now a *compaction* pass, not the visibility
    mechanism. It still runs for structural changes a single-file diff can't
    express (a new/removed directory, a .qlever/converters.json or
    .qleverignore edit — either can change which files are indexed at all),
    and periodically once enough incremental deltas have piled up (see
    COMPACTION_DELTA_TRIPLES) because QLever's query performance degrades as
    unmerged delta triples accumulate (ad-freiburg/qlever#2449).

State machine (drives FULL REBUILDs only — incremental updates bypass it
entirely and are applied as soon as they're seen, debounced only by
INCREMENTAL_DELAY):
  IDLE      -> watching; the first qualifying change schedules a rebuild
               REBUILD_DELAY seconds in the future. Further changes during
               that window do not push back the deadline — this guarantees
               a rebuild starts at most REBUILD_DELAY seconds after the
               first change, even on a continuously changing directory.
               REBUILD_DELAY is now purely a compaction-debounce knob — it no
               longer gates how soon a change is visible to queries.
  BUILDING  -> rebuild running; further qualifying changes set
               change_pending=True

After a build completes in BUILDING state:
  - If change_pending: immediately start another build (stays BUILDING)
  - Else: return to IDLE

This guarantees no two rebuilds run in parallel, but rebuilds run back-to-back
if the filesystem keeps changing during a build.

Resuming across a restart: whenever /index-a and/or /index-b still hold the
index of a previous run — always the case for `docker restart` of the same
container, and across recreation too when those paths are volumes or bind
mounts — main() immediately serves whichever slot holds the most recently
COMPLETED build (see find_resumable_slot()) instead of sitting on 502s for a
full rebuild. A full rebuild still always runs on startup regardless (the
container couldn't see filesystem changes while it was down), but it targets
the *other* slot, so the resumed slot keeps serving queries the whole time.
When both index dirs come up empty — a first-ever start, or a container
recreated without volumes on them — this is a no-op and startup behaves
exactly as before this feature: one blocking build with no slot to resume
from.
"""

import glob
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URI = os.environ.get("BASE_URI", "https://example.org/data/")
DATA_ROOT = os.environ.get("DATA_ROOT", "/data")
REBUILD_DELAY = int(os.environ.get("REBUILD_DELAY", "15"))
INCREMENTAL_DELAY = int(os.environ.get("INCREMENTAL_DELAY", "2"))
COMPACTION_DELTA_TRIPLES = int(os.environ.get("COMPACTION_DELTA_TRIPLES", "100000"))
RECONCILE_INTERVAL = int(os.environ.get("RECONCILE_INTERVAL", "3600"))
# Upper bound on how long a freshly started qlever-server may take to answer
# its first query before the slot is considered failed. Generous by design:
# load time grows with index size, and this is only an upper bound — a server
# that dies is detected immediately (health_check() watches its process), so
# raising it costs nothing on the failure path.
HEALTH_CHECK_TIMEOUT = int(os.environ.get("HEALTH_CHECK_TIMEOUT", "300"))

SLOT_CONFIG = {
    "a": {"index_dir": "/index-a", "port": 7101},
    "b": {"index_dir": "/index-b", "port": 7102},
}
NGINX_UPSTREAM_FILE = "/run/nginx-upstream.conf"
INDEX_NAME = "rdf-store"
MAX_UPDATE_RETRIES = 5
# Written into a slot's index dir only once a build has both finished AND
# proven servable (its qlever-server passed a health check) — see
# mark_build_complete() and find_resumable_slot() for why this, and not
# manifest.tsv, is the signal a resume checks for.
BUILD_COMPLETE_SENTINEL = ".orchestrator-build-complete"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[{ts}] [orchestrator] {msg}", flush=True)


def redact(text: str, token: str) -> str:
    """Replace every occurrence of the access token with "[redacted]".

    qlever-server prints the token in cleartext in its own startup banner,
    and a URL we log ourselves can carry it as the access-token query
    param — this is the one guard between either of those and the container
    logs, so every log path that might echo untrusted/generated text that
    could contain the token must run it through here first.
    """
    if not token:
        return text
    return text.replace(token, "[redacted]")


# ---------------------------------------------------------------------------
# nginx upstream management
# ---------------------------------------------------------------------------


def write_upstream(port: int) -> None:
    """Write the nginx upstream config fragment. Does NOT reload nginx —
    call reload_nginx() separately for the new upstream to take effect."""
    content = f"upstream qlever_active {{ server 127.0.0.1:{port}; }}\n"
    Path(NGINX_UPSTREAM_FILE).write_text(content)
    log(f"Wrote nginx upstream -> 127.0.0.1:{port}")


def reload_nginx() -> None:
    result = subprocess.run(
        ["nginx", "-s", "reload"], capture_output=True, text=True
    )
    if result.returncode != 0:
        log(f"nginx reload stderr: {result.stderr.strip()}")
    else:
        log("nginx reloaded")


def read_nginx_pid() -> int | None:
    """Read the nginx master PID from the pid file (nginx.conf sets
    `pid /run/nginx.pid;`). Returns None if the file doesn't exist yet or
    doesn't contain a valid pid."""
    try:
        return int(Path("/run/nginx.pid").read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def start_nginx() -> int:
    """Start nginx and return its master PID.

    `nginx` (without -g "daemon off;") daemonizes itself: subprocess.run()
    only confirms the initial fork/exec succeeded, not that the master
    process is still alive afterwards. We additionally wait for and read
    back /run/nginx.pid so the caller has a real PID to supervise.
    """
    result = subprocess.run(
        ["nginx", "-t"], capture_output=True, text=True
    )
    if result.returncode != 0:
        log(f"nginx config test failed: {result.stderr.strip()}")
        sys.exit(1)
    subprocess.run(["nginx"], check=True)
    # The pid file is written asynchronously by the daemonizing master;
    # give it a brief moment to appear.
    pid = None
    for _ in range(50):  # up to ~5s
        pid = read_nginx_pid()
        if pid is not None:
            break
        time.sleep(0.1)
    if pid is None:
        log("nginx started but /run/nginx.pid never appeared — cannot supervise it")
        sys.exit(1)
    log(f"nginx started (master pid={pid})")
    return pid


def nginx_is_alive(pid: int) -> bool:
    """Check whether the nginx master process is still running.

    We are PID 1 in this container, so a dead child is reparented to us
    and lingers as a zombie (state "Z") until reaped — os.kill(pid, 0)
    still succeeds on a zombie, so it can't tell "exited, not yet reaped"
    apart from "still running". Instead we read /proc/<pid>/stat and look
    at the process state field directly: a missing /proc entry or state
    "Z" both mean nginx is effectively gone.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    # Format: "pid (comm) state ...". comm can contain spaces/parens, so
    # split off everything after the *last* ')' to find the state field.
    fields_after_comm = stat.rsplit(")", 1)[-1].split()
    state = fields_after_comm[0] if fields_after_comm else ""
    return state != "Z"


def reap_zombies() -> None:
    """Opportunistically reap exited children so PID 1 doesn't accumulate
    zombies (any reparented grandchild becomes our direct child on exit).

    This uses os.waitpid(-1, WNOHANG), which reaps whichever eligible
    child happens to have exited — it is NOT restricted to a specific pid.
    That means it must never run before active_proc.poll() has had a
    chance to observe/reap its own child: subprocess.Popen tracks a
    specific pid and gets confused if that pid is reaped out from under
    it by someone else first. Callers must call this only *after* calling
    .poll() on every Popen object they still care about in this same
    iteration, never before.
    """
    try:
        while True:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
    except ChildProcessError:
        # No children left to wait for.
        pass


# ---------------------------------------------------------------------------
# QLever server process management
# ---------------------------------------------------------------------------


def start_qlever(slot: str, token: str) -> subprocess.Popen:
    """Start qlever-server for the given slot and return the Popen object."""
    cfg = SLOT_CONFIG[slot]
    index_dir = cfg["index_dir"]
    port = cfg["port"]
    log(f"Starting qlever-server slot={slot} port={port} cwd={index_dir}")
    proc = subprocess.Popen(
        [
            "qlever-server",
            "-i", INDEX_NAME,
            "-p", str(port),
            "-j", "4",
            "-m", "2G",
            "-c", "1G",
            "-e", "512M",
            "-k", "1000",
            "-a", token,  # --access-token: gates writes/admin commands; see
                          # ACCESS_TOKEN generation in main() for why this
                          # replaces qlever-server's fail-closed
                          # --no-access-check default rather than using it.
        ],
        cwd=index_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Drain server output in a daemon thread so it doesn't block. qlever-server
    # prints the access token in cleartext in its own startup banner — redact
    # every line before it ever reaches the container logs.
    def _drain(proc: subprocess.Popen, slot: str, token: str) -> None:
        for line in proc.stdout:
            text = line.decode(errors="replace").rstrip()
            print(f"[qlever-server:{slot}] {redact(text, token)}", flush=True)

    t = threading.Thread(target=_drain, args=(proc, slot, token), daemon=True)
    t.start()
    return proc


def stop_qlever(proc: subprocess.Popen, slot: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    log(f"Stopping qlever-server slot={slot} pid={proc.pid}")
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        log(f"Forcefully killing slot={slot} pid={proc.pid}")
        proc.kill()
        proc.wait()
    log(f"Slot {slot} stopped")


def health_check(
    port: int,
    timeout_seconds: int = HEALTH_CHECK_TIMEOUT,
    proc: subprocess.Popen | None = None,
) -> bool:
    """Poll the SPARQL endpoint until it responds 200, the server dies, or
    the timeout expires.

    Pass the server's Popen as `proc` whenever it is available: a
    qlever-server that cannot serve its index at all (missing files, an
    index written by an incompatible version) exits within seconds, and
    noticing that exit is what makes a generous `timeout_seconds` cheap —
    the wait ends when the process does, not when the deadline does. The
    timeout is then only what it should be: an upper bound for a server
    that is still alive and simply loading a large index.
    """
    url = f"http://127.0.0.1:{port}/api?query=ASK+%7B%7D&outputType=json"
    deadline = time.time() + timeout_seconds
    log(f"Health-checking port {port} (timeout={timeout_seconds}s) ...")
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    log(f"Port {port} healthy after {attempt} attempt(s)")
                    return True
        except Exception:
            pass
        if proc is not None and proc.poll() is not None:
            log(
                f"qlever-server for port {port} exited with "
                f"returncode={proc.returncode} before becoming healthy — "
                f"not waiting out the remaining timeout"
            )
            return False
        time.sleep(3)
    log(f"Port {port} did not become healthy within {timeout_seconds}s")
    return False


# ---------------------------------------------------------------------------
# Index build
# ---------------------------------------------------------------------------


def _sentinel_path(slot: str) -> str:
    return os.path.join(SLOT_CONFIG[slot]["index_dir"], BUILD_COMPLETE_SENTINEL)


def mark_build_complete(slot: str) -> None:
    """Record that this slot holds a build that is complete AND servable.

    Called only once the slot's own qlever-server has passed a health check:
    a successful qlever-index run alone does not prove the result can be
    served (an index an incompatible qlever-server version cannot load still
    builds fine), and find_resumable_slot() must never prefer a slot that
    was already rejected once for exactly that reason.
    """
    try:
        Path(_sentinel_path(slot)).touch()
    except OSError as exc:
        # Not fatal: the slot is serving either way, we just lose the
        # ability to resume from it after a restart.
        log(f"WARNING: could not mark slot={slot} as complete: {exc}")


def clear_build_complete(slot: str) -> None:
    """Drop this slot's completed-build marker — its index is being replaced,
    or it has proven unservable and must not be resumed from again."""
    try:
        os.remove(_sentinel_path(slot))
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(f"WARNING: could not clear the completed-build marker for slot={slot}: {exc}")


def build_index(slot: str) -> bool:
    """Build a fresh index into the given slot's index dir. Returns success.

    Clears BUILD_COMPLETE_SENTINEL first: from the moment the build starts,
    this slot no longer holds the completed index it may have held before.
    build_index.sh itself empties the index dir at the start of every build
    (removing any prior sentinel along with everything else), but clearing it
    here too means a build that dies before that point — or a future
    build_index.sh that stops emptying the dir — can never leave a stale
    sentinel behind that would make an interrupted build look complete.

    The sentinel is *written* by mark_build_complete(), not here: finishing
    qlever-index is not yet proof the slot can be served, and only a servable
    slot is worth resuming from.
    """
    cfg = SLOT_CONFIG[slot]
    index_dir = cfg["index_dir"]
    clear_build_complete(slot)
    log(f"Building index into slot={slot} dir={index_dir}")
    result = subprocess.run(
        ["/usr/local/bin/build_index.sh", index_dir],
        env={**os.environ, "BASE_URI": BASE_URI},
    )
    if result.returncode != 0:
        log(f"Index build FAILED for slot={slot} (exit {result.returncode})")
        return False
    log(f"Index build succeeded for slot={slot}")
    return True


def find_resumable_slot() -> str | None:
    """The slot with the most recently COMPLETED build, if one survived into
    this run — determined by BUILD_COMPLETE_SENTINEL's mtime, never
    manifest.tsv: build_index.sh writes that early, before the index is
    actually built (see its module comment), so its mere presence can't
    distinguish a finished index from one a crash interrupted mid-build.

    Returns None when neither slot has a sentinel: a first-ever start, or a
    container recreated (image update, `docker compose up` after an edit)
    without volumes on /index-a and /index-b, so both index dirs came up
    empty. Behaviour is then unchanged from before this feature — start with
    a fresh build; see main().
    """
    candidates = []
    for slot in SLOT_CONFIG:
        sentinel = _sentinel_path(slot)
        try:
            mtime = os.path.getmtime(sentinel)
        except FileNotFoundError:
            continue
        candidates.append((mtime, slot))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


# ---------------------------------------------------------------------------
# Companion scripts (emit_file.sh, qleverignore_filter.py) — resolution
# ---------------------------------------------------------------------------


def _resolve_companion_script(name: str, executable: bool) -> str:
    """Resolve a companion script the same way build_index.sh resolves
    EMIT_FILE / QLEVERIGNORE_FILTER_PY: prefer BIN_DIR (default
    /usr/local/bin, where the Dockerfile COPYs it), falling back to
    alongside this file so a repo checkout (e.g. under test) works without
    installing anything system-wide. `executable` selects which test
    build_index.sh uses for that script (-x for emit_file.sh, -f for
    qleverignore_filter.py, which is invoked as `python3 <path>` rather than
    directly)."""
    bin_dir = os.environ.get("BIN_DIR", "/usr/local/bin")
    installed = os.path.join(bin_dir, name)
    ok = os.access(installed, os.X_OK) if executable else os.path.isfile(installed)
    if ok:
        return installed
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def emit_file_sh() -> str:
    return _resolve_companion_script("emit_file.sh", executable=True)


def qleverignore_filter_py() -> str:
    return _resolve_companion_script("qleverignore_filter.py", executable=False)


# ---------------------------------------------------------------------------
# SPARQL Update — incremental per-file whole-graph replace
# ---------------------------------------------------------------------------


def is_ignored(filepath: str) -> bool:
    """True if qleverignore_filter.py says filepath is excluded by some
    .qleverignore under DATA_ROOT."""
    result = subprocess.run(
        ["python3", qleverignore_filter_py(), "--check", filepath],
        env={**os.environ, "DATA_ROOT": DATA_ROOT},
    )
    return result.returncode == 1


def graph_iri_for(filepath: str) -> str:
    """The percent-encoded graph IRI for filepath, via emit_file.sh
    graph-iri — the single authority for that computation (see emit_file.sh's
    module docstring), so this can never drift from what a build indexed it
    under."""
    result = subprocess.run(
        [emit_file_sh(), "graph-iri", filepath],
        env={**os.environ, "DATA_ROOT": DATA_ROOT, "BASE_URI": BASE_URI},
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def sparql_update(port: int, update_str: str, token: str, timeout: int = 60) -> bool:
    """POST update_str as a SPARQL 1.1 Update request to the slot on PORT.

    The token is presented BOTH as an `Authorization: Bearer` header and as
    an `access-token` query parameter: QLever has historically accepted the
    URL param for write/admin auth, and Bearer-header support is newer.
    Sending both covers whichever the deployed qlever-server build actually
    honours — VERIFY AT DEPLOY which one(s) it accepts, and drop the
    redundant one once confirmed.

    Returns whether the request succeeded (HTTP 2xx). Never logs the token
    itself; the request URL carries it in the query string, so any URL we
    log is redacted first.
    """
    qs = urllib.parse.urlencode({"access-token": token})
    url = f"http://127.0.0.1:{port}/?{qs}"
    req = urllib.request.Request(
        url,
        data=update_str.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/sparql-update",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True
            log(f"SPARQL update to port {port} failed: status={resp.status}")
            return False
    except urllib.error.HTTPError as e:
        body = e.read(500).decode(errors="replace")
        log(f"SPARQL update to port {port} failed: status={e.code} body={redact(body, token)!r}")
        return False
    except Exception as e:
        log(f"SPARQL update to port {port} failed: {redact(str(e), token)}")
        return False


def apply_file_update(port: int, token: str, filepath: str) -> tuple[bool, int]:
    """Whole-graph replace for one source file against the slot on PORT:
    DROP the file's graph and, if the file still exists and isn't
    .qleverignore'd, INSERT its current triples — both in ONE SPARQL 1.1
    Update request (a ';'-separated sequence), so there is never a window
    where the graph is empty between the DROP and the INSERT.

    Returns (success, triple_count). triple_count is 0 for a DROP-only
    update: the file was deleted/moved away, is now ignored, or (for a
    converter-extension file) has no applicable converter and therefore
    produced no triples.
    """
    graph_iri = graph_iri_for(filepath)

    # lexists (not exists!): a broken symlink must still go through
    # emit_file.sh, which detects the brokenness itself and emits a
    # diagnostic triple for it — exactly like a full build would. Only a
    # path with no filesystem entry at all (deleted/moved-away) counts as
    # "gone" here.
    exists = os.path.lexists(filepath)
    ignored = exists and is_ignored(filepath)

    if not exists or ignored:
        update = f"DROP SILENT GRAPH <{graph_iri}>"
        triple_count = 0
    else:
        result = subprocess.run(
            [emit_file_sh(), "triples", filepath],
            env={**os.environ, "DATA_ROOT": DATA_ROOT, "BASE_URI": BASE_URI},
            capture_output=True,
            text=True,
        )
        triples = result.stdout
        if not triples.strip():
            update = f"DROP SILENT GRAPH <{graph_iri}>"
            triple_count = 0
        else:
            # The graph MUST be named explicitly in both halves: an
            # unqualified INSERT DATA writes to QLever's default graph and
            # reports success while changing nothing an application ever
            # queries (ad-freiburg/qlever#1730) — never drop the `GRAPH <g>`
            # wrapper here even for a "just insert" refactor later. DROP
            # SILENT so a graph that doesn't exist yet (first update for a
            # brand-new file) doesn't fail the sequence — VERIFY AT DEPLOY
            # that this qlever-server build's DROP SILENT actually no-ops on
            # a missing graph; if not, the fallback primitive is
            # `DELETE WHERE { GRAPH <g> { ?s ?p ?o } }`.
            update = (
                f"DROP SILENT GRAPH <{graph_iri}> ;\n"
                f"INSERT DATA {{ GRAPH <{graph_iri}> {{\n{triples}}} }}"
            )
            triple_count = triples.count("\n")

    ok = sparql_update(port, update, token)
    return ok, triple_count


# ---------------------------------------------------------------------------
# Reconciliation sweep
# ---------------------------------------------------------------------------


def load_manifest(index_dir: str) -> dict[str, str]:
    """graph_iri -> md5 (or "broken-symlink"), from a slot's manifest.tsv as
    build_index.sh last wrote it. Missing file (e.g. a slot that has never
    been built) -> empty manifest, everything currently on disk looks new."""
    manifest: dict[str, str] = {}
    path = os.path.join(index_dir, "manifest.tsv")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                h, _, g = line.partition("\t")
                manifest[g] = h
    except FileNotFoundError:
        log(f"reconcile: no manifest.tsv at {path} — treating baseline as empty")
    return manifest


def file_hash(filepath: str) -> str:
    """md5 of filepath's bytes, matching build_index.sh's `md5sum < file`
    (source bytes, not converter output). "broken-symlink" for a symlink
    whose target is missing or not a regular file, matching manifest.tsv's
    convention that there are no bytes to hash there."""
    if os.path.islink(filepath) and not os.path.isfile(filepath):
        return "broken-symlink"
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_source_files(data_root: str) -> list[str]:
    """Every file reconcile() should consider: .nt/.ttl/.n3 plus any
    converter extension, skipping .git and .qlever directories,
    .qleverignore'd files excluded. Mirrors build_index.sh's `find`
    selection (KEEP IN SYNC, same as converter_extensions() above).

    os.walk(..., followlinks=False) already gives us -P semantics for free
    (a symlinked directory is listed but never descended into), and — per
    its documented behaviour — a broken symlink's DirEntry.is_dir() reports
    False, so broken symlinks land in `filenames` right alongside regular
    files and working symlinks; no separate broken-symlink pass is needed
    here the way build_index.sh's `find -type l -not -xtype f` needs one.
    """
    exts = {"nt", "ttl", "n3"} | converter_extensions(data_root)
    found = []
    for dirpath, dirnames, filenames in os.walk(data_root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".qlever")]
        for name in filenames:
            ext = os.path.splitext(name)[1].lstrip(".").lower()
            if ext in exts:
                found.append(os.path.join(dirpath, name))
    if not found:
        return []
    # One batch invocation of the filter (NUL-separated, same as
    # build_index.sh uses it) instead of a --check subprocess per file —
    # this sweep runs in the supervision loop, so per-file process startups
    # would stall liveness checks for the whole scan.
    result = subprocess.run(
        ["python3", qleverignore_filter_py()],
        input=b"\0".join(f.encode("utf-8", "surrogateescape") for f in found) + b"\0",
        env={**os.environ, "DATA_ROOT": data_root},
        capture_output=True,
    )
    return [
        f.decode("utf-8", "surrogateescape")
        for f in result.stdout.split(b"\0")
        if f
    ]


def filepath_for_graph(graph_iri: str) -> str:
    """Reverse of emit_file.sh's graph-iri computation: recover the (maybe
    no-longer-existing) source path a manifest graph IRI corresponds to, so
    a vanished manifest entry can be dropped via apply_file_update — reusing
    its "file doesn't exist -> DROP SILENT" logic — rather than duplicating
    that DROP-string construction here. Only meaningful for graph IRIs of
    the BASE_URI + percent-encoded-relpath shape emit_file.sh produces,
    which is everything in our own manifest.tsv (we are the only writer of
    graph IRIs deployed here)."""
    relpath = urllib.parse.unquote(graph_iri[len(BASE_URI):])
    return os.path.join(DATA_ROOT, relpath)


def reconcile(port: int, token: str, index_dir: str, overlay: dict[str, str]) -> tuple[int, int, int]:
    """Compare the active slot's build-time manifest.tsv against DATA_ROOT's
    CURRENT state and patch any drift via apply_file_update. Backstop for
    the incremental path: catches anything a coalesced/lost inotify event,
    an incremental update that exhausted its retries, or an edge case in
    event classification let slip through.

    manifest.tsv on disk is intentionally never rewritten by this sweep —
    it stays the build's own snapshot, which is what the NEXT full rebuild's
    replay-before-swap step (see do_rebuild) and any human debugging it
    reasonably expect it to mean. Instead, confirmed fixes are tracked in
    `overlay` (graph_iri -> current hash, "" for a dropped graph), mutated
    in place and kept by the caller across calls, so repeated sweeps don't
    re-apply the same fix. The caller resets it to {} whenever a new slot
    becomes active (new manifest baseline, old overlay meaningless).

    Bounded and simple by design: O(files) with one md5 read each,
    sequential — no threading, no batching.

    Returns (scanned, reapplied, dropped).
    """
    manifest = load_manifest(index_dir)
    current_files = scan_source_files(DATA_ROOT)

    scanned = len(current_files)
    reapplied = 0
    dropped = 0
    seen_graphs = set()

    for filepath in current_files:
        graph_iri = graph_iri_for(filepath)
        seen_graphs.add(graph_iri)
        current_hash = file_hash(filepath)
        known_hash = overlay.get(graph_iri, manifest.get(graph_iri))
        if current_hash == known_hash:
            continue
        ok, _ = apply_file_update(port, token, filepath)
        if ok:
            overlay[graph_iri] = current_hash
            reapplied += 1
            log(f"reconcile: re-applied {filepath} (graph={graph_iri})")
        else:
            log(f"reconcile: FAILED to re-apply {filepath} (graph={graph_iri}) — will retry next sweep")

    for graph_iri in manifest:
        if graph_iri in seen_graphs:
            continue
        if overlay.get(graph_iri) == "":
            continue  # already dropped by an earlier sweep
        filepath = filepath_for_graph(graph_iri)
        ok, _ = apply_file_update(port, token, filepath)
        if ok:
            overlay[graph_iri] = ""
            dropped += 1
            log(f"reconcile: dropped {graph_iri} (source file no longer present: {filepath})")
        else:
            log(f"reconcile: FAILED to drop {graph_iri} — will retry next sweep")

    if reapplied + dropped > 0:
        log(f"reconcile: scanned {scanned}, re-applied {reapplied}, dropped {dropped}")
    return scanned, reapplied, dropped


# ---------------------------------------------------------------------------
# Blue-green rebuild
# ---------------------------------------------------------------------------


def do_rebuild(
    active_slot: str | None,
    active_proc: subprocess.Popen | None,
    token: str,
    dirty_paths: set[str],
    dirty_lock: threading.Lock,
):
    """
    Full blue-green rebuild cycle — now also a compaction pass: it folds
    every incremental delta applied since the current active slot's own
    last build into one clean index.

    Builds into the slot opposite of active_slot. If active_slot is None
    (initial build), uses slot "a".

    Returns (new_active_slot, new_active_proc).
    """
    if active_slot is None:
        target_slot = "a"
    else:
        target_slot = "b" if active_slot == "a" else "a"

    target_port = SLOT_CONFIG[target_slot]["port"]
    log(f"Blue-green rebuild: building into slot={target_slot}")

    if not build_index(target_slot):
        log("Rebuild aborted: index build failed; keeping current slot active")
        return active_slot, active_proc

    new_proc = start_qlever(target_slot, token)

    if not health_check(target_port, proc=new_proc):
        log("Rebuild aborted: new instance failed health check; keeping current slot active")
        stop_qlever(new_proc, target_slot)
        # No mark_build_complete(): the index built, but it could not be
        # served, so it must not out-rank the still-healthy active slot as a
        # resume candidate after a restart. build_index() already cleared
        # any marker this slot carried from an earlier build.
        return active_slot, active_proc

    # The index is built AND proven servable — only now is this slot worth
    # resuming from after a restart. Written before the swap so that a crash
    # between here and the flip still leaves the newest good index findable.
    mark_build_complete(target_slot)

    # Replay-before-flip: the scan build_index.sh just ran may predate any
    # filesystem changes that landed while it was building (or, for the
    # initial build, before the watcher even started). Take the CURRENT
    # dirty_paths snapshot now — after the scan, right before the slot goes
    # live — and apply each one against the new slot so it never serves
    # data staler than what's already been (or is about to be) visible on
    # the outgoing active slot. A superset snapshot is fine: apply_file_update
    # is a full whole-graph replace, so replaying an already-current path is
    # a harmless no-op, not a correctness problem.
    with dirty_lock:
        replay_snapshot = set(dirty_paths)
    if replay_snapshot:
        log(f"Replaying {len(replay_snapshot)} pending change(s) into slot={target_slot} before swap")
        succeeded = set()
        for filepath in replay_snapshot:
            ok, _ = apply_file_update(target_port, token, filepath)
            if ok:
                succeeded.add(filepath)
            else:
                log(f"Replay FAILED for {filepath} against slot={target_slot} — leaving it dirty; "
                    f"reconciliation sweep will catch it. Not aborting the swap.")
        with dirty_lock:
            dirty_paths.difference_update(succeeded)

    write_upstream(target_port)
    reload_nginx()
    log(f"Traffic swapped to slot={target_slot} port={target_port}")

    if active_proc is not None and active_slot is not None:
        # Give nginx's old workers a moment to finish in-flight requests
        # against the previous backend before we SIGTERM it — `nginx -s
        # reload` starts new workers with the new upstream and tells old
        # workers to shut down gracefully, but that handoff is not
        # instantaneous, so stopping the old qlever-server immediately
        # after reload_nginx() can race a request still in flight.
        time.sleep(2)
        stop_qlever(active_proc, active_slot)

    log(f"Blue-green swap complete: active_slot={target_slot}")
    return target_slot, new_proc


# ---------------------------------------------------------------------------
# Converter-extension discovery
# ---------------------------------------------------------------------------


def converter_extensions(data_root: str = "/data") -> set[str]:
    """Extensions with a converter declared in some .qlever/converters.json
    under data_root, e.g. {"md", "csv"}.

    KEEP IN SYNC: this mirrors the inline `python3 -` heredoc in
    build_index.sh (the one that populates CONVERTER_EXTS) line-for-line in
    semantics — same glob, same "keys of each JSON object, lstrip('.'),
    lower(), ignore unreadable/invalid files". build_index.sh decides what
    gets indexed; this decides what the watcher reacts to, and the two sets
    must agree or the watcher will miss (or spuriously fire for) files the
    builder does/doesn't pick up. If you change one, change the other.
    """
    exts = set()
    for cfg in glob.glob(f"{data_root}/**/.qlever/converters.json", recursive=True):
        try:
            mapping = json.load(open(cfg))
        except Exception:
            continue
        for key in mapping:
            exts.add(key.lstrip(".").lower())
    return exts


# ---------------------------------------------------------------------------
# inotify watcher
# ---------------------------------------------------------------------------


def classify_watch_event(path: str, flags: str, converter_exts: set[str]) -> str | None:
    """Decide whether an inotify event line should trigger a rebuild.

    Returns a short reason string (for logging) if it should, or None if the
    event should be ignored. converter_exts is the caller's current cached
    result of converter_extensions() — this function never re-globs.

    Trigger rules, in order:
      - native RDF extension (.nt/.ttl/.n3)              -> "rdf-extension"
      - a directory event (ISDIR in flags)                -> "isdir"
      - the file is a .qlever/converters.json itself       -> "converters-json"
        (build_index.sh's find excludes */.qlever/* from the index scan, so
        this file's own content is never indexed — but it can WIDEN the set
        of extensions that ARE indexed, so it must still trigger a rebuild)
      - the file is a .qleverignore itself                 -> "qleverignore"
        (never indexed either — it has no RDF/converter extension — but it
        changes which files build_index.sh's filter drops, so it must still
        trigger a rebuild. Unlike converters-json this can't change
        converter_exts, so no cache refresh is needed here.)
      - the file's extension is a currently-known converter
        extension                                          -> "converter-extension"
    """
    if path.endswith((".nt", ".ttl", ".n3")):
        return "rdf-extension"
    if "ISDIR" in flags:
        return "isdir"
    if os.path.basename(path) == "converters.json" and "/.qlever/" in path:
        return "converters-json"
    if os.path.basename(path) == ".qleverignore":
        return "qleverignore"
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext and ext in converter_exts:
        return "converter-extension"
    return None


def watch_data_dir(event_callback):
    """Run inotifywait in a background thread; call event_callback(path, reason)
    on every qualifying change — reason is whatever classify_watch_event()
    returned (never None; None events are filtered out below and never
    reach the callback)."""
    def _run():
        cmd = [
            "inotifywait",
            "-m",          # monitor mode (don't exit after first event)
            "-r",          # recursive
            "-e", "close_write,create,delete,move",
            "--format", "%e %w%f",
            "/data",
        ]
        # Cache of converter extensions (e.g. {"md", "csv"}), so we don't
        # re-glob /data/**/.qlever/converters.json on every single event.
        # Computed fresh each time inotifywait (re)starts, and refreshed
        # whenever a converters.json change or a directory event is seen —
        # either can introduce/remove a .qlever/converters.json and change
        # the set.
        converter_exts = converter_extensions()
        log(f"Converter extensions: {sorted(converter_exts) or '(none)'}")

        # inotifywait can exit on its own (watch limit hit, killed, /data
        # unmounted, ...); restart it rather than let the watcher die silently.
        while True:
            log("Starting inotifywait on /data ...")
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            # Drain stderr in its own thread so it doesn't block: inotifywait
            # writes "Setting up watches" and one line per failed watch there,
            # and an undrained pipe fills up and makes it hang in write().
            def _drain_stderr(proc: subprocess.Popen) -> None:
                for line in proc.stderr:
                    log(f"[inotifywait] {line.decode(errors='replace').rstrip()}")

            t_stderr = threading.Thread(target=_drain_stderr, args=(proc,), daemon=True)
            t_stderr.start()

            for line in proc.stdout:
                line_str = line.decode(errors="replace").strip()
                # Split event flags from path (flags never contain spaces; paths may)
                flags, _, path = line_str.partition(" ")
                reason = classify_watch_event(path, flags, converter_exts)
                if reason is None:
                    continue

                if reason in ("isdir", "converters-json"):
                    # A new directory or a changed converters.json can widen
                    # (or narrow) the indexable extension set — refresh the
                    # cache before the next ordinary-file event relies on it.
                    converter_exts = converter_extensions()

                if reason == "isdir":
                    log(f"FS change detected: {path} (flags: {flags}) — triggering rebuild to rescan directory")
                elif reason == "converters-json":
                    log(f"FS change detected: {path} (flags: {flags}) — converters.json changed, refreshed extension set to {sorted(converter_exts) or '(none)'} — triggering rebuild")
                elif reason == "qleverignore":
                    log(f"FS change detected: {path} (flags: {flags}) — .qleverignore changed — triggering rebuild")
                elif reason == "converter-extension":
                    ext = os.path.splitext(path)[1].lstrip(".").lower()
                    log(f"FS change detected: {path} — converter extension '.{ext}' — triggering rebuild")
                else:  # rdf-extension
                    log(f"FS change detected: {path}")
                event_callback(path, reason)

            rc = proc.wait()
            log(f"inotifywait exited rc={rc} — restarting watcher in 5s")
            time.sleep(5)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main():
    # Generated fresh per container start: this is what gates every write/
    # admin request qlever-server accepts, replacing its fail-closed
    # --no-access-check default. Never logged in the clear — see redact()
    # and every call site below that touches it.
    token = secrets.token_hex(32)

    log(
        f"Starting orchestrator BASE_URI={BASE_URI} REBUILD_DELAY={REBUILD_DELAY}s "
        f"INCREMENTAL_DELAY={INCREMENTAL_DELAY}s "
        f"COMPACTION_DELTA_TRIPLES={COMPACTION_DELTA_TRIPLES} "
        f"RECONCILE_INTERVAL={RECONCILE_INTERVAL}s"
    )

    active_slot: str | None = None
    active_proc: subprocess.Popen | None = None

    state = "IDLE"          # "IDLE" | "BUILDING" — drives FULL REBUILDs only
    change_pending = False
    debounce_deadline: float | None = None
    state_lock = threading.Lock()

    # --- Incremental-update bookkeeping, all protected by state_lock (reused
    # rather than adding a second lock — see module docstring). ------------
    # path -> time it becomes eligible for the drain loop to apply it. Set to
    # (now + INCREMENTAL_DELAY) on every qualifying FS event for that path —
    # a dict, so repeated events on the same file naturally coalesce into
    # one entry, and each new event pushes eligibility out, so a burst of
    # writes from one editor save is applied once, after the burst quiets
    # down, not once per event.
    pending: dict[str, float] = {}
    pending_retries: dict[str, int] = {}   # path -> retry count so far
    # Every path changed since the currently-building (or about to build)
    # slot's manifest was scanned — replayed into the new slot right before
    # it goes live (see do_rebuild), cleared per-path only once replayed
    # successfully. A superset is harmless (whole-graph replace is
    # idempotent), so this is never cleared just because the ordinary
    # incremental drain below also happened to apply the same path.
    dirty_paths: set[str] = set()
    delta_triples_applied = 0   # reset to 0 whenever a full rebuild completes

    # In-memory reconcile() overlay (graph_iri -> current hash / "" for
    # dropped) — see reconcile()'s docstring for why this exists instead of
    # rewriting manifest.tsv. Reset whenever active_slot changes: a new slot
    # means a new manifest.tsv baseline, so the old overlay no longer means
    # anything.
    reconcile_overlay: dict[str, str] = {}
    next_reconcile_at: float | None = None

    def schedule_rebuild(reason: str):
        nonlocal debounce_deadline, change_pending
        with state_lock:
            if state == "IDLE":
                if debounce_deadline is None:
                    debounce_deadline = time.time() + REBUILD_DELAY
                    log(f"Rebuild scheduled in {REBUILD_DELAY}s ({reason})")
            else:
                change_pending = True
                log(f"Change detected during build: queuing one more rebuild ({reason})")

    def on_fs_change(path: str, reason: str):
        # Structural changes (a new/removed directory, or something that can
        # change *which* files get indexed at all) can't be expressed as a
        # single-file diff — they still need a full rescan/rebuild.
        if reason in ("isdir", "converters-json", "qleverignore"):
            schedule_rebuild(reason)
            return
        # rdf-extension / converter-extension: one file's content changed.
        # Route it to the incremental path instead — no debounce-triggered
        # rebuild at all.
        eligible_at = time.time() + INCREMENTAL_DELAY
        with state_lock:
            pending[path] = eligible_at
            dirty_paths.add(path)

    Path("/run").mkdir(parents=True, exist_ok=True)

    # Resume from an index that outlived the previous run, so the endpoint
    # serves the last known-good build immediately instead of sitting on
    # 502s for the whole first build — see find_resumable_slot(). When
    # neither index dir carries a completed build (a first-ever start, or a
    # recreated container without volumes on /index-a and /index-b),
    # resume_slot is None and this whole block is skipped, exactly matching
    # pre-existing behaviour: rebuild from scratch is still correct here —
    # the container couldn't react to filesystem changes while it was down —
    # the point is only that a stale-but-valid index beats no index at all
    # while catching up.
    resume_slot = find_resumable_slot()
    if resume_slot is not None:
        resume_port = SLOT_CONFIG[resume_slot]["port"]
        log(f"Found a completed build in slot={resume_slot} from a previous run — resuming from it")
        write_upstream(resume_port)
        nginx_pid = start_nginx()
        resume_proc = start_qlever(resume_slot, token)
        if health_check(resume_port, proc=resume_proc):
            active_slot, active_proc = resume_slot, resume_proc
            log(f"Resumed slot={resume_slot} is serving queries — catching up on any changes made while the container was down")
        else:
            log(f"Resumed slot={resume_slot} failed its health check — discarding it and doing a fresh build instead")
            stop_qlever(resume_proc, resume_slot)
            # Whatever is in that dir cannot be served by this image (an
            # index written by an incompatible qlever-server version, say):
            # don't let the next restart pay the same failed startup again.
            clear_build_complete(resume_slot)
    else:
        write_upstream(SLOT_CONFIG["a"]["port"])
        nginx_pid = start_nginx()

    resumed_slot = active_slot   # None unless the block above resumed one
    if resumed_slot is None:
        log("Performing initial index build ...")
    else:
        log("Running the startup catch-up build while the resumed slot serves queries ...")
    state = "BUILDING"
    active_slot, active_proc = do_rebuild(active_slot, active_proc, token, dirty_paths, state_lock)
    state = "IDLE"
    if active_slot is None:
        log("Initial build failed — exiting")
        sys.exit(1)
    if active_slot == resumed_slot:
        # do_rebuild() aborted and handed the resumed slot straight back:
        # nothing was swapped in, so the endpoint is still answering from an
        # index built before the container went down. That is far better
        # than exiting (restarting would only lose a working endpoint and
        # retry the same build), but it must not be the end of the story —
        # schedule the retry that the watch loop reschedules for as long as
        # the build keeps failing.
        log(
            f"Startup catch-up build FAILED — slot={active_slot} keeps serving the "
            f"index it resumed from, which may be stale; retrying in {REBUILD_DELAY}s"
        )
        schedule_rebuild("startup catch-up build failed")
    else:
        log(f"Startup build complete. Active slot={active_slot}")
    reconcile_overlay = {}
    reconcile(SLOT_CONFIG[active_slot]["port"], token, SLOT_CONFIG[active_slot]["index_dir"], reconcile_overlay)
    if RECONCILE_INTERVAL > 0:
        next_reconcile_at = time.time() + RECONCILE_INTERVAL

    # Start filesystem watcher
    watch_data_dir(on_fs_change)

    # Main event loop
    log("Entering watch loop ...")
    while True:
        time.sleep(1)

        # --- Supervision -----------------------------------------------
        # Nothing else watches the active qlever-server or the nginx
        # master: if either dies, nginx would keep proxying to (or simply
        # stop listening on) a dead backend while PID 1 stays up, so
        # `restart: unless-stopped` never triggers. Detect that here and
        # exit non-zero so the container's restart policy can recover.

        # Check active_proc FIRST (before reap_zombies()) so Python's own
        # Popen bookkeeping gets to observe/reap its exit before our
        # generic zombie sweep below could reap it out from under it.
        if active_proc is not None and active_proc.poll() is not None:
            log(
                f"qlever-server (active slot={active_slot}) exited "
                f"unexpectedly with returncode={active_proc.returncode} — "
                f"exiting orchestrator so the container restart policy "
                f"can recover"
            )
            sys.exit(1)

        if not nginx_is_alive(nginx_pid):
            log(
                f"nginx master (pid={nginx_pid}) is no longer running — "
                f"port 7001 has stopped listening; exiting orchestrator "
                f"so the container restart policy can recover"
            )
            sys.exit(1)

        # Mop up any other reparented children (e.g. orphaned nginx
        # workers) so they don't accumulate as zombies under PID 1.
        reap_zombies()

        # --- Incremental updates -----------------------------------------
        # Applied straight to the ACTIVE slot, independent of the BUILDING
        # state machine above — a compaction rebuild running in the
        # background targeting the idle slot does not pause these.
        if active_slot is not None:
            now = time.time()
            with state_lock:
                ready = [p for p, eligible_at in pending.items() if now >= eligible_at]
                for p in ready:
                    del pending[p]
            if ready:
                active_port = SLOT_CONFIG[active_slot]["port"]
                for filepath in ready:
                    ok, triple_count = apply_file_update(active_port, token, filepath)
                    if ok:
                        with state_lock:
                            pending_retries.pop(filepath, None)
                        # A DROP-only update (triple_count == 0) still
                        # changed the index — it counts as 1 delta, not 0,
                        # so an all-deletions workload still triggers
                        # compaction eventually.
                        delta_triples_applied += triple_count if triple_count > 0 else 1
                        if delta_triples_applied >= COMPACTION_DELTA_TRIPLES:
                            log(
                                f"delta_triples_applied={delta_triples_applied} >= "
                                f"COMPACTION_DELTA_TRIPLES={COMPACTION_DELTA_TRIPLES} — "
                                f"scheduling a compaction rebuild"
                            )
                            schedule_rebuild("compaction")
                    else:
                        with state_lock:
                            retries = pending_retries.get(filepath, 0) + 1
                            if retries > MAX_UPDATE_RETRIES:
                                pending_retries.pop(filepath, None)
                                log(
                                    f"ERROR: giving up on incremental update for {filepath} "
                                    f"after {MAX_UPDATE_RETRIES} retries — the reconciliation "
                                    f"sweep will catch it"
                                )
                            else:
                                pending_retries[filepath] = retries
                                pending[filepath] = time.time() + 30
                                log(
                                    f"Incremental update failed for {filepath} "
                                    f"(attempt {retries}/{MAX_UPDATE_RETRIES}) — retrying in 30s"
                                )
        # active_slot is None (initial build still running): pending/dirty
        # entries are simply left as-is and picked up once it completes.

        # --- Periodic reconciliation sweep --------------------------------
        if (
            active_slot is not None
            and next_reconcile_at is not None
            and time.time() >= next_reconcile_at
        ):
            reconcile(
                SLOT_CONFIG[active_slot]["port"],
                token,
                SLOT_CONFIG[active_slot]["index_dir"],
                reconcile_overlay,
            )
            next_reconcile_at = time.time() + RECONCILE_INTERVAL

        with state_lock:
            if state == "IDLE" and debounce_deadline is not None:
                if time.time() >= debounce_deadline:
                    debounce_deadline = None
                    state = "BUILDING"
                    trigger_build = True
                else:
                    trigger_build = False
            else:
                trigger_build = False

        if trigger_build:
            log("Debounce expired — starting rebuild")
            build_failed = False
            while True:
                prev_slot = active_slot
                new_slot, new_proc = do_rebuild(active_slot, active_proc, token, dirty_paths, state_lock)
                active_slot = new_slot
                active_proc = new_proc
                # do_rebuild() always targets the *other* slot, so an
                # unchanged slot means it aborted: the build failed, or the
                # new slot never became healthy.
                build_failed = active_slot == prev_slot

                if active_slot != prev_slot:
                    # A swap actually happened (as opposed to do_rebuild
                    # aborting and returning the slot unchanged): this is a
                    # fresh manifest baseline, and every incremental delta
                    # applied against the old active slot is now folded in.
                    delta_triples_applied = 0
                    reconcile_overlay = {}
                    reconcile(
                        SLOT_CONFIG[active_slot]["port"],
                        token,
                        SLOT_CONFIG[active_slot]["index_dir"],
                        reconcile_overlay,
                    )
                    if RECONCILE_INTERVAL > 0:
                        next_reconcile_at = time.time() + RECONCILE_INTERVAL

                with state_lock:
                    if change_pending:
                        change_pending = False
                        log("Queued change pending — starting next rebuild immediately")
                        continue
                    state = "IDLE"
                    log("Rebuild failed — back to IDLE" if build_failed
                        else "Rebuild complete — back to IDLE")
                    break

            if build_failed:
                # The active slot is serving an index that is now known to be
                # behind the filesystem, and no filesystem event is
                # guaranteed to arrive to trigger another attempt. Keep
                # retrying on the REBUILD_DELAY cadence until one succeeds.
                schedule_rebuild("retrying the failed rebuild")


def handle_sigterm(signum, frame):
    log("Received SIGTERM — shutting down")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted — shutting down")
        sys.exit(0)
