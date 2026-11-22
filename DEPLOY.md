# Put the invoice desk online (free, no card)

Hugging Face now charges for Docker and Gradio Spaces, so this uses **Render's
free web service** (Docker, no credit card per Render) with **SnapDeploy** as a
backup. Both deploy from a GitHub repository. About 20 minutes, mostly waiting
for the first build.

You need: a GitHub account, your Gemini API key. The key goes into the host's
environment settings, never into a file or the repository.

## 1. Put the code on GitHub

1. On github.com, create a **new repository** (for example `invoice-desk`).
   Private is fine.
2. Unzip `invoice-desk-deploy.zip`. On the new repository's page, choose
   **uploading an existing file**, drag in everything from the unzipped folder
   (`Dockerfile`, `requirements-app.txt`, `.dockerignore`, `README.md`,
   `DEPLOY.md` and the `apflow` folder), and commit.
3. Check that `Dockerfile` sits at the top level of the repository and that
   `apflow/web/static/fonts/` exists.

## 2. Deploy on Render

1. Sign in at render.com with your GitHub account.
2. **New > Web Service**, then pick the `invoice-desk` repository (grant
   Render access to it if asked).
3. Render detects the Dockerfile. Choose the **Free** instance type.
4. Under **Environment Variables**, add `GEMINI_API_KEY` with your key.
   Optional: `GROQ_API_KEY` (backup model), `GEMINI_MODEL` (pin a model name).
5. **Create Web Service.** The first build takes about 5 to 10 minutes. Your
   link is `https://<name>.onrender.com`.

If Render asks for a card before letting you pick the Free type, use
SnapDeploy instead (section 4).

## 3. Check it works

1. Open the link. The top-right corner should say *Scans are read by:
   Gemini (...)*. If it says *stand-in model*, the variable is missing or
   misnamed: fix it under **Environment** and redeploy.
2. **Dashboard > Test the model** sends two sample scans to Gemini and
   compares the reading with their answer keys, field by field. If it fails,
   the message says why (wrong key, quota, model name).
3. **Run all 20 samples** to fill the dashboard before you share the link or
   record the demo.

## 4. Backup: SnapDeploy

Sign up at snapdeploy.dev, create a container from the same GitHub repository
(it builds the Dockerfile), add `GEMINI_API_KEY` as an environment variable,
and deploy. The free tier gives 512 MB, which is enough (the app peaks around
200 MB while reading a scan).

## Good to know

- **The ERP.** The same service hosts Kaveri ERP at `/erp/`; the desk reads it over HTTP with a
  token (`APFLOW_ERP_URL=self` in the Dockerfile). Optionally set `APFLOW_ERP_TOKEN` as a secret;
  otherwise the ERP generates its own. Records testers add, and all runs, reset when the free
  service restarts after being idle.

- **Readers.** The public demo reads scans with the free Gemini tier. Private readers
  (NuExtract3, Qwen3-VL) need a machine you control: run the app on it and start
  `scripts/serve_reader.py` (see README). Render's free plan cannot host a model (512 MB).
  Anyone using the public link can switch the reader for everyone; set
  `APFLOW_READER_LOCKED=1` on Render if you want to stop that during your demo.

- **The free service sleeps after about 15 minutes without visits**, and the
  next visit takes up to a minute to wake it. Open the link a minute before
  you demo, and before the evaluators are likely to look, run the samples once.
- **Restarts clear the run history** (free hosts have no persistent disk).
  The 20 samples are built into the image, so **Run all 20 samples** brings
  the dashboard back in seconds.
- **Scans go to Gemini first**, and OCR is used only if Gemini fails. On a
  tenth of a CPU, OCR takes tens of seconds; Gemini a few. If Gemini fails,
  the scan is still read by OCR and held for a person with the reason shown.
- **Every Gemini answer is cached**, so re-running the same samples costs no
  quota. Text PDFs never call the model.
