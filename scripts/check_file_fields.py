#!/usr/bin/env python3
"""Check that every render config declares the files its task's data can hold.

nsw-srilanka only protects the values a render config lists under "files": it
hands them out as file references, takes references back on submit, and sends
OGAs references issued to them. A file value at a path that isn't listed goes
out as it is stored. This check finds every such value.

A task's data is its active step's mapped inputs plus that step's submission
under its output namespace, so a render config must list the union, over its
task's steps, of the paths that can hold a file. Files start at form fields
with "format": "file" (a step's submission), at an OGA's callback payload, or
at a step that generates a file (HTML_DOCUMENT_GENERATOR, at its .key),
and move through input_mapping and output_mapping ({source: destination}) at
both workflow levels, and across TNSW and the agencies.

It reports, per render config:
  - a file path that isn't declared (error);
  - a file path declared in the wrong shape, such as "x" for a list of files,
    which must be "x[*]" (error);
  - a markdown link to a file that doesn't use the files: scheme (error);
  - a declared path no file reaches, or a files: link to one (warning:
    harmless, but likely stale).

Usage: python3 scripts/check_file_fields.py [--root DIR]
Exits 1 when it finds an error.
"""

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict

TNSW = "tnsw"

# Step types that store a file they generate, under {output_namespace}.key.
GENERATED_FILE_STEPS = {"HTML_DOCUMENT_GENERATOR"}


def load(root, path):
    with open(os.path.join(root, path)) as f:
        return json.load(f)


def clean(path):
    # "reviewerform?" marks an optional mapping; "?" can only end a segment.
    return ".".join(seg.rstrip("?") for seg in path.split("."))


def transfer(paths, mapping):
    """Move file paths through a {source: destination} mapping."""
    out = {}
    for src, dst in (mapping or {}).items():
        s, d = clean(src), clean(dst)
        for p, origin in paths.items():
            if p == s:
                out.setdefault(d, origin)
            elif p.startswith(s + ".") or p.startswith(s + "["):
                out.setdefault(d + p[len(s):], origin)
    return out


def ui_multi_files(ui):
    """Paths of controls whose uiSchema allows more than one file."""
    out = set()

    def walk(node):
        if isinstance(node, dict):
            if (node.get("options") or {}).get("maxFiles", 1) > 1 and isinstance(node.get("scope"), str):
                path = ""
                for part in node["scope"].lstrip("#/").split("/"):
                    if part in ("properties", ""):
                        continue
                    if part == "items":
                        path += "[*]"
                    else:
                        path = f"{path}.{part}" if path else part
                out.add(path)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(ui)
    return out


def form_files(form):
    """The file paths in a form's data, relative to the data's root."""
    multi = ui_multi_files(form.get("uiSchema") or form.get("uischema"))
    out = set()

    def walk(schema, prefix):
        if not isinstance(schema, dict):
            return
        if schema.get("format") == "file":
            # A file upload allowing several files stores a list.
            several = (schema.get("x-file") or {}).get("maxFiles", 1) > 1 or prefix in multi
            out.add(prefix + "[*]" if several else prefix)
        for key in ("allOf", "anyOf", "oneOf"):
            for sub in schema.get(key) or []:
                walk(sub, prefix)
        for key in ("then", "else"):
            walk(schema.get(key), prefix)
        for name, sub in (schema.get("properties") or {}).items():
            walk(sub, f"{prefix}.{name}" if prefix else name)
        items = schema.get("items")
        if isinstance(items, dict):
            walk(items, prefix + "[*]")

    walk(form.get("schema"), "")
    return out


class Scope:
    """One deployment's artifacts, from its manifest. Ids are unique per kind."""

    def __init__(self, root, name):
        self.name = name
        self.by_kind = defaultdict(dict)
        self.path_of = {}
        base = name
        for a in load(root, f"{name}/manifest.json")["artifacts"]:
            path = os.path.join(base, a["path"])
            if not path.endswith(".json"):
                continue  # an HTML or other text template: it declares nothing
            self.by_kind[a["kind"]][a["id"]] = load(root, path)
            self.path_of[(a["kind"], a["id"])] = path
        generic = self.by_kind["generic_template"]
        self.forms = {i: d for i, d in generic.items() if isinstance(d, dict) and "schema" in d}
        self.templates = {i: d for i, d in generic.items() if isinstance(d, dict) and isinstance(d.get("template"), str)}
        self.renders = {i: d for i, d in generic.items() if isinstance(d, dict) and "sections" in d}
        self.steps = self.by_kind["subtask_template"]
        self.tasks = self.by_kind["task_template"]
        self.workflows = self.by_kind["workflow"]
        task_workflows = {t.get("workflow_id") for t in self.tasks.values()}
        self.macros = {i: w for i, w in self.workflows.items() if i not in task_workflows}
        # An agency's taskCode -> the workflow its inject starts, and back.
        self.inject_code = {}
        for config in self.by_kind["task_config"].values():
            if isinstance(config, dict) and config.get("schemaVersion") == 2 and config.get("workflow"):
                self.inject_code[config["workflow"]] = config["taskCode"]
        self.codes_of_task = defaultdict(set)
        for wf_id, code in self.inject_code.items():
            for node in self.workflows.get(wf_id, {}).get("nodes", []):
                if node.get("task_template_id") in self.tasks:
                    self.codes_of_task[node["task_template_id"]].add(code)
        self.vars = defaultdict(dict)  # workflow id -> {file path: origin}
        self.files = defaultdict(dict)  # render config id -> {file path: origin}
        self.pushed = defaultdict(dict)  # TNSW task_code -> {path in pushed data: origin}
        self.callback = defaultdict(dict)  # agency taskCode -> {path in callback payload: origin}

    def render_path(self, render_id):
        return self.path_of.get(("generic_template", render_id), render_id)

    def form(self, form_id, all_forms):
        return self.forms.get(form_id) or all_forms.get(form_id)


def legacy_review_forms(root):
    """Legacy OGA task configs: taskCode -> their review form, which their callback sends."""
    out = {}
    for p in glob.glob(os.path.join(root, "*", "**", "*.taskconfig.json"), recursive=True):
        with open(p) as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("forms"), dict) and d["forms"].get("review"):
            out[d["taskCode"]] = (d["forms"]["review"], os.path.relpath(p, root))
    return out


def all_forms(root):
    out = {}
    for p in glob.glob(os.path.join(root, "**", "*.json"), recursive=True):
        try:
            with open(p) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and "schema" in d and "id" in d:
            out.setdefault(d["id"], d)
    return out


class Checker:
    def __init__(self, root):
        self.root = root
        names = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(root, "*", "manifest.json")))
        self.tnsw = Scope(root, TNSW)
        self.agencies = []
        for name in names:
            if name == TNSW:
                continue
            scope = Scope(root, name)
            if scope.inject_code:  # runs as an agency: injected workflows
                self.agencies.append(scope)
        self.forms = all_forms(root)
        self.legacy = legacy_review_forms(root)

    def submission_files(self, scope, step, render):
        """The file paths in what a step stores under its namespace, and where they come from."""
        if step.get("task_type") in GENERATED_FILE_STEPS:
            # It stores a file it generates: {"key": <storage key>, ...}.
            return {"key": f"the file {step.get('task_type')} {step.get('id')} generates"}
        if step.get("task_type") == "EXTERNAL_REVIEW":
            # Until an OGA's cutover, a deployment may run its legacy service or
            # its agency (<agency>/): the step can get either one's callback.
            code = (step.get("plugin_properties") or {}).get("task_code")
            out = {}
            for agency in self.agencies:
                if code in agency.inject_code.values():
                    for p, origin in agency.callback.get(code, {}).items():
                        out.setdefault(p, origin)
            if code in self.legacy:
                form_id, config = self.legacy[code]
                form = self.forms.get(form_id)
                for p in form_files(form) if form else ():
                    out.setdefault(p, f"the callback from {config}")
            return out
        form_ids = set()
        pp = step.get("plugin_properties") or {}
        if pp.get("user_jsonforms_id"):
            form_ids.add(pp["user_jsonforms_id"])
        ns = step.get("output_namespace")
        for section in (render or {}).get("sections", {}).values():
            if section.get("projector") == "FORM" and ns and section.get("dataKey") == ns and section.get("handles"):
                form_ids.add(section["templateId"])
        out = {}
        for form_id in sorted(form_ids):
            form = scope.form(form_id, self.forms)
            if form:
                for p in form_files(form):
                    out.setdefault(p, f"form {form_id}")
        return out

    def run_task(self, scope, task_id):
        task = scope.tasks[task_id]
        wf_id = task.get("workflow_id")
        workflow = scope.workflows.get(wf_id)
        render_id = task.get("render_config_id")
        render = scope.renders.get(render_id)
        if not workflow:
            return False
        changed = False
        for node in workflow.get("nodes", []):
            step = scope.steps.get(node.get("task_template_id") or "")
            if not step or node.get("type") not in ("TASK", "SPLIT_TASK"):
                continue
            inputs = transfer(scope.vars[wf_id], node.get("input_mapping"))
            submission = self.submission_files(scope, step, render)
            before = len(scope.vars[wf_id])
            produced = dict(submission)
            if step.get("task_type") in GENERATED_FILE_STEPS and step.get("output_namespace"):
                # A generator writes into the task data under its namespace,
                # so its output_mapping reads "<namespace>.key".
                ns = step["output_namespace"]
                produced.update({f"{ns}.{p}": origin for p, origin in submission.items()})
            for p, origin in transfer(produced, node.get("output_mapping")).items():
                scope.vars[wf_id].setdefault(p, origin)
            changed |= len(scope.vars[wf_id]) != before
            ns = step.get("output_namespace")
            view = {p: f"{origin}, an input of step {node['id']}" for p, origin in inputs.items()}
            if ns:
                for p, origin in submission.items():
                    view.setdefault(f"{ns}.{p}" if p else ns, f"{origin}, stored by step {node['id']}")
            if render is not None:
                for p, origin in view.items():
                    scope.files[render_id].setdefault(p, origin)
            props = step.get("plugin_properties") or {}
            if step.get("task_type") == "EXTERNAL_REVIEW":
                code = props.get("task_code")
                # The engine nests mapped inputs, so a mapping to "submission"
                # or to "submission.<field>" gives the step a "submission"
                # input, and the push then sends only that.
                sends_submission = any(
                    clean(d) == "submission" or clean(d).startswith("submission.")
                    for d in (node.get("input_mapping") or {}).values()
                )
                if sends_submission:
                    sent = {p[len("submission."):]: o for p, o in inputs.items() if p.startswith("submission.")}
                else:
                    sent = inputs
                before = len(scope.pushed[code])
                for p, origin in sent.items():
                    scope.pushed[code].setdefault(p, origin)
                changed |= len(scope.pushed[code]) != before
            if step.get("task_type") == "AUTH_API_CALL" and props.get("service_id") == TNSW:
                body = {p[len("payload.payload."):]: o for p, o in inputs.items() if p.startswith("payload.payload.")}
                for code in scope.codes_of_task.get(task_id, ()):
                    before = len(scope.callback[code])
                    for p, origin in body.items():
                        scope.callback[code].setdefault(p, f"{scope.name}'s callback ({origin})")
                    changed |= len(scope.callback[code]) != before
        return changed

    def run_macros(self, scope):
        changed = False
        for macro_id, macro in scope.macros.items():
            if macro_id in scope.inject_code:
                # An agency's injected workflow starts with TNSW's push in "notification".
                code = scope.inject_code[macro_id]
                for p, origin in self.tnsw.pushed.get(code, {}).items():
                    key = f"notification.{p}" if p else "notification"
                    if key not in scope.vars[macro_id]:
                        scope.vars[macro_id][key] = f"TNSW's push for {code} ({origin})"
                        changed = True
            for node in macro.get("nodes", []):
                task = scope.tasks.get(node.get("task_template_id") or "")
                if not task:
                    continue
                wf_id = task.get("workflow_id")
                for target, source, mapping in (
                    (wf_id, macro_id, node.get("input_mapping")),
                    (macro_id, wf_id, node.get("output_mapping")),
                ):
                    before = len(scope.vars[target])
                    for p, origin in transfer(scope.vars[source], mapping).items():
                        scope.vars[target].setdefault(p, origin)
                    changed |= len(scope.vars[target]) != before
        return changed

    def trace(self):
        scopes = [self.tnsw] + self.agencies
        for _ in range(50):
            changed = False
            for scope in scopes:
                changed |= self.run_macros(scope)
                for task_id in scope.tasks:
                    changed |= self.run_task(scope, task_id)
            if not changed:
                return
        raise RuntimeError("file paths did not settle; is a mapping cyclic?")

    def check(self):
        self.trace()
        errors, warnings = [], []
        for scope in [self.tnsw] + self.agencies:
            for render_id, render in sorted(scope.renders.items()):
                where = scope.render_path(render_id)
                declared = render.get("files") or []
                if not isinstance(declared, list):
                    errors.append(f"{where}: files must be a list")
                    continue
                declared_set = set(declared)
                traced = scope.files.get(render_id, {})
                for path, origin in sorted(traced.items()):
                    if path in declared_set:
                        continue
                    other = path[:-3] if path.endswith("[*]") else path + "[*]"
                    if other in declared_set:
                        errors.append(f"{where}: {other!r} is declared, but the field holds "
                                      f"{'a list of files' if path.endswith('[*]') else 'one file'}; declare {path!r} ({origin})")
                    else:
                        errors.append(f"{where}: {path!r} can hold a file but isn't declared in files ({origin})")
                for path in declared:
                    other = path[:-3] if path.endswith("[*]") else path + "[*]"
                    if path not in traced and other not in traced:
                        warnings.append(f"{where}: {path!r} is declared, but no file reaches it")
                link_errors, link_warnings = self.check_links(scope, render_id, render, where, declared_set | set(traced))
                errors += link_errors
                warnings += link_warnings
        return errors, warnings

    LINK = re.compile(r"\]\((files:)?\{\{\s*\.([A-Za-z0-9_.\-]*)\s*\}\}\)")
    # The block a {{.}} link sits in: {{with .x}} makes . the value of x,
    # {{range .x}} each element of the list x.
    BLOCK = re.compile(r"\{\{\s*(range|with)\s+\.([A-Za-z0-9_.\-]+)\s*\}\}")

    def block_path(self, before):
        """The path . stands for at the end of before: the last block opened."""
        blocks = self.BLOCK.findall(before)
        if not blocks:
            return None
        kind, name = blocks[-1]
        return name + "[*]" if kind == "range" else name

    def check_links(self, scope, render_id, render, where, files):
        errors, warnings = [], []
        for key, section in render.get("sections", {}).items():
            template = scope.templates.get(section.get("templateId") or "")
            if not template or section.get("projector") != "MARKDOWN":
                continue
            text, data_key = template["template"], section.get("dataKey")
            for m in self.LINK.finditer(text):
                rel = m.group(2)
                if not rel:
                    rel = self.block_path(text[: m.start()])
                    if not rel:
                        continue
                path = f"{data_key}.{rel}" if data_key else rel
                link = m.group(0)
                template_path = scope.path_of.get(("generic_template", section["templateId"]))
                if m.group(1) and path not in files:
                    warnings.append(f"{template_path}: {link} in section {key!r} of {where} links {path!r}, which no file reaches")
                elif not m.group(1) and path in files:
                    errors.append(f"{template_path}: {link} in section {key!r} of {where} links a file; write it as files:")
        return errors, warnings


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    args = parser.parse_args()
    errors, warnings = Checker(os.path.abspath(args.root)).check()
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
