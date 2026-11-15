"""Model wrappers. Three providers, each with a different job:
  Gemini      - the actors (Walter, Dr. Okafor, Ayşe) and the interpreter
  Claude      - the independent checkers (back-translation, second handoff judge, pharmacist)
  ElevenLabs  - voices (Walter in English, Ayşe in Turkish)
Every call returns None when its provider is unavailable, so the app never hard-crashes."""
import json
import os
import re
from typing import Any, Optional

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
_gemini = None


def _parse_json(text: str) -> Optional[Any]:
    text = (text or "").strip()
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    try:
        return json.loads(m.group(0) if m else text)
    except Exception:
        return None


# ---------------- Gemini ----------------
def gemini():
    global _gemini
    if _gemini is None and os.getenv("GEMINI_API_KEY"):
        try:
            from google import genai
            _gemini = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        except Exception:
            _gemini = None
    return _gemini


def ask_json(prompt: str, temperature: float = 0.2) -> Optional[Any]:
    c = gemini()
    if c is None:
        return None
    try:
        from google.genai import types
        r = c.models.generate_content(
            model=GEMINI_MODEL, contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=temperature))
        return json.loads(r.text)
    except Exception as e:
        print("[gemini]", type(e).__name__, e)
        return None


def ask_text(prompt: str, temperature: float = 0.8) -> Optional[str]:
    c = gemini()
    if c is None:
        return None
    try:
        from google.genai import types
        r = c.models.generate_content(
            model=GEMINI_MODEL, contents=prompt, config=types.GenerateContentConfig(temperature=temperature))
        return (r.text or "").strip() or None
    except Exception as e:
        print("[gemini]", type(e).__name__, e)
        return None


# ---------------- Claude ----------------
def claude_text(prompt: str, system: str = "", max_tokens: int = 800, temperature: float = 0.2) -> Optional[str]:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": max_tokens, "temperature": temperature,
                  "system": system or "You are a careful clinical reviewer.",
                  "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip() or None
    except Exception as e:
        print("[claude]", type(e).__name__, e)
        return None


def claude_json(prompt: str, system: str = "") -> Optional[Any]:
    out = claude_text(prompt + "\n\nReply with JSON only, no prose.", system)
    return _parse_json(out) if out else None


# ---------------- ElevenLabs ----------------
VOICES = {  # premade voices; override in .env with any voice id from your ElevenLabs library
    "walter": os.getenv("ELEVEN_VOICE_WALTER", "pNInz6obpgDQGcFmaJgB"),
    "ayse": os.getenv("ELEVEN_VOICE_AYSE", "21m00Tcm4TlvDq8ikWAM"),
    "okafor": os.getenv("ELEVEN_VOICE_OKAFOR", "EXAVITQu4vr4xnSDxMaL"),
}


def speak(text: str, voice: str, language: str = "en") -> Optional[bytes]:
    key = os.getenv("ELEVENLABS_API_KEY")
    vid = VOICES.get(voice)
    if not key or not vid or not text.strip():
        return None
    model = os.getenv("ELEVEN_MODEL", "eleven_multilingual_v2")
    try:
        r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{vid}", timeout=60,
                       headers={"xi-api-key": key, "content-type": "application/json", "accept": "audio/mpeg"},
                       json={"text": text, "model_id": model, "language_code": language} if model == "eleven_flash_v2_5"
                       else {"text": text, "model_id": model})
        r.raise_for_status()
        return r.content
    except Exception as e:
        print("[elevenlabs]", type(e).__name__, e)
        return None
