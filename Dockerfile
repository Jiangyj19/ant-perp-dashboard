FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/backend DATA_DIR=/app/data CONFIG_PATH=/app/config/strategy.yaml
RUN groupadd -g 10001 monitor && useradd -u 10001 -g monitor -M monitor
WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./backend/
COPY config/ ./config/
COPY --from=frontend /build/dist/ ./frontend/dist/
RUN mkdir -p /app/data && chown -R monitor:monitor /app
USER monitor
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3)"
CMD ["uvicorn","antmonitor.app:create_app","--factory","--host","0.0.0.0","--port","8787","--no-access-log","--no-proxy-headers"]
