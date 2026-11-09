# Per-task MCP sidecar; no DeepSeek SDK, Engine admin key or Gateway admin key.
# The only credential it receives is a one-time, task-bound Bridge capability.
# Do not attach Agent workspace/home volumes or expose its port publicly.
FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN python -m pip install --no-cache-dir \
    "mcp==2.3.0" "httpx==0.28.1" "uvicorn==0.52.1"
WORKDIR /app
COPY seealpha_harness ./seealpha_harness
RUN useradd --uid 10003 --create-home --shell /usr/sbin/nologin mcpbridge
USER mcpbridge
ENV HOME=/home/mcpbridge
EXPOSE 8782
CMD ["uvicorn", "seealpha_harness.mcp_bridge:app", "--host", "0.0.0.0", "--port", "8782", "--workers", "1"]
