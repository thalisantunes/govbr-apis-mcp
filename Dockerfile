# Reproducible build for MCP directory indexers (Glama) and for running the
# server in a container. The server speaks stdio, so run it with -i.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN pip install --no-cache-dir .

# Unprivileged user; its home holds ~/.cache/govbr-apis-mcp (OpenAPI spec cache).
RUN useradd --system --uid 10001 --create-home app
USER app

ENTRYPOINT ["govbr-apis-mcp"]
