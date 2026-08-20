#!/usr/bin/env python3
"""
orchestrator.py — Blue-green QLever index manager with inotify-based rebuild.

Slots:
  a -> index dir /index-a, port 7101
  b -> index dir /index-b, port 7102

nginx on port 7001 proxies to the active slot.

State machine:
  IDLE      -> watching; the first FS change schedules a rebuild
               REBUILD_DELAY seconds in the future. Further changes during
               that window do not push back the deadline — this guarantees
               a rebuild starts at most REBUILD_DELAY seconds after the
               first change, even on a continuously changing directory.
  BUILDING  -> rebuild running; further changes set change_pending=True

After a build completes in BUILDING state:
  - If change_pending: immediately start another build (stays BUILDING)
  - Else: return to IDLE

This guarantees no two rebuilds run in parallel, but rebuilds run back-to-back
if the filesystem keeps changing during a build.
"""

import glob
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URI = os.environ.get("BASE_URI", "https://example.org/data/")
REBUILD_DELAY = int(os.environ.get("REBUILD_DELAY", "15"))

SLOT_CONFIG = {
    "a": {"index_dir": "/index-a", "port": 7101},
    "b": {"index_dir": "/index-b", "port": 7102},
}
NGINX_UPSTREAM_FILE = "/run/nginx-upstream.conf"
INDEX_NAME = "rdf-store"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[{ts}] [orchestrator] {msg}", flush=True)


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


def start_qlever(slot: str) -> subprocess.Popen:
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
        ],
        cwd=index_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Drain server output in a daemon thread so it doesn't block
    def _drain(proc: subprocess.Popen, slot: str) -> None:
        for line in proc.stdout:
            print(f"[qlever-server:{slot}] {line.decode(errors='replace').rstrip()}", flush=True)

    t = threading.Thread(target=_drain, args=(proc, slot), daemon=True)
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


def health_check(port: int, timeout_seconds: int = 300) -> bool:
    """Poll the SPARQL endpoint until it responds 200 or timeout."""
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
        time.sleep(3)
    log(f"Port {port} did not become healthy within {timeout_seconds}s")
    return False


# ---------------------------------------------------------------------------
# Index build
# ---------------------------------------------------------------------------


def build_index(slot: str) -> bool:
    """Build a fresh index into the given slot's index dir. Returns success."""
    cfg = SLOT_CONFIG[slot]
    index_dir = cfg["index_dir"]
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


# ---------------------------------------------------------------------------
# Blue-green rebuild
# ---------------------------------------------------------------------------


def do_rebuild(active_slot: str | None, active_proc: subprocess.Popen | None):
    """
    Full blue-green rebuild cycle.

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

    new_proc = start_qlever(target_slot)

    if not health_check(target_port):
        log("Rebuild aborted: new instance failed health check; keeping current slot active")
        stop_qlever(new_proc, target_slot)
        return active_slot, active_proc

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
      - the file's extension is a currently-known converter
        extension                                          -> "converter-extension"
    """
    if path.endswith((".nt", ".ttl", ".n3")):
        return "rdf-extension"
    if "ISDIR" in flags:
        return "isdir"
    if os.path.basename(path) == "converters.json" and "/.qlever/" in path:
        return "converters-json"
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext and ext in converter_exts:
        return "converter-extension"
    return None


def watch_data_dir(event_callback):
    """Run inotifywait in a background thread; call event_callback on changes."""
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
                elif reason == "converter-extension":
                    ext = os.path.splitext(path)[1].lstrip(".").lower()
                    log(f"FS change detected: {path} — converter extension '.{ext}' — triggering rebuild")
                else:  # rdf-extension
                    log(f"FS change detected: {path}")
                event_callback()

            rc = proc.wait()
            log(f"inotifywait exited rc={rc} — restarting watcher in 5s")
            time.sleep(5)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main():
    log(f"Starting orchestrator BASE_URI={BASE_URI} REBUILD_DELAY={REBUILD_DELAY}s")

    active_slot: str | None = None
    active_proc: subprocess.Popen | None = None

    state = "IDLE"          # "IDLE" | "BUILDING"
    change_pending = False
    debounce_deadline: float | None = None
    state_lock = threading.Lock()

    def on_fs_change():
        nonlocal debounce_deadline, change_pending
        with state_lock:
            if state == "IDLE":
                if debounce_deadline is None:
                    debounce_deadline = time.time() + REBUILD_DELAY
                    log(f"Rebuild scheduled in {REBUILD_DELAY}s")
            else:
                change_pending = True
                log("Change detected during build: queuing one more rebuild")

    Path("/run").mkdir(parents=True, exist_ok=True)
    write_upstream(SLOT_CONFIG["a"]["port"])
    nginx_pid = start_nginx()

    log("Performing initial index build ...")
    state = "BUILDING"
    active_slot, active_proc = do_rebuild(None, None)
    state = "IDLE"
    if active_slot is None:
        log("Initial build failed — exiting")
        sys.exit(1)
    log(f"Initial build complete. Active slot={active_slot}")

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
            while True:
                new_slot, new_proc = do_rebuild(active_slot, active_proc)
                active_slot = new_slot
                active_proc = new_proc

                with state_lock:
                    if change_pending:
                        change_pending = False
                        log("Queued change pending — starting next rebuild immediately")
                        continue
                    state = "IDLE"
                    log("Rebuild complete — back to IDLE")
                    break


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
