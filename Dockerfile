FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY account_monitor_web.py .
COPY config.example.yaml .

ENV AUTH_DIR=/app/data
ENV CLIPROXYAPI_MANAGEMENT_KEY=""
ENV PYTHONIOENCODING=utf-8

EXPOSE 8320

VOLUME ["/app/data"]

CMD ["python", "-u", "account_monitor_web.py", "--host", "0.0.0.0", "--port", "8320"]
