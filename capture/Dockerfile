FROM python:3.12-slim

WORKDIR /srv

# Build context is the repo root (see compose.yaml).
COPY capture/requirements.txt /srv/capture/requirements.txt
RUN pip install --no-cache-dir -r /srv/capture/requirements.txt

COPY capture/ /srv/capture/

ENV CAPTURE_DB_PATH=/data/capture.db \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data && chown 1000:1000 /data
USER 1000:1000

EXPOSE 8000
CMD ["uvicorn", "--factory", "capture.web:make_app", "--host", "0.0.0.0", "--port", "8000"]
