#!/usr/bin/env bash
# emit_file.sh <quads|triples|graph-iri|broken-symlink> FILEPATH
#
# Single authority for turning one source file into RDF. build_index.sh's
# full-rebuild loop calls this for every matched file, and the (future)
# incremental SPARQL Update path will call it too — so the two paths can
# never drift about how a file becomes triples.
#
# Modes:
#   quads FILEPATH
#       N-Quads for the file on stdout, each triple's graph term appended.
#       This is exactly what build_index.sh streams into qlever-index today.
#   triples FILEPATH
#       The same content as `quads`, but without the trailing graph term —
#       plain, blank-node-prefixed N-Triples. Meant to be wrapped in
#       GRAPH <g> { ... } by an incremental INSERT DATA statement.
#   graph-iri FILEPATH
#       Prints just the file's percent-encoded graph IRI (no angle
#       brackets), followed by a newline.
#   broken-symlink FILEPATH
#       Emits the diagnostic quad for a symlink whose target is missing or
#       is not a regular file.
#
# Both `quads` and `triples` detect a broken symlink themselves (a symlink
# whose target is missing or not a regular file) and emit the matching
# diagnostic — as a quad or a bare triple, respectively — rather than
# requiring the caller to classify the file first.
#
# Exit status is always 0, even when the file fails to parse/convert or is a
# broken symlink — the diagnostic quad/triple written to stdout IS the
# output in that case. This matches build_index.sh's historical behaviour:
# a broken file surfaces as a queryable annotation, it never fails the build.
#
# Env vars:
#   DATA_ROOT    Root directory relative paths (and graph IRIs) are computed
#                against. Default: /data
#   BASE_URI     URI prefix prepended to the percent-encoded relative path to
#                form a graph IRI. Default: https://example.org/data/
#   ERRORS_FILE  If set to a non-empty path, one line (the input FILEPATH) is
#                appended to it whenever the file produced a diagnostic
#                instead of real content. This is how build_index.sh's final
#                ok=/errors= summary keeps working. Unset/empty: don't record.

set -euo pipefail

MODE="${1:?Usage: emit_file.sh <quads|triples|graph-iri|broken-symlink> FILEPATH}"
FILEPATH="${2:?Usage: emit_file.sh <quads|triples|graph-iri|broken-symlink> FILEPATH}"

export DATA_ROOT="${DATA_ROOT:-/data}"
export BASE_URI="${BASE_URI:-https://example.org/data/}"
ERROR_PREDICATE="urn:qlever-dir:parsingError"

log() { echo "[emit_file] $*" >&2; }

# Escape a string for use inside an N-Triples/N-Quads literal:
#   backslash -> \\, double-quote -> \", any C0 control char (newline, tab,
#   carriage return, ...) -> single space
escape_literal() {
    sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\000-\037' ' '
}

# Percent-encode a path (relative to DATA_ROOT) for safe use inside a graph
# IRI. Letters, digits, and IRI-safe punctuation ("/-._~!$&'()*+,;=:@") pass
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
# .qlever/converters.json (walking up to DATA_ROOT). Echoes the resolved
# command (program path made absolute relative to that .qlever dir, plus any
# args) or nothing if no converter applies to the file's extension.
find_converter() {
    python3 - "$1" <<'PY'
import json, os, sys
fp = os.path.abspath(sys.argv[1])
ext = os.path.splitext(fp)[1].lstrip('.').lower()
data_root = os.path.abspath(os.environ.get('DATA_ROOT', '/data'))
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

record_error() {
    local filepath="$1"
    if [[ -n "${ERRORS_FILE:-}" ]]; then
        echo "${filepath}" >> "${ERRORS_FILE}"
    fi
}

# Emit a diagnostic for a file that could not be processed, and record it.
# append_graph=1 -> quad (trailing " <graph> ."); append_graph=0 -> bare
# triple (trailing " .").
emit_error() {
    local graph_iri="$1" stderr_file="$2" context="$3" filepath="$4" append_graph="$5"
    local error_msg
    error_msg=$(escape_literal < "${stderr_file}")
    [[ -z "${error_msg}" ]] && error_msg="${context} exited non-zero with no stderr output"
    log "${context^^} ERROR ${filepath}: ${error_msg}"
    if [[ "${append_graph}" == 1 ]]; then
        printf '<%s> <%s> "%s" <%s> .\n' \
            "${graph_iri}" "${ERROR_PREDICATE}" "${error_msg}" "${graph_iri}"
    else
        printf '<%s> <%s> "%s" .\n' \
            "${graph_iri}" "${ERROR_PREDICATE}" "${error_msg}"
    fi
    record_error "${filepath}"
}

# Emit a diagnostic for a symlink matched by name but unusable as a source
# file — dangling (target does not exist) or pointing at something that
# isn't a regular file. No parse/convert is attempted; the failure is
# already known from the filesystem itself.
emit_broken_symlink() {
    local filepath="$1" append_graph="$2"
    local relpath="${filepath#${DATA_ROOT}/}"
    local graph_iri="${BASE_URI}$(urlencode_relpath "${relpath}")"
    local target msg escaped
    target=$(readlink -- "${filepath}")
    if [[ ! -e "${filepath}" ]]; then
        msg="broken symlink, target does not exist: ${target}"
    else
        msg="symlink target is not a regular file: ${target}"
    fi
    log "SYMLINK ERROR ${filepath}: ${msg}"
    escaped=$(printf '%s' "${msg}" | escape_literal)
    if [[ "${append_graph}" == 1 ]]; then
        printf '<%s> <%s> "%s" <%s> .\n' \
            "${graph_iri}" "${ERROR_PREDICATE}" "${escaped}" "${graph_iri}"
    else
        printf '<%s> <%s> "%s" .\n' \
            "${graph_iri}" "${ERROR_PREDICATE}" "${escaped}"
    fi
    record_error "${filepath}"
}

# Shared body for `quads` and `triples`: parse/convert the file, rewrite
# blank node labels with a per-file prefix, and either append the graph term
# (append_graph=1) or leave a bare triple (append_graph=0).
emit_rdf() {
    local filepath="$1" append_graph="$2"
    local relpath="${filepath#${DATA_ROOT}/}"
    local graph_iri="${BASE_URI}$(urlencode_relpath "${relpath}")"

    # A symlink whose target is missing or not a regular file: no
    # parse/convert attempted, diagnostic straight from the filesystem.
    if [[ -L "${filepath}" ]] && { [[ ! -e "${filepath}" ]] || [[ ! -f "${filepath}" ]]; }; then
        emit_broken_symlink "${filepath}" "${append_graph}"
        return 0
    fi

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
                emit_error "${graph_iri}" "${stderr_file}" "convert" "${filepath}" "${append_graph}"
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

        # Append the graph term (quads mode) by replacing the trailing " ."
        # rapper's ntriples output always ends each line with, or just drop
        # it (triples mode); either way rewrite blank node labels to carry
        # this file's prefix. The IRI and prefix are passed to awk via
        # ENVIRON (not -v, and not interpolated into the program text) so
        # they are treated purely as data: no backslash-escape decoding and
        # no '&'-means-matched-text sub()/gsub() replacement magic can
        # corrupt them, however they or the filename they were derived from
        # are spelled.
        #
        # Blank nodes only ever appear, in rapper's canonical ntriples
        # output, as the whole subject token (line starts with "_:") or as
        # the whole object token (the last whitespace-delimited token
        # before the trailing " ." that was just stripped). A literal
        # object always ends with a closing '"' (optionally followed by
        # ^^<...> or @lang), so it can never equal a bare "_:label" token —
        # matching on those two exact token positions rewrites every real
        # blank node and nothing inside a literal.
        G="${graph_iri}" BNP="${bnode_prefix}" APPEND_GRAPH="${append_graph}" awk '{
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

            if (ENVIRON["APPEND_GRAPH"] == "1") {
                print line " <" ENVIRON["G"] "> ."
            } else {
                print line " ."
            }
        }' "${stdout_file}"
    else
        emit_error "${graph_iri}" "${stderr_file}" "parse" "${filepath}" "${append_graph}"
    fi

    rm -f "${stdout_file}" "${stderr_file}" ${conv_out:+"${conv_out}"}
}

case "${MODE}" in
    quads)
        emit_rdf "${FILEPATH}" 1
        ;;
    triples)
        emit_rdf "${FILEPATH}" 0
        ;;
    graph-iri)
        relpath="${FILEPATH#${DATA_ROOT}/}"
        printf '%s\n' "${BASE_URI}$(urlencode_relpath "${relpath}")"
        ;;
    broken-symlink)
        emit_broken_symlink "${FILEPATH}" 1
        ;;
    *)
        echo "Usage: emit_file.sh <quads|triples|graph-iri|broken-symlink> FILEPATH" >&2
        exit 1
        ;;
esac
