FROM python:3.9-slim-buster

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app /app

# Install Gunicorn
RUN pip install gunicorn

# Install curl for health checks (if needed) and lsof for cleanup
RUN apt-get update && apt-get install -y --no-install-recommends curl lsof && rm -rf /var/lib/apt/lists/*

EXPOSE 5000

# When used with Kubernetes, we'll start workers with Kubernetes Jobs, not gunicorn
CMD ["gunicorn", "--workers", "4", "--bind", "0.0.0.0:5000", "app:app"]