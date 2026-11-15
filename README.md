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

## Portal (professor and student)
Open **http://localhost:5500/portal.html**.
- **Professor:** describe a patient in a few lines → Gemini drafts the case → code checks it runs → Claude reviews it clinically → you read the **answer key** (`key.html?case=ID`) → publish → create a class (gets a join code) → assign → see each student's results and transcript.
- **Student:** enter your NetID and the class code → see assignments → **Start rounds** → results go to the professor automatically.

| Call | Does |
|---|---|
| `GET /cases?include_drafts=true` | all cases (students see published only) |
| `GET /cases/{id}` · `PUT /cases/{id}` | full case JSON / save edits |
| `GET /cases/{id}/key` | answer key: facts, unlock hints, ideal SBAR, pharmacist logic, discharge points, checks |
| `POST /cases/generate` `{notes, title}` | AI-drafted case, saved as draft with `validation` and `review` |
| `POST /cases/upload` `{case}` | upload a case JSON as a draft |
| `POST /cases/{id}/publish` | publish (refused if the structure check fails) |
| `POST /classes` `{name, section}` · `GET /classes` | classes with join codes |
| `POST /classes/join` `{code, student_id}` | student joins |
| `POST /assignments` `{class_id, case_id, due}` · `GET /assignments?class_id=` / `?student_id=` | assign / list |
| `GET /assignments/{id}/results` · `GET /results/{session_id}` | professor results / one transcript |
| `POST /session` `{case_id \| assignment_id, student_id}` | start a case (results save when feedback loads) |

Portal data (classes, assignments, results, the encounter event log, the agent's memory) lives in `data/` (gitignored). Cases live in `cases/*.json`.

## Run
```bash
python3.12 -m venv .venv && source .venv/bin/activate   # 3.11+ for deepagents
pip install -r requirements.txt
cp .env.example .env        # paste your Gemini key into .env (never commit it)
uvicorn backend.app:app --reload
```
Open http://127.0.0.1:8000/docs. For the app: `cd frontend && python3 -m http.server 5500`, then open http://localhost:5500/portal.html. With no key, everything still runs on keyword fallbacks.
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

## Prize tracks: what to point judges at
| Track | Where it lives |
|---|---|
| **LangChain: Best use of deepagents** | `backend/case_agent.py` + `backend/agent_workspace/`. The case author is a Deep Agent using the full harness: **planning** (`write_todos`, shown live), a real **filesystem** per run (`FilesystemBackend`: the case is `/draft/case.json`, written with `write_file` and fixed with small `edit_file` edits; reviewers write `/reviews/*.md`; the professor sees the workspace), **skills** (`/skills/case-authoring`, `/skills/teaching-design`), **memory** (`/AGENTS.md`, persists across runs: when a professor sends a draft back, the agent writes the lesson there and follows it next time), **three sub-agents** (`patient-voice`, `clinical-reviewer` on Claude, `learning-designer`), **custom tools** (`check_case` = structure check in plain code, `save_case` refuses invalid cases), and **human-in-the-loop** (`interrupt_on` save: the professor approves, or sends it back with a note). It also writes adaptive follow-up cases from a student's results. |
| **Amazon: Best Practical AI** | Reason → plan → act → adapt: multi-agent encounters (patient, specialist, pharmacist, interpreter, caregiver), code-decided grading, and **adaptive practice**: `feedback.focus` names the skills a student missed, and "Make follow-up case" has the agent write their next case aimed at exactly that (`POST /results/{id}/follow-up`). |
| **MLH: Best Use of Gemini API** | Gemini plays every character, interprets into the caregiver's language, judges the handoff, and drives the case-author deep agent. |
| **MLH: Best Use of Vultr** | `Dockerfile`, `docker-compose.yml`, `deploy/Caddyfile`, `deploy/VULTR.md`: the whole app (API, deep agent, front end) runs on a Vultr Cloud Compute server, with automatic HTTPS when a domain is set. |
| **Peri: Forward Deployed** | Needs a real org consulted during the event: name, role, and evidence. |

**Python 3.11+ is required for the deep agent.** On older Python everything else runs, and case drafting falls back to the plain Gemini → code check → Claude pipeline. `GET /health` shows `case_author` and `storage`. `GET /` opens the portal when the front end is served by the API (Vultr).
