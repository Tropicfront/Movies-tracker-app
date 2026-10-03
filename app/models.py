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


class ItemBibliotheque(BaseModel):
    """Un film ou une série de la bibliothèque Jellyfin, avec sa note AlloCiné si connue."""
    id: str
    titre: str
    type: str  # "film" ou "serie"
    annee: Optional[int] = None
    genres: List[str] = []
    synopsis: Optional[str] = None
    a_une_affiche: bool = False  # sert à savoir s'il faut appeler /jellyfin/image/{id}
    poster_tag: Optional[str] = None  # version de l'affiche (change si l'affiche change) : clé du cache
    allocine: Optional[NoteAlloCine] = None
    # Ce que Jellyfin contient réellement MAINTENANT (lu dans la liste, pas supposé)
    jellyfin_community_rating: Optional[float] = None  # /10
    jellyfin_critic_rating: Optional[float] = None     # /100
    # Comparaison avec la note AlloCiné connue : "a_jour" | "different" | "absente",
    # ou null s'il n'y a aucune note AlloCiné à comparer.
    jellyfin_etat: Optional[str] = None


class StatutScraping(BaseModel):
    derniere_maj: Optional[str] = None
    nb_films: int = 0
    erreurs: List[str] = []


class StatutSyncJellyfin(BaseModel):
    en_cours: bool = False           # une synchro tourne actuellement en arrière-plan
    nb_traites: int = 0              # titres déjà traités (progression)
    derniere_sync: Optional[str] = None
    nb_items_bibliotheque: int = 0
    nb_notes_appliquees: int = 0
    # Jellyfin a répondu "OK" mais, relue juste après, la note n'est PAS enregistrée
    nb_non_persistees: int = 0
    nb_non_trouves: int = 0
    # Titres pour lesquels une note AlloCiné est connue (cache ou recherche) : c'est le
    # chiffre qui compte quand l'écriture vers Jellyfin est désactivée (cas par défaut).
    nb_notes_trouvees: int = 0
    # JELLYFIN_WRITE_RATINGS : les notes sont-elles écrites dans l'étoile / la tomate ?
    ecriture_jellyfin: bool = False
    # Opération en cours ou dernière : "synchro" | "application_cache" | "nettoyage"
    operation: str = "synchro"
    # Pour le nettoyage (retrait des notes écrites auparavant dans l'étoile / la tomate)
    nb_nettoyees: int = 0
    nb_ignorees: int = 0
    erreurs: List[str] = []
