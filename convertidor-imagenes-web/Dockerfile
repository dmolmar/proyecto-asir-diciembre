FROM python:3.9-slim-buster

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app /app

# Install curl for health checks (if needed) and lsof for cleanup
RUN apt-get update && apt-get install -y --no-install-recommends curl lsof telnet && rm -rf /var/lib/apt/lists/*

EXPOSE 5000

# We are using Kubernetes Jobs to manage workers, not Gunicorn
CMD ["python", "app.py"]