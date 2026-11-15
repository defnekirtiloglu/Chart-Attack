"""Portal storage: classes, assignments, results, and a log of encounter events (JSON files in data/)."""
import json
import random
import string
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

DATA = Path(__file__).resolve().parents[1] / "data"
DATA.mkdir(exist_ok=True)
_lock = threading.Lock()

BACKEND = "files"


def _load(name: str) -> list:
    p = DATA / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _save(name: str, rows: list):
    (DATA / f"{name}.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def new_id() -> str:
    return uuid.uuid4().hex[:8]


# ---------------- classes ----------------
def create_class(name: str, section: str = "") -> dict:
    with _lock:
        rows = _load("classes")
        taken = {c["code"] for c in rows}
        code = ""
        while not code or code in taken:
            code = "".join(random.choice(string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789") for _ in range(6))
        c = {"id": new_id(), "name": name, "section": section, "code": code, "students": [], "created": now()}
        rows.append(c)
        _save("classes", rows)
        return c


def classes() -> List[dict]:
    return _load("classes")


def get_class(cid: str) -> Optional[dict]:
    return next((c for c in _load("classes") if c["id"] == cid), None)


def join_class(code: str, student_id: str) -> Optional[dict]:
    with _lock:
        rows = _load("classes")
        c = next((x for x in rows if x["code"] == code.strip().upper()), None)
        if not c:
            return None
        if student_id not in c["students"]:
            c["students"].append(student_id)
            _save("classes", rows)
        return c


# ---------------- assignments ----------------
def create_assignment(class_id: str, case_id: str, title: str, due: str = "", notes: str = "",
                      student_id: Optional[str] = None) -> dict:
    with _lock:
        rows = _load("assignments")
        existing = next((a for a in rows if a["class_id"] == class_id and a["case_id"] == case_id
                         and a.get("student_id") == student_id), None)
        if existing:
            existing.update({"title": title or existing["title"], "due": due, "notes": notes})
            _save("assignments", rows)
            return existing
        a = {"id": new_id(), "class_id": class_id, "case_id": case_id, "title": title, "due": due,
             "notes": notes, "student_id": student_id, "created": now()}
        rows.append(a)
        _save("assignments", rows)
        return a


def assignments(class_id: Optional[str] = None) -> List[dict]:
    rows = _load("assignments")
    return [a for a in rows if class_id is None or a["class_id"] == class_id]


def get_assignment(aid: str) -> Optional[dict]:
    return next((a for a in _load("assignments") if a["id"] == aid), None)


def delete_assignment(aid: str) -> bool:
    with _lock:
        rows = _load("assignments")
        keep = [a for a in rows if a["id"] != aid]
        _save("assignments", keep)
        return len(keep) != len(rows)


# ---------------- results ----------------
def save_result(row: dict):
    with _lock:
        rows = _load("results")
        rows = [r for r in rows if r["session_id"] != row["session_id"]]
        rows.append(row)
        _save("results", rows)


def results(assignment_id: Optional[str] = None, student_id: Optional[str] = None) -> List[dict]:
    return [r for r in _load("results")
            if (assignment_id is None or r["assignment_id"] == assignment_id)
            and (student_id is None or r["student_id"] == student_id)]


def get_result(session_id: str) -> Optional[dict]:
    return next((r for r in _load("results") if r["session_id"] == session_id), None)


# ---------------- encounter events ----------------
def log_event(session_id: str, kind: str, assignment_id: Optional[str] = None, student_id: Optional[str] = None,
              case_id: Optional[str] = None, stage: Optional[str] = None, fact_id: Optional[str] = None,
              detail: Optional[dict] = None):
    a = get_assignment(assignment_id) if assignment_id else None
    row = {"time": now(), "session_id": session_id, "assignment_id": assignment_id, "class_id": a and a["class_id"],
           "student_id": student_id, "case_id": case_id, "stage": stage, "kind": kind, "fact_id": fact_id, "detail": detail or {}}
    with _lock, open(DATA / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _events() -> List[dict]:
    p = DATA / "events.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def analytics(class_id: str) -> dict:
    ev = [e for e in _events() if e.get("class_id") == class_id]
    lost = {}
    for e in ev:
        if e["kind"] == "fact_lost":
            k = (e["case_id"], e["fact_id"], e["stage"])
            lost[k] = lost.get(k, 0) + 1
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 3600))
    live = {}
    for e in ev:
        if e["time"] >= cutoff:
            k = (e["time"][:16], e["kind"])
            live[k] = live.get(k, 0) + 1
    mine = {a["id"]: a for a in assignments(class_id)}
    trend = [{"student_id": r["student_id"], "finished": r["finished"][:16], "carried_home": r["feedback"].get("carried_home"),
              "total_threads": r["feedback"].get("total_threads"), "title": mine[r["assignment_id"]]["title"]}
             for r in sorted(results(), key=lambda r: (r["student_id"], r["finished"])) if r["assignment_id"] in mine]
    totals = {}
    for e in ev:
        totals[e["kind"]] = totals.get(e["kind"], 0) + 1
    return {"engine": "files",
            "most_lost": [{"case_id": k[0], "fact_id": k[1], "stage": k[2], "losses": n} for k, n in sorted(lost.items(), key=lambda x: -x[1])][:12],
            "live": [{"t": k[0], "kind": k[1], "n": n} for k, n in sorted(live.items())],
            "trend": trend, "totals": totals}
