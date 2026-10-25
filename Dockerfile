# The bot's image, spelled out rather than detected. Railway's auto-detection
# started building this repo without Python once dashboard/ (a Next.js app)
# landed beside it, and the worker crash-looped on "python: command not
# found". An explicit image cannot be talked out of being Python.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
# tzdata: slim images carry no zoneinfo database, and the reports run in WIB.
RUN pip install -r requirements.txt tzdata

COPY wolf ./wolf
COPY anomaly ./anomaly
COPY pyproject.toml ./

CMD ["python", "-m", "wolf.main"]
