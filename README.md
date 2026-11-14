# OSCEnario Rounds

One virtual patient, three encounters, one shared record. Built fresh at HackPrinceton Fall 2026.

Walter Brennan (72, heart failure, sassy) comes into the ED. The student interviews him, hands off to an AI cardiologist
who knows **only what the handoff says**, then teaches the discharge plan to his wife **Ayşe, who speaks Turkish**,
through an AI interpreter. The feedback timeline shows where each key fact was found, handed off, or lost.

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
`python3 scripts/demo_run.py` plays a whole session and saves every response to `samples/`.

## API (front end)
| Call | When | Returns |
|---|---|---|
| `POST /session` `{}` | start | full state: `session_id`, `stage`, `encounter`, `chart`, `hidden_count` |
| `GET /session/{id}` | any time | same state, plus `transcript`, `handoff`, `plan` |
| `POST /session/{id}/chat` `{text}` | stage `ed` | `reply`, `new_facts[]` (pop into chart), `dodged` (Walter dodged), `hidden_count` |
| `POST /session/{id}/handoff` `{text}` | after ED | cardiologist `reply`, `handed_off[]`, `missing_critical[]` (with `consequence`), `plan[]`; stage → `discharge` |
| `POST /session/{id}/chat` `{text}` | stage `discharge` | `interpreted_tr` (what the interpreter said), `reply_tr` + `reply_en` (Ayşe), `points[]` (plan checklist: `covered`, `clear`, `jargon`) |
| `POST /session/{id}/teachback` | end of discharge | Ayşe's repeat-back `tr` + `en`, `understood[]`, `missed[]`; stage → `done` |
| `GET /session/{id}/feedback` | after teachback | `threads[]`: each critical fact with 3 `steps` (ed, handoff, discharge), `lost_at`, `consequence` |

Stages: `ed` → `handoff` → `discharge` → `done`. Wrong-stage calls return 409 with a plain message.

Fictional teaching case. Not medical advice.
