FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies first so code edits do not invalidate the install layer.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
      "fastapi>=0.115" \
      "uvicorn[standard]>=0.32" \
      "python-multipart>=0.0.9" \
      "azure-identity>=1.19" \
      "azure-storage-blob>=12.24" \
      "azure-data-tables>=12.4" \
      "azure-ai-documentintelligence>=1.0" \
      "pydantic-settings>=2.6"

COPY app ./app

# Run unprivileged.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 3333
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3333/healthz', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3333"]
