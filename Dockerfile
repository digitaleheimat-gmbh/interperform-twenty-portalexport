FROM python:3.12-slim

RUN useradd --create-home --shell /usr/sbin/nologin worker
WORKDIR /app

COPY worker.py twenty_client.py openimmo.py validate.py portals.py website.py ./
COPY templates/ ./templates/

USER worker

CMD ["python", "worker.py"]
