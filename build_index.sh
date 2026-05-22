#!/usr/bin/env bash
# build_index.sh <index_dir>
#
# Scans /data for .nt, .ttl, .n3 files. For each file, parses it via rapper
# into canonical N-Triples and converts every triple into a quad by appending
# a graph IRI derived from the file path:
#
#   graph IRI = <BASE_URI><relative path from /data>
#
# e.g. /data/health/obs.nt -> <https://example.org/data/health/obs.nt>
#
# If a file fails to parse, instead of its (partial) triples we emit one
# diagnostic quad describing the failure:
#
#   <graph> <urn:qlever-dir:parsingError> "stderr message" <graph> .
#
# The build itself still succeeds — broken files surface as queryable
# annotations rather than blocking the whole store update.

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

mapfile -d '' RDF_FILES < <(
    find /data -type f \( -name "*.nt" -o -name "*.ttl" -o -name "*.n3" \) -print0 2>/dev/null \
    | sort -z
)

# Escape a string for use inside an N-Triples/N-Quads literal:
#   backslash -> \\, double-quote -> \", newlines/tabs -> single space
escape_literal() {
    sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n\t' '  '
}

# Track parse failures across the pipeline subshell via a shared file
# (the for-loop runs in a subshell because of the pipe to qlever-index).
ERRORS_FILE=$(mktemp)
trap 'rm -f "${ERRORS_FILE}"' EXIT

stream_as_nquads() {
    local filepath="$1"
    local relpath="${filepath#/data/}"
    local graph_iri="${BASE_URI}${relpath}"
    local format

    case "${filepath##*.}" in
        nt)  format="ntriples" ;;
        ttl) format="turtle"   ;;
        n3)  format="turtle"   ;;
        *)   log "Skipping unknown extension: ${filepath}"; return 0 ;;
    esac

    local stdout_file stderr_file
    stdout_file=$(mktemp)
    stderr_file=$(mktemp)

    if rapper -q -i "${format}" -o ntriples "${filepath}" \
            > "${stdout_file}" 2> "${stderr_file}"; then
        sed "s| \\.\$| <${graph_iri}> .|" "${stdout_file}"
    else
        local error_msg
        error_msg=$(escape_literal < "${stderr_file}")
        [[ -z "${error_msg}" ]] && error_msg="rapper exited non-zero with no stderr output"
        log "PARSE ERROR ${filepath}: ${error_msg}"
        printf '<%s> <%s> "%s" <%s> .\n' \
            "${graph_iri}" "${ERROR_PREDICATE}" "${error_msg}" "${graph_iri}"
        echo "${filepath}" >> "${ERRORS_FILE}"
    fi

    rm -f "${stdout_file}" "${stderr_file}"
}

if [[ ${#RDF_FILES[@]} -eq 0 ]]; then
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

log "Streaming as N-Quads into qlever-index ..."
{
    for filepath in "${RDF_FILES[@]}"; do
        log "Processing: ${filepath}"
        stream_as_nquads "${filepath}"
    done
} | qlever-index \
    -i "${INDEX_NAME}" \
    -s "${INDEX_NAME}.settings.json" \
    --vocabulary-type on-disk-compressed \
    -F nq \
    -f -

ERROR_COUNT=$(wc -l < "${ERRORS_FILE}" | tr -d ' ')
OK_COUNT=$(( ${#RDF_FILES[@]} - ERROR_COUNT ))
log "Index build complete in ${INDEX_DIR} — ok=${OK_COUNT} errors=${ERROR_COUNT}"
