# apflow: invoice to decision (Zamp ASA case study, PS-1)

Takes one invoice PDF (text or scanned) and returns **APPROVE / HOLD / REJECT**,
with every stage logged and every reason backed by evidence. The AI model reads
documents; deterministic checks decide.

Status: parts 1-6 of the plan are built (test company + generator, extraction,
checks + decision, run store, interface, real vision model + deployment).
See DEPLOY.md to put it online (Render free web service, no card).

## Run

```bash
pip install -r requirements.txt           # + tesseract-ocr and poppler-utils on the system
python -m apflow.web.app                  # the invoice desk at http://localhost:8000
python -m pytest                          # 249 tests, ~70 s (browser tests skip without Chrome)
PYTHONPATH=. python scripts/run_scenarios.py -v   # 20 golden scenarios end to end
python -m apflow.evaluate                 # extraction accuracy by route and layout
```

## Layout

| Path | What it does |
| --- | --- |
| `apflow/config.py` | Every threshold, with the reason for its value |
| `apflow/gstin.py` | GSTIN checksum (verified on real GeM GSTINs) and OCR repair |
| `apflow/company.py` | The invented company: vendors, POs, goods receipts, invoice history |
| `apflow/generator.py` | Invoice PDFs in 4 layouts (D is held out) + scan simulation + ground truth |
| `apflow/scenarios.py` | 20 scenarios with expected decision and expected failed checks |
| `apflow/extract/parser.py` | Label-anchored header parser; line rows accepted only if qty x rate = amount |
| `apflow/extract/routes.py` | Text / OCR / vision-model routes and the router that escalates |
| `apflow/checks.py` | Vendor + PO resolution and checks C01-C15, X01 |
| `apflow/decide.py` | Decision policy (no AI) |
| `apflow/pipeline.py` | One run end to end, one event per stage |
| `apflow/evaluate.py` | Field-level extraction accuracy against ground truth |
| `apflow/models.py` | Gemini client (model fallback list, one retry on rate limit, image downscaling), Groq backup, cache |
| `apflow/store.py` | SQLite run store: runs, stage events, checks, human actions; feeds approved runs back as invoice history; dashboard metrics |
| `apflow/service.py` | Runs in background threads, samples, uploads (validated), human actions (approve, reject, part-approve, confirm PO) |
| `apflow/web/app.py` | Flask API + live stage stream (server-sent events) |
| `apflow/web/static/` | The interface: desk (live run, stamp, decision, details) and dashboard; bundled OFL fonts |

## Interface

- **Desk:** pick a sample or upload a PDF; stages appear as they finish (real timings shown); the decision lands as a stamp on the document. Holds show the owner, the one question to answer, and the actions that fit (confirm the inferred PO, approve the part within the PO balance, approve anyway with a reason, reject with a message for the vendor).
- **Dashboard:** invoices processed, share approved with no person involved, value waiting on a person, typical time to a decision, why invoices were held, who needs to act, and every run (select one to replay it on the desk).
- **Memory:** approved runs become invoice history, so the same invoice uploaded twice is rejected and PO balances include what the desk already approved.
- **Model:** with `GEMINI_API_KEY` set (free tier works; `GROQ_API_KEY` optional as backup), every scan and unfamiliar layout is read by the real model, and every answer is cached so repeat demo runs cost no quota. Without a key, the bundled sample scans are read by a clearly labelled stand-in that returns their answer key, and uploaded scans are read by OCR and held for a person. **Dashboard > Test the model** scores the real model on two scans against their answer keys.

## Results (this build)

- Golden scenarios: 20/20 exact (decision and the exact set of failed checks).
- Extraction, text route, layouts A-C: 100% fields, 100% lines (24 invoices, strict scoring).
- Held-out layout D: 37.5% header fields, 100% lines; escalated to the model 100% of the time.
- OCR route on simulated scans: 79-86% fields; escalated 100% of the time. Without a model, scans are never auto-approved (tested).
- Mutation tests: 10 injected engine bugs caught 10/10; 10 injected store/service/API/UI bugs caught 10/10.
- Browser end-to-end (Playwright + Chrome): 8 flows, including confirm-PO, override-needs-reason, upload error and dashboard drill-down.

## Not verified from the build sandbox

The build sandbox has no internet, so the model clients are tested against a
fake network (request shape, fallbacks, retries, cache, failures). The real key
is verified on the deployed app with **Test the model**.
