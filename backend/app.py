"""OSCEnario Rounds API. Run:  uvicorn backend.app:app --reload  (from the repo root)"""
from typing import Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import engine, llm

app = FastAPI(title="OSCEnario Rounds")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class StartIn(BaseModel):
    case_id: Optional[str] = "walter"


class TextIn(BaseModel):
    text: str
    interpreter: Optional[bool] = True   # discharge only: False = talk to Ayşe directly in English


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


@app.get("/")
def health():
    return {"ok": True, "app": "OSCEnario Rounds", "docs": "/docs"}


@app.post("/session")
def start(body: StartIn):
    try:
        return engine.start(body.case_id or "walter").state()
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
    return engine.feedback(session(sid))


class SpeakIn(BaseModel):
    text: str
    voice: str = "walter"      # walter | ayse | okafor
    language: Optional[str] = None


@app.post("/tts")
def tts(body: SpeakIn):
    """ElevenLabs voice. The key stays on the server; the front end gets audio/mpeg."""
    lang = body.language or ("tr" if body.voice == "ayse" else "en")
    audio = llm.speak(body.text, body.voice, lang)
    if audio is None:
        raise HTTPException(503, "Voice is offline. Check ELEVENLABS_API_KEY, or show the text instead.")
    return Response(content=audio, media_type="audio/mpeg")
