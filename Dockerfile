# Securifi - single-tenant deployment image (API + web UI + MCP over HTTP).
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels ".[postgres]"

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SECURIFI_HOST=0.0.0.0 SECURIFI_PORT=8000 \
    SECURIFI_DATABASE_URL=sqlite:////data/securifi.db
RUN useradd --create-home --uid 10001 securifi && mkdir /data && chown securifi /data
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
USER securifi
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status == 200 else 1)"
ENTRYPOINT ["securifi"]
CMD ["serve"]
