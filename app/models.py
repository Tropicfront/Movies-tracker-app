from typing import List, Optional
from pydantic import BaseModel


class Seance(BaseModel):
    """Une séance (horaire) pour un film, éventuellement avec sa salle/version."""
    date: Optional[str] = None       # ex: "2026-07-21"
    heure: Optional[str] = None      # ex: "20:15"
    version: Optional[str] = None    # ex: "VF", "VOST"
    salle: Optional[str] = None


class NoteAlloCine(BaseModel):
    """Notes AlloCiné pour un film ou une série."""
    note_presse: Optional[float] = None
    note_spectateurs: Optional[float] = None
    url_fiche: Optional[str] = None
    trouve: bool = False


class Film(BaseModel):
    """Un film à l'affiche au Pathé Toulouse Wilson, enrichi de sa note AlloCiné."""
    titre: str
    slug: Optional[str] = None
    affiche_url: Optional[str] = None
    synopsis: Optional[str] = None
    duree: Optional[str] = None
    genres: List[str] = []
    seances: List[Seance] = []
    allocine: Optional[NoteAlloCine] = None


class StatutScraping(BaseModel):
    derniere_maj: Optional[str] = None
    nb_films: int = 0
    erreurs: List[str] = []


class StatutSyncJellyfin(BaseModel):
    derniere_sync: Optional[str] = None
    nb_items_bibliotheque: int = 0
    nb_notes_appliquees: int = 0
    nb_non_trouves: int = 0
    erreurs: List[str] = []
