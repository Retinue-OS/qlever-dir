#!/usr/bin/env python3
"""Convert a project Markdown file (YAML frontmatter) to Turtle on stdout.

Example converter for qlever-dir's .qlever/converters.json mechanism. It is
deliberately dependency-free (Python standard library only) so it runs on the
stock qlever-dir image without adding anything to the Dockerfile.

Contract (see build_index.sh): invoked as `md2ttl.py <input.md>`, emit Turtle on
stdout, exit non-zero with a message on stderr to surface a diagnostic quad.

The frontmatter parsed here is a small, deterministic subset of YAML — scalar
`key: value` pairs plus simple `- item` lists. That is intentional: structured
frontmatter is a mechanical key->predicate mapping and needs no LLM. Free-text
prose in the body is a separate, on-demand extraction job, not done here.
"""

import re
import sys

SUBJECT_BASE = "urn:retinue:"
P = "https://w3id.org/retinue/project#"

# field -> (predicate, value-kind). kind: "str" | "date" | "bool" | "iri-actor"
SCALAR_FIELDS = {
    "title": ("title", "str"),
    "goal": ("goal", "str"),
    "goal_status": ("goalStatus", "str"),
    "current_next_action": ("currentNextAction", "str"),
    "current_actor": ("currentActor", "iri-actor"),
    "waiting_since": ("waitingSince", "date"),
    "expected_by": ("expectedBy", "date"),
    "paused": ("paused", "bool"),
    "category": ("category", "str"),
}


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def parse_frontmatter(text):
    """Return a dict from the leading --- ... --- YAML frontmatter block."""
    m = re.match(r"^---\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        fail("no YAML frontmatter block (expected a leading '---' fence)")
    data, current_list = {}, None
    for raw in m.group(1).splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and current_list is not None:
            data[current_list].append(strip_quotes(item.group(1).strip()))
            continue
        kv = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", line)
        if not kv:
            continue
        key, value = kv.group(1), kv.group(2).strip()
        if value == "":
            data[key] = []
            current_list = key
        else:
            data[key] = strip_quotes(value)
            current_list = None
    return data


def strip_quotes(v):
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def ttl_string(v):
    esc = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{esc}"'


def actor_iri(v):
    return f"<{SUBJECT_BASE}{v}>"


def main():
    if len(sys.argv) != 2:
        fail("usage: md2ttl.py <input.md>")
    with open(sys.argv[1], encoding="utf-8") as f:
        fm = parse_frontmatter(f.read())

    pid = fm.get("id")
    if not pid:
        fail("frontmatter is missing required field: id")
    subject = f"<{SUBJECT_BASE}project:{pid}>"

    out = [
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        f"@prefix p: <{P}> .",
        "",
        f"{subject} a p:Project ;",
    ]

    triples = []
    for field, (pred, kind) in SCALAR_FIELDS.items():
        if field not in fm or fm[field] == "":
            continue
        value = fm[field]
        if kind == "str":
            obj = ttl_string(value)
        elif kind == "date":
            obj = f'"{value}"^^xsd:date'
        elif kind == "bool":
            obj = "true" if str(value).lower() in ("true", "yes", "1") else "false"
        elif kind == "iri-actor":
            obj = actor_iri(value)
        triples.append(f"    p:{pred} {obj}")

    for link in fm.get("links", []):
        # A value with a URI scheme becomes an IRI; otherwise a plain literal.
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", link):
            triples.append(f"    p:link <{link}>")
        else:
            triples.append(f"    p:link {ttl_string(link)}")

    out.append(" ;\n".join(triples) + " .")
    print("\n".join(out))


if __name__ == "__main__":
    main()
