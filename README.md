# OSCEnario Rounds

One virtual patient, three encounters, one shared record. Built fresh at HackPrinceton Fall 2026.

Walter Brennan (72, heart failure, sassy) comes into the ED. The student interviews him, hands off to an AI cardiologist
who knows **only what the handoff says**, then teaches the discharge plan to his wife **Ayşe, who speaks Turkish**,
through an AI interpreter. The feedback timeline shows where each key fact was found, handed off, or lost.

## Three models, separate jobs
| Provider | Job |
|---|---|
| **Gemini** | The actors: Walter, Dr. Okafor, Ayşe, and the Turkish interpreter |
| **Claude** | The independent checkers: second handoff judge, back-translation, pharmacist |
| **ElevenLabs** | Voices: Walter in English, Ayşe in Turkish (`eleven_multilingual_v2`) |

**Independent checks, code decides**
- **Handoff:** Gemini and Claude each judge which facts your SBAR conveyed. Both agree → counted. They disagree → `unclear`, not counted.
- **Pharmacist (Dr. Tanaka):** never sees your handoff. She reads the chart, sees creatinine 1.2 → 1.6, and asks Walter about pain pills. If you missed ibuprofen, she catches it and adds "stop ibuprofen" to the plan (`rescued_by: pharmacist` in feedback).
- **Back-translation:** Gemini interprets into Turkish. Claude translates it back to English *without seeing the original*. Code compares the numbers, and Claude compares the meaning. Any change → `flagged`, and that message doesn't count as understood. No checker available → `unchecked` (never a fake "ok").

**The model answers, code decides.** Gemini maps the student's words to facts and writes the replies.
Plain code decides what Walter reveals, what the cardiologist knows, what goes in the plan, and what Ayşe understood.

## Run
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # paste your Gemini key into .env (never commit it)
uvicorn backend.app:app --reload
```
Open http://127.0.0.1:8000/docs. With no key, everything still runs on keyword fallbacks.
`python3 scripts/test_checks.py` tests the multi-model checks with fake models.
`python3 scripts/demo_run.py` plays a whole session and saves every response to `samples/`.

## API (front end)
| Call | When | Returns |
|---|---|---|
| `POST /session` `{}` | start | full state: `session_id`, `stage`, `encounter`, `chart`, `hidden_count` |
| `GET /session/{id}` | any time | same state, plus `transcript`, `handoff`, `plan` |
| `POST /session/{id}/chat` `{text}` | stage `ed` | `reply`, `new_facts[]` (pop into chart), `dodged` (Walter dodged), `hidden_count` |
| `POST /session/{id}/handoff` `{text}` | after ED | cardiologist `reply`, `handed_off[]`, `unclear[]`, `pharmacist` (`note`, `caught[]`), `missing_critical[]` (with `consequence`), `plan[]`; stage → `discharge` |
| `POST /session/{id}/chat` `{text, interpreter}` | stage `discharge` | `interpreted_tr` (what the interpreter said), `translation_check` (`status` ok / flagged / unchecked, `back_translation`, `issues[]`), `reply_tr` (exactly what Ayşe said: broken English mixed with Turkish) + `reply_en` (plain English meaning), `too_long` (direct mode: she lost you), `points[]` (plan checklist: `covered`, `clear`, `jargon`) |
| `POST /session/{id}/teachback` | end of discharge | Ayşe's repeat-back `tr` + `en`, `understood[]`, `missed[]`; stage → `done` |
| `POST /tts` `{text, voice}` | any reply | `audio/mpeg`. voice = `walter`, `ayse` (Turkish), or `okafor`. 503 if voice is offline |
| `GET /session/{id}/feedback` | after teachback | `threads[]`: each critical fact with 3 `steps` (ed, handoff, discharge), `lost_at`, `rescued_by`, `consequence`; plus `translation_flags[]` |

Stages: `ed` → `handoff` → `discharge` → `done`. Wrong-stage calls return 409 with a plain message.

Fictional teaching case. Not medical advice.

## Ayşe: two ways to teach her
Ayşe understands simple, slow English and speaks broken English mixed with Turkish.
- `interpreter: true` (default): Gemini interprets into Turkish, and Claude back-translates to check it.
- `interpreter: false`: talk to her directly. Code decides she only follows sentences averaging 15 words or fewer, with no unexplained jargon. Long sentences → `too_long: true`, and she says so, half in Turkish.
