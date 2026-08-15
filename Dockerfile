# Image officielle Playwright : Python + Chromium + toutes les dépendances
# système déjà installées et testées par Microsoft pour cette combinaison
# exacte. Évite les échecs de `playwright install --with-deps` qui peuvent
# survenir sur une image générique selon la version de Debian/Ubuntu sous-jacente.
# Le tag de version doit correspondre à celui de requirements.txt (playwright==1.47.0).
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# Dépendances système minimales pour lxml (l'image de base est Ubuntu, apt disponible)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2-dev \
    libxslt1-dev \
    gcc \
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
