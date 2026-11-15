"""Professor side: create, check, and explain cases.

  generate(notes)  Gemini drafts a full case from a few lines -> code validates it ->
                   one repair pass if broken -> Claude reviews it clinically -> saved as a draft.
  validate(case)   plain-code checks that the engine can actually run the case.
  answer_key(case) the "doctor script": every hidden fact, how to unlock it, the ideal SBAR,
                   what the pharmacist watches for, and what Ayşe must understand.
"""
import json
import re
from typing import List, Optional

from . import engine, llm

TIERS = {
    "volunteered": "Shares it on his own at the start.",
    "asked": "Shares it only if you ask about that topic.",
    "reluctant": "Embarrassed. Dodges with a joke unless you ask kindly and without judgment, or ask a second time.",
    "chart": "Already on the chart (vitals, labs, imaging). He never says it.",
}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "case"


def save(case: dict) -> dict:
    (engine.CASES / f"{case['id']}.json").write_text(json.dumps(case, indent=2, ensure_ascii=False), encoding="utf-8")
    return case


def unique_id(base: str) -> str:
    cid, n = slug(base), 2
    while (engine.CASES / f"{cid}.json").exists():
        cid, n = f"{slug(base)}-{n}", n + 1
    return cid


# ---------------- validation (code decides) ----------------
def validate(case: dict) -> dict:
    errors: List[str] = []
    warnings: List[str] = []
    for key in ("id", "title", "patient", "facts", "encounters", "cardiologist", "plan"):
        if key not in case:
            errors.append(f"Missing '{key}'.")
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}
    p = case["patient"]
    for k in ("name", "age", "sex", "persona"):
        if not p.get(k):
            errors.append(f"Patient is missing '{k}'.")
    ids = [f.get("id") for f in case["facts"]]
    if len(ids) != len(set(ids)):
        errors.append("Two facts share the same id.")
    for f in case["facts"]:
        if f.get("tier") not in TIERS:
            errors.append(f"Fact '{f.get('id')}' has an unknown tier '{f.get('tier')}'.")
        if f.get("tier") != "chart" and not f.get("says"):
            errors.append(f"Fact '{f.get('id')}' needs a 'says' line in the patient's voice.")
        if not f.get("label"):
            errors.append(f"Fact '{f.get('id')}' needs a label.")
    crit = [f for f in case["facts"] if f.get("critical")]
    if not crit:
        errors.append("Mark at least one fact as critical; those are the threads on the subway map.")
    if not any(f.get("tier") == "volunteered" for f in case["facts"]):
        warnings.append("No volunteered fact: the patient won't say anything on his own at the start.")
    if not any(f.get("tier") == "reluctant" and f.get("critical") for f in case["facts"]):
        warnings.append("No reluctant critical fact: there's no hidden secret for students to earn.")
    plan = case["plan"]
    if not plan.get("base"):
        errors.append("The discharge plan needs at least one standard point.")
    fact_ids = set(ids)
    plan_ids = {x["id"] for x in plan.get("base", []) + plan.get("conditional", [])}
    for x in plan.get("conditional", []):
        if x.get("needs") not in fact_ids:
            errors.append(f"Plan point '{x.get('id')}' depends on fact '{x.get('needs')}', which doesn't exist.")
    for f in crit:
        if f["id"] not in {x.get("needs") for x in plan.get("conditional", [])}:
            warnings.append(f"Critical fact '{f['id']}' has no plan point, so it can never 'make it home'.")
        if f["id"] not in plan.get("missed_consequence", {}):
            warnings.append(f"Critical fact '{f['id']}' has no consequence text for when it's missed.")
    rule = (case.get("pharmacist") or {}).get("rule")
    if rule:
        if rule.get("watch") not in fact_ids:
            errors.append(f"Pharmacist watches '{rule.get('watch')}', which isn't a fact.")
        if rule.get("catches") not in fact_ids:
            errors.append(f"Pharmacist catches '{rule.get('catches')}', which isn't a fact.")
        if rule.get("adds") and rule.get("adds") not in plan_ids:
            errors.append(f"Pharmacist adds plan point '{rule.get('adds')}', which isn't in the plan.")
    cg = case.get("caregiver")
    if not cg:
        errors.append("Discharge needs a caregiver (or the patient) to teach.")
    elif not cg.get("language_code"):
        errors.append("Caregiver needs a language_code, like 'tr', 'es' or 'en'.")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


# ---------------- generation ----------------
def _example() -> str:
    ex = engine.load_case("walter")
    for k in ("status", "review", "validation", "source_notes", "created"):
        ex.pop(k, None)
    return json.dumps(ex, ensure_ascii=False)


def generate(notes: str, title: Optional[str] = None) -> dict:
    prompt = (
        "You write teaching cases for a medical-education simulator. One patient goes through three encounters: "
        "ED interview -> SBAR handoff to a specialist -> discharge teaching to a caregiver who may need an interpreter.\n\n"
        "Here is a complete example case (follow this exact JSON structure, keys and value types):\n"
        f"{_example()}\n\n"
        "Now write a NEW case from the faculty notes below. Rules:\n"
        "- 8-12 facts. Tiers: volunteered (1-2), asked, reluctant (1-2, embarrassing habits), chart (vitals/labs/imaging).\n"
        "- 3-5 facts are critical: the ones that change the discharge plan. At least one critical fact is reluctant.\n"
        "- 'says' is how the patient would say it, in his or her own voice and personality. Chart facts have no 'says'.\n"
        "- patient.persona: a vivid personality with a clear attitude toward students. patient.backstory: 8-10 everyday life "
        "details (name, hometown, job, family, home, hobbies, habits, who brought them in). patient.free_medical: 4-5 harmless "
        "history items (allergies, past surgeries, smoking, known conditions in lay words) that are NOT facts in the list.\n"
        "- Every critical fact has a conditional plan point (needs = that fact id) and a missed_consequence.\n"
        "- Give every non-chart fact an 'unlock_hint' (what question unlocks it) and 4-8 lowercase 'keywords'.\n"
        "- The 'cardiologist' key is the specialist for this case (any specialty); keep the key name.\n"
        "- pharmacist.rule: a chart fact she 'watch'es, the hidden fact she 'catches', the plan point she 'adds', "
        "'why' in plain words, and 'note_if_caught'. Only if a med safety catch makes clinical sense; otherwise omit 'rule'.\n"
        "- caregiver: give language_code (ISO 639-1), language_name (English name), native_name.\n"
        "- Write plan points in plain language a caregiver could follow. Keep it clinically realistic.\n"
        "- It is fictional. Set disclaimer to 'Fictional teaching case. Not medical advice.'\n\n"
        f"Faculty notes:\n\"\"\"{notes}\"\"\"\n" + (f"Title: {title}\n" if title else "") + "\nReturn only the JSON object.")
    case = llm.ask_json(prompt, 0.7)
    if not isinstance(case, dict):
        raise RuntimeError("Gemini didn't return a case. Check GEMINI_API_KEY, or upload a case file instead.")
    case = _finish(case, notes, title)
    v = validate(case)
    if not v["ok"]:   # one repair pass
        fixed = llm.ask_json(
            "This case JSON has problems. Fix ONLY these problems and return the full corrected JSON.\n"
            f"Problems: {v['errors']}\n\nJSON:\n{json.dumps(case, ensure_ascii=False)}", 0.2)
        if isinstance(fixed, dict):
            case = _finish(fixed, notes, title, keep_id=case["id"])
    case["validation"] = validate(case)
    case["review"] = review(case)
    return save(case)


def _finish(case: dict, notes: str, title: Optional[str], keep_id: Optional[str] = None) -> dict:
    case["id"] = keep_id or unique_id(title or case.get("title") or case.get("patient", {}).get("name", "case"))
    if title:
        case["title"] = title
    case.setdefault("disclaimer", "Fictional teaching case. Not medical advice.")
    case.setdefault("encounters", engine.load_case("walter")["encounters"])
    case["status"] = "draft"
    case["source_notes"] = notes
    case["created"] = __import__("time").strftime("%Y-%m-%dT%H:%M:%S")
    return case


def review(case: dict) -> dict:
    """Claude reads the draft independently and flags clinical or teaching problems for the professor."""
    out = llm.claude_json(
        "You are a clinician-educator reviewing an AI-written simulation case before a professor publishes it.\n"
        f"{json.dumps({k: case.get(k) for k in ('patient', 'caregiver', 'facts', 'plan', 'pharmacist')}, ensure_ascii=False)}\n\n"
        "Flag anything clinically wrong or unsafe, any plan point that contradicts the facts, and anything that would "
        "make the case unfair to students. Return {\"verdict\": \"looks good\" | \"needs edits\", "
        "\"issues\": [{\"severity\": \"high\"|\"medium\"|\"low\", \"where\": str, \"issue\": str, \"suggestion\": str}]}.",
        system="You are a careful clinician-educator.")
    if not isinstance(out, dict):
        return {"verdict": "not reviewed", "issues": [], "note": "The Claude reviewer is offline, so check the clinical content yourself."}
    return out


def upload(case: dict) -> dict:
    case = dict(case)
    case["id"] = unique_id(case.get("id") or case.get("title") or "case")
    case["status"] = "draft"
    case["validation"] = validate(case)
    return save(case)


# ---------------- answer key ----------------
def answer_key(case: dict) -> dict:
    p, cg = case["patient"], case.get("caregiver", {})
    plan = case["plan"]
    cond = {x["needs"]: x for x in plan.get("conditional", [])}
    rule = (case.get("pharmacist") or {}).get("rule")
    facts = []
    for f in case["facts"]:
        facts.append({
            "id": f["id"], "label": f["label"], "tier": f["tier"], "tier_means": TIERS.get(f["tier"], ""),
            "critical": bool(f.get("critical")), "unlock_hint": f.get("unlock_hint", ""),
            "patient_says": f.get("says", ""),
            "changes_plan": cond[f["id"]]["point"] if f["id"] in cond else None,
            "if_missed": plan.get("missed_consequence", {}).get(f["id"]),
            "pharmacist_backup": bool(rule and rule.get("catches") == f["id"]),
        })
    crit = [f for f in case["facts"] if f.get("critical")]
    chart = [f for f in case["facts"] if f["tier"] == "chart"]
    sbar = {
        "S": f"{p['age']}-year-old {p['sex']} with {facts[0]['label'].lower() if facts else 'the presenting problem'}.",
        "B": "; ".join(f["label"] for f in crit),
        "A": "; ".join(f["label"] for f in chart[:3]),
        "R": "Please see and plan for discharge: " + "; ".join(cond[f["id"]]["point"].split(".")[0] for f in crit if f["id"] in cond) + ".",
    }
    return {
        "case": {"id": case["id"], "title": case["title"], "status": case.get("status", "draft")},
        "patient": {"name": p["name"], "age": p["age"], "sex": p["sex"], "persona": p["persona"]},
        "caregiver": {"name": cg.get("name"), "relation": cg.get("relation"), "language": cg.get("language_name"),
                      "persona": cg.get("persona")},
        "how_it_works": [
            "The AI plays the patient, but plain code decides what he may reveal on each turn, using the tiers below.",
            f"The handoff is judged by two models (Gemini and Claude). A fact only counts if both agree it was in the SBAR. "
            f"{case['cardiologist']['name']} knows only what was handed off.",
            "The discharge plan is built from the handoff: each critical fact handed off adds its plan point.",
            "At discharge the student teaches the caregiver. A point counts only if it was specific, used no unexplained jargon, "
            "and (with the interpreter) survived the back-translation check.",
            "The feedback map follows each critical fact from ED to handoff to home and shows where it was lost.",
        ],
        "facts": facts,
        "ideal_sbar": sbar,
        "pharmacist": {
            "name": (case.get("pharmacist") or {}).get("name"),
            "watches": next((f["label"] for f in case["facts"] if rule and f["id"] == rule.get("watch")), None),
            "catches": next((f["label"] for f in case["facts"] if rule and f["id"] == rule.get("catches")), None),
            "adds": next((x["point"] for x in plan.get("conditional", []) if rule and x["id"] == rule.get("adds")), None),
            "why": rule.get("why") if rule else None,
            "explained": ("She never reads the handoff. She reads the chart on her own. If she sees "
                          f"'{next((f['label'] for f in case['facts'] if f['id'] == rule.get('watch')), '')}' and the handoff "
                          "didn't cover the related medicine, she asks the patient herself and adds the fix to the plan. "
                          "It's an independent safety net: the system catches what the student missed, and the feedback "
                          "shows that it was the pharmacist, not the student.") if rule else "No pharmacist rule in this case.",
        },
        "discharge": {
            "standard_points": [x["point"] for x in plan.get("base", [])],
            "conditional_points": [{"point": x["point"], "only_if_handed_off": next((f["label"] for f in case["facts"] if f["id"] == x["needs"]), x["needs"])}
                                   for x in plan.get("conditional", [])],
            "teaching_tips": [
                "Short sentences (15 words or fewer) when speaking directly; long ones lose the caregiver.",
                "No unexplained medical words: say 'water pill', not 'diuretic'; 'salt', not 'sodium'.",
                "Give exact numbers and times, then ask her to say it back.",
                f"With the interpreter, numbers and units are checked by back-translation into English.",
            ],
        },
        "review": case.get("review"),
        "validation": case.get("validation") or validate(case),
    }
