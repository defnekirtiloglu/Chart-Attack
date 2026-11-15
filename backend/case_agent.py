"""The case author: a LangChain Deep Agent.

The professor types a few lines (or the system asks for a follow-up case aimed at a student's gaps).
A deep agent then works in its own workspace:

  Planning     write_todos: the plan streams live to the portal.
  Filesystem   FilesystemBackend per run. The case is a real file, /draft/case.json, written with
               write_file and fixed with small edit_file edits. Reviewers write /reviews/*.md.
  Skills       /skills/case-authoring and /skills/teaching-design (SKILL.md folders).
  Memory       /AGENTS.md: house rules plus professor preferences. It persists across runs:
               when a professor sends a draft back with a note, the agent records the lesson.
  Sub-agents   patient-voice (writes in-character lines), clinical-reviewer (Claude),
               learning-designer (checks the case trains the target skills).
  Tools        check_case: the structure check in plain code. save_case: refuses invalid cases.
  HITL         interrupt_on save_case: the agent pauses; the professor approves or sends it back.

Needs Python 3.11+. On older Python the API falls back to the plain pipeline in casegen.py.
"""
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Dict, Iterator, Optional

from . import casegen, engine

try:
    from deepagents import create_deep_agent
    from deepagents.backends import FilesystemBackend
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    AVAILABLE = True
except Exception:          # Python < 3.11 or not installed
    AVAILABLE = False

TEMPLATE = Path(__file__).resolve().parent / "agent_workspace"
DATA = Path(__file__).resolve().parents[1] / "data"
RUNS_DIR = DATA / "agent_runs"
MEMORY = DATA / "agent_memory" / "AGENTS.md"
RUNS: Dict[str, dict] = {}


def _models():
    from langchain_google_genai import ChatGoogleGenerativeAI
    from . import llm
    fast = {"level": {"thinking_level": "low"}, "budget": {"thinking_budget": 0}}.get(llm.thinking())
    main = ChatGoogleGenerativeAI(model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
                                  google_api_key=os.getenv("GEMINI_API_KEY"), temperature=0.4,
                                  timeout=90, max_retries=1, **({"thinking_config": fast} if fast else {}))
    reviewer = main
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            from langchain_anthropic import ChatAnthropic
            reviewer = ChatAnthropic(model=os.getenv("CLAUDE_MODEL", "claude-haiku-5-5"),
                                     api_key=os.getenv("ANTHROPIC_API_KEY"), temperature=0.2, max_tokens=3000,
                                     timeout=90, max_retries=1)
        except Exception:
            pass
    return main, reviewer, main


SYSTEM = """You are the case author for OSCEnario Rounds, a medical-education simulator.
One virtual patient goes through: ED interview -> SBAR handoff to a specialist -> discharge teaching to a caregiver.
Turn the professor's notes into a complete, valid, clinically sound case file.

You work in a filesystem. Use the case-authoring skill (and teaching-design when the notes name skills to practice).
Follow /AGENTS.md, including every line under "Professor preferences".

Workflow:
1. write_todos with your plan.
2. Read /reference/example_case.json. Write the full case to /draft/case.json.
   If the notes name skills to practice, also write /notes/design.md (skill -> case feature).
3. Delegate to patient-voice: it rewrites the 'says' lines and personas in /draft/case.json so the patient and caregiver sound like real people.
4. check_case. Fix errors with small edit_file edits; repeat until ok.
5. Delegate to clinical-reviewer and learning-designer (they write /reviews/clinical.md and /reviews/learning.md).
   Read both. Fix every high-severity issue with edit_file, then check_case again.
6. Call save_case with a short summary. A professor must approve it.
   If the professor sends it back with a note: add ONE short line under "## Professor preferences" in /AGENTS.md
   capturing the lesson, fix the case, check_case, and call save_case again.
Never call save_case while check_case reports errors."""

VOICE = """You make simulated patients sound like real people. Read /draft/case.json and the persona.
Rewrite every non-chart fact's 'says' line in the patient's own voice (consistent personality, everyday words,
no medical terms, no diagnoses), and tighten the patient and caregiver personas. Change ONLY 'says' and 'persona'
fields, using edit_file. Keep the JSON valid. Reply with one sentence on what you changed."""

CLINICAL = """You are a clinician-educator reviewing a simulation case before a professor sees it.
Read /draft/case.json. Flag anything clinically wrong or unsafe, any plan point that contradicts the facts,
and unrealistic labs or meds. Write /reviews/clinical.md as a list: **severity** (high/medium/low) | where | issue | suggestion.
Do not edit the case. Reply with the number of high-severity issues."""

LEARNING = """You are a medical-education designer. Read /draft/case.json and, if present, /notes/design.md.
Check: each hidden fact is reachable with one good question (unlock_hint), the reluctant facts are fair,
the case trains the skills named in the notes, and the discharge plan is teachable in plain language.
Write /reviews/learning.md as a list: **severity** | where | issue | suggestion. Do not edit the case.
Reply with the number of high-severity issues."""


def _workspace(run_id: str) -> Path:
    d = RUNS_DIR / run_id
    shutil.copytree(TEMPLATE, d, dirs_exist_ok=True)
    MEMORY.parent.mkdir(parents=True, exist_ok=True)
    if not MEMORY.exists():
        shutil.copy(TEMPLATE / "AGENTS.md", MEMORY)
    shutil.copy(MEMORY, d / "AGENTS.md")
    (d / "reference").mkdir(exist_ok=True)
    (d / "reference" / "example_case.json").write_text(json.dumps(json.loads(casegen._example()), indent=2, ensure_ascii=False))
    for sub in ("draft", "reviews", "notes"):
        (d / sub).mkdir(exist_ok=True)
    return d


def files_of(run_id: str) -> list:
    d = RUNS_DIR / run_id
    out = []
    for p in sorted(d.rglob("*")):
        rel = "/" + str(p.relative_to(d))
        if p.is_file() and not rel.startswith(("/skills", "/reference")):
            out.append({"path": rel, "size": p.stat().st_size,
                        "content": p.read_text(encoding="utf-8", errors="replace")[:20000]})
    return out


def start(notes: str, title: Optional[str] = None, extra: Optional[dict] = None) -> Iterator[dict]:
    if not AVAILABLE:
        yield {"type": "error", "message": "deepagents needs Python 3.11+."}
        return
    if not os.getenv("GEMINI_API_KEY"):
        yield {"type": "error", "message": "GEMINI_API_KEY is missing, so the agent can't run."}
        return
    run_id = uuid.uuid4().hex[:8]
    d = _workspace(run_id)
    state = {"id": None, "validation": None, "saved": None}

    def read_case() -> Optional[dict]:
        p = d / "draft" / "case.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def check_case() -> str:
        """Run the structure check (plain code) on /draft/case.json. Returns ok, errors, warnings."""
        try:
            case = read_case()
        except Exception as e:
            v = {"ok": False, "errors": [f"/draft/case.json is not valid JSON: {e}"], "warnings": []}
            state["validation"] = v
            return json.dumps(v)
        if not isinstance(case, dict):
            v = {"ok": False, "errors": ["Write the case to /draft/case.json first."], "warnings": []}
        else:
            v = casegen.validate(case)
        state["validation"] = v
        return json.dumps(v)

    def save_case(summary: str) -> str:
        """Save /draft/case.json as a draft for the professor. Needs professor approval. Refused if check_case fails."""
        v = json.loads(check_case())
        if not v["ok"]:
            return "Refused: fix these first: " + "; ".join(v["errors"])
        case = casegen._finish(read_case(), notes, title, keep_id=state["id"])
        state["id"] = case["id"]
        case["validation"] = v
        reviews = {p.stem: p.read_text(encoding="utf-8") for p in (d / "reviews").glob("*.md")}
        case["review"] = {"verdict": "reviewed by sub-agents", "issues": [], "reviews": reviews, "note": summary}
        case["agent_summary"] = summary
        case["agent_run"] = run_id
        if extra:
            case.update(extra)
        casegen.save(case)
        state["saved"] = case["id"]
        return f"Saved as draft '{case['id']}'."

    main, reviewer, voice = _models()
    agent = create_deep_agent(
        model=main,
        tools=[check_case, save_case],
        system_prompt=SYSTEM,
        backend=FilesystemBackend(root_dir=str(d), virtual_mode=True),
        skills=["/skills/"],
        memory=["/AGENTS.md"],
        subagents=[
            {"name": "patient-voice", "description": "Rewrites the patient's and caregiver's lines in /draft/case.json so they sound like real people.",
             "system_prompt": VOICE, "model": voice, "tools": []},
            {"name": "clinical-reviewer", "description": "Reviews /draft/case.json for clinical accuracy and safety; writes /reviews/clinical.md.",
             "system_prompt": CLINICAL, "model": reviewer, "tools": [check_case]},
            {"name": "learning-designer", "description": "Checks /draft/case.json teaches the target skills fairly; writes /reviews/learning.md.",
             "system_prompt": LEARNING, "model": reviewer, "tools": []},
        ],
        interrupt_on={"save_case": {"allowed_decisions": ["approve", "reject"]}},
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 120}
    RUNS[run_id] = {"agent": agent, "config": config, "dir": d, "state": state}
    yield {"type": "run", "run_id": run_id}
    ask = f"Professor's notes:\n\"\"\"{notes}\"\"\"" + (f"\nTitle: {title}" if title else "")
    yield from _drive(run_id, {"messages": [{"role": "user", "content": ask}]})


def decide(run_id: str, approve: bool, note: str = "") -> Iterator[dict]:
    r = RUNS.get(run_id)
    if not r:
        yield {"type": "error", "message": "That agent run has ended. Start a new draft."}
        return
    decision = {"type": "approve"} if approve else {"type": "reject", "message": "Professor sent it back: " + (note or "please improve it.")}
    yield {"type": "tool", "tool": "professor", "label": "Approved. Saving the case" if approve else f"Sent back with a note: {note}"}
    yield from _drive(run_id, Command(resume={"decisions": [decision]}), resumed=True)


MAX_SECONDS = int(os.getenv("AGENT_MAX_SECONDS", "420"))


def _drive(run_id: str, payload, resumed: bool = False) -> Iterator[dict]:
    import time as _t
    r = RUNS[run_id]
    agent, config, d, state = r["agent"], r["config"], r["dir"], r["state"]
    t0 = _t.time()
    try:
        for update in agent.stream(payload, config=config, stream_mode="updates"):
            if _t.time() - t0 > MAX_SECONDS:
                yield {"type": "error", "message": f"The agent took longer than {MAX_SECONDS // 60} minutes, so it was stopped. "
                       "Try shorter notes, or try again."}
                return
            if "__interrupt__" in update:
                continue
            for ev in _events(update):
                if resumed and ev.get("tool") == "save_case":   # replay of the call we just answered
                    resumed = False
                    continue
                yield ev
    except Exception as e:
        yield {"type": "error", "message": f"The agent stopped: {type(e).__name__}: {e}"}
        return
    finally:
        if (d / "AGENTS.md").exists():           # memory persists across runs
            shutil.copy(d / "AGENTS.md", MEMORY)
    snap = agent.get_state(config)
    if snap.interrupts:
        case = None
        try:
            case = json.loads((d / "draft" / "case.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        yield {"type": "approval", "run_id": run_id,
               "summary": next((tc["args"].get("summary", "") for m in reversed(snap.values.get("messages", []))
                                for tc in (getattr(m, "tool_calls", None) or []) if tc["name"] == "save_case"), ""),
               "title": (case or {}).get("title"), "patient": (case or {}).get("patient", {}).get("name"),
               "validation": state["validation"], "files": files_of(run_id)}
        return
    if state["saved"]:
        c = engine.load_case(state["saved"])
        yield {"type": "saved", "case_id": c["id"], "title": c["title"], "summary": c.get("agent_summary", ""),
               "validation": c["validation"], "review": c.get("review"), "files": files_of(run_id)}
        RUNS.pop(run_id, None)
    else:
        yield {"type": "error", "message": "The agent finished without a saved case."}


LABELS = {"check_case": "Running the structure check (code)", "save_case": "Asking you to approve the case",
          "write_file": "Writing", "edit_file": "Editing", "read_file": "Reading", "ls": "Listing files",
          "glob": "Finding files", "grep": "Searching files"}
AGENT_LABELS = {"patient-voice": "Patient-voice sub-agent is rewriting the lines in character",
                "clinical-reviewer": "Clinical-reviewer sub-agent (Claude) is checking the medicine",
                "learning-designer": "Learning-designer sub-agent is checking what it teaches"}


def _events(update: dict) -> Iterator[dict]:
    for node, data in (update or {}).items():
        msgs = (data or {}).get("messages", []) if isinstance(data, dict) else []
        if hasattr(msgs, "value"):
            msgs = msgs.value
        for m in msgs if isinstance(msgs, list) else []:
            for tc in getattr(m, "tool_calls", None) or []:
                name, args = tc.get("name"), tc.get("args") or {}
                if name == "write_todos":
                    yield {"type": "plan", "items": [{"text": t.get("content", ""), "status": t.get("status", "pending")} for t in args.get("todos", [])]}
                elif name == "task":
                    sub = args.get("subagent_type", "")
                    yield {"type": "subagent", "name": sub, "label": AGENT_LABELS.get(sub, f"Sub-agent {sub}")}
                elif name in ("write_file", "edit_file", "read_file"):
                    path = args.get("file_path") or args.get("path") or ""
                    yield {"type": "file", "op": name, "path": path, "label": f"{LABELS[name]} {path}"}
                elif name in LABELS:
                    yield {"type": "tool", "tool": name, "label": LABELS[name]}
            if getattr(m, "type", "") == "tool" and getattr(m, "name", "") == "check_case":
                try:
                    v = json.loads(str(m.content))
                    yield {"type": "result", "ok": v.get("ok"), "errors": v.get("errors", []), "warnings": v.get("warnings", [])}
                except Exception:
                    pass
            if getattr(m, "type", "") == "tool" and getattr(m, "name", "") == "task":
                yield {"type": "review", "text": str(m.content)[:600]}
