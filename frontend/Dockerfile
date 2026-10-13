FROM python:3.15-alpine3.24

# Build from this directory:
#   docker build -t invoice-upload ./frontend
#   docker build -t <registry>.azurecr.io/invoice-upload:v1 ./frontend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

COPY ./static /app/static

RUN addgroup -g 10001 -S appuser \
 && adduser -u 10001 -S -G appuser -h /home/appuser -s /sbin/nologin appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
