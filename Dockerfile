FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
# The client /install serves (client_dist.py); CLIENT_VERSION is `git describe`, gclaude's version.
COPY install.sh install.ps1 ./client/
COPY scripts/claude-gateway scripts/statusline.sh scripts/gclaude-sync.py ./client/scripts/
COPY scripts/windows ./client/scripts/windows
COPY examples/opencode ./client/examples/opencode
ARG CLIENT_VERSION=""
ENV CLAUDE_PROXY_CLIENT_DIR=/app/client CLAUDE_PROXY_CLIENT_VERSION=$CLIENT_VERSION
RUN useradd --system --home /data gateway && mkdir -p /data && chown gateway /data
USER gateway
ENV CLAUDE_PROXY_DB=/data/claude_proxy.db \
    CLAUDE_PROXY_HOST=0.0.0.0 \
    CLAUDE_PROXY_DASHBOARD_HOST=0.0.0.0
VOLUME /data
EXPOSE 8080 8081
# Required at run time: CLAUDE_PROXY_CREDENTIAL_KEY (or _KEY_FILE mounted read-only), optionally META_API_KEY.
CMD ["claude-proxy", "serve"]
