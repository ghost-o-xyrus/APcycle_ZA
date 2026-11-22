# Invoice desk. Runs on Render's free web service, SnapDeploy, or any container host.
FROM python:3.13-slim

# OCR (Tesseract, English data included) and PDF rendering (Poppler's pdftoppm).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1 \
    APFLOW_DATA=/home/user/data APFLOW_MODEL=auto PORT=8000 APFLOW_ERP_URL=self
WORKDIR /home/user/app

COPY --chown=user requirements-app.txt .
RUN pip install --no-cache-dir --user -r requirements-app.txt
COPY --chown=user apflow ./apflow
# The launcher testers download from the desk (How it works, part 3) to run a model on their own computer.
COPY --chown=user scripts/serve_reader.py ./scripts/serve_reader.py

# Build the sample invoices and load the ERP's records (POs, goods receipts, their documents) now,
# so a small instance starts serving at once. The desk reads the ERP over HTTP with its token (APFLOW_ERP_URL=self).
RUN python -c "from apflow.service import Service; Service('/home/user/data', model_mode='none')"

# The host sets PORT (Render uses 10000); the app listens on whatever it is given.
EXPOSE 8000
# Secrets go in the host's environment settings, never in this file: GEMINI_API_KEY, optional GROQ_API_KEY,
# optional APFLOW_ERP_TOKEN (otherwise the ERP generates one and keeps it with its data).
CMD ["python", "-m", "apflow.web.app"]
