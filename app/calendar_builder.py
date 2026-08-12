"""
Génère un flux iCal (.ics) listant les films actuellement à l'affiche au
Pathé Toulouse Wilson dont le titre correspond à un film/série déjà présent
dans la bibliothèque Jellyfin.

Compatible avec le widget "Calendar" de Homepage (gethomepage.dev) et le
widget "Calendar" / intégration iCal de Homarr : les deux savent consommer
une URL .ics standard.

Limite connue : le rapprochement de titres (voir app/matching.py) est basé
sur une normalisation + similarité approximative, avec un fichier d'alias
pour les cas où le titre français (Pathé/AlloCiné) diffère fortement du titre
Jellyfin (souvent en anglais).
"""
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from icalendar import Calendar, Event

from app.config import CALENDAR_NAME
from app.matching import load_title_aliases, titles_match
from app.storage import get_all_films, get_jellyfin_library_titles

logger = logging.getLogger("calendar_builder")


def _parse_date(date_str: Optional[str]) -> Optional[date]:
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


def _build_description(film, seances_du_jour) -> str:
    lignes = []
    if film.allocine and film.allocine.trouve:
        notes = []
        if film.allocine.note_spectateurs is not None:
            notes.append(f"Spectateurs {film.allocine.note_spectateurs}/5")
        if film.allocine.note_presse is not None:
            notes.append(f"Presse {film.allocine.note_presse}/5")
        if notes:
            lignes.append("AlloCiné: " + " · ".join(notes))
        if film.allocine.url_fiche:
            lignes.append(film.allocine.url_fiche)

    if seances_du_jour:
        horaires = []
        for s in seances_du_jour:
            parts = [p for p in [s.heure, s.version, s.salle and f"Salle {s.salle}"] if p]
            if parts:
                horaires.append(" - ".join(parts))
        if horaires:
            lignes.append("Séances: " + ", ".join(horaires))
    else:
        lignes.append("Film à l'affiche (horaires détaillés non disponibles)")

    return "\n".join(lignes)


def generate_calendar_ics() -> bytes:
    """
    Construit le flux ICS. Ne lève jamais d'exception : en cas de problème
    (Jellyfin injoignable, etc.), retourne un calendrier vide plutôt que de
    faire échouer l'endpoint (les clients iCal gèrent mal les erreurs HTTP).
    """
    cal = Calendar()
    cal.add("prodid", "-//AlloCine-Pathe-Jellyfin-Sync//FR")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", CALENDAR_NAME)
    cal.add("x-wr-timezone", "Europe/Paris")

    try:
        films = get_all_films()
        jellyfin_titles = get_jellyfin_library_titles()
        aliases = load_title_aliases()

        if not jellyfin_titles:
            logger.warning(
                "Aucun titre récupéré depuis Jellyfin (non configuré ou injoignable) : "
                "le calendrier sera vide."
            )

        matched_films = [
            film for film in films
            if any(titles_match(film.titre, jf_titre, aliases) for jf_titre in jellyfin_titles)
        ]

        today = date.today()

        for film in matched_films:
            groups = defaultdict(list)
            if film.seances:
                for s in film.seances:
                    groups[s.date].append(s)
            else:
                groups[None] = []

            for date_key, seances_du_jour in groups.items():
                event_date = _parse_date(date_key) or today

                event = Event()
                event.add("summary", f"🎬 {film.titre}")
                event.add("dtstart", event_date)
                event.add("dtend", event_date + timedelta(days=1))
                event.add("description", _build_description(film, seances_du_jour))
                if film.affiche_url:
                    event.add("url", film.affiche_url)
                event["uid"] = f"{film.slug}-{event_date.isoformat()}@pathe-toulouse-wilson"
                cal.add_component(event)

        logger.info(
            "Calendrier généré: %d film(s) correspondant(s) à la bibliothèque Jellyfin sur %d à l'affiche",
            len(matched_films), len(films),
        )
    except Exception:
        logger.exception("Erreur lors de la génération du calendrier, retour d'un calendrier vide")

    return cal.to_ical()
