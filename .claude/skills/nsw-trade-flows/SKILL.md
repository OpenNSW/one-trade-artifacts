---
name: nsw-trade-flows
description: Trace, edit, and validate the tnsw ↔ agency artifact pairs in this repo (Sri Lanka NSW trade workflow JSON configs) before opening a PR. Use when explaining an end-to-end certificate/approval flow, adding or editing a step/task/officer form under tnsw/<agency>/ or an <agency>/ folder, tracing how an EXTERNAL_REVIEW subtask_template's task_code links to an agency task_config, or checking manifest consistency prior to a pull request.
---

# NSW trade-flow artifacts

This skill operationalizes the architecture in `CLAUDE.md` into concrete
procedures: how to trace a flow end-to-end, how to change one safely, and how
to validate the repo's cross-file invariants before opening a PR. Read
`CLAUDE.md` first if you haven't — it's the map; this is the field guide plus
a checker.

## Mental model, restated as a trace

`tnsw` is the orchestrator. It never renders an officer's screen itself — it
hands a `task_code` to an agency's own backend service and waits. Concretely,
for `tnsw/npqs/1-apply/`:

1. `workflow.json` (kind `workflow`) has a `TASK` node `officer_review` whose
   `task_template_id` is `npqs-apply-phyto-cert--external-review`.
2. That id resolves (via `tnsw/manifest.json`) to `reviewerinput.json`, a
   `subtask_template` with `task_type: "EXTERNAL_REVIEW"` and
   `plugin_properties: { service_id: "npqs", task_code: "npqs_application_review" }`.
3. `service_id` names the agency, whose tasks live in `<service_id>/`:
   `npqs/manifest.json` has a `task_config` with id
   `npqs_application_review`
   (`npqs_application_review/npqs_application_review.taskconfig.json`, whose
   `taskCode` field repeats the id). The NPQS service loads it by that id
   and creates the officer's task from it when TNSW injects the case.
4. The taskconfig's `workflow` names the workflow in that same task folder
   (`workflow.json` → `task_template.json` → `review_workflow.json`). Its
   steps are the officer's own: here, generate a reference number, the
   officer's review form (`officer_review.json` +
   `officer_review_jsonform.json`), then a gateway on its `review_outcome`.
   `needs_more_info` goes to `request_info.json`, an `EXTERNAL_REVIEW` back to
   `tnsw` (`reply_command: "needs_more_info"`) that waits for the trader's
   resubmission and loops back to the form. `approve` goes to
   `notify_decision.json`, an `AUTH_API_CALL` that posts the decision to
   TNSW's `/api/v1/callbacks/{callbackToken}`.
5. The officer form's `review_outcome` value (`approve`,
   `needs_more_info`) comes back as the `EXTERNAL_REVIEW` step's output, and
   that is what satisfies the `condition`s on `workflow.json`'s
   `review_gateway` edges (`reviewerform.review_outcome == 'approve'`).

**To trace any flow**, walk this same chain: macro workflow → step
`workflow.json` → step's `subtask_template` files → (for any
`EXTERNAL_REVIEW`) the `<service_id>/` task_config whose id is that `task_code` →
its task folder's workflow, forms and `render.json`. Do this before answering
"what happens when an officer does X" or "where does field Y come from" — the
answer is almost always split across both halves.

## What CLAUDE.md simplifies — don't be surprised by these

- **Node vocabulary is bigger than START/TASK/GATEWAY/END.** `TIMER` (a
  polling wait with `timer.duration` / `timer.counter_key`, e.g.
  `tnsw/npqs/9-ephyto/workflow.json`'s submit-and-poll loop) and `SPLIT_TASK`
  (fan-out to parallel agency flows via `split_task.items_variable`, e.g.
  `tnsw/trade/trade_workflow.json`) both appear in real workflows. Neither
  carries a `task_template_id`.
- **The two sides' forms are separate copies.** The read-only "here's what
  the applicant originally submitted" form (id like `<flow>--user-form`) has
  an agency-side copy, kept in one task folder and used by id from that
  agency's other tasks, and it often shares its id with the trader's form in
  `tnsw/`. The two sides are not kept byte-identical: an agency-side copy may
  carry `readOnly` fields the trader never sees, and many shared ids already
  differ between the two sides. Edit the copy the screen you're changing
  actually loads.
- **An agency can have more than one process.** An agency that runs more
  than one certificate/approval process nests *both* halves one level
  deeper, under one folder per process:
  `tnsw/<agency>/<process-slug>/<agency>_workflow.json` + its step folders,
  and `<agency>/<process-slug>/<task folder>/` for that process's
  agency-side tasks. `tnsw/manifest.json` and `<agency>/manifest.json`
  stay put either way — only the artifact `path`s inside them gain the
  `<process-slug>/` segment. CDA is the only agency nested so far
  (`1-export-coconut-certificate-registration/`,
  `2-king-coconut-fresh-nuts-recommendation/`, ...); everything else stays
  flat until it needs a second process. This doesn't change how you trace
  or validate anything, but **don't assume every agency's steps live
  directly under `tnsw/<agency>/`**; check for a process folder first.
- **Giving the officer a field the trader doesn't see.** When the officer
  needs one extra field (typically something an earlier step already
  produced, e.g. a certificate/letter the officer must reference but the
  trader doesn't re-submit), add it — marked `readOnly` in both the schema
  property and its uiSchema `Control`'s `options` — to the agency-side form
  only, and wire its value into the officer's `EXTERNAL_REVIEW` `TASK` node
  via `input_mapping` (see CLAUDE.md's merge-idiom note under "Data flow
  between steps"), not by adding it to the trader-facing form or routing it
  through the producing task's own output_mapping. Adding it to the trader's
  form puts an unpopulated field in front of the trader and risks breaking
  their form (this is exactly what went wrong the first time around for
  `cda-kcfn-upload-docs--user-form`: adding the field, plus a round-trip
  through the *trader's* task input/output_mapping, broke the trader's
  render; sourcing it directly into the officer's own `input_mapping` and
  leaving the trader copy untouched fixed it).

## Step sub-workflows: 3 shapes cover 93% of them

Surveyed across all 54 `tnsw/<agency>/<step>/workflow.json` files. Before
authoring a new step from scratch, check whether it's one of these three —
if so, copy the closest existing folder of that shape verbatim and rename,
rather than designing edges from first principles.

**1. Single-task pass-through (35/54)** — no gateway at all:
```
start -> <one TASK node, any task_type> -> end
```
Used for a lone trader upload/view, or a lone officer
wait/inspect/decide/issue step. Branching, if the macro flow needs it, is
expressed one level up in `<agency>_workflow.json`, not inside this step.
Examples: `cda/1-export-coconut-certificate-registration/5-view_certificate`, `customs/2-wait_payment`,
`npqs/8-issue_certificate`, `trade/1b-persist_cha`.

**2. Payment step (6/54)** — a fixed 2-task shape, no gateway:
```
start -> select_method_task[USER_INPUT] -> pay_<x>_task[PAYMENT] -> end
```
The `select_method_task`/`pay_..._task` id convention and the absence of a
gateway are consistent across every instance — payment success/failure is
handled inside the `PAYMENT` plugin, never exposed as a workflow branch.
Examples: `cda/1-export-coconut-certificate-registration/2-payment_app_fee`, `npqs/7-payment`, `sltb/4-lab_payment`.

**3. Submit → review → resubmit loop (9/54)**:
```
start -> applicant_submission[USER_INPUT] -> officer_review[EXTERNAL_REVIEW]
       -> review_gateway[EXCLUSIVE_SPLIT]
            --[...outcome == 'approve']--> end
            --[...outcome == 'needs_more_info']--> applicant_submission  (loop back)
```
Examples: `npqs/1-apply`, `fcau/1-application`, `fcau/4-3-payment_lab_fee`
(the "receipt" being reviewed instead of an application). Known variants,
still recognizably this pattern:
- `sltb/1-application` adds a third **terminal** edge, `outcome == 'reject' -> end`.
- `cda/1-export-coconut-certificate-registration/4-lot_adjustment` prepends an extra trader-choice gateway
  *before* `officer_review` (skip review entirely on one branch).
- `customs/1-cusdec_submission` inserts an automated dispatch-and-retry
  gateway (`cig.accepted == true/false`) between submission and review.

**The other 4/54 are genuinely bespoke — don't force a new step into one of
the 3 above if it's actually one of these:**
- `npqs/2-sample_collection` — `EXCLUSIVE_SPLIT`/`EXCLUSIVE_JOIN` fan-out by
  collection method, no loop.
- `npqs/4-2-visual_consignment` — two unconditional tasks chained, no gateway.
- `npqs/9-ephyto` — `TIMER`-driven submit-and-poll loop (see the node
  vocabulary note above).
- `sltb/3-schedule_pickup` — two-way negotiation with compound `&&`/`||`
  edge conditions.

## Making a change

- **Editing a form** (`*_jsonform.json`): find every path its id resolves to
  with `grep -rn '"<the-id>"' tnsw/manifest.json <agency>/manifest.json`,
  not just the one you happened to open. Each manifest lists an id once (the
  registry keeps only the last of two rows), and the same id in `tnsw/` is
  the trader's side, which may legitimately differ.
- **Adding a new artifact file**: it does nothing until it's registered.
  Add an entry to `tnsw/manifest.json` (tnsw-side files) or the relevant
  `<agency>/manifest.json` (agency-side files) — there is no per-agency
  manifest under `tnsw/<agency>/`, only the one shared `tnsw/manifest.json`.
- **Adding a new step**: first match it against the 3 shapes above and pick
  the closest existing folder of that shape as your template — copy its
  `workflow.json`, `task_template.json`, `userinput.json` +
  `userinput_jsonform.json`, and (for the review-loop shape)
  `reviewerinput.json` (`EXTERNAL_REVIEW`, with `service_id`/`task_code`) +
  its `reviewerinput_jsonform.json`, verbatim, then rename. Then add the
  matching officer task folder under `<agency>/` (copy the closest
  existing task folder: taskconfig, workflow, templates, forms,
  `render.json`), and wire the new step into the macro
  `<agency>_workflow.json` with a `TASK` node and gateway edges as needed.
- **Giving the officer one extra field the trader doesn't submit** (e.g. a
  value an earlier step already pushed to the macro workflow, and the
  current step already receives back via its own node's `input_mapping`):
  don't route it through the trader's `USER_INPUT` task's schema/
  output_mapping. Instead add it directly to the officer's
  `EXTERNAL_REVIEW` `TASK` node's `input_mapping`, merged into whatever
  destination the whole-namespace mapping already targets — e.g. next to
  `"uploadform": "submission"`, add
  `"recommendation_letter_file": "submission.recommendation_letter_file"`.
  Then add the field, `readOnly`, only to the agency-side form the officer
  sees — leave the trader-facing copy alone. See the bullet above for the
  full rationale.
- **Changing a gateway condition or officer outcome**: the condition string
  (`reviewerform.review_outcome == 'approve'`) and the value the agency's
  officer form produces (the `review_outcome` enum in that task folder's
  officer `*_jsonform.json`) must use the same literal — grep both sides
  when renaming an outcome value.
- `version` fields in every `manifest.json` are consistently `""` in this
  repo — there's no version-bump convention to follow.

## Validate before opening a PR

This repo has no build or test suite; `scripts/validate_artifacts.py`
(stdlib-only, no deps) is the closest thing to one. Run it from the repo
root:

```
python3 .claude/skills/nsw-trade-flows/scripts/validate_artifacts.py --root .
```

It checks, across `tnsw/manifest.json` and every `<agency>/manifest.json`:

- every manifest entry's `path` points to a file that exists;
- every `*.json` file under a manifest's tree is actually registered in it;
- no id is listed twice *for the same artifact kind* within one manifest
  (only the last row would load);
- every `EXTERNAL_REVIEW` subtask_template's `service_id` + `task_code`
  resolves to a real `task_config` in `<service_id>/manifest.json`, and
  that task_config's `workflow` resolves to a workflow in the same manifest;
- every workflow node's `type`/`gateway_type` is one core's `workflow/dsl.go`
  defines, every `TASK` node's `task_template_id` resolves, and every edge's
  `source_id`/`target_id` names a real node in that workflow;
- every draft step (a `subtask_template` with `draft`) has a handle with
  element `draft_action` for its draft command, legal in some state, in a
  section whose `dataKey` is the step's `output_namespace`; every
  `draft_action` handle belongs to a draft step; and no workflow still loops
  drafts back through a `command == 'save_as_draft'` edge.

Errors (non-zero exit) are invariant violations worth fixing or explaining in
the PR description. If `main` itself isn't clean, compare against a run on
`main` and only chase errors your change adds.

## Opening the PR

1. Create a branch, make the edits, register any new files in the relevant
   `manifest.json`(s).
2. Run the validator above; resolve any *new* errors it reports (compare
   against a run on `main` so you don't chase a bug that isn't yours).
3. `git diff` and re-check every place a changed id/path is referenced
   (`grep -rn '"<id>"' .`) — a manifest path typo or a half-updated copy of a
   form is the most common way this repo breaks silently, since there's no
   loader to catch it at build time.
4. Commit, push, and open the PR against `main` (single CODEOWNER,
   `@Aravinda-HWK`, per `.github/CODEOWNERS` — no special routing needed).
   Describe *which* flow/agency changed and, if relevant, why the officer-
   facing form or gateway condition changed, since that's the part a
   reviewer can't get from the diff alone.
