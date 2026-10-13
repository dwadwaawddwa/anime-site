"""Middleware d'en-têtes de sécurité + limiteur de débit simple en mémoire."""
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Ajoute des en-têtes de sécurité + une CSP stricte.

    On n'autorise que nos propres assets, les images de couverture Kitsu
    (media.kitsu.app) et les avatars Discord (cdn.discordapp.com).
    """
    CSP = (
        "default-src 'self'; "
        "img-src 'self' https://media.kitsu.app https://media.kitsu.io https://s4.anilist.co https://cdn.discordapp.com data: blob:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "script-src 'self'; "
        "connect-src 'self' https://media.kitsu.app https://media.kitsu.io; "
        "font-src 'self' https://fonts.gstatic.com; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    )

    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = self.CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        return resp


class RateLimiter:
    """Fenêtre glissante par clé (ex: IP+route). Process-local, suffisant ici."""
    def __init__(self, max_calls: int, window: float):
        self.max_calls = max_calls
        self.window = window
        self._hits: dict[str, deque] = defaultdict(deque)

    def _queue(self, key: str) -> deque:
        now = time.monotonic()
        # purge des clés expirées pour que la mémoire ne grossisse pas indéfiniment
        if len(self._hits) > 10000:
            for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window]:
                del self._hits[k]
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def allow(self, key: str) -> bool:
        q = self._queue(key)
        if len(q) >= self.max_calls:
            return False
        q.append(time.monotonic())
        return True

    # Variante « compter seulement les échecs » (ex. mots de passe faux par compte) :
    # blocked() vérifie sans consommer, hit() enregistre un échec, reset() efface.
    def blocked(self, key: str) -> bool:
        return len(self._queue(key)) >= self.max_calls

    def hit(self, key: str):
        self._queue(key).append(time.monotonic())

    def reset(self, key: str):
        self._hits.pop(key, None)


# Recherche : max 30 req / minute par IP (protège l'API AniList d'un abus).
search_limiter = RateLimiter(max_calls=30, window=60.0)
# Écritures : max 120 mutations / minute par IP.
write_limiter = RateLimiter(max_calls=120, window=60.0)
# Connexion pseudo/mdp : 10 tentatives / 5 min par IP (anti force brute)…
login_ip_limiter = RateLimiter(max_calls=10, window=300.0)
# … et 10 échecs / 15 min par pseudo (même s'il n'existe pas : ne révèle rien),
# pour freiner une attaque répartie sur plusieurs IP.
login_fail_limiter = RateLimiter(max_calls=10, window=900.0)
# Inscriptions : 5 comptes / heure par IP (anti-spam de comptes).
register_limiter = RateLimiter(max_calls=5, window=3600.0)
