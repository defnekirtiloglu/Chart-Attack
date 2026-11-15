"""Tests the multi-model checks with fake models (no keys needed): handoff disagreement, pharmacist catch, back-translation flag."""
import sys; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import llm, engine
# Fake models: Gemini and Claude disagree on one fact; interpreter turns pounds into kilos.
def g_json(prompt, temperature=0.2):
    if "handoff note" in prompt: return {"fact_ids": ["breathless", "skips_diuretic", "wife_pills"]}
    return None
def c_json(prompt, system=""):
    if "handoff note" in prompt: return {"fact_ids": ["breathless", "skips_diuretic"]}
    if "Did any instruction change" in prompt: return {"same_meaning": False, "changes": ["2 pounds became 1 kilo"]}
    return None
def c_text(prompt, system="", **k):
    if "Translate this Turkish" in prompt: return "Weigh him every morning. Call if he gains 1 kilo in a day."
    return None
def g_text(prompt, temperature=0.8):
    if "Translate into natural" in prompt: return "Her sabah tartın. Bir günde 1 kilo alırsa arayın."
    return None
llm.ask_json, llm.claude_json, llm.claude_text, llm.ask_text = g_json, c_json, c_text, g_text
s = engine.start()
for q in ["What brings you in?", "No judgment, lots of people skip it. Do you ever miss the water pill?", "Who helps with your pills at home?"]:
    engine.ed_chat(s, q)
h = engine.do_handoff(s, "72M short of breath, skips furosemide on bowling days, wife does pill box.")
print("judges:", h["judges"], "| handed:", h["handed_off"], "| unclear:", h["unclear"])
print("pharmacist caught:", h["pharmacist"]["caught"], "| plan has stop_ibuprofen:", any(p["id"]=="stop_ibuprofen" for p in h["plan"]))
d = engine.discharge_chat(s, "Weigh him every morning. Call if he gains 2 pounds in a day.")
print("translation:", d["translation_check"]["status"], d["translation_check"]["issues"])
print("weigh_daily clear?", next(p for p in d["points"] if p["id"]=="weigh_daily")["clear"])
engine.do_teachback(s); f = engine.feedback(s)
for t in f["threads"]: print(" ", t["id"], "lost_at:", t["lost_at"], "rescued_by:", t["rescued_by"])
print("translation flags:", len(f["translation_flags"]))
