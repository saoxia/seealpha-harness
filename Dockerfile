FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY seealpha_harness ./seealpha_harness
RUN python -m pip install --no-cache-dir .
RUN useradd --uid 10001 --create-home --shell /bin/bash research \
    && mkdir -p /state \
    && chown research:research /state
USER research
ENV HARNESS_STATE_DIR=/state HOME=/home/research
EXPOSE 8777
CMD ["uvicorn", "seealpha_harness.api:app", "--host", "0.0.0.0", "--port", "8777", "--workers", "1"]
