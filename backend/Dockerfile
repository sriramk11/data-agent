# Backend API service. Note: this container talks to Docker (to launch
# sandbox containers) via the host's Docker socket -- see docker-compose.yml
# for the bind mount. That socket access is itself a privilege boundary
# worth calling out: anything that can reach it can launch arbitrary
# containers, so in production this service should run on a host dedicated
# to it, not shared with untrusted workloads.
FROM python:3.12-slim

# Docker CLI so the backend can `docker run` sandbox containers.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
