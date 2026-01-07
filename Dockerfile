FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir gunicorn \
    && python -m playwright install --with-deps chromium

COPY . .

EXPOSE 5000

CMD ["sh", "-c", "gunicorn -w 2 -k gthread -t 120 -b 0.0.0.0:${PORT} --chdir python main:app"]
