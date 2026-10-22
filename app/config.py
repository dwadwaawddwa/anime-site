"""Configuration chargée depuis l'environnement (.env)."""
import os
import secrets

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Application Discord (réutilise la même app que le site de guilde : OAuth "identify")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")

# URL publique de base
BASE_URL = os.getenv("BASE_URL", "http://localhost:8030").rstrip("/")
REDIRECT_URI = BASE_URL + "/auth/callback"

# Secret de session : DOIT être fixe en prod (défini dans .env), sinon les sessions
# sont invalidées à chaque redémarrage.
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") == "1"  # 1 en prod (HTTPS via Cloudflare)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'anime.db')}")

# IDs Discord toujours admin (bootstrap), séparés par des virgules
ADMIN_BOOTSTRAP_IDS = {x.strip() for x in os.getenv("ADMIN_BOOTSTRAP_IDS", "").split(",") if x.strip()}

# Mode LOCAL : si défini, on ouvre automatiquement la session de cet utilisateur
# (id Discord déjà présent en base) et on saute complètement l'auth Discord.
LOCAL_USER_ID = os.getenv("LOCAL_USER_ID", "").strip()

DISCORD_API = "https://discord.com/api/v10"
OAUTH_SCOPE = "identify"

ANILIST_API = "https://graphql.anilist.co"

# Durée de validité du cache AniList d'une fiche (jours) avant rafraîchissement.
ANIME_CACHE_TTL_DAYS = int(os.getenv("ANIME_CACHE_TTL_DAYS", "7"))

# Sites satellites qui délèguent leur connexion à ce site (SSO signé HMAC, un secret chacun).
MUSIC_URL = os.getenv("MUSIC_URL", "").rstrip("/")
MUSIC_SSO_SECRET = os.getenv("MUSIC_SSO_SECRET", "")
BLINDTEST_URL = os.getenv("BLINDTEST_URL", "").rstrip("/")
BLINDTEST_SSO_SECRET = os.getenv("BLINDTEST_SSO_SECRET", "")
SSO_CLIENTS = {
    name: (url, secret)
    for name, url, secret in (("music", MUSIC_URL, MUSIC_SSO_SECRET),
                              ("blindtest", BLINDTEST_URL, BLINDTEST_SSO_SECRET))
    if url and secret
}

# Fuseau horaire utilisé pour découper l'activité en journées (heatmap du profil).
# On veut « aujourd'hui » au sens de l'utilisateur, pas en UTC.
WATCH_TZ = os.getenv("WATCH_TZ", "Europe/Paris")


def is_configured() -> bool:
    return bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET)
