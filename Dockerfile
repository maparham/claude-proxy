FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md* ./
COPY src ./src
RUN pip install --no-cache-dir -e .
COPY config.example.toml ./config.example.toml
EXPOSE 8080 8081
CMD ["uvicorn", "claude_proxy.app:create_app", "--host", "0.0.0.0", "--port", "8080"]
