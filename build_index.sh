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
# The combined N-Quads stream is piped into qlever-index. The index files are
# written into <index_dir> with the basename "rdf-store".

set -euo pipefail

INDEX_DIR="${1:?Usage: build_index.sh <index_dir>}"
BASE_URI="${BASE_URI:-https://example.org/data/}"
INDEX_NAME="rdf-store"

log() { echo "[build_index] $*" >&2; }

# Clean any previous index in this slot so the new build is from scratch.
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

    # rapper outputs canonical N-Triples: '<s> <p> <o> .' per line.
    # Replace the trailing ' .' with ' <graph> .' to form an N-Quad.
    rapper -q -i "${format}" -o ntriples "${filepath}" \
        | sed "s| \\.\$| <${graph_iri}> .|"
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

log "Index build complete in ${INDEX_DIR}"
