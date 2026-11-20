FROM python:3.15-slim
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY schema.sql ./
COPY schema ./schema
RUN uv sync --frozen --no-dev --extra web --extra mcp  # one image: the API, the web app and /mcp
RUN useradd --system --create-home --uid 10001 dayfile && mkdir /data && chown dayfile /data
USER dayfile
ENV DAYFILE_DB=/data/dayfile.db PATH="/app/.venv/bin:$PATH"
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
CMD ["uvicorn", "dayfile.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
