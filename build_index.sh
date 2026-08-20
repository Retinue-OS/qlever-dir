#!/usr/bin/env bash
# build_index.sh <index_dir>
#
# Scans /data for .nt, .ttl, .n3 files. For each file, parses it via rapper
# into canonical N-Triples and converts every triple into a quad by appending
# a graph IRI derived from the file path:
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

# Extensions that have a converter declared in any .qlever/converters.json
# under /data. Files with these extensions are routed through their converter
# (see find_converter / stream_as_nquads) instead of being parsed as RDF.
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

# Track parse/convert failures across the pipeline subshell via a shared file
# (the for-loop runs in a subshell because of the pipe to qlever-index).
ERRORS_FILE=$(mktemp)
trap 'rm -f "${ERRORS_FILE}"' EXIT

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
        # Append the graph IRI by replacing the trailing " ." rapper's
        # ntriples output always ends each line with. The IRI is passed to
        # awk via ENVIRON (not -v, and not interpolated into the program
        # text) so it is treated purely as data: no backslash-escape
        # decoding and no '&'-means-matched-text sub()/gsub() replacement
        # magic can corrupt it, however it or the filename it was derived
        # from is spelled.
        G="${graph_iri}" awk '{
            line = $0
            n = sub(/ \.$/, "", line)
            if (n) print line " <" ENVIRON["G"] "> ."
            else print $0
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
