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
python -m pytest                          # 442 tests, ~3 min (browser tests skip without Chrome)
PYTHONPATH=. python scripts/run_scenarios.py -v   # 25 golden scenarios end to end
python -m apflow.evaluate                 # extraction accuracy by route and layout
```

## Layout

| Path | What it does |
| --- | --- |
| `apflow/config.py` | Every threshold, with the reason for its value |
| `apflow/erp/` | Kaveri ERP: database (`db.py`), NetSuite-style API with token (`api.py`), screens (`web.py`, `static/`), PO and receipt reading (`reader.py`) |
| `apflow/erp_client.py` | How the desk reads the ERP: over HTTP with the token, or directly |
| `apflow/taxid.py` | EIN format, EU VAT ID formats, German and French VAT check digits |
| `apflow/gstin.py` | GSTIN checksum (verified on real GeM GSTINs) and OCR repair |
| `apflow/company.py` | The invented company: vendors, POs, goods receipts, invoice history |
| `apflow/generator.py` | Invoice PDFs in 4 layouts (D is held out) + scan simulation + ground truth |
| `apflow/scenarios.py` | 20 scenarios with expected decision and expected failed checks |
| `apflow/extract/parser.py` | Label-anchored header parser; line rows accepted only if qty x rate = amount |
| `apflow/extract/routes.py` | Text / OCR / vision-model routes and the router that escalates |
| `apflow/checks.py` | Vendor + PO resolution and checks C01-C16, X01 |
| `apflow/decide.py` | Decision policy (no AI) |
| `apflow/pipeline.py` | One run end to end, one event per stage |
| `apflow/evaluate.py` | Field-level extraction accuracy against ground truth |
| `apflow/models.py` | Gemini client (model fallback list, one retry on rate limit, image downscaling), Groq backup, cache |
| `apflow/store.py` | SQLite run store: runs, stage events, checks, human actions; feeds approved runs back as invoice history; dashboard metrics |
| `apflow/service.py` | Runs in background threads, samples, uploads (validated), human actions (approve, reject, part-approve, confirm PO) |
| `apflow/web/app.py` | Flask API + live stage stream (server-sent events) |
| `apflow/local_readers.py` | Private readers: finds vision models on this machine (registered or on usual ports), NuExtract3 and Qwen3-VL clients |
| `scripts/serve_reader.py` | Starts a private reader on a free port, on the least busy GPU, lets the desk page call it, and registers it (standard library only; also served at `/downloads/serve_reader.py`) |
| `apflow/relay.py`, `apflow/web/static/local-model.js` | A model on the viewer's computer, reached through their open desk page |
| `apflow/money.py` | Rupee amounts in Indian grouping (₹1,47,500.00) |
| `apflow/web/static/` | The interface: desk (live run, stamp, decision, details) and dashboard; bundled OFL fonts |

## The ERP: where POs and goods receipts come from

An AP product does not keep its own POs: it reads the client's ERP (NetSuite, SAP S/4HANA).
**Kaveri ERP** (`/erp/`) stands in for that system of record:

- Its own database and documents: vendor master, purchase orders (with printed PO PDFs) and goods
  receipts (with printed receipt notes); one real public GeM contract kept for reference.
- An integration API shaped like NetSuite's REST records: `vendor`, `purchaseOrder`, `itemReceipt`,
  and `POST purchaseOrder/{id}/!transform/itemReceipt` to receive goods. Every call needs
  `Authorization: Bearer <token>`; without it, 401.
- Screens for Purchasing and Stores: upload a PO or a goods receipt PDF, the ERP reads it into a
  draft, a person checks and saves it. Records added there are marked "tester" and can be removed.

The desk reads vendors, POs and receipts on every run (`apflow/erp_client.py`). Deployed, it calls
the ERP over HTTP with the token (`APFLOW_ERP_URL=self`, set in the Dockerfile); in tests it can read
the ERP's database directly. Connecting real NetSuite or SAP means writing one client that returns the
same snapshot. The three-way match tab shows each invoice line beside its PO line and every goods
receipt against it, with links to both documents; "Check again" re-runs a held invoice once the ERP
has the missing PO or delivery.

To test with your own invoice: in the ERP add the vendor (if new), upload the PO, record the goods
receipt; then upload the invoice at the desk.

## Countries

The buyer is a group: Kaveri Instruments Pvt Ltd (India, GSTIN, rupees), Kaveri Instruments Inc.
(US, EIN, dollars) and Kaveri Instruments GmbH (Germany, VAT ID, euros). The billed company comes
from the PO, the buyer's tax number or the buyer's name, and the tax rules follow where the
supplier and the billed company are:

| Supplier to company | Rule |
| --- | --- |
| India to India | GST: CGST + SGST within a state, IGST across states; GSTIN checksums |
| Abroad to India | Import: no GST on the supplier's invoice (IGST at customs or reverse charge) |
| US to US | Sales tax as on the PO; none charged = self-assess use tax |
| Same EU country | Domestic VAT expected |
| EU to another EU country | Reverse charge: no VAT on the invoice; VAT charged is held |
| Anything else | Import: no tax on the invoice |

Tax numbers: GSTIN checksum, EIN format, EU VAT format, German and French VAT check digits.
EU invoices must carry the supplier's VAT ID. Currency must match the PO (or the vendor
record). One approval limit in rupees for the group, at fixed demo rates (USD 84, EUR 91).
Scenarios I1-I5 cover a US purchase, an import into India in euros (manager sign-off after
conversion), VAT charged where reverse charge applies, a euro PO billed in dollars, and an EU
invoice without the supplier's VAT ID. `invoice-desk-india-only.zip` is the India-only build.

## Interface

- **Desk:** pick a sample or upload a PDF; stages appear as they finish (real timings shown); the decision lands as a stamp on the document. Holds show the owner, the one question to answer, and the actions that fit (confirm the inferred PO, approve the part within the PO balance, approve anyway with a reason, reject with a message for the vendor).
- **Dashboard:** invoices processed, share approved with no person involved, value waiting on a person, typical time to a decision, why invoices were held, who needs to act, and every run (select one to replay it on the desk).
- **Memory:** approved runs become invoice history, so the same invoice uploaded twice is rejected and PO balances include what the desk already approved.
- **Model:** with `GEMINI_API_KEY` set (free tier works; `GROQ_API_KEY` optional as backup), every scan and unfamiliar layout is read by the real model, and every answer is cached so repeat demo runs cost no quota. Without a key, the bundled sample scans are read by a clearly labelled stand-in that returns their answer key, and uploaded scans are read by OCR and held for a person. **Dashboard > Test the reader** scores the current reader on two scans against their answer keys.

## Private readers (no cloud)

Scanned invoices can be read by an open model instead of a cloud API. Two are supported by name,
both about 4B parameters and Apache-2.0:

- **NuExtract3** (Numind): built for template-based extraction. The invoice template goes in
  `chat_template_kwargs`, as its model card says. The default; runs best under llama.cpp.
- **Qwen3-VL 4B Instruct**: a general vision model; gets the cloud prompt. Runs best under Ollama.

Any other vision model on the same kinds of server is offered too (labelled "Vision model").
Step-by-step instructions for testers are on the deployed site itself: **How it works, part 3**
(`/#/how/models`), with the launcher downloadable at `/downloads/serve_reader.py`.

**Hosted desk, model on your computer (the browser relay).** A hosted desk cannot reach a
tester's laptop, but the desk page open on that laptop can. The page searches `127.0.0.1`
(ports 8100-8120 and the usual model ports) only when the person presses *Find models on my
computer*; Chrome asks permission first (Local Network Access). Once connected, the server
queues each model request (`apflow/relay.py`); the page long-polls `/api/relay/next`, calls the
local model and posts the answer to `/api/relay/answer`. Only the page that connected (it holds
a token) receives requests. If no page is connected, a run falls back to OCR at once; a model
that has not answered in 10 minutes (`APFLOW_RELAY_TIMEOUT`) does the same. The model server
must allow the desk's origin (CORS): the launcher does this for Ollama (`OLLAMA_ORIGINS`) and
vLLM (`--allowed-origins`); llama.cpp allows any origin by default. The PDF itself is still
uploaded to the desk; for full privacy run the desk on your own machine.

```bash
python scripts/serve_reader.py                     # NuExtract3 on the first free port from 8100
python scripts/serve_reader.py --model qwen3-vl    # Qwen3-VL 4B
python scripts/serve_reader.py --origin http://localhost:8000   # a desk you run yourself
python scripts/serve_reader.py --dry-run           # show the plan only
python scripts/serve_reader.py --list --prune      # registered readers; drop dead ones
```

The launcher does not assume an idle machine. It skips ports that are taken (and moves on if
another program grabs the port while the model loads), runs vLLM only on the GPU with the most
free memory and claims only that free memory, and otherwise uses llama.cpp (`llama-server`) or
its own Ollama on a free port (an Ollama already running is left alone). It allows the desk's
page to call the model, checks that a browser would be allowed (CORS) and says so, registers the
server in `~/.apflow/readers`, and removes the entry on Ctrl+C.

**Self-hosted desk next to the models.** When the desk runs on the same machine or network as
the models, "the host" and "your computer" are the same, and the server can look for them itself:
set `APFLOW_SERVER_READERS=1` and a *Find models on the desk's server* control appears. It is off
on the hosted demo, where it would only search the host's own machine.

| Env | Effect |
| --- | --- |
| `APFLOW_SERVER_READERS=1` | let the server search its own machine for models (self-hosted only) |
| `APFLOW_READER=local` | start on the first private model the server finds (or give an address); implies the above |
| `APFLOW_READER_HOSTS=gpu-box` | allow server-side model connections to other hosts; by default only this machine, so a public deployment cannot be used to probe a network |
| `APFLOW_READERS_DIR` | registry folder (default `~/.apflow/readers`) |
| `APFLOW_READER_LOCKED=1` | nobody can switch the reader from the interface (also blocks the browser relay) |
| `APFLOW_LOCAL_TIMEOUT` | seconds per page read on a server-side reader (default 300) |
| `APFLOW_RELAY_TIMEOUT` | seconds to wait for a model on the viewer's computer (default 600) |

Tested against stand-in servers that speak the vLLM/llama.cpp and Ollama APIs, including in a
real Chrome talking to a CORS-enabled stand-in on 127.0.0.1; the real models were not run in
the build sandbox (no internet, no GPU).

## Results (this build)

- Golden scenarios: 25/25 exact (decision and the exact set of failed checks), 5 of them outside India.
- Extraction, text route, layouts A-C: 100% fields, 100% lines (24 invoices, strict scoring); Western layout X: 100% fields, 100% lines (8 invoices), 93% fields from scans.
- Held-out layout D: 37.5% header fields, 100% lines; escalated to the model 100% of the time.
- OCR route on simulated scans: 79-86% fields; escalated 100% of the time. Without a model, scans are never auto-approved (tested).
- Mutation tests: 10 injected engine bugs caught 10/10; 10 injected store/service/API/UI bugs caught 10/10.
- Browser end-to-end (Playwright + Chrome): 8 flows, including confirm-PO, override-needs-reason, upload error and dashboard drill-down.

## Real documents (DocILE, 22 Nov)

25 real business documents from the DocILE dataset (12 tax invoices, a utility bill, a receipt, a
credit note, a debit note, a proforma, a purchase order, a sales order, an order form, 5 unlabelled;
mostly US media invoices in dollars), scored against DocILE's own labels with
`scripts/eval_docile.py <folder> out.json none|auto`.

Without a model (text layer and OCR only):
- Decisions: 22 held, 3 rejected, none approved. The 3 rejections are the order confirmation,
  the purchase requisition and the sales order: none is an invoice.
- Document type: 18 of 20 labelled documents right; the other 2 are "unknown" and held.
- Header fields read: invoice number 5/14, date 7/16, total 4/17, line amounts 9/152. These
  US layouts are unlike the Indian layouts the rules were written for; such files are meant
  to go to the model (run with `auto` and a key to score it).

Bugs this found and fixed:
- Real tax invoices carrying an "Estimate #" field were rejected as quotations. Documents are
  now classified by their title, not by words used as field labels; receipts ("thank you for
  your payment") and debit notes are recognised; "THIS IS NOT A CREDIT MEMO" is not a credit memo.
- A scan guessed (by OCR) to be a quotation never reached the model. Only a confident,
  text-layer classification now skips the model.
- Amounts in dollars or yen were read as rupees. New check C16 holds any non-rupee invoice.
- US dates (9/20/2020, 01/11/99, MAY 21, 1999), "Inv #" numbers, "Amount Due" totals and
  doubled bold text ("AAmmeerriiccaa") were missed; vendor names like "Page 1 of 2" or
  "Please Remit To:" were taken as names.
- A person could "approve anyway" an invoice from a vendor not in the vendor master, or one with
  no readable number or total, and an exact copy of an approved file was not caught when its
  number could not be read. Approval now needs an onboarded vendor and the required fields, and
  an exact copy of an approved file is rejected whatever can be read from it.

## Not verified from the build sandbox

The build sandbox has no internet, so the model clients are tested against a
fake network (request shape, fallbacks, retries, cache, failures). The real key
is verified on the deployed app with **Test the model**.

## Robustness (adversarial campaign, 21 Nov)

69 documents through the full pipeline, none crashing: 25 real RVL-CDIP invoice-class scans,
20 FATURA layouts, 10 public GeM procurement PDFs, and 14 hostile files (password-protected,
zero pages, damaged, 50 pages, bundled invoices, sideways and upside-down scans, a blank page,
HTML in vendor text). No real-world document is approved; unreadable files are rejected with a
message the vendor can act on; anything not fully read goes to a person. Each case is a test in
`tests/test_robustness.py`.
