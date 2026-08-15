FROM python:3.12-slim

WORKDIR /app

# Dépendances système minimales pour lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2-dev \
    libxslt1-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium + dépendances système pour Playwright (nécessaire pour contourner
# la protection Akamai Bot Manager sur pathe.fr, qui exige une exécution JS
# réelle). --with-deps installe automatiquement les paquets apt requis
# (libnss3, libatk, etc.) : plus fiable que de les lister manuellement.
RUN playwright install --with-deps chromium

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
