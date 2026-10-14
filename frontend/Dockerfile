FROM dhi.io/python:3.14

# Build from this directory:
#   docker build -t invoice-upload ./frontend
#   docker build -t <registry>.azurecr.io/invoice-upload:v1 ./frontend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app

COPY static /app/static

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
