FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.py /app/server.py
COPY bridge.py /app/bridge.py
COPY runtime.py /app/runtime.py
COPY stations.json /app/stations.json
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
EXPOSE 8765
CMD ["python", "/app/bridge.py"]
