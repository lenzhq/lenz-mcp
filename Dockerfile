# The Lenz MCP server's image: the server and its built card, nothing else.
#
#   docker build --build-arg LENZ_MCP_VERSION=2.0.0 -t lenz-mcp .
#   docker run -p 8080:8080 -e FRONTEND_URL=https://lenz.io lenz-mcp
#
# Dependencies are installed from uv.lock with their hashes, so an image built
# from a commit installs exactly the packages the tests ran against. Base images
# are pinned by digest. The server runs as an unprivileged user.
FROM python:3.11.16-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.11.2@sha256:c4f5de312ee66d46810635ffc5df34a1973ba753e7241ce3a08ef979ddd7bea5 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --system --require-hashes --no-deps -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt pyproject.toml uv.lock

# The package, less what only development needs: the card's sources, tests and
# fixtures (the server serves the committed dist/ bundles) and the test harness.
COPY src/lenz_mcp lenz_mcp
RUN cd lenz_mcp \
    && rm -rf testing.py card/node_modules card/src card/tests card/fixtures card/dist-dev \
       card/build.mjs card/package.json card/package-lock.json card/.gitignore \
    && useradd --system --no-create-home --uid 10001 mcp
USER 10001

# What `serverInfo.version` reports. Unset reports `dev`.
ARG LENZ_MCP_VERSION=
ENV APP_VERSION=${LENZ_MCP_VERSION} \
    PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn lenz_mcp.asgi:application --host 0.0.0.0 --port ${PORT}"]
