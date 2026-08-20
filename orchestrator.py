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
    """Write the nginx upstream config fragment and reload nginx."""
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


def start_nginx() -> None:
    result = subprocess.run(
        ["nginx", "-t"], capture_output=True, text=True
    )
    if result.returncode != 0:
        log(f"nginx config test failed: {result.stderr.strip()}")
        sys.exit(1)
    subprocess.run(["nginx"], check=True)
    log("nginx started")


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
        stop_qlever(active_proc, active_slot)

    log(f"Blue-green swap complete: active_slot={target_slot}")
    return target_slot, new_proc


# ---------------------------------------------------------------------------
# inotify watcher
# ---------------------------------------------------------------------------


def watch_data_dir(event_callback):
    """Run inotifywait in a background thread; call event_callback on changes."""
    def _run():
        cmd = [
            "inotifywait",
            "-m",          # monitor mode (don't exit after first event)
            "-r",          # recursive
            "-e", "close_write,create,delete,move",
            "--format", "%w%f",
            "/data",
        ]
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
                path = line.decode(errors="replace").strip()
                # Only react to RDF triple files
                if path.endswith((".nt", ".ttl", ".n3")):
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
    start_nginx()

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
