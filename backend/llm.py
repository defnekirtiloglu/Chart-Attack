"""Thin Gemini wrapper. Every function returns None when Gemini is unavailable,
so the rest of the app falls back to plain code and the demo never hard-crashes."""
import json
import os
from typing import Any, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
_client = None


def client():
    global _client
    if _client is None and os.getenv("GEMINI_API_KEY"):
        try:
            from google import genai
            _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        except Exception:
            _client = None
    return _client


def ask_json(prompt: str, temperature: float = 0.2) -> Optional[Any]:
    c = client()
    if c is None:
        return None
    try:
        from google.genai import types
        r = c.models.generate_content(
            model=MODEL, contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=temperature))
        return json.loads(r.text)
    except Exception as e:  # network, quota, bad JSON
        print("[gemini]", type(e).__name__, e)
        return None


def ask_text(prompt: str, temperature: float = 0.8) -> Optional[str]:
    c = client()
    if c is None:
        return None
    try:
        from google.genai import types
        r = c.models.generate_content(
            model=MODEL, contents=prompt,
            config=types.GenerateContentConfig(temperature=temperature))
        return (r.text or "").strip() or None
    except Exception as e:
        print("[gemini]", type(e).__name__, e)
        return None
