# ScrapeClaude: Python API + the official Claude Code CLI.
# Chromium runs in the separate "browser" service (docker-compose.yml), not in this image.
FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.303

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    DISABLE_AUTOUPDATER=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Claude Code CLI (used when LLM_PROVIDER=claude_cli, i.e. your Claude subscription). Pinned version.
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} && npm cache clean --force

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv $VIRTUAL_ENV \
 && pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as an unprivileged user
RUN userdel -r node 2>/dev/null; useradd -m -u 1000 scraper && mkdir -p /app/data && chown -R scraper /app
USER scraper

ENV DB_PATH=/app/data/scrapeclaude.db
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
