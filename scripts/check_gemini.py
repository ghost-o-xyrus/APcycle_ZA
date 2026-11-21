"""Run once with your real key to confirm Gemini works and measure how well it reads.

    GEMINI_API_KEY=... python scripts/check_gemini.py          (PowerShell: $env:GEMINI_API_KEY="..."; python scripts/check_gemini.py)

1. Lists the Flash models your key can use.
2. Reads the scanned samples plus the held-out layout D with Gemini and scores
   every field against the answer key. Uses about 10 requests of the free quota.
"""
import json
import os
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apflow.evaluate import FIELDS, score  # noqa: E402
from apflow.extract.gemini import from_env  # noqa: E402
from apflow.extract.routes import extract_llm_route  # noqa: E402
from apflow.generator import build  # noqa: E402
from apflow.scenarios import random_clean, scenarios  # noqa: E402

key = os.environ.get("GEMINI_API_KEY", "").strip()
if not key:
    sys.exit("Set GEMINI_API_KEY first.")

req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                             headers={"x-goog-api-key": key})
try:
    models = json.load(urllib.request.urlopen(req, timeout=30))["models"]
except Exception as e:
    sys.exit(f"Could not list models (bad key or no network): {e}")
flash = [m["name"].split("/")[-1] for m in models
         if "flash" in m["name"] and "generateContent" in m.get("supportedGenerationMethods", [])]
print("Flash models on this key:", ", ".join(flash) or "none")

c = from_env(cache_dir=os.path.join(tempfile.gettempdir(), "apflow_gemini_cache"))
out = tempfile.mkdtemp()
docs = [(s.id, s.spec) for s in scenarios() if s.spec.scanned]
docs += [(f"D-{seed}", random_clean(seed, layout="D", scanned=False)) for seed in (2001, 2002, 2003)]
docs += [(f"scan-{seed}", random_clean(seed, layout=l, scanned=True)) for seed, l in ((2101, "A"), (2102, "C"))]
total, ok = 0, 0
for name, spec in docs:
    pdf, truth = build(spec, out, name)
    try:
        ex = extract_llm_route(pdf, c)
    except Exception as e:
        print(f"{name:10s} FAILED: {e}")
        continue
    r = score(truth, ex)
    good = sum(r[f] for f in FIELDS)
    total += len(FIELDS)
    ok += good
    wrong = [f for f in FIELDS if not r[f]]
    print(f"{name:10s} {c.last_model:22s} fields {good}/{len(FIELDS)}  lines {r['_lines_hit']}/{r['_lines_n']}"
          + (f"  wrong: {', '.join(wrong)}" if wrong else ""))
if total:
    print(f"\nGemini field accuracy: {ok / total:.1%} on {total // len(FIELDS)} documents")
