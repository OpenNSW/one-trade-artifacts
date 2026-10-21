#!/usr/bin/env python3
"""Check that every task of an agency (*-v2) deployment says who may act on it.

An agency has no per-case owner, so any role in the agency's catalog owns every
injected workflow. What a role may do is decided by the task alone:

  * the officer step's `authz` extension lists, for each state and command, the roles
    allowed to run it (deny by default);
  * the task's render config lists in `read.roles` the roles allowed to open it.

The backend checks a step only when it declares the extension, and lets any catalog
role read a render config that has no `read` block, so a task that omits either is
open to every role. This script fails on such a task.

Role names are not checked here: the agency's catalog (configs/agency/<agency>/catalog.json
in nsw-srilanka) maps them to IdP roles.

Usage: check_agency_authz.py [repo-root]
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Step types that wait for a person to act. Only these need an authz rule; the others
# (REFID_GENERATOR, AUTH_API_CALL, ...) run without a caller.
INTERACTIVE_STEP_TYPES = {"USER_INPUT"}
AUTHZ_EXTENSION_ID = "authz"


def non_empty_names(value):
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v for v in value)


def check_authz(step, render):
    """Return the problems with a step's authz extension, given its task's render config."""
    exts = [e for e in step.get("extensions", []) if e.get("id") == AUTHZ_EXTENSION_ID]
    if not exts:
        return [f"step {step['id']} has no '{AUTHZ_EXTENSION_ID}' extension"]
    rules = exts[0].get("properties") or {}
    if not any(non_empty_names(roles) for cmds in rules.values() if isinstance(cmds, dict) for roles in cmds.values()):
        return [f"step {step['id']}: the '{AUTHZ_EXTENSION_ID}' extension names no role"]

    problems = []
    for state, cmds in rules.items():
        for command, roles in (cmds or {}).items():
            if not non_empty_names(roles):
                problems.append(f"step {step['id']}: {state}/{command} names no role")
    # A rule is deny-by-default, so a command the render config offers but the rule omits
    # is refused for everyone.
    for state, body in (render.get("states") or {}).items():
        for action in body.get("actions", []):
            if action["command"] not in (rules.get(state) or {}):
                problems.append(
                    f"step {step['id']}: render config offers {state}/{action['command']}, "
                    f"which the '{AUTHZ_EXTENSION_ID}' extension does not allow anyone"
                )
    return problems


MANIFEST_VERSION = re.compile(r"^manifest_v([1-9][0-9]*)\.json$")


def current_manifest(bundle):
    """The bundle's highest manifest_vN.json, or manifest.json in a bundle that has no
    versioned manifest yet. None if it has neither."""
    versions = [int(m.group(1)) for m in (MANIFEST_VERSION.match(p.name) for p in bundle.iterdir()) if m]
    if versions:
        return bundle / f"manifest_v{max(versions)}.json"
    return bundle / "manifest.json" if (bundle / "manifest.json").exists() else None


def check_agency(root, manifest_path):
    agency_dir = manifest_path.parent
    paths = defaultdict(dict)
    for artifact in json.loads(manifest_path.read_text())["artifacts"]:
        paths[artifact["kind"]][artifact["id"]] = agency_dir / artifact["path"]

    def load(kind, artifact_id):
        return json.loads(paths[kind][artifact_id].read_text())

    problems, tasks = [], 0
    for task_id in paths["task_template"]:
        tasks += 1
        task = load("task_template", task_id)
        where = f"{agency_dir.name}: task {task_id}"

        render_id = task.get("render_config_id")
        if render_id not in paths["generic_template"]:
            problems.append(f"{where}: render config {render_id!r} is not in the manifest")
            continue
        render = load("generic_template", render_id)
        if not non_empty_names((render.get("read") or {}).get("roles")):
            problems.append(f"{where}: render config {render_id} has no read.roles")

        workflow = load("workflow", task["workflow_id"])
        for node in workflow["nodes"]:
            step_id = node.get("task_template_id")
            if node["type"] != "TASK" or step_id not in paths["subtask_template"]:
                continue
            step = load("subtask_template", step_id)
            if step.get("task_type") in INTERACTIVE_STEP_TYPES:
                problems += [f"{where}: {p}" for p in check_authz(step, render)]
    return tasks, problems


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    manifests = sorted(m for m in (current_manifest(d) for d in root.glob("*-v2") if d.is_dir()) if m)
    if not manifests:
        sys.exit("no *-v2 manifest found")

    all_problems, total = [], 0
    for manifest in manifests:
        tasks, problems = check_agency(root, manifest)
        total += tasks
        all_problems += problems
    for problem in all_problems:
        print(f"::error::{problem}")
    if all_problems:
        sys.exit(f"{len(all_problems)} problem(s) in {total} task(s) across {len(manifests)} agencies")
    print(f"OK: {total} tasks across {len(manifests)} agencies declare who may act")


if __name__ == "__main__":
    main()
