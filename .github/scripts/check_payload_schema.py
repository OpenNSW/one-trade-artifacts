#!/usr/bin/env python3
"""Check that every step where a person fills in a form names the right form for the
server to validate their submission against.

A step opts in to the payload schema extension (nsw-srilanka) by listing it:

  {"id": "payload_schema", "phase": "PRE_RESUME", "properties": {"form": "<form id>"}}

The extension reads only its own `form` property, never `plugin_properties`, and holds
every command except a draft to that form's whole JSON Schema. So the form it names must
be the one the step's render shows, with a button, in the state the step waits in; a
step that names another form refuses every submission, or checks it against rules the
user never saw.

For each USER_INPUT step a person completes, in tnsw/ and in every agency:

  * a person fills in a form there when the step names one (`user_jsonforms_id`) or its
    render has a button, in the state it waits in, on a section whose dataKey is the
    step's output_namespace. Such a step must list one `payload_schema` extension in
    phase PRE_RESUME, unless it is in EXEMPT;
  * `form` is a generic_template of the same manifest with a JSON Schema, and the same
    id as `user_jsonforms_id` where the step names one;
  * the step's render shows `form`, in the state it waits in, on a section with a button
    (a handle other than the draft's, for a command the render offers in that state) and
    a dataKey that is the step's output_namespace (a section for a custom projector has
    none), and shows no other form with a button there under that dataKey, unless the
    step is in SHARED_BUTTONS.

A step that no person completes (EXTERNAL_REVIEW, PAYMENT, or a USER_INPUT step only the
backend resumes) must not list the extension: a reply-token callback has no form to
name, so it would refuse every reply.

Usage: check_payload_schema.py [repo-root]
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

from check_agency_authz import TNSW, Deployment, completed_by_system, props, waits_in

EXTENSION_ID = "payload_schema"
PRE_RESUME = "PRE_RESUME"
DRAFT_ELEMENT = "draft_action"

# Steps a person fills in that don't list the extension, and why.
EXEMPT = {
    "customs-cusdec--user-input": (
        "its Verify button is a secondary_action on a seven-step form, so it sends a partly "
        "filled declaration that the extension would refuse"
    ),
}

# Steps whose render shows a second form with a button, and why that is safe.
SHARED_BUTTONS = {
    "slpa-consolidation--select": (
        "its refresh form's secondary_action sends the stored values, which the select "
        "form's schema (no required fields) accepts"
    ),
}


def person_completes(step):
    return step.get("task_type") == "USER_INPUT" and not completed_by_system(step)


def button_sections(render, state, namespace):
    """The sections the render shows in state that have a button (a handle other than the
    draft's, whose command the render offers in state) and write to namespace:
    [(section name, section)]."""
    offered = {a["command"] for a in ((render.get("states") or {}).get(state) or {}).get("actions", [])}
    sections = []
    for name, section in (render.get("sections") or {}).items():
        states = (section.get("visibleWhen") or {}).get("states")
        if states is not None and state not in states:
            continue
        buttons = [
            h for h in section.get("handles") or []
            if h.get("element") != DRAFT_ELEMENT and h.get("command") in offered
        ]
        if not buttons:
            continue
        if section.get("dataKey") not in (None, namespace):
            continue
        sections.append((name, section))
    return sections


def has_schema(form):
    schema = form.get("schema", form)
    return isinstance(schema, dict) and isinstance(schema.get("properties"), dict)


def check_step(deployment, step, renders, used):
    """Return the problems with a step's payload_schema extension."""
    sid, state, namespace = step["id"], waits_in(step), step.get("output_namespace")
    exts = [e for e in step.get("extensions") or [] if e.get("id") == EXTENSION_ID]
    named = props(step).get("user_jsonforms_id")
    sections = [button_sections(render, state, namespace) for render in renders]
    fills_form = bool(named) or any(
        section.get("dataKey") == namespace for found in sections for _, section in found
    )

    if not exts:
        if not fills_form:
            return []
        if sid in EXEMPT:
            used.add(sid)
            return []
        return [f"step {sid}: a person fills in a form in {state}, but it lists no '{EXTENSION_ID}' extension"]

    if sid in EXEMPT:
        used.add(sid)
        return [f"step {sid} is in EXEMPT ({EXEMPT[sid]}) but lists '{EXTENSION_ID}'; drop it from EXEMPT"]
    if len(exts) != 1 or exts[0].get("phase") != PRE_RESUME:
        return [f"step {sid} needs one '{EXTENSION_ID}' extension in phase {PRE_RESUME}"]
    form_id = (exts[0].get("properties") or {}).get("form")
    if not isinstance(form_id, str) or not form_id:
        return [f"step {sid}: '{EXTENSION_ID}' names no form"]
    if not fills_form:
        return [f"step {sid}: lists '{EXTENSION_ID}', but no person fills in a form in {state}"]

    problems = []
    if not deployment.has("generic_template", form_id):
        return [f"step {sid}: form {form_id!r} is not a generic_template in {deployment.folder}/manifest.json"]
    if not has_schema(deployment.load("generic_template", form_id)):
        problems.append(f"step {sid}: form {form_id!r} has no JSON Schema")
    if named and named != form_id:
        problems.append(f"step {sid}: names form {form_id!r}, but its user_jsonforms_id is {named!r}")

    for found in sections:
        shown = {section.get("templateId"): name for name, section in found}
        if form_id not in shown:
            problems.append(
                f"step {sid}: the render shows {sorted(shown) or 'no form'} with a button in {state} "
                f"under {namespace!r}, not {form_id!r}"
            )
        elif set(shown) != {form_id}:
            if sid in SHARED_BUTTONS:
                used.add(sid)
            else:
                others = sorted(set(shown) - {form_id})
                problems.append(f"step {sid}: the render also shows {others} with a button in {state} under {namespace!r}")
    return problems


def check_deployment(deployment, used):
    """Return the number of steps checked and the problems found."""
    renders = defaultdict(list)  # step id -> the render configs of the tasks that run it
    for task_id in deployment.paths["task_template"]:
        task = deployment.load("task_template", task_id)
        if not deployment.has("generic_template", task.get("render_config_id")):
            continue  # check_agency_authz.py reports it
        render = deployment.load("generic_template", task["render_config_id"])
        for node in deployment.load("workflow", task["workflow_id"])["nodes"]:
            step = deployment.step(node)
            if person_completes(step):
                renders[step["id"]].append(render)

    problems = []
    for step_id in deployment.paths["subtask_template"]:
        step = deployment.load("subtask_template", step_id)
        if person_completes(step):
            if step_id in renders:
                problems += check_step(deployment, step, renders[step_id], used)
        elif any(e.get("id") == EXTENSION_ID for e in step.get("extensions") or []):
            problems.append(f"step {step_id}: no person completes it, so it must not list '{EXTENSION_ID}'")
    return len(renders), [f"{deployment.folder}: {p}" for p in problems]


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    folders = [TNSW] + sorted(m.parent.name for m in root.glob("*/manifest.json") if m.parent.name != TNSW)
    if not (root / TNSW / "manifest.json").exists() or len(folders) == 1:
        sys.exit("need tnsw/manifest.json and at least one agency manifest.json")

    all_problems, steps, used = [], 0, set()
    for folder in folders:
        count, problems = check_deployment(Deployment(root, folder), used)
        steps += count
        all_problems += problems
    for step_id in sorted((set(EXEMPT) | set(SHARED_BUTTONS)) - used):
        all_problems.append(f"{step_id} is in EXEMPT or SHARED_BUTTONS, but no step needs the exception any more")
    for problem in all_problems:
        print(f"::error::{problem}")
    if all_problems:
        sys.exit(f"{len(all_problems)} problem(s) in {steps} person-completed step(s)")
    print(f"OK: {steps} person-completed steps name the form their render shows")


if __name__ == "__main__":
    main()
