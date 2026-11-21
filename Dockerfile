# Invoice desk. Runs on Render's free web service, SnapDeploy, or any container host.
FROM python:3.13-slim

# OCR (Tesseract, English data included) and PDF rendering (Poppler's pdftoppm).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1 \
    APFLOW_DATA=/home/user/data APFLOW_MODEL=auto PORT=8000
WORKDIR /home/user/app

COPY --chown=user requirements-app.txt .
RUN pip install --no-cache-dir --user -r requirements-app.txt
COPY --chown=user apflow ./apflow

# Build the 20 sample invoices now, so a small instance starts serving at once.
RUN python -c "from apflow.service import Service; Service('/home/user/data', model_mode='none')"

# The host sets PORT (Render uses 10000); the app listens on whatever it is given.
EXPOSE 8000
# Secrets go in the host's environment settings, never in this file: GEMINI_API_KEY, optional GROQ_API_KEY.
CMD ["python", "-m", "apflow.web.app"]
