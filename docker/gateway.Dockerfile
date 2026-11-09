# Trusted gateway is deliberately not the DeepSeek SDK runtime image.
# It contains no Agent code executor, Docker socket, or model credentials.
FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN python -m pip install --no-cache-dir \
    "fastapi==0.141.1" "httpx==0.28.1" "uvicorn==0.52.1"
WORKDIR /app
COPY seealpha_harness ./seealpha_harness
RUN useradd --uid 10002 --create-home --shell /usr/sbin/nologin gateway \
    && mkdir -p /gateway-state \
    && chown gateway:gateway /gateway-state
USER gateway
ENV GATEWAY_STATE_DIR=/gateway-state HOME=/home/gateway
EXPOSE 8780
CMD ["uvicorn", "seealpha_harness.gateway_api:app", "--host", "0.0.0.0", "--port", "8780", "--workers", "1"]
