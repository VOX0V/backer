FROM python:3.11-slim

WORKDIR /app

# zip natif pour les archives, curl pour le healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    zip \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Utilisateur non-root, UID/GID 1000:1000 pour matcher les permissions
# des volumes montés sur DebServ (pas de privileged, pas de root).
RUN groupadd -g 1000 backer && useradd -u 1000 -g backer -m backer \
    && mkdir -p /app/data \
    && chown -R backer:backer /app
USER backer

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:5000/health || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--timeout", "120", "app:app"]
