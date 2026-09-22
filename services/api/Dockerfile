FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY packages ./packages
RUN pip install --upgrade pip && pip install -e .

COPY migrations ./migrations
COPY data ./data

EXPOSE 8000
CMD ["uvicorn", "boro_gtm.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
