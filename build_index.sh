#!/usr/bin/env bash
# build_index.sh <index_dir>
#
# Scans /data for .nt, .ttl, .n3 files. For each file, parses it via rapper
# into canonical N-Triples and converts every triple into a quad by appending
# a graph IRI derived from the file path. Blank node labels are rewritten
# with a per-file prefix (derived from the relative path) so that rapper's
# per-invocation "_:genid1"-style labels never collide across files.
#
#   graph IRI = <BASE_URI><percent-encoded relative path from /data>
#
# The relative path is percent-encoded (see urlencode_relpath) so that any
# character a filename can legally contain — spaces, quotes, backslashes,
# '&', '|', control characters, ... — yields a well-formed IRI instead of
# corrupting or breaking it.
#
# e.g. /data/health/obs.nt -> <https://example.org/data/health/obs.nt>
#
# Non-RDF files can also be indexed when a converter is declared for their
# extension in a .qlever/converters.json file. The nearest such file walking up
# from the source file (up to /data) wins, so a .qlever/ directory provides
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
# filter_qleverignore() below and the README for the exact pattern semantics
# (notably: no "!" negation support).

set -euo pipefail

INDEX_DIR="${1:?Usage: build_index.sh <index_dir>}"
BASE_URI="${BASE_URI:-https://example.org/data/}"
INDEX_NAME="rdf-store"
ERROR_PREDICATE="urn:qlever-dir:parsingError"

log() { echo "[build_index] $*" >&2; }

mkdir -p "${INDEX_DIR}"
find "${INDEX_DIR}" -mindepth 1 -delete

cd "${INDEX_DIR}"

cat > "${INDEX_NAME}.settings.json" <<'EOF'
{ "num-triples-per-batch": 500000 }
EOF

# Track parse/convert failures across the pipeline subshell via a shared file
# (the for-loop runs in a subshell because of the pipe to qlever-index), and
# hold the .qleverignore filter script (see filter_qleverignore() below) —
# it needs to live in a real file because the filter needs the *pipe* for
# its stdin (the NUL-separated candidate paths), which a heredoc would
# otherwise steal.
ERRORS_FILE=$(mktemp)
QLEVERIGNORE_FILTER=$(mktemp)
trap 'rm -f "${ERRORS_FILE}" "${QLEVERIGNORE_FILTER}"' EXIT

# Extensions that have a converter declared in any .qlever/converters.json
# under /data. Files with these extensions are routed through their converter
# (see find_converter / stream_as_nquads) instead of being parsed as RDF.
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
exts = set()
for cfg in glob.glob('/data/**/.qlever/converters.json', recursive=True):
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
FIND_BASE=( -P /data -not -path '*/.qlever/*' -not -path '*/.git/*' \( "${FIND_NAME_ARGS[@]}" \) )

mapfile -d '' RDF_FILES < <(
    find "${FIND_BASE[@]}" -xtype f -print0 2>/dev/null | sort -z
)

# Symlinks matching the name patterns whose target is missing or is not a
# regular file (e.g. a directory) — -xtype f above does not match these, so
# without this pass they would vanish from the scan with no diagnostic.
mapfile -d '' BROKEN_SYMLINKS < <(
    find "${FIND_BASE[@]}" -type l -not -xtype f -print0 2>/dev/null | sort -z
)

# .qleverignore: reads NUL-separated absolute candidate paths on stdin,
# writes the ones that survive filtering NUL-separated to stdout. A file is
# dropped if it matches a pattern in a .qleverignore found in its own
# directory or any ancestor up to /data (any match from any ancestor
# excludes it — there is no "nearest wins" and no negation). See the README
# for the full pattern semantics; this is deliberately a small subset of
# real gitignore behaviour.
cat > "${QLEVERIGNORE_FILTER}" <<'PY'
import fnmatch
import os
import sys

data_root = os.path.abspath("/data")


def log(msg):
    print(f"[build_index] {msg}", file=sys.stderr)


# Discover every .qleverignore under data_root, skipping .git and .qlever
# directories (same directories the RDF scan itself excludes).
ignore_files = []
for dirpath, dirnames, filenames in os.walk(data_root):
    dirnames[:] = [d for d in dirnames if d not in (".git", ".qlever")]
    if ".qleverignore" in filenames:
        ignore_files.append(os.path.join(dirpath, ".qleverignore"))

# directory -> list of glob patterns declared by that directory's .qleverignore
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

exclude_counts = {d: 0 for d in patterns_by_dir}


def excluding_dir(filepath):
    """The directory of the first (closest-or-not, any) .qleverignore whose
    patterns match filepath, or None if no ancestor .qleverignore matches."""
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


stdin_bytes = sys.stdin.buffer.read()
candidates = stdin_bytes.split(b"\0") if stdin_bytes else []
if candidates and candidates[-1] == b"":
    candidates = candidates[:-1]

out = sys.stdout.buffer
for candidate in candidates:
    filepath = candidate.decode("utf-8", errors="surrogateescape")
    excl_dir = excluding_dir(filepath)
    if excl_dir is None:
        out.write(candidate)
        out.write(b"\0")
    else:
        exclude_counts[excl_dir] += 1

for d in sorted(exclude_counts):
    count = exclude_counts[d]
    if count > 0:
        rel_d = os.path.relpath(d, data_root)
        label = "." if rel_d == "." else rel_d
        log(f"Excluded {count} file(s) via {label}/.qleverignore")
PY

filter_qleverignore() {
    python3 "${QLEVERIGNORE_FILTER}"
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

# Escape a string for use inside an N-Triples/N-Quads literal:
#   backslash -> \\, double-quote -> \", any C0 control char (newline, tab,
#   carriage return, ...) -> single space
escape_literal() {
    sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\000-\037' ' '
}

# Percent-encode a path (relative to /data) for safe use inside a graph IRI.
# Letters, digits, and IRI-safe punctuation ("/-._~!$&'()*+,;=:@") pass
# through unescaped; non-ASCII bytes also pass through raw (IRIs permit them
# directly). Everything else — spaces, '<', '>', '"', '{', '}', '|', '^',
# backtick, backslash, '%', C0 control characters, ... — is percent-encoded,
# so the result is always a legal IRI component regardless of what odd bytes
# a filename contains.
urlencode_relpath() {
    python3 - "$1" <<'PY'
import sys
s = sys.argv[1]
safe = "/-._~!$&'()*+,;=:@"
out = []
for ch in s:
    if ord(ch) > 127 or ch in safe or (ch.isalnum() and ord(ch) < 128):
        out.append(ch)
    else:
        out.extend('%%%02X' % b for b in ch.encode('utf-8'))
print(''.join(out))
PY
}

# For a non-RDF file, resolve the converter command from the nearest ancestor
# .qlever/converters.json (walking up to /data). Echoes the resolved command
# (program path made absolute relative to that .qlever dir, plus any args) or
# nothing if no converter applies to the file's extension.
find_converter() {
    python3 - "$1" <<'PY'
import json, os, sys
fp = os.path.abspath(sys.argv[1])
ext = os.path.splitext(fp)[1].lstrip('.').lower()
data_root = os.path.abspath('/data')
d = os.path.dirname(fp)
while True:
    cfg = os.path.join(d, '.qlever', 'converters.json')
    if os.path.isfile(cfg):
        try:
            mapping = json.load(open(cfg))
        except Exception:
            mapping = {}
        cmd = mapping.get(ext) or mapping.get('.' + ext)
        if cmd:
            parts = cmd.split()
            prog = parts[0]
            if not os.path.isabs(prog):
                prog = os.path.normpath(os.path.join(d, '.qlever', prog))
            print(' '.join([prog] + parts[1:]))
            break
    if os.path.abspath(d) == data_root:
        break
    parent = os.path.dirname(d)
    if parent == d:
        break
    d = parent
PY
}

# Emit a diagnostic quad for a file that could not be processed, and record it.
emit_error_quad() {
    local graph_iri="$1" stderr_file="$2" context="$3" filepath="$4"
    local error_msg
    error_msg=$(escape_literal < "${stderr_file}")
    [[ -z "${error_msg}" ]] && error_msg="${context} exited non-zero with no stderr output"
    log "${context^^} ERROR ${filepath}: ${error_msg}"
    printf '<%s> <%s> "%s" <%s> .\n' \
        "${graph_iri}" "${ERROR_PREDICATE}" "${error_msg}" "${graph_iri}"
    echo "${filepath}" >> "${ERRORS_FILE}"
}

stream_as_nquads() {
    local filepath="$1"
    local relpath="${filepath#/data/}"
    local graph_iri="${BASE_URI}$(urlencode_relpath "${relpath}")"
    local format source conv_out=""

    local stdout_file stderr_file
    stdout_file=$(mktemp)
    stderr_file=$(mktemp)

    case "${filepath##*.}" in
        nt)     format="ntriples"; source="${filepath}" ;;
        ttl|n3) format="turtle";   source="${filepath}" ;;
        *)
            local converter
            converter=$(find_converter "${filepath}")
            if [[ -z "${converter}" ]]; then
                # An extension only reaches here when some converters.json
                # declares it, but none applies to this file's location.
                rm -f "${stdout_file}" "${stderr_file}"
                return 0
            fi
            conv_out=$(mktemp)
            if ${converter} "${filepath}" > "${conv_out}" 2> "${stderr_file}"; then
                format="turtle"; source="${conv_out}"   # contract: converter emits Turtle
            else
                emit_error_quad "${graph_iri}" "${stderr_file}" "convert" "${filepath}"
                rm -f "${stdout_file}" "${stderr_file}" "${conv_out}"
                return 0
            fi
            ;;
    esac

    if rapper -q -i "${format}" -o ntriples "${source}" \
            > "${stdout_file}" 2> "${stderr_file}"; then
        # Blank node label prefix for this file: rapper labels blank nodes
        # per invocation (_:genid1, _:genid2, ...), so without rewriting,
        # two files would hand qlever-index the same labels and their blank
        # nodes would merge into one. The prefix must itself be a legal
        # BLANK_NODE_LABEL prefix (start with a letter — no '%', no '/'),
        # so it is a short hash of relpath rather than relpath itself.
        local bnode_prefix
        bnode_prefix="b$(printf '%s' "${relpath}" | md5sum | cut -c1-12)"

        # Append the graph IRI by replacing the trailing " ." rapper's
        # ntriples output always ends each line with, and rewrite blank
        # node labels to carry this file's prefix. The IRI and prefix are
        # passed to awk via ENVIRON (not -v, and not interpolated into the
        # program text) so they are treated purely as data: no
        # backslash-escape decoding and no '&'-means-matched-text
        # sub()/gsub() replacement magic can corrupt them, however they or
        # the filename they were derived from are spelled.
        #
        # Blank nodes only ever appear, in rapper's canonical ntriples
        # output, as the whole subject token (line starts with "_:") or as
        # the whole object token (the last whitespace-delimited token
        # before the trailing " ." that was just stripped). A literal
        # object always ends with a closing '"' (optionally followed by
        # ^^<...> or @lang), so it can never equal a bare "_:label" token —
        # matching on those two exact token positions rewrites every real
        # blank node and nothing inside a literal.
        G="${graph_iri}" BNP="${bnode_prefix}" awk '{
            line = $0
            n = sub(/ \.$/, "", line)
            if (!n) { print $0; next }

            prefix = ENVIRON["BNP"]

            if (line ~ /^_:/) {
                sp = index(line, " ")
                label = substr(line, 3, sp - 3)
                line = "_:" prefix "_" label substr(line, sp)
            }

            lastsp = 0
            for (i = length(line); i > 0; i--) {
                if (substr(line, i, 1) == " ") { lastsp = i; break }
            }
            if (lastsp > 0) {
                lasttok = substr(line, lastsp + 1)
                if (lasttok ~ /^_:[A-Za-z0-9_][A-Za-z0-9_.-]*$/) {
                    label = substr(lasttok, 3)
                    line = substr(line, 1, lastsp) "_:" prefix "_" label
                }
            }

            print line " <" ENVIRON["G"] "> ."
        }' "${stdout_file}"
    else
        emit_error_quad "${graph_iri}" "${stderr_file}" "parse" "${filepath}"
    fi

    rm -f "${stdout_file}" "${stderr_file}" ${conv_out:+"${conv_out}"}
}

# Emit a diagnostic quad for a symlink matched by name but unusable as a
# source file — dangling (target does not exist, e.g. it points outside
# /data, which is all this container has mounted) or pointing at something
# that isn't a regular file. No parse/convert is attempted; the failure is
# already known from the filesystem itself.
stream_broken_symlink() {
    local filepath="$1"
    local relpath="${filepath#/data/}"
    local graph_iri="${BASE_URI}$(urlencode_relpath "${relpath}")"
    local target msg
    target=$(readlink -- "${filepath}")
    if [[ ! -e "${filepath}" ]]; then
        msg="broken symlink, target does not exist: ${target}"
    else
        msg="symlink target is not a regular file: ${target}"
    fi
    log "SYMLINK ERROR ${filepath}: ${msg}"
    printf '<%s> <%s> "%s" <%s> .\n' \
        "${graph_iri}" "${ERROR_PREDICATE}" "$(printf '%s' "${msg}" | escape_literal)" "${graph_iri}"
    echo "${filepath}" >> "${ERRORS_FILE}"
}

if [[ ${#RDF_FILES[@]} -eq 0 && ${#BROKEN_SYMLINKS[@]} -eq 0 ]]; then
    log "No .nt/.ttl/.n3 files found under /data — building empty index"
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
        stream_as_nquads "${filepath}"
    done
    for filepath in "${BROKEN_SYMLINKS[@]}"; do
        log "Processing: ${filepath}"
        stream_broken_symlink "${filepath}"
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
