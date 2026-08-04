# one-trade-artifacts

This repo has no build, no tests, no package manager — it is a pure collection of JSON
configuration artifacts that drive Sri Lanka's National Single Window (NSW) trade
workflows across several government regulatory agencies. Editing here means editing JSON
by hand and reasoning about it as a workflow spec, not writing/running code.

## Agencies

- `cda/` — Coconut Development Authority
- `customs/` — Sri Lanka Customs
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
2. **`<agency>/`** (top-level, e.g. `sltb/`) — owned by the agency's own backend service.
   Flat list of `task_config` artifacts only (no workflow logic), each with a
   `manifest.json` indexing them. These are the officer-facing forms that the agency
   service injects into a task at runtime.

The link between the two: a step's `EXTERNAL_REVIEW` task template
(`tnsw/<agency>/<step>/officer_*.json`) references `service_id` + `task_code`, and that
`task_code` resolves to the matching file under the agency's own top-level folder. The
officer-facing JSONForm (`*_jsonform.json`) is duplicated in both locations byte-for-byte
— **when changing an officer-facing form, update both copies.**

## Artifact kinds (see any `manifest.json`)

- `workflow` — an FSM: `nodes` (`START`, `TASK`, `GATEWAY`, `END`) + `edges` (with
  optional `condition` string on branches out of a gateway). `gateway_type` is one of
  `EXCLUSIVE_SPLIT` / `EXCLUSIVE_JOIN` / `PARALLEL_SPLIT` / `PARALLEL_JOIN`. Applies both
  to macro workflows (`*_workflow.json`) and step sub-workflows (`<step>/workflow.json`)
  — both use the same `kind: "workflow"`, there is no separate kind for the macro level.
- `task_template` — binds a workflow id to a render config (`render_config_id`).
- `subtask_template` — a single node's behavior inside a step workflow: `task_type`
  (`USER_INPUT`, `EXTERNAL_REVIEW`, `PAYMENT`, `CHA_PERSIST_WRITER`,
  `HSCODE_SPLIT_BUILDER`, `CUSTOMS_CUSDEC_DISPATCH`, ...), `output_namespace`, and
  `plugin_properties` (for `EXTERNAL_REVIEW`: `service_id`, `path`, `task_code`,
  `officer_jsonforms_id`).
- `generic_template` — a JSONForms schema+uiSchema (`*_jsonform.json`), a markdown
  template, or a `render.json` UI layout (sections, `visibleWhen` states, action
  `handles`).
- `task_config` — an agency-side officer task definition (top-level agency folders only):
  `taskCode`, `meta.title/description`, and a `forms` map to a `generic_template` id.

## Data flow between steps

Steps pass data via dotted-path `input_mapping` / `output_mapping` on each `TASK` node,
namespaced per step's `output_namespace` (e.g. `schedule.visit_date`,
`sltb.schedule.visit_date`). A trailing `?` on a mapping key/value marks it optional.
Gateway edge `condition` strings reference these namespaced fields (e.g.
`sltb.collection_method == 'pickup'`).

## Conventions

- IDs are kebab-case and scoped by agency/flow, e.g. `sltb-schedule-pickup-flow`,
  `npqs-draw-sample--officer-draw`.
- `manifest.json` files are flat arrays of `{id, kind, version, path}` (top-level agency
  folders also add `"loader": "local"`) — adding a new artifact file means also adding an
  entry to the relevant `manifest.json`. The two sides are asymmetric: each top-level
  agency folder (`cda/`, `customs/`, `fcau/`, `npqs/`, `sltb/`) has its own manifest, but
  the `tnsw/` side has a single shared `tnsw/manifest.json` covering every agency's step
  artifacts — there is no per-agency manifest under `tnsw/<agency>/`.
- Single repo owner: `@Aravinda-HWK` (`.github/CODEOWNERS`, wildcard rule).

## When asked to explain or change a flow

Read both halves before answering: the `tnsw/<agency>/` macro workflow + step
sub-workflows for the control flow, and the top-level `<agency>/` folder for what the
officer actually sees/fills in. Don't assume the two are independent — the officer-facing
form content is intentionally duplicated, not divergent, unless a diff proves otherwise.