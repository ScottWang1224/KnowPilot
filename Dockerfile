FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Single worker only: concurrency gates and per-user locks live in process memory.
# Behind Nginx: trust X-Forwarded-Proto/For so HTTPS redirects and login throttling see the real scheme and client.
# "*" is safe only because the host publishes the port on 127.0.0.1 (docker run -p 127.0.0.1:8000:8000).
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]