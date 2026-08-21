#!/usr/bin/env bash
# build_index.sh <index_dir>
#
# Scans DATA_ROOT for .nt, .ttl, .n3 files. For each file, parses it via
# rapper into canonical N-Triples and converts every triple into a quad by
# appending a graph IRI derived from the file path. Blank node labels are
# rewritten with a per-file prefix (derived from the relative path) so that
# rapper's per-invocation "_:genid1"-style labels never collide across files.
#
#   graph IRI = <BASE_URI><percent-encoded relative path from DATA_ROOT>
#
# The relative path is percent-encoded so that any character a filename can
# legally contain — spaces, quotes, backslashes, '&', '|', control
# characters, ... — yields a well-formed IRI instead of corrupting or
# breaking it.
#
# e.g. /data/health/obs.nt -> <https://example.org/data/health/obs.nt>
#
# Non-RDF files can also be indexed when a converter is declared for their
# extension in a .qlever/converters.json file. The nearest such file walking up
# from the source file (up to DATA_ROOT) wins, so a .qlever/ directory provides
# converters for the files in its directory and subtree. A converter is any
# executable invoked as `<converter> <input-file>` that emits Turtle on stdout;
# the source file keeps its own path-derived graph IRI, so triples stay
# attributed to the Markdown/CSV/... they came from.
#
# If a file fails to parse — or a converter exits non-zero — instead of its
# (partial) triples we emit one diagnostic quad describing the failure:
#
#   <graph> <urn:qlever-dir:parsingError> "stderr message" <graph> .
#
# The build itself still succeeds — broken files surface as queryable
# annotations rather than blocking the whole store update.
#
# Symlinks are scanned by dereferencing at the type-test step only (-xtype f),
# not while walking directories, so a matching symlink is indexed like a
# regular file but a symlinked directory is never descended into. A symlink
# whose target is missing or is not a regular file cannot be indexed either
# way, so it gets a diagnostic quad of its own instead of being silently
# dropped from the scan.
#
# A data directory can exclude files from its own subtree by dropping a
# .qleverignore file next to them — one gitignore-style glob pattern per
# line, relative to the directory containing that .qleverignore. This lets a
# chamber declare its own exclusions (e.g. a large dump meant for a different
# store) without the container needing to know about it. See
# qleverignore_filter.py and the README for the exact pattern semantics
# (notably: no "!" negation support).
#
# The actual file -> triples/quads conversion (converter lookup, rapper
# invocation, blank-node prefixing, error-quad emission, ...) lives in
# emit_file.sh, not in this script — it is the single authority for that so
# the (future) incremental SPARQL Update path can reuse it and the two paths
# cannot drift. This script owns the *scan*: finding which files to feed it,
# in what order, filtered by .qleverignore.

set -euo pipefail

INDEX_DIR="${1:?Usage: build_index.sh <index_dir>}"
export DATA_ROOT="${DATA_ROOT:-/data}"
export BASE_URI="${BASE_URI:-https://example.org/data/}"
INDEX_NAME="rdf-store"

log() { echo "[build_index] $*" >&2; }

# Locate emit_file.sh and qleverignore_filter.py: prefer the installed
# location (BIN_DIR, as COPY'd into the image by the Dockerfile), but fall
# back to this script's own directory so a repo checkout (e.g. under test)
# works without installing anything system-wide.
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
if [[ -x "${BIN_DIR}/emit_file.sh" ]]; then
    EMIT_FILE="${BIN_DIR}/emit_file.sh"
else
    EMIT_FILE="${SCRIPT_DIR}/emit_file.sh"
fi
if [[ -f "${BIN_DIR}/qleverignore_filter.py" ]]; then
    QLEVERIGNORE_FILTER_PY="${BIN_DIR}/qleverignore_filter.py"
else
    QLEVERIGNORE_FILTER_PY="${SCRIPT_DIR}/qleverignore_filter.py"
fi

mkdir -p "${INDEX_DIR}"
find "${INDEX_DIR}" -mindepth 1 -delete

cd "${INDEX_DIR}"

cat > "${INDEX_NAME}.settings.json" <<'EOF'
{ "num-triples-per-batch": 500000 }
EOF

# Track parse/convert failures across the pipeline subshell via a shared file
# (the for-loop runs in a subshell because of the pipe to qlever-index).
# emit_file.sh appends one line per failed file to this when ERRORS_FILE is
# exported, which is how the final ok=/errors= summary below stays accurate.
ERRORS_FILE=$(mktemp)
export ERRORS_FILE
trap 'rm -f "${ERRORS_FILE}"' EXIT

# Extensions that have a converter declared in any .qlever/converters.json
# under DATA_ROOT. Files with these extensions are routed through their
# converter (see emit_file.sh) instead of being parsed as RDF.
#
# KEEP IN SYNC: orchestrator.py's converter_extensions() mirrors this inline
# python3 heredoc line-for-line in semantics (same glob, same "keys of each
# JSON object, lstrip('.'), lower(), ignore unreadable/invalid files"). The
# orchestrator's inotify watcher uses it to decide which non-RDF file
# extensions should trigger a rebuild, so it must agree with what this
# script actually indexes. If you change one, change the other.
mapfile -t CONVERTER_EXTS < <(
    python3 - <<'PY'
import glob, json, os
data_root = os.environ.get('DATA_ROOT', '/data')
exts = set()
for cfg in glob.glob(f'{data_root}/**/.qlever/converters.json', recursive=True):
    try:
        mapping = json.load(open(cfg))
    except Exception:
        continue
    for key in mapping:
        exts.add(key.lstrip('.').lower())
print('\n'.join(sorted(exts)))
PY
)

# Native RDF formats plus any extension a converter handles. The .qlever/
# directories themselves (and .git) are excluded from the scan.
FIND_NAME_ARGS=( -name "*.nt" -o -name "*.ttl" -o -name "*.n3" )
for ext in "${CONVERTER_EXTS[@]}"; do
    [[ -n "${ext}" ]] && FIND_NAME_ARGS+=( -o -name "*.${ext}" )
done

# -P (physical, the default): never follow a symlink while descending into
# directories, so a symlink to a directory is skipped rather than walked —
# no double-visited files, no loop.
# -xtype f: test the type of what a symlink points to, so a symlink to a
# regular file is picked up like the file itself would be. For a non-symlink
# entry -xtype behaves exactly like -type.
FIND_BASE=( -P "${DATA_ROOT}" -not -path '*/.qlever/*' -not -path '*/.git/*' \( "${FIND_NAME_ARGS[@]}" \) )

mapfile -d '' RDF_FILES < <(
    find "${FIND_BASE[@]}" -xtype f -print0 2>/dev/null | sort -z
)

# Symlinks matching the name patterns whose target is missing or is not a
# regular file (e.g. a directory) — -xtype f above does not match these, so
# without this pass they would vanish from the scan with no diagnostic.
mapfile -d '' BROKEN_SYMLINKS < <(
    find "${FIND_BASE[@]}" -type l -not -xtype f -print0 2>/dev/null | sort -z
)

# .qleverignore filtering: reads NUL-separated absolute candidate paths on
# stdin, writes the ones that survive filtering NUL-separated to stdout. A
# file is dropped if it matches a pattern in a .qleverignore found in its
# own directory or any ancestor up to DATA_ROOT (any match from any ancestor
# excludes it — there is no "nearest wins" and no negation). See
# qleverignore_filter.py and the README for the full pattern semantics; this
# is deliberately a small subset of real gitignore behaviour.
filter_qleverignore() {
    python3 "${QLEVERIGNORE_FILTER_PY}"
}

# Guard against empty arrays: printf '%s\0' with no arguments still emits a
# single NUL, which would round-trip as one empty-string "file".
if [[ ${#RDF_FILES[@]} -gt 0 ]]; then
    mapfile -d '' RDF_FILES < <(
        printf '%s\0' "${RDF_FILES[@]}" | filter_qleverignore
    )
fi
if [[ ${#BROKEN_SYMLINKS[@]} -gt 0 ]]; then
    mapfile -d '' BROKEN_SYMLINKS < <(
        printf '%s\0' "${BROKEN_SYMLINKS[@]}" | filter_qleverignore
    )
fi

# manifest.tsv — per-graph content manifest, written into this build's index
# directory (CWD) for the future incremental-update reconciliation sweep to
# diff against the current DATA_ROOT tree. One line per indexed file
# (including broken symlinks), TAB-separated:
#
#   <md5 of the SOURCE file's bytes>\t<graph IRI>
#
# For a converter-routed file this hashes the original file (e.g. the .md),
# not the converter's Turtle output, so touching the source file — not the
# converter program — is what invalidates the manifest entry. A broken
# symlink's "hash" is the literal string "broken-symlink" (there are no
# bytes to hash). Built from the same filtered file lists the build streams
# from, so it always reflects exactly what got indexed. Rewritten from
# scratch on every build.
log "Writing manifest.tsv ..."
: > manifest.tsv
for filepath in "${RDF_FILES[@]}"; do
    graph_iri=$("${EMIT_FILE}" graph-iri "${filepath}")
    # Hash via stdin redirection, not `md5sum FILE`: when FILE contains a
    # backslash or newline, GNU md5sum prefixes the whole output line with
    # "\" and escapes those characters in the printed filename, which would
    # otherwise corrupt the hash field below. Reading through stdin never
    # prints a filename, so there is nothing to escape.
    hash=$(md5sum < "${filepath}" | cut -d' ' -f1)
    printf '%s\t%s\n' "${hash}" "${graph_iri}" >> manifest.tsv
done
for filepath in "${BROKEN_SYMLINKS[@]}"; do
    graph_iri=$("${EMIT_FILE}" graph-iri "${filepath}")
    printf '%s\t%s\n' "broken-symlink" "${graph_iri}" >> manifest.tsv
done

if [[ ${#RDF_FILES[@]} -eq 0 && ${#BROKEN_SYMLINKS[@]} -eq 0 ]]; then
    log "No .nt/.ttl/.n3 files found under ${DATA_ROOT} — building empty index"
    PLACEHOLDER='<urn:qlever-dir:empty> <urn:qlever-dir:status> "empty" <urn:qlever-dir:meta> .'
    echo "${PLACEHOLDER}" | qlever-index \
        -i "${INDEX_NAME}" \
        -s "${INDEX_NAME}.settings.json" \
        --vocabulary-type on-disk-compressed \
        -F nq \
        -f -
    log "Empty index built in ${INDEX_DIR}"
    exit 0
fi

log "Found ${#RDF_FILES[@]} file(s):"
for f in "${RDF_FILES[@]}"; do
    log "  ${f}"
done
if [[ ${#BROKEN_SYMLINKS[@]} -gt 0 ]]; then
    log "Found ${#BROKEN_SYMLINKS[@]} unusable symlink(s):"
    for f in "${BROKEN_SYMLINKS[@]}"; do
        log "  ${f}"
    done
fi

log "Streaming as N-Quads into qlever-index ..."
{
    for filepath in "${RDF_FILES[@]}"; do
        log "Processing: ${filepath}"
        "${EMIT_FILE}" quads "${filepath}"
    done
    for filepath in "${BROKEN_SYMLINKS[@]}"; do
        log "Processing: ${filepath}"
        "${EMIT_FILE}" broken-symlink "${filepath}"
    done
} | qlever-index \
    -i "${INDEX_NAME}" \
    -s "${INDEX_NAME}.settings.json" \
    --vocabulary-type on-disk-compressed \
    -F nq \
    -f -

TOTAL_COUNT=$(( ${#RDF_FILES[@]} + ${#BROKEN_SYMLINKS[@]} ))
ERROR_COUNT=$(wc -l < "${ERRORS_FILE}" | tr -d ' ')
OK_COUNT=$(( TOTAL_COUNT - ERROR_COUNT ))
log "Index build complete in ${INDEX_DIR} — ok=${OK_COUNT} errors=${ERROR_COUNT}"
