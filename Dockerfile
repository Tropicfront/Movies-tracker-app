FROM python:3.12-slim

WORKDIR /app

# Dépendances système : lxml (libxml2/libxslt + gcc) et curl (utilisé
# directement en subprocess par le scraper Pathé — voir app/scrapers/
# pathe_scraper.py pour l'explication : `curl` passe la protection Akamai
# de pathe.fr là où les librairies HTTP Python sont bloquées).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2-dev \
    libxslt1-dev \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Dossier static (favicon, assets éventuels) — créé explicitement au cas où
# il serait vide et donc absent du contexte de build.
RUN mkdir -p /app/app/static

# Dossier pour les HTML de debug (sélecteurs à ajuster si besoin)
RUN mkdir -p /app/data

EXPOSE 8000

ENV DEBUG_SAVE_HTML=true
ENV DEBUG_DATA_DIR=/app/data

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
