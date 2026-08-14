---
name: nsw-trade-flows
description: Trace, edit, and validate the tnsw ↔ agency artifact pairs in this repo (Sri Lanka NSW trade workflow JSON configs) before opening a PR. Use when explaining an end-to-end certificate/approval flow, adding or editing a step/task/officer form under tnsw/<agency>/ or a top-level agency folder, tracing how an EXTERNAL_REVIEW subtask_template's task_code links to an agency task_config, or checking manifest/duplication consistency prior to a pull request.
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
   `plugin_properties: { service_id: "npqs", task_code: "npqs_application_review_v1" }`.
3. `service_id` names the top-level agency folder: `npqs/manifest.json` has a
   `task_config` artifact whose id (and `taskCode` field) is
   `npqs_application_review_v1` — that's the file the NPQS service actually
   injects into the officer's task at runtime.
4. That `task_config`'s `forms.view` / `forms.review` name `generic_template`
   ids (`npqs-apply-phyto-cert--user-form`, `...--reviewer-form`). Those ids
   may resolve inside `npqs/manifest.json` itself, or only in
   `tnsw/manifest.json` — both are valid (see below); look in both before
   concluding a form is missing.
5. `behavior.statusMap` on the task_config (`approve` → `DONE`,
   `needs_more_info` → `FEEDBACK_REQUESTED`) is what eventually satisfies the
   `condition`s on `workflow.json`'s `review_gateway` edges
   (`reviewerform.review_outcome == 'approve'`).

**To trace any flow**, walk this same chain: macro workflow → step
`workflow.json` → step's `subtask_template` files → (for any
`EXTERNAL_REVIEW`) the named agency's own `manifest.json` → that
`task_config`'s `forms`. Do this before answering "what happens when an
officer does X" or "where does field Y come from" — the answer is almost
always split across both halves.

## Two things CLAUDE.md simplifies — don't be surprised by them

- **Node vocabulary is bigger than START/TASK/GATEWAY/END.** `TIMER` (a
  polling wait with `timer.duration` / `timer.counter_key`, e.g.
  `tnsw/npqs/9-ephyto/workflow.json`'s submit-and-poll loop) and `SPLIT_TASK`
  (fan-out to parallel agency flows via `split_task.items_variable`, e.g.
  `tnsw/trade/trade_workflow.json`) both appear in real workflows. Neither
  carries a `task_template_id`.
- **"Duplicated byte-for-byte" forms aren't always duplicated.** The
  read-only "here's what the applicant originally submitted" form (id like
  `<flow>--user-form`) is typically copied into *every* sibling
  `task_config` folder in the agency (e.g. 13 copies under `npqs/`, one per
  officer task) — that's deliberate, not drift, and it's how the agency
  service can render it without calling back into tnsw. But some
  officer-review forms (e.g. `customs-payment-wait--officer-form`) are
  defined **only** in `tnsw/manifest.json`, with no local agency copy at
  all — the agency's `task_config.forms` entry just references that id
  directly. Both patterns are correct; what's *not* correct is the same id
  resolving to genuinely different content in two places it's supposed to
  be identical (the validator below catches that).

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
Examples: `cda/5-view_certificate`, `customs/2-wait_payment`,
`npqs/8-issue_certificate`, `trade/1b-persist_cha`.

**2. Payment step (6/54)** — a fixed 2-task shape, no gateway:
```
start -> select_method_task[USER_INPUT] -> pay_<x>_task[PAYMENT] -> end
```
The `select_method_task`/`pay_..._task` id convention and the absence of a
gateway are consistent across every instance — payment success/failure is
handled inside the `PAYMENT` plugin, never exposed as a workflow branch.
Examples: `cda/2-payment_app_fee`, `npqs/7-payment`, `sltb/4-lab_payment`.

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
- `cda/4-lot_adjustment` and `sltb/3-schedule_pickup` prepend an extra
  trader-choice gateway *before* `officer_review` (skip review entirely on
  one branch).
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

- **Editing an officer-facing form** (`*_jsonform.json`): if the id appears in
  both `tnsw/<agency>/<step>/` and one or more `<agency>/<task_config>/`
  folders, edit **every** copy identically — check with `grep -rn
  '"<the-id>"' tnsw/manifest.json <agency>/manifest.json` to find every path
  that id resolves to, not just the one you happened to open.
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
  matching `task_config` + form copies under the agency's own top-level
  folder, and wire the new step into the macro `<agency>_workflow.json` with
  a `TASK` node and gateway
  edges as needed.
- **Changing a gateway condition or officer outcome**: the condition string
  (`reviewerform.review_outcome == 'approve'`) and the agency
  `task_config.behavior.statusMap` key (`approve`) must use the same
  literal — grep both sides when renaming an outcome value.
- `version` fields in every `manifest.json` are consistently `""` in this
  repo — there's no version-bump convention to follow.

## Validate before opening a PR

This repo has no build or test suite; `scripts/validate_artifacts.py`
(stdlib-only, no deps) is the closest thing to one. Run it from the repo
root:

```
python3 .claude/skills/nsw-trade-flows/scripts/validate_artifacts.py --root .
```

It checks, across `tnsw/manifest.json` and every top-level agency
`manifest.json`:

- every manifest entry's `path` points to a file that exists;
- every `*.json` file under a manifest's tree is actually registered in it;
- an id reused *for the same artifact kind* within one manifest (the
  deliberate duplicated-user-form pattern) has byte-identical content across
  every copy — **warns** if it's drifted;
- an id shared between `tnsw/manifest.json` and an agency's manifest has
  byte-identical content in both — **errors** if it's drifted, since
  CLAUDE.md's "update both copies" rule makes this a hard invariant;
- every `EXTERNAL_REVIEW` subtask_template's `service_id` + `task_code`
  resolves to a real `task_config` in that agency's manifest, and that
  task_config's `forms.*` resolve to a real `generic_template` id somewhere
  in the agency's manifest or `tnsw/manifest.json`;
  - every workflow node's `type`/`gateway_type` is in the observed
  vocabulary, every `TASK` node's `task_template_id` resolves, and every edge's
  `source_id`/`target_id` names a real node in that workflow.

Errors (non-zero exit) are invariant violations worth fixing or explaining in
the PR description; warnings are worth a glance but are sometimes the
deliberate reused-form pattern working as intended.

**Known pre-existing drift** (as of writing this skill, unrelated to
whatever change you're making): `cda-apply-coconut-cert--user-form` and
`customs-cusdec--reviewer-form` currently differ between their `tnsw/` and
agency-side copies. If your PR doesn't touch CDA or Customs, this is not
your bug to fix — just don't be alarmed that the validator isn't 100% clean
on a fresh checkout.

## Opening the PR

1. Create a branch, make the edits, register any new files in the relevant
   `manifest.json`(s).
2. Run the validator above; resolve any *new* errors it reports (compare
   against the known pre-existing drift noted above so you don't chase a bug
   that isn't yours).
3. `git diff` and re-check every place a changed id/path is referenced
   (`grep -rn '"<id>"' .`) — a manifest path typo or a half-updated duplicate
   form is the most common way this repo breaks silently, since there's no
   loader to catch it at build time.
4. Commit, push, and open the PR against `main` (single CODEOWNER,
   `@Aravinda-HWK`, per `.github/CODEOWNERS` — no special routing needed).
   Describe *which* flow/agency changed and, if relevant, why the officer-
   facing form or gateway condition changed, since that's the part a
   reviewer can't get from the diff alone.
