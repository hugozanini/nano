# Pinned base image so a deploy cannot silently change Python version.
# Cloud Build prefers a Dockerfile over buildpacks when one is present.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first: this layer is cached unless requirements change.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

# Run as a non-root user.
RUN useradd --create-home --uid 1000 nano && chown -R nano:nano /app
USER nano

# Cloud Run supplies PORT. One worker: concurrency is handled by the event loop
# for the webhook and by the threadpool for task processing, and a single
# instance is ample for a two-person household.
ENV PORT=8080
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1
