"""Model wrappers. Three providers, each with a different job:
  Gemini      - the actors (Walter, Dr. Okafor, Ayşe) and the interpreter
  Claude      - the independent checkers (back-translation, second handoff judge, pharmacist)
  ElevenLabs  - voices (Walter in English, Ayşe in Turkish)
Every call returns None when its provider is unavailable, so the app never hard-crashes."""
import json
import os
import re
import time
from typing import Any, Optional

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
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
            from google.genai import types as _types
            _gemini = genai.Client(api_key=os.getenv("GEMINI_API_KEY"),
                                   http_options=_types.HttpOptions(timeout=60_000))   # ms: no call hangs forever
        except Exception:
            _gemini = None
    return _gemini


_THINK = "unknown"   # which "think less" setting this model accepts: "level", "budget", or None


def thinking():
    """Newer Gemini models think before answering, which makes every call slow. Ask for minimal thinking.
    Probed once with a tiny call: try thinking_level=low, then thinking_budget=0, else the model default."""
    global _THINK
    if _THINK != "unknown":
        return _THINK
    c = gemini()
    if c is None:
        return None
    from google.genai import types
    for mode, cfg in (("level", types.ThinkingConfig(thinking_level="low")), ("budget", types.ThinkingConfig(thinking_budget=0))):
        try:
            c.models.generate_content(model=GEMINI_MODEL, contents="Say ok.",
                                      config=types.GenerateContentConfig(thinking_config=cfg, max_output_tokens=5,
                                                                         http_options=types.HttpOptions(timeout=15_000)))
            _THINK = mode
            print(f"[gemini] fast mode: thinking_{mode}")
            return _THINK
        except Exception as e:
            if "not found" in str(e).lower() or "404" in str(e):
                break            # wrong model name: don't keep probing
    _THINK = None
    return _THINK


def _cfg(timeout_s: Optional[float] = None, **kw):
    from google.genai import types
    if timeout_s:
        kw["http_options"] = types.HttpOptions(timeout=int(timeout_s * 1000))
    mode = thinking()
    if mode == "level":
        kw["thinking_config"] = types.ThinkingConfig(thinking_level="low")
    elif mode == "budget":
        kw["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    return types.GenerateContentConfig(**kw)


BACKUP_TIMEOUT = float(os.getenv("CLAUDE_BACKUP_SECONDS", "10"))
LAST_PROVIDER = None   # "gemini" or "claude": who answered the last ask_* call


def ask_json(prompt: str, temperature: float = 0.2, timeout_s: Optional[float] = None, tag: str = "json",
             backup: bool = False) -> Optional[Any]:
    global LAST_PROVIDER
    out = _gemini_json(prompt, temperature, timeout_s, tag)
    LAST_PROVIDER = "gemini" if out is not None else None
    if out is None and backup:
        out = claude_json(prompt, timeout_s=BACKUP_TIMEOUT, tag=tag + " (Claude backup)")
        LAST_PROVIDER = "claude" if out is not None else None
    return out


def ask_text(prompt: str, temperature: float = 0.8, timeout_s: Optional[float] = None, tag: str = "text",
             backup: bool = False) -> Optional[str]:
    global LAST_PROVIDER
    out = _gemini_text(prompt, temperature, timeout_s, tag)
    LAST_PROVIDER = "gemini" if out is not None else None
    if out is None and backup:
        out = claude_text(prompt, system="Follow the instructions exactly. Output only what is asked for.",
                          temperature=min(temperature, 1.0), timeout_s=BACKUP_TIMEOUT, tag=tag + " (Claude backup)")
        LAST_PROVIDER = "claude" if out is not None else None
    return out


def _gemini_json(prompt: str, temperature: float = 0.2, timeout_s: Optional[float] = None, tag: str = "json") -> Optional[Any]:
    c = gemini()
    if c is None:
        return None
    t0 = time.time()
    try:
        r = c.models.generate_content(
            model=GEMINI_MODEL, contents=prompt,
            config=_cfg(timeout_s, response_mime_type="application/json", temperature=temperature))
        print(f"[gemini] {tag}: {time.time() - t0:.1f}s")
        return json.loads(r.text)
    except Exception as e:
        print(f"[gemini] {tag} failed after {time.time() - t0:.1f}s:", type(e).__name__, str(e)[:200])
        return None


def _gemini_text(prompt: str, temperature: float = 0.8, timeout_s: Optional[float] = None, tag: str = "text") -> Optional[str]:
    c = gemini()
    if c is None:
        return None
    t0 = time.time()
    try:
        r = c.models.generate_content(
            model=GEMINI_MODEL, contents=prompt, config=_cfg(timeout_s, temperature=temperature))
        print(f"[gemini] {tag}: {time.time() - t0:.1f}s")
        return (r.text or "").strip() or None
    except Exception as e:
        print(f"[gemini] {tag} failed after {time.time() - t0:.1f}s:", type(e).__name__, str(e)[:200])
        return None


# ---------------- Claude ----------------
def claude_text(prompt: str, system: str = "", max_tokens: int = 800, temperature: float = 0.2,
                timeout_s: float = 60, tag: str = "") -> Optional[str]:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    t0 = time.time()
    try:
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=timeout_s, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": max_tokens,
                  "system": system or "You are a careful clinical reviewer.",
                  "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        if tag:
            print(f"[claude] {tag}: {time.time() - t0:.1f}s")
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip() or None
    except Exception as e:
        body = getattr(getattr(e, "response", None), "text", "")
        print(f"[claude] {tag or 'call'} failed after {time.time() - t0:.1f}s:", type(e).__name__, str(e)[:120], body[:300])
        return None


def claude_json(prompt: str, system: str = "", timeout_s: float = 60, tag: str = "") -> Optional[Any]:
    out = claude_text(prompt + "\n\nReply with JSON only, no prose.", system, timeout_s=timeout_s, tag=tag)
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
