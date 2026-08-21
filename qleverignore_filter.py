#!/usr/bin/env python3
"""qleverignore_filter.py -- .qleverignore filtering, factored out of
build_index.sh so the exact same logic can also answer a single-file
"is this ignored?" question for the (future) incremental update path.

A data directory can exclude files from its own subtree by dropping a
.qleverignore file next to them -- one gitignore-style glob pattern per
line, relative to the directory containing that .qleverignore. See the
README for the full pattern semantics (notably: no "!" negation support).

Modes:
  (no arguments)
      Reads NUL-separated absolute candidate paths on stdin, writes the
      ones that survive filtering NUL-separated to stdout. Per-.qleverignore
      exclusion counts are logged to stderr. This is build_index.sh's batch
      filtering pass over its full file scan.
  --check FILEPATH
      Exit 0 if FILEPATH is NOT ignored, exit 1 if it is. No stdout output,
      nothing logged. Meant for the incremental path to check a single
      changed file without re-scanning everything through the pipe protocol.

Env vars:
  DATA_ROOT   Root directory .qleverignore files are discovered under, and
              candidate/checked paths are considered relative to.
              Default: /data
"""
import fnmatch
import os
import sys

DATA_ROOT = os.path.abspath(os.environ.get("DATA_ROOT", "/data"))


def log(msg):
    print(f"[build_index] {msg}", file=sys.stderr)


def discover_patterns(data_root):
    """directory -> list of glob patterns declared by that directory's
    .qleverignore, for every .qleverignore found under data_root (skipping
    .git and .qlever directories, same as the RDF scan itself)."""
    ignore_files = []
    for dirpath, dirnames, filenames in os.walk(data_root):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".qlever")]
        if ".qleverignore" in filenames:
            ignore_files.append(os.path.join(dirpath, ".qleverignore"))

    patterns_by_dir = {}
    for path in ignore_files:
        d = os.path.dirname(path)
        patterns = []
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError as e:
            log(f".qleverignore ERROR {path}: {e}")
            continue
        for line in text.splitlines():
            line = line.rstrip()
            if not line or line.lstrip().startswith("#"):
                continue
            if line.startswith("!"):
                log(
                    f".qleverignore WARNING {path}: negation ('!') patterns are "
                    f"not supported and this line is ignored: {line}"
                )
                continue
            patterns.append(line)
        patterns_by_dir[d] = patterns
    return patterns_by_dir


def excluding_dir(filepath, patterns_by_dir, data_root):
    """The directory of an ancestor .qleverignore whose patterns match
    filepath, or None if no ancestor .qleverignore matches. Any match from
    any ancestor excludes the file -- there is no "nearest wins"."""
    d = os.path.dirname(filepath)
    while True:
        patterns = patterns_by_dir.get(d)
        if patterns:
            rel = os.path.relpath(filepath, d)
            base = os.path.basename(filepath)
            for pat in patterns:
                if fnmatch.fnmatchcase(rel, pat):
                    return d
                # A pattern without '/' also matches the basename at any
                # depth under this directory (gitignore behaviour).
                if "/" not in pat and fnmatch.fnmatchcase(base, pat):
                    return d
        if d == data_root:
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def run_filter():
    patterns_by_dir = discover_patterns(DATA_ROOT)
    exclude_counts = {d: 0 for d in patterns_by_dir}

    stdin_bytes = sys.stdin.buffer.read()
    candidates = stdin_bytes.split(b"\0") if stdin_bytes else []
    if candidates and candidates[-1] == b"":
        candidates = candidates[:-1]

    out = sys.stdout.buffer
    for candidate in candidates:
        filepath = candidate.decode("utf-8", errors="surrogateescape")
        excl_dir = excluding_dir(filepath, patterns_by_dir, DATA_ROOT)
        if excl_dir is None:
            out.write(candidate)
            out.write(b"\0")
        else:
            exclude_counts[excl_dir] += 1

    for d in sorted(exclude_counts):
        count = exclude_counts[d]
        if count > 0:
            rel_d = os.path.relpath(d, DATA_ROOT)
            label = "." if rel_d == "." else rel_d
            log(f"Excluded {count} file(s) via {label}/.qleverignore")


def run_check(filepath):
    patterns_by_dir = discover_patterns(DATA_ROOT)
    filepath = os.path.abspath(filepath)
    ignored = excluding_dir(filepath, patterns_by_dir, DATA_ROOT) is not None
    sys.exit(1 if ignored else 0)


def main():
    if len(sys.argv) == 1:
        run_filter()
    elif len(sys.argv) == 3 and sys.argv[1] == "--check":
        run_check(sys.argv[2])
    else:
        print("Usage: qleverignore_filter.py [--check FILEPATH]", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
