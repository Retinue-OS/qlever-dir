---
type: project
id: proj-rollstuhl-bluetooth
title: "Understand wheelchair Bluetooth protocol"
goal: "The wheelchair control protocol is documented and reproducibly tested."
goal_status: not_achieved
current_next_action: "Wait for the manufacturer's response."
current_actor: actor-manufacturer
waiting_since: 2026-06-20
expected_by: 2026-07-05
paused: false
category: technology
links:
  - gmail:abc123
  - file:/data/captures/wheelchair-ble-2026-06-20.pcapng
  - manufacturer support ticket #4711
---

# Understand wheelchair Bluetooth protocol

## Goal
The wheelchair control protocol is documented and reproducibly tested.

## Success criteria
- Relevant BLE traffic has been captured.
- Commands and responses are documented.
- A minimal reproduction or test client exists.

## Context
This Markdown file is the canonical, hand-editable source. The frontmatter above
is converted to triples by `.qlever/md2ttl.py`; the prose in this body is left
to a separate, on-demand extraction step.

The three `links:` entries cover all the converter's cases: a custom URI scheme,
a `file:` IRI, and — in the third — a value with no scheme at all, which is
emitted as a plain literal rather than an IRI.
