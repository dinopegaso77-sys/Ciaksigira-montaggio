FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# caratteri stile ciak (gesso + etichette)
RUN mkdir -p /fonts \
    && curl -fsSL -o /fonts/Kalam-Bold.ttf https://github.com/google/fonts/raw/main/ofl/kalam/Kalam-Bold.ttf \
    && curl -fsSL -o /fonts/BebasNeue-Regular.ttf https://github.com/google/fonts/raw/main/ofl/bebasneue/BebasNeue-Regular.ttf

RUN pip install --no-cache-dir runpod requests pillow boto3

WORKDIR /app
COPY handler.py /app/handler.py
CMD ["python", "-u", "/app/handler.py"]
