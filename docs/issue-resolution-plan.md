# Open Issue Resolution Plan

Prioritized order for resolving the 8 open issues (as of 2026-08-20), with a
recommended agent model per task. Every issue's claims were verified against
`orchestrator.py` and `build_index.sh` at the current HEAD; all check out.

Ordering principles:

1. **Severity first**: silent failures (stale/empty data served as healthy)
   outrank noisy ones.
2. **Cluster by file**: the three watcher issues all rewrite
   `watch_data_dir()` in `orchestrator.py`; the two quad-emission issues both
   rewrite `stream_as_nquads()` in `build_index.sh`. Doing each cluster in
   sequence avoids conflicting patches.
3. **Foundations before features**: bug fixes before the enhancement (#2).

Model choice = cheapest model that can reliably complete the task.
Haiku 4.5 ($1/$5 per MTok) for mechanical, well-specified patches;
Sonnet 5 ($3/$15) for multi-file work or code with subtle failure modes;
Opus-tier is not needed for any of these — the issues themselves already
contain most of the design work.

## Cluster A — watcher reliability (`orchestrator.py`, `watch_data_dir`)

### 1. #4 — Watcher dies silently (stderr never drained, exit undetected)

First because it restructures `watch_data_dir()` (stderr drain thread +
restart-on-exit loop) that tasks 2 and 3 then build on, and because a dead
watcher makes every other watcher fix moot. The issue includes a verified
fix sketch. **Model: Sonnet 5** — small diff, but subprocess/threading
plumbing where a subtle mistake recreates the very deadlock being fixed.

### 2. #10 — Files in directories created after startup are never seen

Actively biting the framework's boot emitters (`chambers/_generated/`).
The fix is fully specified in the issue: add `%e` to the inotifywait format,
trigger on `ISDIR` events. **Model: Haiku 4.5** — mechanical, well-specified,
few lines, on top of task 1's restructured function.

### 3. #3 — Watcher ignores converter extensions

Needs a small design decision: lift the converter-extension discovery out of
`build_index.sh` so the watcher can share it, and decide whether a change to
`.qlever/converters.json` itself triggers a rebuild (it should — the issue
argues why). **Model: Sonnet 5** — cross-file refactor plus a judgment call.

## Cluster B — quad emission correctness (`build_index.sh`, `stream_as_nquads`)

### 4. #5 — Graph IRI built with sed: silent misattribution, build-breaking filenames

Highest-severity build bug: a backslash silently corrupts provenance (the
project's core feature), and a space/`|` in one filename fails the *entire*
index build despite the per-file-isolation promise. Fix: replace the `sed`
interpolation with `awk -v`, percent-encode the relative path, extend
`escape_literal` to CR/C0 controls. **Model: Sonnet 5** — shell quoting and
IRI encoding are exactly where a cheaper model produces plausible-but-wrong
escapes.

### 5. #8 — Blank nodes collide across files

Builds directly on task 4's rewritten emission pipeline (same function, same
`awk` stage), which is why it follows #5: add a per-file blank-node label
prefix derived from `relpath`, matching only subject-position and
object-position `_:` tokens. Latent today but becomes reachable the moment
the first blank-node-emitting converter lands (retinue#22 is open).
**Model: Sonnet 5** — the narrow-match requirement (don't touch `_:` inside
literals) has real sharp edges.

## Cluster C — process supervision (`orchestrator.py` main loop, `Dockerfile`, `nginx.conf`)

### 6. #7 — No supervision, no readiness signal, invisible nginx logs

Three small independent fixes: poll `active_proc` and the nginx master in
the 1-second loop (exit non-zero on death so `restart: unless-stopped`
works), add a `HEALTHCHECK` running the existing `ASK {}` query, symlink
nginx logs to stdout/stderr. Optionally fix the `write_upstream` docstring
and the reload-then-stop race. Placed after clusters A/B because its
trigger scenarios (OOM during swap, nginx master death) are less frequently
hit than the watcher/build bugs, though the impact (dead endpoint reported
healthy) is severe. **Model: Sonnet 5** — three files, process lifecycle
reasoning.

## Cluster D — converter example + enhancement

### 7. #6 — md2ttl.py: unescaped/unvalidated frontmatter interpolation

Example code, so lower operational urgency, but it is the de-facto converter
specification people copy. Fix is prescribed in the issue: validate/reject
IRI-valued fields, regex-check dates before emitting `^^xsd:date`.
**Model: Haiku 4.5** — small, single-file Python with the exact behavior
spelled out; cheap to verify by running the converter on the issue's own
test cases.

### 8. #2 — `.qleverignore` support (enhancement)

Last: a new feature, and its scan-pruning logic should land *after* the
scan/emission fixes (#5, #8, #3) so it is written against the final shape of
`build_index.sh`. Needs design care (nearest-wins gitignore-style semantics
in bash/python, README documentation). **Model: Sonnet 5** — open-ended
design within a shell pipeline; the acceptance criteria in the issue make it
testable.

## Summary table

| Order | Issue | Task | Files | Model | Rationale |
|---|---|---|---|---|---|
| 1 | #4 | Watcher stderr drain + restart loop | orchestrator.py | Sonnet 5 | Subtle concurrency; foundation for 2–3 |
| 2 | #10 | React to ISDIR events | orchestrator.py | Haiku 4.5 | Fully specified, mechanical |
| 3 | #3 | Watch converter extensions | orchestrator.py, build_index.sh | Sonnet 5 | Cross-file refactor + design call |
| 4 | #5 | awk + percent-encoded graph IRIs | build_index.sh | Sonnet 5 | Escaping sharp edges |
| 5 | #8 | Per-file blank-node prefixes | build_index.sh | Sonnet 5 | Narrow-match rewrite |
| 6 | #7 | Supervision + HEALTHCHECK + logs | orchestrator.py, Dockerfile, nginx.conf | Sonnet 5 | Multi-file lifecycle work |
| 7 | #6 | Validate md2ttl.py frontmatter | examples/.../md2ttl.py | Haiku 4.5 | Small, prescribed, self-verifiable |
| 8 | #2 | .qleverignore feature | build_index.sh, README.md | Sonnet 5 | New feature, design-sensitive |

Estimated split: 2 tasks on Haiku 4.5, 6 on Sonnet 5, none needing
Opus-tier — the issue reports (which already contain root-cause analysis and
fix sketches) did the expensive reasoning up front.
