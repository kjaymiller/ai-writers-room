# The writers room: the web page, the Temporal worker and the `room` CLI, in one image.
# Everything an agent role needs is here: Python and uv for the room, Node and pi for the agents,
# git for committing drafts, and the nevia CLI for proof-of-concept computers. No credentials are
# in the image: the gateway key comes from the environment at `docker compose up` (via fnox), and
# the Nevia key file is a compose secret.
FROM python:3.13-slim

# Pinned to the versions the room was built and tested with.
ARG PI_VERSION=1.4.0
ARG NEVIA_VERSION=0.1.14

COPY --from=ghcr.io/astral-sh/uv:0.11.23 /uv /uvx /usr/local/bin/
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g --no-fund --no-audit "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && curl -fsSL https://get.nevia.cloud | NEVIA_VERSION=$NEVIA_VERSION NEVIA_INSTALL_DIR=/usr/local/bin sh \
    && rm -rf /var/lib/apt/lists/* /root/.npm

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd -m -u 1000 room && mkdir -p /data /opt/blog-venv && chown room:room /data /opt/blog-venv
USER room

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    ROOM_CONFIG=/app/room.toml \
    ROOM_STATE=/data \
    ROOM_BLOG_PATH=/blog \
    ROOM_BLOG_VENV=/opt/blog-venv \
    ROOM_BLOG_BUILD="uv sync -q --frozen && uv run render-engine build --clean --module-site app:app" \
    ROOM_WEB_BIND=0.0.0.0 \
    ROOM_WEB_HOSTS=localhost:8250,127.0.0.1:8250 \
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=*

EXPOSE 8250
CMD ["room", "web"]
