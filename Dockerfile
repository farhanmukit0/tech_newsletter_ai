FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite lives on a volume by default; point DATABASE_URL at Postgres in production.
ENV DATABASE_URL=sqlite:////data/data.db
VOLUME ["/data"]
EXPOSE 8000

# One worker: the in-process scheduler must run exactly once.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
