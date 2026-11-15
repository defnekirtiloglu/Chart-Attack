---
name: case-authoring
description: How to write a valid OSCEnario Rounds case file (facts, tiers, plan, pharmacist rule, caregiver). Use whenever drafting or fixing /draft/case.json.
---
# Writing a Rounds case

The case lives in `/draft/case.json`. Always start from the example in `/reference/example_case.json`
and keep its exact keys and value types.

## Steps
1. Read `/reference/example_case.json`.
2. Write the whole case to `/draft/case.json` with `write_file`.
3. Call `check_case`. It runs the structure check in code and returns errors and warnings.
4. Fix errors with `edit_file` (small, targeted edits; don't rewrite the file). Call `check_case` again until `ok` is true.

## Patient
- `persona`: a vivid personality and a clear attitude toward students (the patient stays in character).
- `backstory`: 8-10 everyday details he can talk about freely: name, hometown, job, family, home, hobbies, habits, who brought him in.
- `free_medical`: 4-5 harmless history items he answers if asked (allergies, past surgeries, smoking, known conditions in lay words).
  None of these may duplicate a fact below. Without them the patient can't answer small talk and feels robotic.

## Facts
- 8-12 facts. Every fact: `id` (snake_case), `label` (chart wording), `tier`, `critical` (bool).
- Tiers:
  - `volunteered` (1-2): he says it on his own at the start.
  - `asked`: only when the student asks about that topic.
  - `reluctant` (1-2): an embarrassing habit he hides until asked kindly or asked twice.
  - `chart`: vitals, labs, imaging. No `says`.
- Non-chart facts need `says` (his own voice), `unlock_hint`, and 4-8 lowercase `keywords`.
- 3-5 critical facts: the ones that change the discharge plan. At least one critical fact is reluctant.

## Plan
- `plan.base`: 4-6 standard points everyone gets.
- `plan.conditional`: one point per critical fact, with `needs` = that fact's id.
- `plan.missed_consequence`: one plain sentence per critical fact: what goes wrong at home if it's lost.

## Specialist and pharmacist
- The `cardiologist` key holds whichever specialist fits the case. Keep the key name.
- `pharmacist.rule` = {watch: chart fact id, catches: hidden fact id, adds: plan point id, why, note_if_caught}.
  Only include it if a medication safety catch is clinically real (a lab that reveals a drug problem).

## Caregiver
- `caregiver`: name, relation, age, persona, `language_code` (ISO 639-1), `language_name`, `native_name`.
