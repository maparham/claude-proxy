FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN useradd --system --home /data gateway && mkdir -p /data && chown gateway /data
USER gateway
ENV CLAUDE_PROXY_DB=/data/claude_proxy.db \
    CLAUDE_PROXY_HOST=0.0.0.0 \
    CLAUDE_PROXY_DASHBOARD_HOST=0.0.0.0
VOLUME /data
EXPOSE 8080 8081
# Required at run time: CLAUDE_PROXY_CREDENTIAL_KEY (or _KEY_FILE mounted read-only), optionally META_API_KEY.
CMD ["claude-proxy", "serve"]
