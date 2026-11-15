"""OSCEnario Rounds API. Run:  uvicorn backend.app:app --reload  (from the repo root)

Student flow:   POST /session -> /chat (ED) -> /handoff -> /chat (discharge) -> /teachback -> GET /feedback
Professor flow: POST /cases/generate or /cases/upload -> review /cases/{id}/key -> /publish
                -> POST /classes -> POST /assignments -> GET /assignments/{id}/results
"""
import json
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import case_agent, casegen, engine, llm, store

app = FastAPI(title="OSCEnario Rounds")
engine.EVENT_SINK = store.log_event


@app.on_event("startup")
def warm_up():
    """Run Gemini's one-time speed check now, so the first question isn't slow."""
    import threading
    threading.Thread(target=llm.thinking, daemon=True).start()   # every encounter moment goes to the event log
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ======================= models =======================
class StartIn(BaseModel):
    case_id: Optional[str] = "walter"
    assignment_id: Optional[str] = None
    student_id: Optional[str] = None


class TextIn(BaseModel):
    text: str
    interpreter: Optional[bool] = True   # discharge only: False = talk to the caregiver directly in English


class SpeakIn(BaseModel):
    text: str
    voice: str = "walter"      # walter (patient) | ayse (caregiver) | okafor (specialist)
    language: Optional[str] = None


class GenerateIn(BaseModel):
    notes: str
    title: Optional[str] = None


class CaseIn(BaseModel):
    case: Dict[str, Any]


class ClassIn(BaseModel):
    name: str
    section: Optional[str] = ""


class JoinIn(BaseModel):
    code: str
    student_id: str


class AssignIn(BaseModel):
    class_id: str
    case_id: str
    student_id: Optional[str] = None     # set = a personal assignment for one student
    title: Optional[str] = None
    due: Optional[str] = ""
    notes: Optional[str] = ""


# ======================= helpers =======================
def session(sid: str) -> engine.Session:
    try:
        return engine.get(sid)
    except KeyError:
        raise HTTPException(404, "No session with that id. Start a new one with POST /session.")


def guard(fn, *a):
    try:
        return fn(*a)
    except ValueError as e:
        raise HTTPException(409, str(e))


def case_or_404(cid: str) -> dict:
    try:
        return engine.load_case(cid)
    except KeyError:
        raise HTTPException(404, "No case with that id.")


def netid(x: str) -> str:
    x = (x or "").strip().lower()
    if not (2 <= len(x) <= 20) or not x.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(422, "Use 2 to 20 letters or numbers for your NetID.")
    return x


def summary_of(c: dict) -> dict:
    p, cg = c.get("patient") or {}, c.get("caregiver") or {}
    return {"id": c["id"], "title": c.get("title", c["id"]), "status": c.get("status", "published"),
            "disclaimer": c.get("disclaimer", ""),
            "patient": {"name": p.get("name"), "age": p.get("age"), "sex": p.get("sex")},
            "caregiver": {"name": cg.get("name"), "relation": cg.get("relation"), "language": cg.get("language_name")} if cg else None,
            "encounters": [{"id": e.get("id"), "title": e.get("title")} for e in c.get("encounters", [])],
            "review_verdict": (c.get("review") or {}).get("verdict"),
            # flat fields for the case-picker cards in index.html
            "name": p.get("name") or c.get("title", c["id"]),
            "age": p.get("age"), "sex": {"male": "M", "female": "F"}.get(str(p.get("sex", "")).lower(), p.get("sex") or ""),
            "initials": "".join(w[0] for w in str(p.get("name") or c.get("title", "?")).split()[:2]).upper(),
            "chief_complaint": c.get("chief_complaint") or next((f["label"] for f in c.get("facts", []) if f.get("tier") == "volunteered"), c.get("title", "")),
            "route": ["ED", c.get("specialty") or "Specialist", "Home"],
            "tags": c.get("tags", []),
            "caregiver_language": cg.get("language_code"),
            "minutes": c.get("minutes", 15),
            "follow_up_for": (c.get("follow_up_for") or {}).get("student_id"),
            "valid": (c.get("validation") or {"ok": True}).get("ok", True)}


# ======================= health =======================
FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
if FRONTEND.exists():   # one server for everything (Vultr): the app lives at /app/
    app.mount("/app", StaticFiles(directory=str(FRONTEND), html=True), name="app")


@app.get("/")
def root():
    if FRONTEND.exists():
        return RedirectResponse("/app/portal.html")
    return health()


@app.get("/health")
def health():
    return {"ok": True, "app": "OSCEnario Rounds", "docs": "/docs", "storage": store.BACKEND,
            "case_author": "deepagents" if case_agent.AVAILABLE else "pipeline"}


# ======================= student: one encounter =======================
@app.post("/session")
def start(body: StartIn):
    case_id = body.case_id or "walter"
    student = netid(body.student_id) if body.student_id else None
    if body.assignment_id:
        a = store.get_assignment(body.assignment_id)
        if not a:
            raise HTTPException(404, "That assignment no longer exists.")
        case_id = a["case_id"]
    try:
        return engine.start(case_id, body.assignment_id, student).state()
    except KeyError:
        raise HTTPException(404, "Unknown case.")


@app.get("/session/{sid}")
def state(sid: str):
    return session(sid).state()


@app.post("/session/{sid}/chat")
def chat(sid: str, body: TextIn):
    s = session(sid)
    if not body.text.strip():
        raise HTTPException(422, "Say something first.")
    if s.stage == "ed":
        return guard(engine.ed_chat, s, body.text)
    if s.stage == "discharge":
        return guard(engine.discharge_chat, s, body.text, body.interpreter is not False)
    raise HTTPException(409, f"No chat in the {s.stage} stage.")


@app.post("/session/{sid}/handoff")
def handoff(sid: str, body: TextIn):
    if not body.text.strip():
        raise HTTPException(422, "Write the handoff first.")
    return guard(engine.do_handoff, session(sid), body.text)


@app.post("/session/{sid}/teachback")
def teachback(sid: str):
    return guard(engine.do_teachback, session(sid))


@app.get("/session/{sid}/feedback")
def feedback(sid: str):
    s = session(sid)
    fb = engine.feedback(s)
    if s.assignment_id and s.student_id and s.teachback:   # finished an assignment: save it for the professor
        if not getattr(s, "losses_logged", False):
            for th in fb["threads"]:
                if th["lost_at"]:
                    engine.emit(s, "fact_lost", stage=th["lost_at"], fact_id=th["id"], detail={"rescued_by": th.get("rescued_by")})
            engine.emit(s, "finished", detail={"carried_home": fb["carried_home"], "total": fb["total_threads"]})
            s.losses_logged = True
        store.save_result({"session_id": s.id, "assignment_id": s.assignment_id, "student_id": s.student_id,
                           "case_id": s.case["id"], "finished": store.now(), "feedback": fb,
                           "handoff": s.handoff, "plan": s.plan, "transcript": s.transcript})
    return fb


@app.post("/tts")
def tts(body: SpeakIn):
    """ElevenLabs voice. The key stays on the server; the front end gets audio/mpeg."""
    lang = body.language or ("tr" if body.voice == "ayse" else "en")
    audio = llm.speak(body.text, body.voice, lang)
    if audio is None:
        raise HTTPException(503, "Voice is offline. Check ELEVENLABS_API_KEY, or show the text instead.")
    return Response(content=audio, media_type="audio/mpeg")


# ======================= professor: cases =======================
@app.get("/cases")
def cases(include_drafts: bool = False):
    out = []
    for f in sorted(engine.CASES.glob("*.json")):
        c = engine.load_case(f.stem)
        if include_drafts or c.get("status", "published") == "published":
            out.append(summary_of(c))
    return out


@app.get("/cases/{cid}")
def case_full(cid: str):
    return case_or_404(cid)


@app.get("/cases/{cid}/key")
def case_key(cid: str):
    return casegen.answer_key(case_or_404(cid))


@app.post("/cases/generate")
def case_generate(body: GenerateIn):
    if len(body.notes.strip()) < 15:
        raise HTTPException(422, "Write a few lines about the patient first.")
    try:
        c = casegen.generate(body.notes, body.title)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"case": summary_of(c), "validation": c["validation"], "review": c["review"]}


@app.post("/cases/upload")
def case_upload(body: CaseIn):
    c = casegen.upload(body.case)
    return {"case": summary_of(c), "validation": c["validation"]}


@app.put("/cases/{cid}")
def case_update(cid: str, body: CaseIn):
    old = case_or_404(cid)
    c = dict(body.case, id=cid, status=old.get("status", "draft"))
    c["validation"] = casegen.validate(c)
    casegen.save(c)
    return {"case": summary_of(c), "validation": c["validation"]}


@app.post("/cases/{cid}/review")
def case_review(cid: str):
    c = case_or_404(cid)
    c["review"] = casegen.review(c)
    casegen.save(c)
    return c["review"]


@app.post("/cases/{cid}/publish")
def case_publish(cid: str):
    c = case_or_404(cid)
    v = casegen.validate(c)
    if not v["ok"]:
        raise HTTPException(409, "Fix these before publishing: " + " ".join(v["errors"]))
    c["status"], c["validation"] = "published", v
    casegen.save(c)
    return summary_of(c)


# ======================= professor: classes and assignments =======================
@app.post("/classes")
def class_create(body: ClassIn):
    if not body.name.strip():
        raise HTTPException(422, "Give the class a name.")
    return store.create_class(body.name.strip(), (body.section or "").strip())


@app.get("/classes")
def class_list(student_id: Optional[str] = None):
    rows = store.classes()
    if student_id:
        sid = netid(student_id)
        rows = [c for c in rows if sid in c["students"]]
    every = store.assignments()
    for c in rows:
        mine = [a for a in every if a["class_id"] == c["id"]]
        c["assignment_count"] = len([a for a in mine if not a.get("student_id")])
        c["student_count"] = len(c["students"])
    return rows


@app.get("/classes/{cid}")
def class_detail(cid: str):
    c = store.get_class(cid)
    if not c:
        raise HTTPException(404, "No class with that id.")
    return c


@app.get("/classes/{cid}/gradebook")
def gradebook(cid: str):
    """Students x assignments, latest attempt each, like a gradebook."""
    c = store.get_class(cid)
    if not c:
        raise HTTPException(404, "No class with that id.")
    rows = store.assignments(cid)
    cells = {}
    for r in sorted(store.results(), key=lambda r: r["finished"]):
        if any(a["id"] == r["assignment_id"] for a in rows):
            fb = r["feedback"]
            cells.setdefault(r["student_id"], {})[r["assignment_id"]] = {
                "carried_home": fb.get("carried_home"), "total": fb.get("total_threads"),
                "session_id": r["session_id"], "finished": r["finished"]}
    return {"class": c, "assignments": rows, "students": c["students"], "cells": cells}


@app.post("/classes/join")
def class_join(body: JoinIn):
    c = store.join_class(body.code, netid(body.student_id))
    if not c:
        raise HTTPException(404, "No class with that code. Check it with your professor.")
    return {"id": c["id"], "name": c["name"], "section": c["section"]}


@app.get("/classes/{cid}/analytics")
def class_analytics(cid: str):
    """Class analytics from the encounter event log: which facts get lost where, live activity, progress."""
    if not store.get_class(cid):
        raise HTTPException(404, "No class with that id.")
    out = store.analytics(cid)
    labels = {}
    for f in engine.CASES.glob("*.json"):
        c = engine.load_case(f.stem)
        for fact in c.get("facts", []):
            labels[(c["id"], fact["id"])] = fact.get("short") or fact["label"]
    for row in out["most_lost"]:
        row["label"] = labels.get((row["case_id"], row["fact_id"]), row["fact_id"])
    return out


@app.post("/assignments")
def assign(body: AssignIn):
    if not store.get_class(body.class_id):
        raise HTTPException(404, "No class with that id.")
    c = case_or_404(body.case_id)
    if c.get("status", "published") != "published":
        raise HTTPException(409, "Publish the case before assigning it.")
    return store.create_assignment(body.class_id, body.case_id, body.title or c["title"], body.due or "", body.notes or "",
                                   netid(body.student_id) if body.student_id else None)


@app.delete("/assignments/{aid}")
def unassign(aid: str):
    if not store.delete_assignment(aid):
        raise HTTPException(404, "No assignment with that id.")
    return {"ok": True}


@app.get("/assignments")
def assignment_list(class_id: Optional[str] = None, student_id: Optional[str] = None):
    if student_id:
        sid = netid(student_id)
        mine = [c["id"] for c in store.classes() if sid in c["students"]]
        rows = [a for a in store.assignments() if a["class_id"] in mine and a.get("student_id") in (None, sid)]
        done = {r["assignment_id"]: r for r in store.results(student_id=sid)}
        out = []
        for a in rows:
            r = done.get(a["id"])
            cls = store.get_class(a["class_id"])
            out.append(dict(a, class_name=cls["name"] if cls else "", done=bool(r),
                            focus=r["feedback"].get("focus", []) if r else [],
                            score=(f"{r['feedback']['carried_home']} of {r['feedback']['total_threads']}" if r else None),
                            session_id=r["session_id"] if r else None))
        return out
    rows = store.assignments(class_id)
    for a in rows:
        cls = store.get_class(a["class_id"])
        a["submitted"] = len(store.results(assignment_id=a["id"]))
        a["class_size"] = 1 if a.get("student_id") else (len(cls["students"]) if cls else 0)
    return rows


@app.get("/assignments/{aid}/results")
def assignment_results(aid: str):
    a = store.get_assignment(aid)
    if not a:
        raise HTTPException(404, "No assignment with that id.")
    cls = store.get_class(a["class_id"])
    by_student = {}
    for r in sorted(store.results(assignment_id=aid), key=lambda r: r["finished"]):
        by_student[r["student_id"]] = r     # latest attempt wins
    rows = []
    for sid in ([a["student_id"]] if a.get("student_id") else (cls["students"] if cls else [])):
        r = by_student.get(sid)
        fb = r["feedback"] if r else None
        rows.append({"student_id": sid, "done": bool(r), "session_id": r["session_id"] if r else None,
                     "finished": r["finished"] if r else None,
                     "carried_home": fb["carried_home"] if fb else None, "total_threads": fb["total_threads"] if fb else None,
                     "facts_found": fb["facts_found"] if fb else None, "facts_total": fb["facts_total"] if fb else None,
                     "lost": [{"fact": t["short"], "at": t["lost_at"], "rescued_by": t.get("rescued_by")} for t in fb["threads"] if t["lost_at"]] if fb else [],
                     "translation_flags": len(fb.get("translation_flags", [])) if fb else 0,
                     "focus": fb.get("focus", []) if fb else []})
    return {"assignment": a, "class": cls, "rows": rows}


@app.get("/results/{session_id}")
def result_detail(session_id: str):
    r = store.get_result(session_id)
    if not r:
        raise HTTPException(404, "No saved result for that session.")
    return r


# ======================= deep agent: case author (streams its steps) =======================
def _sse(events):
    for ev in events:
        yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"


def _author(notes: str, title: Optional[str], extra: Optional[dict] = None):
    """Deep agent when available (Python 3.11+ and a Gemini key); otherwise the plain pipeline."""
    if case_agent.AVAILABLE and llm.gemini():
        yield from case_agent.start(notes, title, extra)
        return
    yield {"type": "plan", "items": [{"text": t, "status": "pending"} for t in
           ["Draft the case (Gemini)", "Run the structure check (code)", "Fix errors", "Clinical review (Claude)", "Save as draft"]]}
    yield {"type": "tool", "tool": "pipeline", "label": "Drafting with Gemini, checking in code, reviewing with Claude"
           + ("" if case_agent.AVAILABLE else " (deep agent needs Python 3.11+)")}
    try:
        c = casegen.generate(notes, title)
    except RuntimeError as e:
        yield {"type": "error", "message": str(e)}
        return
    if extra:
        c.update(extra)
        casegen.save(c)
    yield {"type": "saved", "case_id": c["id"], "title": c["title"], "summary": "",
           "validation": c["validation"], "review": c["review"], "files": []}


class DecisionIn(BaseModel):
    approve: bool
    note: Optional[str] = ""


@app.post("/cases/agent/{run_id}/decision")
def case_agent_decision(run_id: str, body: DecisionIn):
    """Human in the loop: the professor approves the agent's case, or sends it back with a note."""
    if not body.approve and not (body.note or "").strip():
        raise HTTPException(422, "Say what to change.")
    return StreamingResponse(_sse(case_agent.decide(run_id, body.approve, (body.note or "").strip())), media_type="text/event-stream")


@app.get("/cases/agent/{run_id}/files")
def case_agent_files(run_id: str):
    return case_agent.files_of(run_id)


@app.get("/agent/memory")
def agent_memory():
    """The case author's long-term memory (AGENTS.md), including what professors taught it."""
    p = case_agent.MEMORY if case_agent.MEMORY.exists() else case_agent.TEMPLATE / "AGENTS.md"
    return {"path": "/AGENTS.md", "content": p.read_text(encoding="utf-8")}


@app.post("/cases/agent")
def case_agent_stream(body: GenerateIn):
    if len(body.notes.strip()) < 15:
        raise HTTPException(422, "Write a few lines about the patient first.")
    return StreamingResponse(_sse(_author(body.notes, body.title)), media_type="text/event-stream")


@app.post("/results/{session_id}/follow-up")
def follow_up(session_id: str):
    """Adaptive practice: write the student's next case aimed at exactly what they missed."""
    r = store.get_result(session_id)
    if not r:
        raise HTTPException(404, "No saved result for that session.")
    fb, old = r["feedback"], engine.load_case(r["case_id"])
    focus = fb.get("focus") or [{"skill": "General practice", "why": "Everything made it home. Raise the difficulty."}]
    lost = [t["label"] for t in fb["threads"] if t["lost_at"]]
    notes = (f"Write a NEW follow-up practice case for one student. Use a different patient, condition and caregiver language than "
             f"the last case ({old['patient']['name']}, {old['title']}). Design it to practice exactly these skills: "
             + "; ".join(f"{f['skill']} ({f['why']})" for f in focus)
             + f". In the last case the student lost: {', '.join(lost) or 'nothing'}. "
             "Put the same KIND of challenge in the new case (for example, another embarrassing habit the patient hides "
             "until asked kindly), but in a different clinical story.")
    title = f"Follow-up for {r['student_id']}: " + ", ".join(f["skill"] for f in focus[:2])
    extra = {"follow_up_for": {"student_id": r["student_id"], "session_id": session_id, "class_id": (store.get_assignment(r["assignment_id"]) or {}).get("class_id"),
                               "focus": focus}}
    return StreamingResponse(_sse(_author(notes, title, extra)), media_type="text/event-stream")
