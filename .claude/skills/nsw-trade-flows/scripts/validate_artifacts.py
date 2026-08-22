#!/usr/bin/env python3
"""Consistency checks for the tnsw <-> agency artifact pairing in this repo.

Stdlib-only (this repo has no package manager). Run from anywhere; paths are
resolved relative to the repo root, which is detected as the directory
containing this script's ancestor `.claude/skills/...` (four levels up from
`scripts/`) unless overridden with --root.

Exit code is non-zero iff at least one ERROR was found. WARNINGs never affect
the exit code — they flag things worth a human glance (e.g. an id
deliberately reused across sibling task_config folders) but are not
necessarily wrong.
"""
import argparse
import json
import sys
from pathlib import Path

AGENCY_DIRS = {"cda", "customs", "fcau", "npqs", "sltb"}


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


def diff_contents(paths):
    """True if all given files are byte-identical."""
    contents = {p.read_bytes() for p in paths}
    return len(contents) <= 1


def check_duplicate_ids_within_manifest(index, report):
    """An id repeated within one manifest for the SAME kind is a deliberate
    pattern here (e.g. the same read-only 'original submission' form
    duplicated across every officer task_config folder in an agency) -- only
    flag it if the duplicated files' contents have actually drifted apart.
    (An id shared across *different* kinds -- e.g. a workflow.json and its
    sibling task_template.json both named after the flow -- is normal and
    not comparable at all.)"""
    for label, by_id in index.items():
        for artifact_id, entries in by_id.items():
            by_kind = {}
            for p, kind in entries:
                by_kind.setdefault(kind, []).append(p)
            for kind, paths in by_kind.items():
                if len(paths) < 2:
                    continue
                if not diff_contents(paths):
                    rels = ", ".join(str(p) for p in paths)
                    report.warn(
                        f"[{label}] id {artifact_id!r} ({kind}) is reused {len(paths)}x with DIFFERING content: {rels}"
                    )


def check_shared_ids_across_tnsw_and_agency(index, root, report):
    """Ids shared between tnsw/manifest.json and an agency manifest.json are the
    officer-facing jsonforms/templates the CLAUDE.md convention says must stay
    byte-identical in both places. Flag drift."""
    tnsw_ids = index.get("tnsw", {})
    for label, by_id in index.items():
        if label == "tnsw":
            continue
        shared = set(tnsw_ids) & set(by_id)
        for artifact_id in sorted(shared):
            tnsw_by_kind = {}
            for p, k in tnsw_ids[artifact_id]:
                tnsw_by_kind.setdefault(k, []).append(p)
            agency_by_kind = {}
            for p, k in by_id[artifact_id]:
                agency_by_kind.setdefault(k, []).append(p)
            for kind in sorted(set(tnsw_by_kind) & set(agency_by_kind)):
                all_paths = tnsw_by_kind[kind] + agency_by_kind[kind]
                if not diff_contents(all_paths):
                    rels = ", ".join(str(p.relative_to(root)) for p in all_paths)
                    report.error(
                        f"id {artifact_id!r} ({kind}) differs between tnsw/manifest.json and {label}/manifest.json: {rels}"
                    )


def check_external_review_linkage(manifests, index, root, report):
    """Every EXTERNAL_REVIEW subtask_template's plugin_properties.{service_id,task_code}
    must resolve to a task_config in that agency's own manifest, and that
    task_config's forms.* must resolve to a generic_template id."""
    tnsw_mpath, tnsw_mdir = manifests.get("tnsw", (None, None))
    if tnsw_mdir is None:
        report.error("tnsw/manifest.json not found; cannot check EXTERNAL_REVIEW linkage")
        return
    tnsw_ids = index.get("tnsw", {})
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
        if service_id not in manifests:
            report.error(f"[{where}] EXTERNAL_REVIEW service_id {service_id!r} has no top-level manifest")
            continue
        agency_by_id = index.get(service_id, {})
        matches = [entries for aid, entries in agency_by_id.items() if aid == task_code]
        task_configs = [
            (p, k) for entries in matches for (p, k) in entries if k == "task_config"
        ]
        if not task_configs:
            report.error(f"[{where}] task_code {task_code!r} not found as a task_config in {service_id}/manifest.json")
            continue
        task_config_path, _ = task_configs[0]
        tc = load_json(task_config_path)
        # Officer-facing forms resolve against the union of the agency's own
        # manifest and tnsw/manifest.json -- some forms are physically
        # duplicated into the agency folder (see check_shared_ids_across_*),
        # others are only ever defined on the tnsw side and the agency
        # task_config just references that id directly.
        global_template_ids = {
            aid for aid, entries in (list(tnsw_ids.items()) + list(agency_by_id.items()))
            for _p, k in entries if k == "generic_template"
        }
        for form_role, form_id in (tc.get("forms") or {}).items():
            if form_id not in global_template_ids:
                report.error(
                    f"[{task_config_path.relative_to(root)}] forms.{form_role} references "
                    f"{form_id!r}, not found as a generic_template in {service_id}/manifest.json or tnsw/manifest.json"
                )


# CLAUDE.md documents START/TASK/GATEWAY/END; TIMER (polling waits, e.g.
# npqs 9-ephyto) and SPLIT_TASK (fan-out to parallel agency flows, e.g.
# tnsw/trade/trade_workflow.json) are additional node types observed in the
# repo that don't carry a task_template_id, so they're exempted from that check.
VALID_NODE_TYPES = {"START", "TASK", "GATEWAY", "END", "TIMER", "SPLIT_TASK"}
VALID_GATEWAY_TYPES = {"EXCLUSIVE_SPLIT", "EXCLUSIVE_JOIN", "PARALLEL_SPLIT", "PARALLEL_JOIN"}


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
    check_shared_ids_across_tnsw_and_agency(index, root, report)
    check_external_review_linkage(manifests, index, root, report)
    check_workflow_shapes(manifests, index, root, report)

    report.print_and_exit()


if __name__ == "__main__":
    main()
