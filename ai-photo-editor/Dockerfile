FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ server/
COPY web/ web/

# Platforms inject the port they want; 8000 is the local default.
ENV PORT=8000
EXPOSE 8000

# One worker on purpose: uploaded images live in this process's memory, so a
# second worker would answer requests for images it has never seen.
CMD ["sh", "-c", "python -m uvicorn server.app:app --host 0.0.0.0 --port ${PORT} --workers 1"]
