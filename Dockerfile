# Pinned to a specific Python minor version. The original used `python:3-alpine`,
# which floats to whatever the newest Python is on the day you rebuild.
FROM python:3.12-alpine

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RUNNING_IN_DOCKER=true

# Install dependencies first so this layer is cached across code edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# defaults/ ships in the repo; entrypoint seeds missing files from it into
# the bind-mounted data/ volume on start. data/ itself is live state and is
# never tracked -- a server-side `git reset --hard` cannot touch it.
RUN chmod +x /app/entrypoint.sh

# Graceful shutdown for plain `docker stop` too (compose sets it as well).
STOPSIGNAL SIGINT

ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
CMD ["python", "main.py"]
