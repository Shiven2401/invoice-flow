# Invoice Flow for a public host. Mount a persistent disk at /data: it holds accounts, invoices and logs.
FROM python:3.12-slim

# OpenCV, which the OCR engine loads, needs these system libraries
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY run.py ./
COPY app ./app
COPY static ./static
COPY data ./data
COPY samples ./samples

# Exactly one process: the invoice queue and login throttling live in memory.
# FORWARDED_ALLOW_IPS trusts the host's proxy for the visitor's address, so nothing else may reach this port.
ENV HOST=0.0.0.0 PORT=8000 RUNTIME_DIR=/data COOKIE_SECURE=1 FORWARDED_ALLOW_IPS=*
# Strangers share the one worker and the disk here, so the per-account limits are on (a local copy runs without them).
# Override in the host's settings if needed; 0 turns a limit off.
ENV MAX_QUEUED_PER_USER=25 MAX_RUNS_PER_USER=1000 MAX_UPLOAD_MB=25
EXPOSE 8000
CMD ["python", "run.py"]
