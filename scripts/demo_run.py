"""Plays one full session and saves every API response to samples/ for the front end."""
import json, sys
from pathlib import Path
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app import app

c = TestClient(app); out = Path(__file__).resolve().parents[1] / "samples"
def save(name, r):
    assert r.status_code == 200, (name, r.status_code, r.text)
    (out / f"{name}.json").write_text(json.dumps(r.json(), indent=2, ensure_ascii=False)); return r.json()

s = save("01_start", c.post("/session", json={}))
sid = s["session_id"]
for i, q in enumerate(["Hi Mr. Brennan, what brings you in today?",
                       "Any swelling in your legs or ankles?",
                       "Do you take your water pill every day?",
                       "No judgment at all, a lot of people skip it. Do you ever miss the water pill?",
                       "Who helps you with your pills at home?",
                       "What do you usually eat?"], 1):
    r = save(f"02_ed_chat_{i}", c.post(f"/session/{sid}/chat", json={"text": q}))
    print("ED", i, "| new:", [f["id"] for f in r["new_facts"]], "| dodged:", r["dodged"])
save("03_state_after_ed", c.get(f"/session/{sid}"))
h = save("04_handoff", c.post(f"/session/{sid}/handoff", json={"text":
    "S: 72M with heart failure, 5 days worse breathing, swollen ankles. B: skips his water pill furosemide on bowling days. "
    "Wife Ayse fills his pill box and reads little English, speaks Turkish. A: CHF exacerbation. R: please see."}))
print("handed:", h["handed_off"]); print("missing:", [m["id"] + " (" + m["why"] + ")" for m in h["missing_critical"]])
print("plan:", [p["id"] for p in h["plan"]])
d = save("05_discharge_chat", c.post(f"/session/{sid}/chat", json={"text":
    "Weigh him every morning after the bathroom before breakfast and write it down. Call us if his weight goes up 2 pounds in a day or 5 pounds in a week. "
    "He must take the water pill every single morning, even on bowling days, he can take it after bowling."}))
print("points:", [(p["id"], p["clear"]) for p in d["points"]])
t = save("06_teachback", c.post(f"/session/{sid}/teachback"))
print("understood:", t["understood"], "missed:", t["missed"])
f = save("07_feedback", c.get(f"/session/{sid}/feedback"))
for th in f["threads"]: print(" ", th["id"], "-> lost at", th["lost_at"])
print("errors:", c.post(f"/session/{sid}/chat", json={"text": "hi"}).status_code, c.get("/session/nope").status_code)
