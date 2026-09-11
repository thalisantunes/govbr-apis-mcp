# Reproducible build for MCP directory indexers (Glama) and for running the
# server in a container. The server speaks stdio, so run it with -i.
FROM python:3.12-slim

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .

ENTRYPOINT ["govbr-apis-mcp"]
