FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_PATH=/mcp

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser

COPY --chown=appuser:appuser server.py ./server.py
COPY --chown=appuser:appuser launcher.py ./launcher.py

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os,socket; s=socket.create_connection(('127.0.0.1', int(os.getenv('PORT', os.getenv('MCP_PORT','8000')))), 2); s.close()"

CMD ["python", "launcher.py"]
