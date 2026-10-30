#!/usr/bin/env python3
"""Check that every task says who may act on it and who may read it.

A deployment is the trader-facing NSW (tnsw/) or one agency (<agency>/). In each:

  * every step that waits for a caller (USER_INPUT, EXTERNAL_REVIEW, PAYMENT) carries a
    PRE_RESUME `authz` extension: state -> command -> the logical names allowed to run
    it, deny by default;
  * every task template lists in `read.roles` the roles allowed to open its task.

The backend checks a step only when it declares the extension, and lets every owning
role read a task whose template has no `read` block, so a task that omits either is open
to every caller who reaches it.

A step's rule must be exactly what the step needs: the one state the step waits in, and
the commands it can be completed with there.

  * A USER_INPUT step: the commands its task's render config offers in that state, and
    its draft command. It may name any of the deployment's roles.
  * A step this service's own code completes (a PAYMENT step, or a USER_INPUT step that
    waits in QUEUED_EXTERNALLY for a webhook): SYSTEM_COMMAND, for SYSTEM only. A
    USER_INPUT one may also take the commands its task's render config offers in that
    state, such as a trader cancelling what the webhook would complete; those may name
    any of the deployment's roles.
  * A tnsw EXTERNAL_REVIEW step: what its agency's task sends back, for the agency's
    client (the step's service_id) only. That is every value the task's workflows map to
    the command of a callback to tnsw, and the reply command of every step that replies
    to tnsw.
  * An agency step that waits for tnsw's reply: what tnsw sends it, for tnsw's client
    only. That is the reply commands of the tnsw steps that dispatch to its task, and the
    values tnsw maps to the command of a callback to the agency.

A value mapped to a callback's command is narrowed to the literals the callback node's
incoming edges compare it with (==), when every incoming edge does. Otherwise it is
every value the variable can hold: the enum, const or oneOf (else the default) of the
form field that writes it, or, when it is a step's own command, the commands its render
config offers.

Every role a step's rule names must also be in its task template's read.roles.

The names are the logical names each deployment's catalog defines (NAMES), mirrored from
nsw-srilanka: configs/catalog.example.json for tnsw, configs/agency/<agency>/catalog.json
for each agency. SYSTEM is in no catalog: the backend reserves it.

Usage: check_agency_authz.py [repo-root]
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Step types that wait for a caller. Only these need an authz rule; the others
# (REFID_GENERATOR, AUTH_API_CALL, ...) run without one.
PARKING_STEP_TYPES = {"USER_INPUT", "EXTERNAL_REVIEW", "PAYMENT"}
AUTHZ_EXTENSION_ID = "authz"
PRE_RESUME = "PRE_RESUME"
COMMAND_VAR = "__command"
CALLBACKS_PATH = "/api/v1/callbacks/"

# The name a rule gives this service's own in-process callers, and the command they
# complete a step with: nsw-srilanka's customs and SLPA webhook handlers send it, and
# its payment.TaskCompleter names it for the payment service.
SYSTEM = "system"
SYSTEM_COMMAND = "submit"

TNSW = "tnsw"
NAMES = {
    TNSW: {"roles": {"trader", "cha"}, "clients": {"cda", "efc", "fcau", "govpay", "npqs", "slce", "slpa", "sltb"}},
    "agency": {"roles": {"officer"}, "clients": {TNSW}},
}


class Deployment:
    """One manifest's artifacts, by kind and id."""

    def __init__(self, root, folder):
        self.folder = folder
        self.service = folder
        self.names = NAMES[TNSW if folder == TNSW else "agency"]
        self.paths = defaultdict(dict)
        for artifact in json.loads((root / folder / "manifest.json").read_text())["artifacts"]:
            self.paths[artifact["kind"]][artifact["id"]] = root / folder / artifact["path"]

    def has(self, kind, artifact_id):
        return artifact_id in self.paths[kind]

    def load(self, kind, artifact_id):
        return json.loads(self.paths[kind][artifact_id].read_text())

    def step(self, node):
        """The subtask_template a workflow node runs, or {} for any other node."""
        step_id = node.get("task_template_id")
        if node["type"] != "TASK" or not self.has("subtask_template", step_id):
            return {}
        return self.load("subtask_template", step_id)

    def workflows_from(self, workflow_id):
        """workflow_id and every workflow its TASK nodes start, through task_templates."""
        found, pending = [], [workflow_id]
        while pending:
            current = pending.pop()
            if current in found or not self.has("workflow", current):
                continue
            found.append(current)
            for node in self.load("workflow", current)["nodes"]:
                if node["type"] == "TASK" and self.has("task_template", node.get("task_template_id")):
                    pending.append(self.load("task_template", node["task_template_id"])["workflow_id"])
        return found

    def renders_for(self, workflow_ids):
        """The render configs of the tasks that run any of workflow_ids."""
        renders = []
        for task_id in self.paths["task_template"]:
            task = self.load("task_template", task_id)
            if task["workflow_id"] in workflow_ids and self.has("generic_template", task.get("render_config_id")):
                renders.append(self.load("generic_template", task["render_config_id"]))
        return renders


def non_empty_names(value):
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v for v in value)


def props(step):
    return step.get("plugin_properties") or {}


def waits_in(step):
    """The state a parked step waits in."""
    if step["task_type"] == "USER_INPUT":
        return props(step).get("status_override") or "PENDING_USER"
    if step["task_type"] == "EXTERNAL_REVIEW":
        return "QUEUED_EXTERNALLY"
    return "PENDING_PAYMENT"


def completed_by_system(step):
    """Whether this service's own code completes the step: the payment service a PAYMENT
    step, a webhook handler a USER_INPUT step that waits in QUEUED_EXTERNALLY."""
    if step["task_type"] == "PAYMENT":
        return True
    return step["task_type"] == "USER_INPUT" and waits_in(step) == "QUEUED_EXTERNALLY"


def offered(renders, state):
    """The commands the render configs offer in state."""
    return {
        action["command"]
        for render in renders
        for action in ((render.get("states") or {}).get(state) or {}).get("actions", [])
    }


def field_values(schema, field):
    """The values a JSON Schema allows a (dotted) property: enum, const or oneOf, else its default."""
    node = schema
    for part in field.split("."):
        node = (node.get("properties") or {}).get(part)
        if node is None:
            return set()
    values = set(node.get("enum", []))
    if "const" in node:
        values.add(node["const"])
    for alternative in node.get("oneOf", []) + node.get("anyOf", []):
        values |= set(alternative.get("enum", []))
        if "const" in alternative:
            values.add(alternative["const"])
    if not values and "default" in node:
        values.add(node["default"])
    return values


def variable_values(deployment, workflow, renders, var):
    """Every value var can hold in workflow, from the steps whose output_mapping writes it."""
    values = set()
    for node in workflow["nodes"]:
        step = deployment.step(node)
        for source, target in (node.get("output_mapping") or {}).items():
            if not step or target.rstrip("?") != var:
                continue
            if source.rstrip("?") == COMMAND_VAR:
                values |= offered(renders, waits_in(step)) - {(step.get("draft") or {}).get("command")}
                continue
            form_id = props(step).get("user_jsonforms_id")
            if deployment.has("generic_template", form_id):
                form = deployment.load("generic_template", form_id)
                values |= field_values(form.get("schema", form), source.rstrip("?"))
    return values


def compared_literals(workflow, node_id, var):
    """The literals var is compared with (==) on every edge into node_id, or None when an
    edge doesn't restrict it."""
    pattern = re.compile(r"(?<![\w.?])" + re.escape(var) + r"\s*==\s*'([^']*)'")
    incoming = [edge for edge in workflow["edges"] if edge["target_id"] == node_id]
    literals = set()
    for edge in incoming:
        condition = edge.get("condition") or ""
        found = pattern.findall(condition)
        if not found or "||" in condition:
            return None
        literals |= set(found)
    return literals if incoming else None


def calls_back(step, service):
    """Whether step completes one of service's parked steps through its callbacks route."""
    return (
        step.get("task_type") == "AUTH_API_CALL"
        and props(step).get("service_id") == service
        and CALLBACKS_PATH in (props(step).get("path") or "")
    )


def reply_command(step, service):
    """The command step replies to service with, if it is an EXTERNAL_REVIEW reply step."""
    if step.get("task_type") == "EXTERNAL_REVIEW" and props(step).get("service_id") == service:
        return props(step).get("reply_command")
    return None


def callback_commands(deployment, workflow, renders, node):
    """The commands a callback node sends: the values mapped to payload.command."""
    commands = set()
    for source, target in (node.get("input_mapping") or {}).items():
        if target.rstrip("?") != "payload.command":
            continue
        var = source.rstrip("?")
        values = variable_values(deployment, workflow, renders, var)
        literals = compared_literals(workflow, node["id"], var)
        if literals is not None:
            values = (values & literals) or literals
        commands |= values
    return commands


def agency_sends(agency, task_code):
    """The commands an agency's task can send back to tnsw."""
    workflow_ids = agency.workflows_from(agency.load("task_config", task_code)["workflow"])
    renders = agency.renders_for(workflow_ids)
    commands = set()
    for workflow_id in workflow_ids:
        workflow = agency.load("workflow", workflow_id)
        for node in workflow["nodes"]:
            step = agency.step(node)
            if reply_command(step, TNSW):
                commands.add(reply_command(step, TNSW))
            if calls_back(step, TNSW):
                commands |= callback_commands(agency, workflow, renders, node)
    return commands


def tnsw_sends(tnsw, service, task_codes):
    """The commands tnsw can send a step of the agency's tasks task_codes that waits for it."""
    commands = set()
    for workflow_id in tnsw.paths["workflow"]:
        workflow = tnsw.load("workflow", workflow_id)
        steps = [(node, tnsw.step(node)) for node in workflow["nodes"]]
        dispatches = [
            step for _, step in steps
            if step.get("task_type") == "EXTERNAL_REVIEW"
            and props(step).get("service_id") == service
            and props(step).get("task_code") in task_codes
        ]
        if not dispatches:
            continue
        commands |= {reply_command(step, service) for step in dispatches if reply_command(step, service)}
        renders = tnsw.renders_for([workflow_id])
        for node, step in steps:
            if calls_back(step, service):
                commands |= callback_commands(tnsw, workflow, renders, node)
    return commands


def expected_rule(deployment, deployments, step, renders, owners):
    """The commands the step's rule must allow, the names it must allow them for (None:
    any of the deployment's roles), and the problems in finding them."""
    if completed_by_system(step):
        if step["task_type"] == "USER_INPUT":
            return {SYSTEM_COMMAND} | offered(renders, waits_in(step)), None, []
        return {SYSTEM_COMMAND}, [SYSTEM], []
    if step["task_type"] == "USER_INPUT":
        draft = (step.get("draft") or {}).get("command")
        return offered(renders, waits_in(step)) | ({draft} if draft else set()), None, []
    service = props(step).get("service_id")
    if step["task_type"] == "EXTERNAL_REVIEW" and deployment.folder == TNSW:
        agency = deployments.get(service)
        task_code = props(step).get("task_code")
        if agency is None or not agency.has("task_config", task_code):
            return set(), [service], [f"its task_code {task_code!r} is not a task_config of {service}/"]
        if service not in deployment.names["clients"]:
            return set(), [service], [f"its service_id {service!r} is not a client of the tnsw catalog"]
        return agency_sends(agency, task_code), [service], []
    if step["task_type"] == "EXTERNAL_REVIEW" and service == TNSW:
        return tnsw_sends(deployments[TNSW], deployment.service, owners), [TNSW], []
    return set(), None, [f"no one is known to complete a {step['task_type']} step for {service!r}"]


def check_step(deployment, deployments, step, renders, owners):
    """Return the problems with a step's authz rule."""
    exts = [e for e in step.get("extensions") or [] if e.get("id") == AUTHZ_EXTENSION_ID]
    if len(exts) != 1 or exts[0].get("phase") != PRE_RESUME:
        return [f"step {step['id']} needs one '{AUTHZ_EXTENSION_ID}' extension in phase {PRE_RESUME}"]
    rules = exts[0].get("properties") or {}
    state = waits_in(step)
    commands, only, problems = expected_rule(deployment, deployments, step, renders, owners)
    problems = [f"step {step['id']}: {p}" for p in problems]

    if set(rules) != {state}:
        problems.append(f"step {step['id']} waits in {state}, but its rule covers {sorted(rules)}")
    allowed = rules.get(state) or {}
    if not commands and not problems:
        problems.append(f"step {step['id']}: nothing offers or sends a command that completes it")
    if commands - set(allowed):
        problems.append(f"step {step['id']}: {state} must allow {sorted(commands - set(allowed))}, which its rule does not")
    if commands and set(allowed) - commands:
        problems.append(f"step {step['id']}: {state} allows {sorted(set(allowed) - commands)}, which nothing offers or sends")

    for rule_state, rule_commands in rules.items():
        for command, names in (rule_commands or {}).items():
            where = f"step {step['id']}: {rule_state}/{command}"
            if not non_empty_names(names):
                problems.append(f"{where} names no one")
            elif completed_by_system(step) and command == SYSTEM_COMMAND:
                if sorted(names) != [SYSTEM]:
                    problems.append(f"{where} is completed by this service, so must name exactly {[SYSTEM]}, names {names}")
            elif only is not None and sorted(names) != sorted(only):
                problems.append(f"{where} must name exactly {only}, names {names}")
            elif only is None and set(names) - deployment.names["roles"]:
                problems.append(
                    f"{where} names {sorted(set(names) - deployment.names['roles'])}, "
                    f"which are not roles of the {deployment.folder} catalog"
                )
    return problems


def rule_roles(step, roles):
    """The roles a step's rule names."""
    names = set()
    for ext in step.get("extensions") or []:
        if ext.get("id") == AUTHZ_EXTENSION_ID:
            for commands in (ext.get("properties") or {}).values():
                for allowed in (commands or {}).values():
                    names |= set(allowed or []) & roles
    return names


def check_deployment(deployment, deployments):
    """Return the number of tasks and parking steps checked, and the problems found."""
    owners = defaultdict(set)  # step id -> task_configs whose workflows run it
    for task_code in deployment.paths["task_config"]:
        for workflow_id in deployment.workflows_from(deployment.load("task_config", task_code)["workflow"]):
            for node in deployment.load("workflow", workflow_id)["nodes"]:
                owners[node.get("task_template_id")].add(task_code)

    problems, step_renders = [], defaultdict(list)
    for task_id in deployment.paths["task_template"]:
        task = deployment.load("task_template", task_id)
        where = f"task {task_id}"
        render_id = task.get("render_config_id")
        if not deployment.has("generic_template", render_id):
            problems.append(f"{where}: render config {render_id!r} is not in the manifest")
            continue
        render = deployment.load("generic_template", render_id)
        readers = (task.get("read") or {}).get("roles")
        if not non_empty_names(readers):
            problems.append(f"{where}: has no read.roles")
            readers = []
        elif set(readers) - deployment.names["roles"]:
            problems.append(
                f"{where}: read.roles names {sorted(set(readers) - deployment.names['roles'])}, "
                f"which are not roles of the {deployment.folder} catalog"
            )

        for node in deployment.load("workflow", task["workflow_id"])["nodes"]:
            step = deployment.step(node)
            if step.get("task_type") not in PARKING_STEP_TYPES:
                continue
            step_renders[step["id"]].append(render)
            cannot_read = rule_roles(step, deployment.names["roles"]) - set(readers)
            if readers and cannot_read:
                problems.append(f"{where}: step {step['id']} lets {sorted(cannot_read)} act, but read.roles does not let them open it")

    for step_id, renders in step_renders.items():
        step = deployment.load("subtask_template", step_id)
        problems += check_step(deployment, deployments, step, renders, owners[step_id])
    return len(deployment.paths["task_template"]), len(step_renders), [f"{deployment.folder}: {p}" for p in problems]


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    # Every top-level folder with a manifest is a deployment: tnsw/ and one per agency.
    folders = [TNSW] + sorted(m.parent.name for m in root.glob("*/manifest.json") if m.parent.name != TNSW)
    if not (root / TNSW / "manifest.json").exists() or len(folders) == 1:
        sys.exit("need tnsw/manifest.json and at least one agency manifest.json")
    deployments = {folder: Deployment(root, folder) for folder in folders}

    all_problems, tasks, steps = [], 0, 0
    for deployment in deployments.values():
        task_count, step_count, problems = check_deployment(deployment, deployments)
        tasks, steps = tasks + task_count, steps + step_count
        all_problems += problems
    for problem in all_problems:
        print(f"::error::{problem}")
    if all_problems:
        sys.exit(f"{len(all_problems)} problem(s) in {tasks} task(s) across {len(deployments)} deployments")
    print(f"OK: {tasks} tasks and {steps} steps across {len(deployments)} deployments say who may act and read")


if __name__ == "__main__":
    main()
