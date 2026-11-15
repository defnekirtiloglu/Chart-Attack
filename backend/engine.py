"""OSCEnario Rounds engine: one patient, three encounters, one shared record.

Design rule: the model answers, code decides.
  - Gemini maps the student's words to fact ids and writes the in-character replies.
  - Plain code decides what Walter may reveal, what the cardiologist knows,
    what goes in the plan, and what Ayşe understood.
"""
import json
import re
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from . import llm

CASES = Path(__file__).resolve().parents[1] / "cases"
STAGES = ["ed", "handoff", "discharge", "done"]

# Keyword fallback for when Gemini is down. Lower-case substrings.
KEYWORDS = {
    "breathless": ["breath", "breathing", "brought you", "what's going on", "what brings", "how are you", "sleep"],
    "swelling": ["swell", "ankle", "leg", "feet", "foot", "shoes"],
    "cough": ["cough", "fever", "phlegm", "sputum", "chills", "sick"],
    "skips_diuretic": ["water pill", "furosemide", "lasix", "diuretic", "miss", "skip", "every day", "take your med", "medications", "pills"],
    "ibuprofen": ["pain", "ibuprofen", "advil", "motrin", "knee", "over the counter", "other med", "anything else you take"],
    "salt": ["eat", "diet", "salt", "food", "meal", "soup", "lunch"],
    "wife_pills": ["who helps", "who manages", "pill box", "pillbox", "home", "live with", "wife", "family", "who gives"],
    "no_scale": ["weigh", "weight", "scale"],
}
EMPATHY = ["no judgment", "not judging", "a lot of people", "lots of people", "makes sense", "understand",
           "that sounds", "must be", "it's okay", "it's ok", "honest", "between us", "hard to"]


def load_case(case_id: str) -> dict:
    path = CASES / f"{case_id}.json"
    if not path.exists():
        raise KeyError(case_id)
    return json.loads(path.read_text(encoding="utf-8"))


class Session:
    def __init__(self, case: dict):
        self.id = uuid.uuid4().hex[:10]
        self.case = case
        self.stage = "ed"
        self.turn = 0
        self.transcript: List[dict] = []
        self.discovered: Dict[str, dict] = {}   # fact id -> {encounter, turn, how}
        self.asks: Dict[str, int] = {}          # fact id -> times the student went after it
        self.handoff: Optional[dict] = None
        self.plan: List[dict] = []
        self.points: Dict[str, dict] = {}       # plan point id -> {covered, clear, jargon}
        self.teachback: Optional[dict] = None
        for f in case["facts"]:                 # vitals, labs and imaging are on the chart from the start
            if f["tier"] == "chart":
                self.discovered[f["id"]] = {"encounter": "ed", "turn": 0, "how": "chart"}

    # ---------- helpers ----------
    def fact(self, fid: str) -> dict:
        return next(f for f in self.case["facts"] if f["id"] == fid)

    def log(self, speaker: str, text: str, **extra):
        self.transcript.append({"encounter": self.stage, "turn": self.turn, "speaker": speaker, "text": text, **extra})

    def chart(self) -> List[dict]:
        out = []
        for f in self.case["facts"]:
            d = self.discovered.get(f["id"])
            if d:
                out.append({"id": f["id"], "label": f["label"], "critical": f["critical"], **d})
        return out

    def state(self) -> dict:
        enc = {e["id"]: e for e in self.case["encounters"]}
        return {
            "session_id": self.id,
            "case": {"id": self.case["id"], "title": self.case["title"], "disclaimer": self.case["disclaimer"],
                     "patient": {k: self.case["patient"][k] for k in ("name", "age", "sex", "language")},
                     "caregiver": {"name": self.case["caregiver"]["name"], "relation": self.case["caregiver"]["relation"],
                                   "language": "tr", "language_name": "Türkçe"}},
            "facts": [{"id": c["id"], "label": c["label"], "status": "found", "tone": "bad" if c["critical"] else "good"}
                      for c in self.chart()],
            "stage": self.stage,
            "encounter": enc.get(self.stage),
            "encounters": self.case["encounters"],
            "chart": self.chart(),
            "hidden_count": sum(1 for f in self.case["facts"] if f["id"] not in self.discovered),
            "handoff": self.handoff,
            "plan": self.plan,
            "transcript": self.transcript,
        }


SESSIONS: Dict[str, Session] = {}


def start(case_id: str = "walter") -> Session:
    s = Session(load_case(case_id))
    SESSIONS[s.id] = s
    return s


def get(sid: str) -> Session:
    if sid not in SESSIONS:
        raise KeyError(sid)
    return SESSIONS[sid]


# ======================= ED: interview Walter =======================
def route(s: Session, text: str) -> dict:
    """Which facts is the student going after, and were they non-judgmental?"""
    askable = [f for f in s.case["facts"] if f["tier"] != "chart"]
    menu = "\n".join(f'- {f["id"]}: {f["label"]}' for f in askable)
    out = llm.ask_json(
        "A medical student is interviewing a patient. Decide which topics the student's message is asking about.\n"
        f"Topics:\n{menu}\n\nStudent said: \"{text}\"\n\n"
        "Return JSON {\"fact_ids\": [ids the message directly asks about], "
        "\"empathetic\": true if the tone is warm and non-judgmental, e.g. normalizing or reassuring}. "
        "A broad opener like 'what brings you in' only matches breathless. Use only ids from the list.")
    ids = set()
    empathetic = False
    if isinstance(out, dict):
        ids = {i for i in out.get("fact_ids", []) if i in {f["id"] for f in askable}}
        empathetic = bool(out.get("empathetic"))
    low = text.lower()
    if out is None:
        ids = {fid for fid, kws in KEYWORDS.items() if any(k in low for k in kws)}
    empathetic = empathetic or any(k in low for k in EMPATHY)
    return {"fact_ids": sorted(ids), "empathetic": empathetic}


def disclose(s: Session, routed: dict) -> dict:
    """Plain code decides what Walter will say this turn."""
    new, deflected = [], []
    for f in s.case["facts"]:
        fid = f["id"]
        if fid in s.discovered or f["tier"] == "chart":
            continue
        targeted = fid in routed["fact_ids"]
        if targeted:
            s.asks[fid] = s.asks.get(fid, 0) + 1
        if f["tier"] == "volunteered" and (targeted or s.turn == 1):
            new.append(fid)
        elif f["tier"] == "asked" and targeted:
            new.append(fid)
        elif f["tier"] == "reluctant" and targeted:
            if routed["empathetic"] or s.asks[fid] >= 2:
                new.append(fid)
            else:
                deflected.append(fid)
    for fid in new:
        s.discovered[fid] = {"encounter": "ed", "turn": s.turn, "how": "interview"}
    return {"new": new, "deflected": deflected}


def walter_reply(s: Session, text: str, d: dict) -> str:
    p = s.case["patient"]
    share = [s.fact(fid)["says"] for fid in d["new"]]
    dodge = [s.fact(fid)["label"] for fid in d["deflected"]]
    history = "\n".join(f'{t["speaker"]}: {t["text"]}' for t in s.transcript[-8:] if t["encounter"] == "ed")
    prompt = (
        f"You are {p['name']}, {p['age']}, a patient in the emergency department. {p['persona']}\n\n"
        f"Recent conversation:\n{history}\n\nThe student just said: \"{text}\"\n\n"
        "Reply in character in 1-3 short spoken sentences.\n"
        + (f"Work in these facts, in your own voice (you may paraphrase): {share}\n" if share else "")
        + (f"The student is poking at something you're embarrassed about ({dodge}). Dodge it with a joke and don't admit it.\n" if dodge else "")
        + "Do NOT reveal any other medical history, symptoms or habits beyond what is listed. "
          "If asked something not covered, say you don't know or change the subject with attitude. "
          "Never use medical terms or name a diagnosis.")
    out = llm.ask_text(prompt)
    if out:
        return out
    if share:
        return " ".join(share)
    if dodge:
        return "Ha. Next question, kid."
    return "I dunno, kid. Ask me something useful."


def ed_chat(s: Session, text: str) -> dict:
    if s.stage != "ed":
        raise ValueError("The ED interview is over.")
    s.turn += 1
    s.log("student", text)
    routed = route(s, text)
    d = disclose(s, routed)
    reply = walter_reply(s, text, d)
    s.log("patient", reply)
    return {"speaker": s.case["patient"]["name"], "reply": reply,
            "new_facts": [{"id": f, "label": s.fact(f)["label"], "critical": s.fact(f)["critical"]} for f in d["new"]],
            "dodged": len(d["deflected"]) > 0, "hidden_count": s.state()["hidden_count"]}


# ======================= Handoff to cardiology =======================
def _keyword_hits(text: str, candidates: List[dict]) -> List[str]:
    low = text.lower()
    hits = []
    for f in candidates:
        kws = KEYWORDS.get(f["id"], []) + [w for w in re.findall(r"[a-z]{5,}", f["label"].lower())][:4]
        if any(k in low for k in kws):
            hits.append(f["id"])
    return hits


def facts_in_text(s: Session, text: str, candidates: List[dict]) -> dict:
    """Gemini and Claude judge the handoff independently. Code decides:
    both say yes -> handed off; they disagree -> unclear (not counted)."""
    menu = "\n".join(f'- {f["id"]}: {f["label"]}' for f in candidates)
    prompt = ("Here is a clinical handoff note and a list of facts. Which facts does the note clearly convey? "
              "Paraphrase counts; vague hints do not.\n"
              f"Facts:\n{menu}\n\nNote:\n\"\"\"{text}\"\"\"\n\nReturn JSON {{\"fact_ids\": [...]}}.")
    ok = {f["id"] for f in candidates}
    votes = {}
    for name, out in (("gemini", llm.ask_json(prompt)), ("claude", llm.claude_json(prompt))):
        if isinstance(out, dict):
            votes[name] = {i for i in out.get("fact_ids", []) if i in ok}
    if not votes:
        votes["keywords"] = set(_keyword_hits(text, candidates))
    sets = list(votes.values())
    agreed = set.intersection(*sets)
    unclear = set.union(*sets) - agreed
    return {"handed": sorted(agreed), "unclear": sorted(unclear), "judges": sorted(votes)}


def build_plan(case: dict, handed: List[str]) -> List[dict]:
    plan = [dict(p, source="standard") for p in case["plan"]["base"]]
    for p in case["plan"]["conditional"]:
        if p["needs"] in handed:
            plan.append(dict(p, source=p["needs"]))
    return plan


def do_handoff(s: Session, text: str) -> dict:
    if s.stage == "ed":
        s.stage = "handoff"
    if s.stage != "handoff":
        raise ValueError("Handoff already done.")
    s.turn += 1
    s.log("student", text)
    known = [s.fact(fid) for fid in s.discovered]
    vote = facts_in_text(s, text, known)
    handed = vote["handed"]
    critical = [f for f in s.case["facts"] if f["critical"]]
    missing = []
    for f in critical:
        if f["id"] in handed:
            continue
        if f["id"] in vote["unclear"]:
            missing.append({"id": f["id"], "label": f["label"], "why": "unclear: the two AI checkers disagreed on whether you said it",
                            "consequence": s.case["plan"]["missed_consequence"].get(f["id"])})
            continue
        found = f["id"] in s.discovered
        missing.append({"id": f["id"], "label": f["label"],
                        "why": "found in the ED but left out of the handoff" if found else "never found in the ED",
                        "consequence": s.case["plan"]["missed_consequence"].get(f["id"])})
    s.plan = build_plan(s.case, handed)
    pharm = pharmacist_review(s, handed)
    c = s.case["cardiologist"]
    reply = llm.ask_text(
        f"You are {c['name']}. {c['persona']}\n"
        f"You just got this SBAR handoff from an ED resident:\n\"\"\"{text}\"\"\"\n\n"
        f"Your plan (say it briefly, in this order, as a cardiologist talking to a resident): {[p['point'] for p in s.plan]}\n"
        "Then ask ONE sharp teaching question about something the handoff left vague. "
        "You know ONLY what is in the handoff. Do not mention any patient detail that is not in it. 4-6 sentences.", 0.5)
    if not reply:
        reply = ("Thanks. Here's my plan: " + " ".join(p["point"] for p in s.plan)
                 + " One question: why do you think he keeps coming back?")
    s.handoff = {"text": text, "handed_off": handed, "unclear": vote["unclear"], "judges": vote["judges"],
                 "missing_critical": missing, "reply": reply, "pharmacist": pharm}
    s.log("cardiologist", reply)
    if pharm:
        s.log("pharmacist", pharm["note"])
    s.stage = "discharge"
    return {"speaker": c["name"], "reply": reply, "handed_off": handed, "unclear": vote["unclear"],
            "judges": vote["judges"], "missing_critical": missing, "plan": s.plan, "pharmacist": pharm}


# ======================= Pharmacist: independent safety net =======================
def pharmacist_review(s: Session, handed: List[str]) -> Optional[dict]:
    """The pharmacist never sees the handoff. She reads the chart and does her own med check.
    Code decides what she catches: rising creatinine + no NSAID plan -> she asks Walter about pain pills."""
    p = s.case.get("pharmacist", {"name": "Pharmacist"})
    caught = []
    labs_flag = "creatinine" in s.discovered
    if labs_flag and "ibuprofen" not in handed:
        caught.append("ibuprofen")
        if "ibuprofen" not in s.discovered:
            s.discovered["ibuprofen"] = {"encounter": "handoff", "turn": s.turn, "how": "pharmacist"}
        if not any(x["id"] == "stop_ibuprofen" for x in s.plan):
            pt = next(x for x in s.case["plan"]["conditional"] if x["id"] == "stop_ibuprofen")
            s.plan.append(dict(pt, source="pharmacist"))
    walter_said = s.fact("ibuprofen")["says"] if caught else None
    note = llm.claude_text(
        f"You are {p['name']}, the inpatient pharmacist. {p.get('persona', '')}\n"
        f"Chart: {[s.fact(i)['label'] for i in s.discovered if s.fact(i)['tier'] == 'chart']}\n"
        f"Current discharge plan: {[x['point'] for x in s.plan]}\n"
        + (f"You noticed the creatinine rise, asked Walter about over-the-counter pain pills, and he said: \"{walter_said}\". "
           "You added: stop ibuprofen, use acetaminophen.\n" if caught else
           "Your med review found nothing new to add.\n")
        + "Write a 2-3 sentence note to the team, the way a pharmacist pages a resident. Plain and specific.",
        temperature=0.5)
    if not note:
        note = ("Creatinine went from 1.2 to 1.6, so I checked his OTC meds. He takes ibuprofen most days for his knee. "
                "Added to the plan: stop ibuprofen, acetaminophen instead.") if caught else "Med review done. Nothing to add."
    return {"name": p["name"], "note": note, "caught": caught}


# ======================= Discharge teaching with Ayşe =======================
def judge_points(s: Session, text: str) -> Dict[str, dict]:
    menu = "\n".join(f'- {p["id"]}: {p["point"]}' for p in s.plan)
    out = llm.ask_json(
        "A doctor is explaining discharge instructions to a caregiver with low health literacy (through an interpreter).\n"
        f"Plan points:\n{menu}\n\nDoctor said: \"{text}\"\n\n"
        "For each point the doctor's message actually covers, return whether it was specific enough to act on, "
        "and list any unexplained medical jargon (e.g. 'diuretic', 'sodium', 'edema', 'NSAID', 'HFrEF'). "
        "Return JSON {\"points\": [{\"id\": str, \"specific\": bool, \"jargon\": [str]}]}. Omit points not covered.")
    res = {}
    if isinstance(out, dict):
        for p in out.get("points", []):
            if p.get("id") in {x["id"] for x in s.plan}:
                res[p["id"]] = {"specific": bool(p.get("specific")), "jargon": [str(j) for j in p.get("jargon", [])]}
        return res
    low = text.lower()   # fallback: word overlap
    for p in s.plan:
        words = [w for w in re.findall(r"[a-z]{4,}", p["point"].lower())]
        if sum(w in low for w in set(words)) >= 3:
            jar = [j for j in ["diuretic", "sodium", "edema", "nsaid", "hfref", "ejection", "furosemide"] if j in low]
            res[p["id"]] = {"specific": True, "jargon": jar}
    return res


def interpret(text: str) -> str:
    out = llm.ask_text("Translate into natural, everyday Turkish as a medical interpreter would say it aloud. "
                       "Keep any medical jargon as-is (do not explain it). Keep every number and unit exactly as said. "
                       "Output only the Turkish.\n\n" + text, 0.2)
    return out or f"[Türkçe çeviri] {text}"


NUM_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
             "eight": "8", "nine": "9", "ten": "10", "twice": "2", "once": "1"}


def numbers(text: str) -> set:
    low = text.lower()
    found = set(re.findall(r"\d+(?:[.,]\d+)?", low))
    found |= {v for k, v in NUM_WORDS.items() if re.search(rf"\b{k}\b", low)}
    return {n.replace(",", ".") for n in found}


def check_translation(original: str, turkish: str) -> dict:
    """Claude translates the Turkish back to English WITHOUT seeing the original.
    Then two checks: code compares the numbers, Claude compares the meaning. Code decides."""
    back = llm.claude_text("Translate this Turkish into plain English, literally. Output only the translation.\n\n" + turkish,
                           system="You are a professional Turkish-English medical translator.")
    if not back:
        return {"status": "unchecked", "back_translation": None, "issues": [],
                "why": "The back-translation checker is offline, so this translation was not verified."}
    issues = []
    lost_nums = numbers(original) - numbers(back)
    added_nums = numbers(back) - numbers(original)
    if lost_nums or added_nums:
        issues.append(f"Numbers changed: said {sorted(numbers(original))}, Ayşe heard {sorted(numbers(back))}")
    verdict = llm.claude_json(
        "A doctor said A. An interpreter translated it to Turkish; B is that Turkish translated back to English.\n"
        f"A: {original}\nB: {back}\n"
        "Did any instruction change, drop out, or get added (doses, timing, units, what to stop or start)? "
        "Ignore wording differences. Return {\"same_meaning\": bool, \"changes\": [short strings]}.")
    if isinstance(verdict, dict) and not verdict.get("same_meaning", True):
        issues += [str(c) for c in verdict.get("changes", [])][:3]
    return {"status": "flagged" if issues else "ok", "back_translation": back, "issues": issues,
            "why": "Two checks: numbers match in code, meaning compared by a second model."}


MAX_WORDS_DIRECT = 15   # Ayşe follows short English sentences only


def avg_sentence_words(text: str) -> float:
    parts = [x for x in re.split(r"[.!?]+", text) if x.strip()]
    return sum(len(x.split()) for x in parts) / max(len(parts), 1)


AYSE_STYLE = ("You speak broken English: short phrases, simple words, Turkish word order, dropped articles, "
              "and you slip into Turkish words or whole sentences when you are confused, worried or emotional "
              "(e.g. 'Tamam, every morning weigh... ama neden?'). ")


def discharge_chat(s: Session, text: str, interpreter: bool = True) -> dict:
    """Two ways to teach Ayşe:
    interpreter=True  -> Gemini interprets to Turkish, Claude back-translates to check it.
    interpreter=False -> you talk to her directly in English. She follows only short, plain sentences."""
    if s.stage != "discharge":
        raise ValueError("Finish the handoff first.")
    s.turn += 1
    judged = judge_points(s, text)
    tr, check, too_long = None, None, False
    if interpreter:
        tr = interpret(text)
        check = check_translation(text, tr)
        if check["status"] == "flagged":   # Ayşe heard something different, so nothing from this message counts as clear
            judged = {pid: dict(j, specific=False) for pid, j in judged.items()}
    else:
        too_long = avg_sentence_words(text) > MAX_WORDS_DIRECT
        if too_long:                        # code decides: long English sentences lose her
            judged = {pid: dict(j, specific=False) for pid, j in judged.items()}
    s.log("student", text, text_tr=tr, translation_check=check, interpreter=interpreter)
    for pid, j in judged.items():
        prev = s.points.get(pid, {"covered": False, "clear": False, "jargon": []})
        clear = j["specific"] and not j["jargon"]
        s.points[pid] = {"covered": True, "clear": prev["clear"] or clear,
                         "jargon": [] if (prev["clear"] or clear) else sorted(set(prev["jargon"] + j["jargon"]))}
    confusing = sorted({w for pid in judged for w in s.points[pid]["jargon"]})
    cg = s.case["caregiver"]
    heard = (f"The interpreter just told you in Turkish: \"{tr}\"\nReply mostly in Turkish, with a few English words.\n"
             if interpreter else
             f"The doctor spoke to you directly in English: \"{text}\"\n" + AYSE_STYLE
             + ("The sentences were too long and fast for you; you lost track. Say so, half in Turkish.\n" if too_long else ""))
    out = llm.ask_json(
        f"You are {cg['name']}, {cg['age']}, Walter's wife. {cg['persona']}\n" + heard
        + (f"These words were not explained and you don't know them: {confusing}. Politely ask what they mean.\n" if confusing else "")
        + "1-2 short sentences, as you would actually speak. "
          "Return JSON {\"said\": exactly what you say (mixed Turkish/broken English), \"en\": its meaning in plain English}.", 0.7)
    if not isinstance(out, dict):
        if too_long:
            out = {"said": "Çok hızlı... too fast, sorry. Again, slow?", "en": "Too fast, sorry. Again, slowly?"}
        elif confusing:
            out = {"said": f"Affedersiniz... '{confusing[0]}' — what is this?", "en": f"Excuse me, what does '{confusing[0]}' mean?"}
        else:
            out = {"said": "Tamam, tamam. I understand, teşekkürler.", "en": "Okay, okay. I understand, thank you."}
    said = out.get("said") or out.get("tr", "")
    s.log("caregiver", said, text_en=out.get("en", ""))
    return {"speaker": cg["name"], "interpreter": interpreter, "interpreted_tr": tr, "translation_check": check,
            "too_long": too_long, "reply_tr": said, "reply_en": out.get("en", ""), "points": progress(s)}


def progress(s: Session) -> List[dict]:
    return [{"id": p["id"], "point": p["point"], **s.points.get(p["id"], {"covered": False, "clear": False, "jargon": []})}
            for p in s.plan]


def do_teachback(s: Session) -> dict:
    if s.stage != "discharge":
        raise ValueError("Teach-back happens at discharge.")
    got = [p for p in s.plan if s.points.get(p["id"], {}).get("clear")]
    lost = [p for p in s.plan if p not in got]
    cg = s.case["caregiver"]
    out = llm.ask_json(
        f"You are {cg['name']}. {cg['persona']}\nThe doctor asked you to repeat the plan back in your own words.\n"
        f"You understood ONLY these points: {[p['point'] for p in got]}\n"
        f"You did NOT understand or were never told: {[p['point'] for p in lost]}\n"
        "Repeat back what you understood in simple words, then honestly say what you are unsure about, "
        "without naming the details you never heard. " + AYSE_STYLE + "3-5 short sentences. "
        "Return JSON {\"tr\": exactly what you say (mixed Turkish/broken English), \"en\": its meaning in plain English}.", 0.6)
    if not isinstance(out, dict):
        said = " ".join(p["point"] for p in got)
        out = {"tr": ("Tamam, I understand: " + said + " Ama... the rest, bilmiyorum.") if got else "Açıkçası... I don't understand much. Sorry.",
               "en": ("Okay, I understood: " + said + " But the rest, I don't know.") if got else "Honestly, I didn't understand much. Sorry."}
    s.teachback = {"understood": [p["id"] for p in got], "missed": [p["id"] for p in lost], **out}
    s.log("caregiver", out.get("tr", ""), text_en=out.get("en", ""), teachback=True)
    s.stage = "done"
    return {"speaker": cg["name"], **s.teachback}


# ======================= Feedback timeline =======================
def feedback(s: Session) -> dict:
    handed = set((s.handoff or {}).get("handed_off", []))
    understood = set((s.teachback or {}).get("understood", []))
    cond = {p["needs"]: p for p in s.case["plan"]["conditional"]}
    threads = []
    for f in s.case["facts"]:
        if not f["critical"]:
            continue
        found = s.discovered.get(f["id"])
        plan_pt = cond.get(f["id"])
        rescued = f["id"] in (((s.handoff or {}).get("pharmacist") or {}).get("caught") or [])
        in_ed = bool(found) and found["how"] != "pharmacist"
        steps = [
            {"stage": "ed", "ok": in_ed, "note": f"Found on turn {found['turn']}" if in_ed else "Never asked about"},
            {"stage": "handoff", "ok": f["id"] in handed or rescued,
             "note": "Pharmacist caught it from the kidney labs" if rescued else ("In the SBAR" if f["id"] in handed else "Not handed off")},
            {"stage": "discharge", "ok": bool(plan_pt and plan_pt["id"] in understood),
             "note": "Ayşe understood it" if plan_pt and plan_pt["id"] in understood else "Did not make it home"},
        ]
        check = steps[1:] if rescued else steps
        lost_at = next((st["stage"] for st in check if not st["ok"]), None)
        threads.append({"id": f["id"], "label": f["label"], "short": f.get("short", f["label"]), "steps": steps, "lost_at": lost_at,
                        "rescued_by": "pharmacist" if rescued else None,
                        "consequence": None if lost_at is None else s.case["plan"]["missed_consequence"].get(f["id"])})
    base_ids = [p["id"] for p in s.case["plan"]["base"]]
    return {
        "threads": threads,
        "carried_home": sum(t["lost_at"] is None for t in threads),
        "total_threads": len(threads),
        "facts_found": sum(1 for f in s.case["facts"] if f["tier"] != "chart" and f["id"] in s.discovered),
        "facts_total": sum(1 for f in s.case["facts"] if f["tier"] != "chart"),
        "standard_points_understood": sum(1 for i in base_ids if i in understood),
        "standard_points_total": len(base_ids),
        "jargon_used": sorted({w for p in s.points.values() for w in p.get("jargon", [])}),
        "translation_flags": [{"said": t["text"], **t["translation_check"]} for t in s.transcript
                              if (t.get("translation_check") or {}).get("status") == "flagged"],
        "teachback": s.teachback,
    }
