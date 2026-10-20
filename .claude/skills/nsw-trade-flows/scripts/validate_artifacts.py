#!/usr/bin/env python3
"""Consistency checks for the tnsw <-> agency artifact pairing in this repo.

Stdlib-only (this repo has no package manager). Run from anywhere; paths are
resolved relative to the repo root, which is detected as the directory
containing this script's ancestor `.claude/skills/...` (four levels up from
`scripts/`) unless overridden with --root.

Exit code is non-zero iff at least one ERROR was found. WARNINGs (no check
emits one at the moment) never affect the exit code.
"""
import argparse
import json
import sys
from pathlib import Path

# An EXTERNAL_REVIEW step's service_id names the agency; its tasks live in <service_id>-v2/.
AGENCIES = {"cda", "efc", "fcau", "npqs", "sltb"}
AGENCY_DIRS = {f"{a}-v2" for a in AGENCIES}


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def print_and_exit(self):
        if self.warnings:
            print(f"--- {len(self.warnings)} warning(s) ---")
            for w in self.warnings:
                print(f"WARN  {w}")
        if self.errors:
            print(f"--- {len(self.errors)} error(s) ---")
            for e in self.errors:
                print(f"ERROR {e}")
        if not self.warnings and not self.errors:
            print("No issues found.")
        else:
            print(f"\n{len(self.errors)} error(s), {len(self.warnings)} warning(s).")
        sys.exit(1 if self.errors else 0)


def find_manifests(root):
    """Return {label: (manifest_path, manifest_dir)} for every manifest.json this repo cares about."""
    manifests = {}
    tnsw_manifest = root / "tnsw" / "manifest.json"
    if tnsw_manifest.exists():
        manifests["tnsw"] = (tnsw_manifest, tnsw_manifest.parent)
    for d in sorted(root.iterdir()):
        if d.name in AGENCY_DIRS and (d / "manifest.json").exists():
            manifests[d.name] = (d / "manifest.json", d)
    return manifests


def check_paths_exist(manifests, report):
    """Every manifest entry's `path` must resolve to a real file."""
    index = {}  # label -> {(id, kind): [abs_path, ...]}
    for label, (mpath, mdir) in manifests.items():
        data = load_json(mpath)
        by_id = {}
        for a in data.get("artifacts", []):
            p = mdir / a["path"]
            if not p.is_file():
                report.error(f"[{label}] manifest entry {a['id']!r} ({a['kind']}) points to missing file: {a['path']}")
                continue
            by_id.setdefault(a["id"], []).append((p, a["kind"]))
        index[label] = by_id
    return index


def check_unregistered_files(manifests, report):
    """Every *.json file under a manifest's tree (except manifest.json itself) should be referenced by it."""
    for label, (mpath, mdir) in manifests.items():
        data = load_json(mpath)
        registered = {(mdir / a["path"]).resolve() for a in data.get("artifacts", [])}
        for p in mdir.rglob("*.json"):
            if p.name == "manifest.json":
                continue
            if p.resolve() not in registered:
                report.error(f"[{label}] file not registered in manifest.json: {p.relative_to(mdir)}")


def check_duplicate_ids_within_manifest(index, report):
    """An id listed twice in one manifest for the SAME kind is a bug: the
    artifact registry keeps only the last row, so an edit to the other copy
    silently does nothing. (An id shared across *different* kinds -- e.g. a
    workflow.json and its sibling task_template.json both named after the
    flow -- is normal.)"""
    for label, by_id in index.items():
        for artifact_id, entries in by_id.items():
            by_kind = {}
            for p, kind in entries:
                by_kind.setdefault(kind, []).append(p)
            for kind, paths in by_kind.items():
                if len(paths) < 2:
                    continue
                rels = ", ".join(str(p) for p in paths)
                report.error(f"[{label}] id {artifact_id!r} ({kind}) is listed {len(paths)}x; only the last row loads: {rels}")


def check_external_review_linkage(manifests, index, root, report):
    """Every EXTERNAL_REVIEW subtask_template's plugin_properties.{service_id,task_code}
    must resolve to a task_config in that agency's own <service_id>-v2/ manifest,
    and that task_config's `workflow` must resolve to a workflow there."""
    tnsw_mpath, tnsw_mdir = manifests.get("tnsw", (None, None))
    if tnsw_mdir is None:
        report.error("tnsw/manifest.json not found; cannot check EXTERNAL_REVIEW linkage")
        return
    tnsw_data = load_json(tnsw_mpath)
    for a in tnsw_data.get("artifacts", []):
        if a["kind"] != "subtask_template":
            continue
        fpath = tnsw_mdir / a["path"]
        if not fpath.exists():
            continue  # already reported by check_paths_exist
        sub = load_json(fpath)
        if sub.get("task_type") != "EXTERNAL_REVIEW":
            continue
        props = sub.get("plugin_properties", {})
        service_id = props.get("service_id")
        task_code = props.get("task_code")
        where = a["path"]
        label = f"{service_id}-v2"
        if label not in manifests:
            report.error(f"[{where}] EXTERNAL_REVIEW service_id {service_id!r} has no {label}/manifest.json")
            continue
        agency_by_id = index.get(label, {})
        matches = [entries for aid, entries in agency_by_id.items() if aid == task_code]
        task_configs = [
            (p, k) for entries in matches for (p, k) in entries if k == "task_config"
        ]
        if not task_configs:
            report.error(f"[{where}] task_code {task_code!r} not found as a task_config in {label}/manifest.json")
            continue
        task_config_path, _ = task_configs[0]
        tc = load_json(task_config_path)
        workflow_id = tc.get("workflow")
        workflow_ids = {aid for aid, entries in agency_by_id.items() for _p, k in entries if k == "workflow"}
        if workflow_id not in workflow_ids:
            report.error(
                f"[{task_config_path.relative_to(root)}] workflow {workflow_id!r} "
                f"not found as a workflow in {label}/manifest.json"
            )


# CLAUDE.md documents START/TASK/GATEWAY/END; TIMER (polling waits, e.g.
# npqs 9-ephyto) and SPLIT_TASK (fan-out to parallel agency flows, e.g.
# tnsw/trade/trade_workflow.json) are additional node types observed in the
# repo that don't carry a task_template_id, so they're exempted from that check.
# As core's workflow/dsl.go defines them.
VALID_NODE_TYPES = {"START", "TASK", "GATEWAY", "END", "TIMER", "SPLIT_TASK", "SIGNALING"}
VALID_GATEWAY_TYPES = {
    "EXCLUSIVE_SPLIT", "EXCLUSIVE_JOIN", "PARALLEL_SPLIT", "PARALLEL_JOIN", "BATCH_SPLIT", "BATCH_JOIN",
}


TASK_TEMPLATE_ID_KINDS = {"task_template", "subtask_template"}


def check_workflow_shapes(manifests, index, root, report):
    """Sanity-check node/gateway vocabulary and that every TASK's task_template_id
    resolves to a task_template (macro workflows) or subtask_template (step
    sub-workflows) registered in tnsw/manifest.json."""
    tnsw_mpath, tnsw_mdir = manifests.get("tnsw", (None, None))
    if tnsw_mdir is None:
        return
    tnsw_ids = index.get("tnsw", {})
    tnsw_data = load_json(tnsw_mpath)
    for a in tnsw_data.get("artifacts", []):
        if a["kind"] != "workflow":
            continue
        fpath = tnsw_mdir / a["path"]
        if not fpath.exists():
            continue
        wf = load_json(fpath)
        node_ids = {n["id"] for n in wf.get("nodes", [])}
        for n in wf.get("nodes", []):
            if n["type"] not in VALID_NODE_TYPES:
                report.error(f"[{a['path']}] node {n['id']!r} has unknown type {n['type']!r}")
            if n["type"] == "GATEWAY" and n.get("gateway_type") not in VALID_GATEWAY_TYPES:
                report.error(f"[{a['path']}] gateway {n['id']!r} has unknown gateway_type {n.get('gateway_type')!r}")
            if n["type"] == "TASK":
                ttid = n.get("task_template_id")
                matches = ttid and any(k in TASK_TEMPLATE_ID_KINDS for _p, k in tnsw_ids.get(ttid, []))
                if not matches:
                    report.error(
                        f"[{a['path']}] TASK {n['id']!r} task_template_id {ttid!r} not found as a "
                        f"task_template or subtask_template in tnsw/manifest.json"
                    )
        for e in wf.get("edges", []):
            if e["source_id"] not in node_ids:
                report.error(f"[{a['path']}] edge {e['id']!r} source_id {e['source_id']!r} is not a node in this workflow")
            if e["target_id"] not in node_ids:
                report.error(f"[{a['path']}] edge {e['id']!r} target_id {e['target_id']!r} is not a node in this workflow")


DRAFT_ELEMENT = "draft_action"


def check_drafts(manifests, index, report):
    """A step template with `draft` saves drafts through the task manager, not the
    workflow (core's SaveDraft). For every task template, check its render config
    against the draft steps of its workflow:

    - the draft command has a handle with element 'draft_action' (any other
      element submits it as a step completion, moving the workflow on with a
      partial form) that the render's states make legal;
    - that handle's section reads the step's output_namespace as its dataKey,
      since the draft is stored there and the form is seeded from its dataKey;
    - a 'draft_action' handle belongs to a draft step;
    - no workflow still loops drafts back through a `command == ...` edge.
    """
    tnsw_mpath, tnsw_mdir = manifests.get("tnsw", (None, None))
    if tnsw_mdir is None:
        return
    # Keyed by kind too: a task template and its workflow often share an id.
    by_id = {}
    for a in load_json(tnsw_mpath).get("artifacts", []):
        fpath = tnsw_mdir / a["path"]
        if fpath.exists():
            by_id.setdefault((a["kind"], a["id"]), (a, fpath))

    def load(kind, i):
        hit = by_id.get((kind, i))
        return (hit[0]["path"], load_json(hit[1])) if hit else (None, None)

    for (_kind, _id), (a, fpath) in by_id.items():
        if a["kind"] == "subtask_template":
            st = load_json(fpath)
            draft = st.get("draft")
            if draft is not None and not isinstance(draft, dict):
                report.error(f"[{a['path']}] draft must be an object, like {{\"command\": \"save_as_draft\"}}")
            elif draft is not None and (not draft.get("command") or not st.get("output_namespace")):
                report.error(f"[{a['path']}] draft needs a command and an output_namespace")
        if a["kind"] == "workflow":
            for e in load_json(fpath).get("edges", []):
                condition = e.get("condition")
                if isinstance(condition, str) and "save_as_draft" in condition:
                    report.error(
                        f"[{a['path']}] edge {e['id']!r} routes drafts through the workflow; "
                        f"give the step template a `draft` instead"
                    )
        if a["kind"] != "task_template":
            continue

        tt = load_json(fpath)
        _wpath, wf = load("workflow", tt.get("workflow_id"))
        rpath, rc = load("generic_template", tt.get("render_config_id"))
        if wf is None or rc is None:
            continue
        drafts = {}  # command -> [output_namespace]
        for n in wf.get("nodes", []):
            if n.get("type") != "TASK":
                continue
            _spath, st = load("subtask_template", n.get("task_template_id"))
            draft = (st or {}).get("draft")
            if isinstance(draft, dict) and draft.get("command") and st.get("output_namespace"):
                drafts.setdefault(draft["command"], []).append(st["output_namespace"])

        handles = []  # (section, dataKey, handle)
        for sid, sec in (rc.get("sections") or {}).items():
            for h in sec.get("handles") or []:
                handles.append((sid, sec.get("dataKey"), h))
        legal = {
            act.get("command")
            for state in (rc.get("states") or {}).values()
            for act in (state.get("actions") or [])
        }

        for command, namespaces in drafts.items():
            mine = [(sid, key, h) for sid, key, h in handles if h.get("command") == command]
            if not mine:
                report.error(f"[{rpath}] no handle for draft command {command!r} of a step in {tt['workflow_id']!r}")
            for sid, key, h in mine:
                if h.get("element") != DRAFT_ELEMENT:
                    report.error(
                        f"[{rpath}] section {sid!r}: draft command {command!r} has element "
                        f"{h.get('element')!r}, want {DRAFT_ELEMENT!r}"
                    )
                if key not in namespaces:
                    report.error(
                        f"[{rpath}] section {sid!r}: dataKey {key!r} is not the draft step's "
                        f"output_namespace ({', '.join(namespaces)}), so a saved draft would not show"
                    )
            if command not in legal:
                report.error(f"[{rpath}] draft command {command!r} is not an action of any state")
        for sid, _key, h in handles:
            if h.get("element") == DRAFT_ELEMENT and h.get("command") not in drafts:
                report.error(
                    f"[{rpath}] section {sid!r}: {DRAFT_ELEMENT!r} handle {h.get('command')!r} "
                    f"has no step template with that draft command in {tt['workflow_id']!r}"
                )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=None, help="repo root (default: auto-detect from script location)")
    args = ap.parse_args()

    root = args.root or Path(__file__).resolve().parents[4]
    report = Report()

    manifests = find_manifests(root)
    if "tnsw" not in manifests:
        print(f"Could not find tnsw/manifest.json under {root} -- wrong --root?")
        sys.exit(2)

    index = check_paths_exist(manifests, report)
    check_unregistered_files(manifests, report)
    check_duplicate_ids_within_manifest(index, report)
    check_external_review_linkage(manifests, index, root, report)
    check_workflow_shapes(manifests, index, root, report)
    check_drafts(manifests, index, report)

    report.print_and_exit()


if __name__ == "__main__":
    main()
