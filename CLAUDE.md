# one-trade-artifacts

This repo has no build, no tests, no package manager — it is a pure collection of JSON
configuration artifacts that drive Sri Lanka's National Single Window (NSW) trade
workflows across several government regulatory agencies. Editing here means editing JSON
by hand and reasoning about it as a workflow spec, not writing/running code.

## Agencies

- `cda/` — Coconut Development Authority
- `efc/` — Export Facilitation Center (EFC), which reviews the boat note for the
  `tnsw/customs/` workflow
- `fcau/` — Food Control Administration Unit
- `npqs/` — National Plant Quarantine Service
- `sltb/` — Sri Lanka Tea Board
- `tnsw/` — the NSW orchestration layer that ties all of the above into end-to-end
  trader-facing application flows (plus its own `trade/` sub-flow for consignment setup
  shared across agencies)

## Two halves of every agency

Each agency has artifacts in two places that must be read together, not independently:

1. **`tnsw/<agency>/`** — owned by the NSW side. Contains:
   - `<agency>_workflow.json` — the top-level **macro workflow**: a single FSM stitching
     together every step for that agency's certificate/approval process.
   - Numbered step folders (`1-application/`, `2-payment_app_fee/`, `3a-dropoff/`,
     `3b-schedule_pickup/`, `3c-record_sample_draw/`, ...) — each is a self-contained
     **step sub-workflow** (its own `workflow.json` FSM) representing one macro-workflow
     node. Letter suffixes (`3a`/`3b`/`3c`) mark parallel/exclusive branches of the same
     numbered stage.
2. **`<agency>/`** (top-level, e.g. `sltb/`) — owned by the agency's own backend
   (nsw-srilanka in agency mode). One folder per officer task, each holding the task's
   `*.taskconfig.json` and the workflow, templates and forms that drive it (see "The
   agency shape" below), all indexed in `<agency>/manifest.json`.

### Agencies with more than one process

An agency can run more than one certificate/approval **process** end-to-end (distinct
macro workflows, e.g. a second CDA registration type alongside the existing coconut
export certificate). An agency that needs this nests **both halves** one level deeper,
under one folder per process:

- `tnsw/<agency>/<process-slug>/<agency>_workflow.json` + that process's numbered step
  folders — same internal structure as the single-process layout, just moved down a
  level. `tnsw/manifest.json` stays put; only the `path` of that process's artifacts
  gains the `<process-slug>/` segment.
- `<agency>/<process-slug>/<task folder>/` for that process's agency-side tasks.
  `<agency>/manifest.json` stays at the agency's top level; only its artifact
  `path`s move under `<process-slug>/`.

`<process-slug>` is an arbitrary folder name, not a numbering scheme — it's whatever
that process happens to be called at the time it's split out (e.g. a placeholder like
`process-1` if the real name isn't decided yet, later renamed to something descriptive
once the process is named). Don't assume sibling processes are numbered sequentially,
or read any other meaning into the slug.

Agencies are migrated to this layout only when they actually need a second process —
premature nesting for a single-process agency is not the convention. CDA is the only
agency nested so far, on both sides (`tnsw/cda/` and `cda/`); `tnsw/customs/`,
`tnsw/fcau/`, `tnsw/npqs/`, `tnsw/slpa/`, `tnsw/sltb/`, `tnsw/trade/` and the other
`<agency>/` folders remain flat until one of them needs a second process.

The link between the two: a step's `EXTERNAL_REVIEW` subtask template
(`tnsw/<agency>/<step>/officer_*.json`) names `service_id` + `task_code`. The agency
service loads the `task_config` whose manifest id is that `task_code` from
`<service_id>/` (its `taskCode` field repeats the id), and that taskconfig's
`workflow` is the officer task the case is injected into. The forms on
each side are separate copies, and some share an id with a `tnsw/` form but differ from
it, so change each side's copy on its own terms.

When the officer needs to see a field the trader's form doesn't carry (typically a value
produced by an earlier step — a certificate/letter the officer must reference, not
something the trader re-submits), add it, marked `readOnly`, to the agency-side form
only, and wire its data through the officer's `EXTERNAL_REVIEW` `TASK` node's
`input_mapping` (see the merge idiom below), not through the trader's own task
`output_mapping`.

## The agency shape

The agency folders have the same shape as `tnsw/`: workflows, task templates, subtask
templates and generic templates, plus a `task_config` binding each officer task to its
workflow.

What it looks like (as in `cda/`):

- One folder per officer task, under a process folder when the agency has more than one
  process (`cda/<process-slug>/`), otherwise directly under `<agency>/`.
- Each task folder is like a `tnsw/` step folder plus a taskconfig: its
  `*.taskconfig.json` plus the `workflow.json`, `task_template`, `review_workflow`,
  `subtask_template`s, JSONForms, `render.json`, markdown view and notification template
  that drive that task. All are indexed in the agency's `<agency>/manifest.json`. A
  form several tasks share is kept in one task's folder and listed once; the others use
  it by id.
- Taskconfigs are `schemaVersion: 2`: a `workflow` id binding plus `meta`, with no
  inline `forms`/`behavior`/`permissions`. `schemas/taskconfig.schema.json` dispatches
  on `schemaVersion` to `taskconfig.v1.schema.json` or `taskconfig.v2.schema.json`.
- `<agency>/manifest.json` entries intentionally omit `"loader": "local"`.

**Whenever a new top-level folder is introduced (an `<agency>/` or anything else),
add it to the `aws s3 sync --include` list in both `.github/workflows/push-to-r2-dev.yml`
and `.github/workflows/push-to-r2-staging.yml`.** The sync excludes everything not
listed, so a folder missing from these lists is silently never published to R2. Both
syncs are currently manual (`workflow_dispatch`) only: dev's push-to-`main` trigger is
off until ArgoCD deployment is enabled for the nsw-srilanka dev environment, so merging
alone does not publish anything.

## Artifact kinds (see any `manifest.json`)

- `workflow` — an FSM: `nodes` (`START`, `TASK`, `GATEWAY`, `END`) + `edges` (with
  optional `condition` string on branches out of a gateway). `gateway_type` is one of
  `EXCLUSIVE_SPLIT` / `EXCLUSIVE_JOIN` / `PARALLEL_SPLIT` / `PARALLEL_JOIN`. Applies both
  to macro workflows (`*_workflow.json`) and step sub-workflows (`<step>/workflow.json`)
  — both use the same `kind: "workflow"`, there is no separate kind for the macro level.
- `task_template` — binds a workflow id to a render config (`render_config_id`).
  `"claimable": true` lets an officer claim the task, so the others see who is working
  it (a hint, never a lock). Every agency (`<agency>/`) task template sets it; the
  `tnsw/` ones leave it out, and without it the task has no claims.
- `subtask_template` — a single node's behavior inside a step workflow: `task_type`
  (`USER_INPUT`, `EXTERNAL_REVIEW`, `PAYMENT`, `CHA_PERSIST_WRITER`,
  `HSCODE_SPLIT_BUILDER`, `CUSTOMS_CUSDEC_DISPATCH`, ...), `output_namespace`, and
  `plugin_properties` (for `EXTERNAL_REVIEW`: `service_id`, `path`, `task_code`,
  `officer_jsonforms_id`).
- `generic_template` — a JSONForms schema+uiSchema (`*_jsonform.json`), a markdown
  template, or a `render.json` UI layout (sections, `visibleWhen` states, action
  `handles`).
- `task_config` — an agency-side officer task definition (`<agency>/` only):
  `schemaVersion: 2`, `taskCode`, the `workflow` id it runs, and `meta.title/description`.

### File fields (`render.json` `files`)

A `render.json` lists under `files` every path in its task's data that holds an uploaded
file: `"files": ["userform.invoice", "submission.documents[*].file"]`. Paths start at the
task data's top-level key (a step's `output_namespace`, or an input's destination), and
`[*]` steps into every element of a list. The backend only ever hands these values out
as file references issued to the reader, takes them back the same way on submit, and
sends OGAs references issued to them. It protects nothing else, so a file field left out
of `files` goes out as its raw storage key (see nsw-srilanka's `docs/WORKFLOW_GUIDE.md`,
"File fields").

- A task's data is its active step's mapped inputs plus that step's submission under its
  namespace, so list the union over all of the task's steps: the form's own upload
  fields under its namespace, files that arrive through `input_mapping` (including an
  `EXTERNAL_REVIEW` step's `submission.*`), and the file fields of the OGA form whose
  data an `EXTERNAL_REVIEW` step stores under its namespace.
- Agency-side (`<agency>/`) tasks list the pushed files they show, under the
  destination of `notification` (usually `submission.*`), and their officer's uploads.
- Declare each path once, in the shape of its data: a field that holds several files is
  a list and needs `[*]`. A file upload allowing more than one file (`x-file.maxFiles` or
  uiSchema `options.maxFiles` above 1) stores a list even when its schema says `string`.
  The backend refuses a value of another shape.
- A markdown link to a file is written `[label](files:{{.path}})`, not
  `[label]({{.path}})`, and the path must be one of the render config's `files`. Inside
  `{{with .x}}` or `{{range .x}}`, `[label](files:{{.}})` links `x` or each element of it.
- CI (`.github/workflows/validate-render-configs.yml`) checks both: every `files` entry is
  well formed and listed once (`schemas/render.schema.json`), and
  `scripts/check_file_fields.py` traces every `"format": "file"` field through the
  workflows and fails on an undeclared file field, one declared in the wrong shape, or a
  file link without `files:`. Run it locally with `python3 scripts/check_file_fields.py`.
- Every file field (a `"format": "file"` string, or a list of them) states its rules in
  `x-file`, which the browser and the server both read: `accept`, listing extensions
  (`".pdf,.png"`), or `"*"`/`"*/*"` for any file, never a type such as `application/pdf`
  or `image/*` (the server checks a file by its value's extension); and, on a list,
  `maxFiles` above 1 (without it the file control keeps one file). The same check fails
  on a field without them. Offer only extensions the deployment accepts for upload
  (nsw-srilanka's `storage.allowedUploadTypes`; this repo can't check that).
  `x-spreadsheet` and `x-xml` aren't covered: their files are read in the browser and never
  uploaded.

## Data flow between steps

Steps pass data via dotted-path `input_mapping` / `output_mapping` on each `TASK` node,
namespaced per step's `output_namespace` (e.g. `schedule.visit_date`,
`sltb.schedule.visit_date`). A trailing `?` on a mapping key/value marks it optional.
Gateway edge `condition` strings reference these namespaced fields (e.g.
`sltb.collection_method == 'pickup'`).

Write each condition on a form value as a match on the values it accepts, never as a
catch-all: `item.treatment_result in [nil, 'pass']`, not `item.treatment_result != 'fail'`.
An exclusive or batch split takes the first edge whose condition holds and parks the
flow when none does, so a value no form offers (a renamed or new option) parks instead
of silently taking the catch-all edge. List `nil` with the values when a missing field
must take that edge too. Boolean flags (`cig.accepted != true`) and loops back to the
same step (`cda.payment_status != 'success'` → pay again) can stay as they are.

A `TASK` node's `input_mapping` can combine a whole-namespace mapping
(`"userform": "submission"`) with one or more dotted single-field additions into that
*same* destination object (`"reference_number": "submission.reference_number"`) — these
merge into one object rather than overwrite each other. This is the standard way to
hand an `EXTERNAL_REVIEW` task one extra field that's already sitting in the step's
shared context (e.g. a value an earlier step pushed to the macro workflow and the
current step's own node received back via its input_mapping) without needing that field
to flow through the immediately-preceding `TASK` node's own schema/output_mapping first.
See `tnsw/fcau/3-2-warehouse_inspection/workflow.json` or
`tnsw/customs/4-create_cdn/workflow.json` for existing examples of this idiom.

## Conventions

- IDs are kebab-case and scoped by agency/flow, e.g. `sltb-schedule-pickup-flow`,
  `npqs-draw-sample--officer-draw`.
- Ids, task codes, folders and file names carry no version (`_v1`, `-v3`): this repo
  holds only the current state of each artifact. nsw-srilanka names the trade workflow,
  `trade-export`, as its default consignment flow, so renaming it needs a change there too.
- `manifest.json` files list `{id, kind, version, path}` rows under `artifacts` — adding
  a new artifact file means also adding an entry to the relevant `manifest.json`. List
  each id, kind and version once per manifest: the registry keeps only the last of two
  such rows, so an edit to the other copy silently does nothing. The two
  sides are asymmetric: each agency folder (`cda/`, `efc/`, `fcau/`, `npqs/`,
  `sltb/`) has its own manifest, but
  the `tnsw/` side has a single shared `tnsw/manifest.json` covering every agency's step
  artifacts — there is no per-agency manifest under `tnsw/<agency>/`.
- Every task declares who may act on it and who may read it, in `tnsw/` and in each
  agency deployment (`cda/`, `efc/`, `fcau/`, `npqs/`, `sltb/`; an
  agency has no per-case owner, so any role in its catalog owns every injected
  workflow, and only the task narrows that). Each step that waits for a caller
  (`USER_INPUT`, `EXTERNAL_REVIEW`, `PAYMENT`) carries an `authz` extension for the one
  state it waits in, state → command → logical names, deny by default:
  `"extensions": [{"id": "authz", "phase": "PRE_RESUME", "properties": {"PENDING_USER": {"submit": ["trader", "cha"]}}}]`.
  Whom it names, for which commands:
  - a trader/CHA step: `trader` and/or `cha` (an agency officer step: `officer`), for
    every command its render config offers in that state, and its draft command;
  - a `tnsw` `EXTERNAL_REVIEW` step: its `service_id`, the agency's client, for every
    command the agency's task sends back (its decision values and reply commands);
  - an agency step that waits for TNSW's reply: `tnsw`, for every command TNSW sends it
    (`resubmit`, and the value of any TNSW callback to it);
  - a `PAYMENT` step, or a `USER_INPUT` step that waits in `QUEUED_EXTERNALLY` for a
    webhook: `"submit": ["system"]`. `system` is the name the backend's own code acts
    as; no request can.

  Each task's `task_template` carries `"read": {"roles": [...]}`, an ordered precedence
  list (`["trader", "cha"]` in `tnsw/`, `["officer"]` in an agency) that includes every
  role its steps let act. The names are the catalogs' (`configs/catalog.example.json`
  and `configs/agency/<agency>/catalog.json` in nsw-srilanka).
  `.github/scripts/check_agency_authz.py` works each step's rule out from the artifacts
  and fails CI on any difference.
- A step a person fills in a form on lists a `payload_schema` extension
  (`{"id": "payload_schema", "phase": "PRE_RESUME", "properties": {"form": "<form id>"}}`),
  so the server checks the submission against that form's schema. `form` must be the form
  the step's render shows, with a button, in the state the step waits in.
  `.github/scripts/check_payload_schema.py` fails CI on a form step without it, a `form`
  the render doesn't show, or the extension on a step no person completes. A step that
  can't take it is listed, with the reason, in the script's `EXEMPT`.
- **Drafts never go through the workflow.** A step that offers "Save as Draft"
  declares `"draft": {"command": "save_as_draft"}` on its `subtask_template`, and its
  render handle uses `"element": "draft_action"`. The portal saves it with
  `PUT /api/v1/tasks/{id}/steps/{stepId}/draft` (core's `SaveDraft`), which stores it
  under the step's `output_namespace` without completing the step. So the handle's
  section `dataKey` must be that namespace, the step's workflow needs no draft
  gateway, and outputs the form requires can be required (no `?`) in the
  `output_mapping`. An `authz` rule names the draft command like any other.
- Single repo owner: `@Aravinda-HWK` (`.github/CODEOWNERS`, wildcard rule).

## When asked to explain or change a flow

Read both halves before answering: the `tnsw/<agency>/` macro workflow + step
sub-workflows for the control flow, and the matching task folder under `<agency>/` for
what the officer actually sees and fills in. Don't assume the two are independent, or
that a form id shared by both sides has the same content on each.