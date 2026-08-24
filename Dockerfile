FROM python:3.12-slim

# tzdata matters: every drop time in this bot is America/New_York wall clock,
# and the container needs the zone database to resolve it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/New_York

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 1000 james \
 && mkdir -p /app/state \
 && chown -R james:james /app
USER james

VOLUME ["/app/state"]

ENTRYPOINT ["james"]
CMD ["run", "--config", "/app/config.yaml"]
