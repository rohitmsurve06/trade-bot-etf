# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — dependency builder
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps needed to compile some wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --prefix=/install --no-cache-dir -r requirements.txt

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — lean runtime image
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL maintainer="Value Averaging Bot"
LABEL description="Automated dip-buying bot for Indian ETFs via IBKR"

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy source code
COPY main.py        ./main.py
COPY src/           ./src/
COPY data/          ./data/

# Create a non-root user for security
RUN addgroup --system botgroup \
 && adduser  --system --ingroup botgroup botuser \
 && chown -R botuser:botgroup /app

USER botuser

# ── Environment defaults (override via --env-file or -e flags) ───────────────
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO \
    IB_HOST=host.docker.internal \
    IB_PORT=4002 \
    IB_CLIENT_ID=1

# ── Health check: verify Python can import main modules ──────────────────────
HEALTHCHECK --interval=60s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "from src.logic import load_investments; from src.ib_manager import IBManager; print('OK')"

# Run the daily job once and exit (GitHub Actions triggers the schedule)
ENTRYPOINT ["python", "main.py"]
