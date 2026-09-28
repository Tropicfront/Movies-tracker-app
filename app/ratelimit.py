"""
Limiteur de débit global pour les requêtes vers AlloCiné.

Toutes les requêtes sortantes vers allocine.fr (recherche, fiche, page salle,
nouvelles tentatives après un 429) passent par wait_for_slot(), de sorte que
l'espacement minimal (1 / ALLOCINE_REQUESTS_PER_SECOND secondes) est respecté
quel que soit le fil d'exécution qui émet la requête (synchro Jellyfin en
arrière-plan, rafraîchissement manuel, appel de l'endpoint /allocine/note...).
"""
import threading
import time

from app.config import ALLOCINE_REQUESTS_PER_SECOND

_lock = threading.Lock()
_next_slot = 0.0  # instant (time.monotonic) à partir duquel la prochaine requête est permise


def wait_for_slot() -> float:
    """
    Bloque jusqu'à ce qu'une requête vers AlloCiné soit autorisée et retourne
    le temps attendu (en secondes). Sans effet si la limite est désactivée (0).

    Chaque appelant "réserve" son créneau sous verrou puis dort HORS du
    verrou : plusieurs fils simultanés sont donc espacés proprement, sans se
    bloquer les uns les autres ni se réveiller tous en même temps.
    """
    global _next_slot
    rps = ALLOCINE_REQUESTS_PER_SECOND
    if rps <= 0:
        return 0.0
    interval = 1.0 / rps
    with _lock:
        now = time.monotonic()
        start = max(now, _next_slot)
        _next_slot = start + interval
    wait = start - now
    if wait > 0:
        time.sleep(wait)
    return max(wait, 0.0)
