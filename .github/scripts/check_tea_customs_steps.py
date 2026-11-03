#!/usr/bin/env python3
"""Check that the tea flow runs the same Customs steps as customs-workflow.

A consignment with tea starts the tea flow (tnsw/sltb/sltb_workflow.json) instead of
customs-workflow (tnsw/trade/3-prepare_split_items/transform.json, "replaces"). The tea
flow submits the CusDec SLTB approved itself, then runs the Customs steps that follow
the CusDec in customs-workflow, copied node for node: core has no node that runs a whole
workflow as one step, so the steps can't be shared.

So every node of customs_workflow.json except start, end and the CusDec step itself
must be in the tea flow, unchanged, and so must every edge between them. The edge into
customs-workflow's end goes to the tea flow's end_success. A change to the Customs
steps has to be made in both files, and this check fails until it is.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CUSTOMS = ROOT / "tnsw/customs/customs_workflow.json"
TEA = ROOT / "tnsw/sltb/sltb_workflow.json"

# customs-workflow's own nodes, which the tea flow does not copy.
NOT_COPIED = {"start", "end", "customs_1_cusdec_submission"}
# What the edge into customs-workflow's end goes to in the tea flow.
TEA_END = "end_success"


def main() -> int:
    customs = json.loads(CUSTOMS.read_text())
    tea = json.loads(TEA.read_text())
    tea_nodes = {n["id"]: n for n in tea["nodes"]}
    tea_edges = {(e["source_id"], e["target_id"], e.get("condition")): e for e in tea["edges"]}

    errors = []
    copied = set()
    for node in customs["nodes"]:
        if node["id"] in NOT_COPIED:
            continue
        copied.add(node["id"])
        if node["id"] not in tea_nodes:
            errors.append(f"node {node['id']} is missing from the tea flow")
        elif tea_nodes[node["id"]] != node:
            errors.append(f"node {node['id']} differs from customs-workflow's")

    for edge in customs["edges"]:
        if edge["source_id"] not in copied:
            continue
        target = TEA_END if edge["target_id"] == "end" else edge["target_id"]
        if target not in copied and target != TEA_END:
            continue
        if (edge["source_id"], target, edge.get("condition")) not in tea_edges:
            errors.append(
                f"edge {edge['source_id']} -> {target}"
                + (f" ({edge['condition']})" if edge.get("condition") else "")
                + " is missing from the tea flow"
            )

    # The tea flow must not add its own edges out of the copied steps.
    customs_out = {
        (e["source_id"], TEA_END if e["target_id"] == "end" else e["target_id"], e.get("condition"))
        for e in customs["edges"]
        if e["source_id"] in copied
    }
    for key in tea_edges:
        if key[0] in copied and key not in customs_out:
            errors.append(f"edge {key[0]} -> {key[1]} leaves a copied Customs step but is not in customs-workflow")

    if errors:
        print(f"{TEA.relative_to(ROOT)} does not run the Customs steps of {CUSTOMS.relative_to(ROOT)}:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"OK: the tea flow runs customs-workflow's {len(copied)} Customs steps after the CusDec unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
