---
name: autofill-global-context
description: Wire trader company-level or CHA-level profile data into a workflow's USER_INPUT form as a default value, via input_mapping. Use when asked to autofill, prefill, or autopopulate a form field from company/CHA/session data, or when extending schemas/workflow-context.schema.json with a new field.
---

# Autofill from seeded workflow context

This is a new convention, first introduced on `tnsw/trade/trade_workflow.json`'s
`trade_1_cha_selection` node — there is no prior example in this repo of a `USER_INPUT`
task consuming `input_mapping` to prefill a visible form field (every existing
input_mapping crossing into a step feeds a `SYSTEM`/transform task or backend context,
e.g. `trade_2_hscode_selection` receiving `trade.cha_company_id` without that field
appearing anywhere in its own `userinput_jsonform.json`).

**Known bug — don't use `trade_workflow.json`/`1-cha_selection` as a reference.**
Confirmed by the project owner: that specific wiring (`trade_1_cha_selection` →
`notifyRecipient`) is currently affected by a bug elsewhere in the system, and its
current on-disk shape (destinations written as the fully-qualified
`traderinput.notifyRecipient` at *both* the macro and step level, rather than a bare
local var at the macro level) does not reflect the correct convention. **All other
workflows follow the pattern documented below** — `tnsw/npqs/npqs_workflow.json` /
`tnsw/npqs/1-apply/workflow.json` is the confirmed-working reference example used
throughout this doc. Don't propagate `trade_workflow.json`'s current shape elsewhere,
and don't try to "fix" it yourself — it's being tracked separately.

## What the seeded context is

Two variables are assumed seeded by the runtime — not produced by any
`subtask_template`'s `output_namespace` the way every other namespace in this repo is
(`trade.`, `userform.`, `reviewerform.`, ...):

- **`traderCompany`** — `tnsw/trade/trade_workflow.json`'s own top-level seeded
  variable, present from that macro workflow's `start`. Its shape is
  `definitions.company` in `schemas/workflow-context.schema.json`.
- **`cha`** — the CHA firm an acting session belongs to, if any (see `definitions.cha` in
  the same file). Not currently wired anywhere — see the initiator note below.

`traderCompany` only exists at `trade_workflow.json`'s own scope. `trade_3_prepare_split_items`
receives it as local var `company` (`"traderCompany?": "company"` at the macro level),
passes it into `3-prepare_split_items/workflow.json`'s `build_split_items` node
(`HSCODE_SPLIT_BUILDER`), which bakes it into each entry of `trade.split_items`.
`trade_4_trigger_flows` (`SPLIT_TASK`) then spawns one macro workflow per split item
(e.g. `tnsw/npqs/npqs_workflow.json`).

**Confirmed:** inside a `SPLIT_TASK`-spawned agency flow, company data is read back out
at `_iter.input.company.<field>` — `_iter` is the current split-task iteration context,
`.input` is that split item's own input payload. `npqs_workflow.json`'s `n1_apply` reads
it this way, e.g. `"_iter.input.company.name?": "applicant_name"`. This is *not* the same
path as `traderCompany` at `trade_workflow.json`'s own scope, or plain `company` at
`build_split_items`' own scope — each of those three names is only valid at its own
level; don't assume any of them carries over unprefixed into the next.

**Before assuming `cha` is populated for a given macro workflow, check who initiates
it.** `tnsw/trade/trade_workflow.json` is trader-initiated — the trader is using
`1-cha_selection` to *pick* a CHA, so there is no CHA in that session to begin with, and
`cha.cha_company_id` must not be wired into that field. A CHA-initiated flow (if one
exists) would be the opposite case.

## What's safe to autofill — and what isn't

Only stable, identity-level profile data belongs in `company`/`cha` and is safe to
autofill: company name/address/registration numbers, CHA firm identity, contact details.
Never autofill anything transaction-specific (consignment name, shipment value,
commodity details, dates, permit numbers) — that always comes from the user, every time,
for every consignment.

**Don't invent agency-specific facts you have no source for.** `company.agencies.<slug>`
(see below) exists specifically because different agencies track different things about
a registrant — but only model a field there once it's been confirmed real (ask, don't
guess). An early draft of this schema included
`company.agencies.npqs.nppo_office_location`, assuming NPQS assigns each exporter a
fixed office — that was invented with no evidence anywhere in this repo and was removed;
the only NPQS field modeled today is `registration_number`, confirmed directly by the
project owner.

## The two-level wiring pattern

A step is invoked as one `TASK` node in a macro workflow, but it's also its own FSM
(`tnsw/<agency-or-flow>/<step>/workflow.json`) with its own entry `TASK` node. A value
has to be threaded through **both** levels — the macro workflow doesn't hand its
variable scope down automatically:

1. **Macro level** (`npqs_workflow.json`'s `n1_apply` node): add an `input_mapping`
   entry `{"<seededVar>.<field>?": "<localVarName>"}` — source is the seeded variable's
   dotted path (marked `?` since it may not exist for this session), destination is
   whatever local var name you choose to carry it into the step's own scope. Convention
   in this repo is to just reuse the eventual form field's own name, e.g.:
   ```json
   "input_mapping": { "_iter.input.company.name?": "applicant_name" }
   ```
2. **Step level** (`1-apply/workflow.json`'s `applicant_submission` node): add the
   pass-through that actually lands the value on the bound form's own namespace — see
   the rule immediately below for exactly what destination to write:
   ```json
   "input_mapping": { "applicant_name?": "userform.applicant_name" }
   ```

Skipping step 2 means the macro-level edit is dead — the value never reaches anything a
person can see. For a `SPLIT_TASK`-spawned agency flow (like NPQS), the "macro level" is
that agency's own `<agency>_workflow.json` reading `_iter.input.company.<field>`
directly (see above) — there's no separate seeding step to add there.

## Rule: an input_mapping source key can only be used once per mapping object — fan out with the `?` suffix, not a duplicate key

`input_mapping` is a flat JSON object, so a literal source path string can appear **at
most once** — writing it twice as the exact same string is a duplicate key, and the
second entry silently overwrites the first. This bit us concretely: `applicant_name` and
`exporter_name` both need the company's name, but `_iter.input.company.name` can't be
assigned to both in one entry.

**Resolution, confirmed with the project owner:** exploit that the trailing `?` is
purely an optionality *marker* on the same underlying variable (per CLAUDE.md: "a
trailing `?` on a mapping key/value marks it optional"), not part of a different
variable name. `"_iter.input.company.name?"` and `"_iter.input.company.name"` (no `?`)
are two different JSON key *strings* — not a duplicate key — but both resolve to the
same real value, so each can drive its own destination:

```json
"input_mapping": {
  "_iter.input.company.name?": "applicant_name",
  "_iter.input.company.name": "exporter_name",
  "_iter.input.company.address?": "exporter_address"
}
```

One of the two entries necessarily loses the `?` (there's no third way to write the
same path as a distinct string), which technically makes that occurrence non-optional.
That's acceptable here specifically because `name` is a `required` field on
`definitions.company` in `schemas/workflow-context.schema.json` — it's always expected
to be present anyway, so dropping `?` doesn't introduce real risk. Don't reach for this
trick on a field that's genuinely optional in the schema; in that case, wire only one
destination and leave the other manual instead (as was done first here, before the
project owner asked for both to be populated).

Don't invent a second, distinctly-named source field (e.g. a `trading_name` alongside
`name`) just to route around this — do that only if a genuinely distinct value is
confirmed to exist, never as a workaround.

## Rule: prefix the step-level destination with the subtask's own output_namespace

**Before writing a step-level `input_mapping` destination, read the target node's own
`subtask_template` file (`userinput.json`, `reviewerinput.json`, ...) and copy its
`output_namespace` value.** Every destination that lands on that node's bound
JSONForms schema must be written as `"<output_namespace>.<field>"`, never a bare
`"<field>"`.

Why: a field bound to a `USER_INPUT`/`EXTERNAL_REVIEW` task lives at exactly one address
for its entire lifecycle inside a step, and that address is
`<output_namespace>.<field>` — that's what the node's own `output_mapping` writes to
once the form is submitted (e.g. `tnsw/npqs/1-apply/workflow.json`'s
`applicant_submission` writes `"applicant_name": "userform.applicant_name"`), and it's
what everything downstream in the same step reads from (`officer_review`'s
`input_mapping: {"userform": "submission"}` reads the whole `userform` namespace). A
pre-fill default has to land at that same address, or it's invisible to everything else
in the step that expects to find it there.

Concretely, for `tnsw/npqs/1-apply/userinput.json` (`output_namespace: "userform"`):

```json
"input_mapping": {
  "applicant_name?": "userform.applicant_name",
  "exporter_name?": "userform.exporter_name",
  "exporter_address?": "userform.exporter_address"
}
```

This applies **only to the step-level destination** — the one directly bound to the
form. The macro-level destination (step 1 above) stays a bare local var name; it's just
naming a variable for crossing the macro/step boundary, not writing into any bound
form's namespace. Only the hop that lands *inside* the step, on the node that owns the
form, needs the `output_namespace.` prefix.

**Getting this wrong is a silent failure** — there's no loader/validator that checks an
input_mapping destination against the bound `output_namespace` (see Validating below), so
a missing prefix just means the default quietly never appears, with no error anywhere.

## Match the target field's exact schema shape

Read the destination step's `*_jsonform.json` before wiring anything — the value you
inject must match that property's declared type exactly, not just conceptually:

- **Plain field** (`type: string`, even with `x-search` attached) — inject the raw
  scalar. `trade-cha-selection-flow`'s `cha_company_id` is `type: string` with
  `x-search`, so the CHA's id string alone is correct.
- **`x-search` object field** (`type: object`, `properties: {value, label}`,
  `required: [value]`) — the injected value must be an object with at least `value` set,
  not a bare string. `npqs-apply-phyto-cert--user-form`'s `importing_country` is this
  shape — injecting `"LK"` there would be wrong; it needs `{"value": "LK", "label": "Sri
  Lanka"}` (or at minimum `{"value": "LK"}`).

## Candidate-field judgment: worked example (wired)

For `tnsw/npqs/1-apply`'s `applicant_submission` node
(`npqs-apply-phyto-cert--user-form`), reading the schema field-by-field:

- `applicant_name`, `exporter_name` — safe candidates from `_iter.input.company.name`
  (used for both, per the `?`-suffix fan-out rule above; plain `type: string`). Unlike
  `trade_workflow`'s CHA question, this didn't hinge on who's logged in: this field
  describes the consignment's registered company regardless of whether a trader or
  their CHA is the one typing into the form, so no initiator check was needed before
  wiring it.
- `exporter_address` — **still sourceless in practice.** `schemas/workflow-context.schema.json`'s
  `definitions.company.data` has no `address` field at all in the one confirmed real
  payload seen so far (2026-09-08) — the `_iter.input.company.address?` mapping below
  is left in place pending real address data, but currently reads nothing. Don't treat
  it as a working example; re-verify against a real payload before relying on it.

All three currently wired as:
- Macro (`tnsw/npqs/npqs_workflow.json`, `n1_apply`):
  ```json
  "input_mapping": {
    "_iter.input.company.name?": "applicant_name",
    "_iter.input.company.name": "exporter_name",
    "_iter.input.company.address?": "exporter_address"
  }
  ```
- Step (`tnsw/npqs/1-apply/workflow.json`, `applicant_submission`) — destinations
  prefixed with `userform.`, per the rule above (no fan-out trick needed here, since
  the macro level already produced two distinct local var names):
  ```json
  "input_mapping": {
    "applicant_name?": "userform.applicant_name",
    "exporter_name?": "userform.exporter_name",
    "exporter_address?": "userform.exporter_address"
  }
  ```
- `exporter_country` already defaults to `"LK"` in the schema itself — no injection
  needed.
- `consignee_*`, `importing_country`, everything commodity/shipment/inspection-related —
  **not** autofillable: consignee is the foreign buyer (unrelated to the exporter's own
  profile), and everything else is transaction-specific.
- `nppo_office_location` — **not currently wired**, and don't assume it can be until
  confirmed: it would need a real, confirmed source (e.g. a per-company assigned office
  on file with NPQS), not an assumption. `company.agencies.npqs.registration_number` is
  modeled in the schema as confirmed NPQS-specific data, but has no matching field in
  this form yet, so it isn't wired to anything either — it's there for whenever a
  matching field exists.

Note `applicant_submission` also has a loop-back edge (`review_gateway`'s
`needs_more_info` outcome) back to itself — input_mapping re-applies defaults on
re-entry too, which is fine here since these are stable profile fields, not something a
prior submission attempt would have deliberately changed.

## Extending the schema

Add new fields to `schemas/workflow-context.schema.json`'s `definitions.company` or
`definitions.cha` as needed (`additionalProperties: false`, so undeclared fields won't
validate). Agency-specific company data goes under
`definitions.company.properties.agencies.properties.<slug>` — add a sibling property
there for a new agency's data, confirmed from a real source, never guessed. This file
isn't a manifest artifact and isn't registered in `.vscode/settings.json` /
`.idea/jsonSchemas.xml` (it documents assumed runtime-seeded variables, not a checked-in
artifact file matched by a glob), so no manifest/editor-mapping change is needed when
extending it.

## Checklist for wiring a new autofill

1. Read the target step's `*_jsonform.json` — confirm the field exists, its exact
   `type`, and whether it's `x-search`-wrapped (see the schema-shape section above).
2. Read that same step's `userinput.json`/`reviewerinput.json` — note its
   `output_namespace`.
3. Confirm the source data is real (a schema field you can point to), not guessed — ask
   if it isn't already confirmed.
4. Add the macro-level `input_mapping` entry (seeded-var path → bare local var name).
5. Add the step-level `input_mapping` entry (bare local var name →
   `<output_namespace>.<field>`, per the rule above).
6. Run the validator and grep both mappings (see below).

## Validating a change

`scripts/validate_artifacts.py` (from `nsw-trade-flows`) does **not** check
input_mapping/output_mapping semantics, and does **not** check that a step-level
destination is correctly prefixed with its own `output_namespace` — both are silent
failure modes. After wiring a new autofill:
- `grep -rn '"<localVarName>"' tnsw/<flow>/` to confirm both the macro and step level
  entries exist and use the same local var name at the boundary between them.
- Re-read the target `*_jsonform.json` property and eyeball that the injected shape
  (plain scalar vs `{value, label}` object) matches, per the schema-shape section above.
- Re-read the target `userinput.json`/`reviewerinput.json` and confirm the step-level
  destination's prefix matches its `output_namespace` exactly.
- Run `python3 .claude/skills/nsw-trade-flows/scripts/validate_artifacts.py --root .`
  anyway, to catch any unrelated structural break from the edit.