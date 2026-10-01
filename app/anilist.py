"""Client AniList (GraphQL) + mise en cache locale des fiches d'anime.

AniList est gratuit et sans clé. On récupère les titres romaji / english / native
(+ synonymes, qui contiennent souvent le titre FR), les genres, l'image et le score.
"""
import html
import re
import time

import httpx

from . import config
from .models import Anime, utcnow

_SEARCH_QUERY = """
query ($search: String, $page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH, isAdult: false) {
      id
      title { romaji english native }
      synonyms
      coverImage { large color }
      bannerImage
      genres
      episodes
      format
      seasonYear
      averageScore
      description(asHtml: false)
    }
  }
}
"""

_BY_ID_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title { romaji english native }
    synonyms
    coverImage { large color }
    bannerImage
    genres
    episodes
    format
    seasonYear
    averageScore
    description(asHtml: false)
  }
}
"""

# Petit throttle process-wide pour rester loin de la limite AniList (90 req/min).
_last_call = 0.0
_MIN_INTERVAL = 0.7


def _clean_desc(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


async def _post(query: str, variables: dict) -> dict:
    global _last_call
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        import asyncio
        await asyncio.sleep(wait)
    _last_call = time.monotonic()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(config.ANILIST_API, json={"query": query, "variables": variables},
                         headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


def _media_to_dict(m: dict) -> dict:
    title = m.get("title") or {}
    synonyms = [s for s in (m.get("synonyms") or []) if s]
    romaji = title.get("romaji") or ""
    english = title.get("english") or ""
    native = title.get("native") or ""
    all_titles = [romaji, english, native, *synonyms]
    search_blob = "\n".join(t.lower() for t in all_titles if t)
    cover = m.get("coverImage") or {}
    return {
        "id": m["id"],
        "romaji": romaji,
        "english": english,
        "native": native,
        "synonyms": "\n".join(synonyms),
        "search_blob": search_blob,
        "cover_url": cover.get("large") or "",
        "color": cover.get("color") or "",
        "banner_url": m.get("bannerImage") or "",
        "genres": ",".join(m.get("genres") or []),
        "fmt": m.get("format") or "",
        "episodes": m.get("episodes") or 0,
        "season_year": m.get("seasonYear") or 0,
        "average_score": m.get("averageScore") or 0,
        "description": _clean_desc(m.get("description")),
    }


def upsert_anime(session, data: dict) -> Anime:
    """Insère ou met à jour la fiche cache d'un anime."""
    obj = session.get(Anime, data["id"])
    if obj is None:
        obj = Anime(id=data["id"])
        session.add(obj)
    for k, v in data.items():
        if k == "id":
            continue
        setattr(obj, k, v)
    obj.cached_at = utcnow()
    return obj


async def search(session, term: str, per_page: int = 24) -> list[Anime]:
    """Recherche AniList (multilingue) et met les résultats en cache local."""
    term = (term or "").strip()
    if not term:
        return []
    data = await _post(_SEARCH_QUERY, {"search": term, "page": 1, "perPage": per_page})
    media = (data.get("Page") or {}).get("media") or []
    out = []
    for m in media:
        out.append(upsert_anime(session, _media_to_dict(m)))
    session.commit()
    return out


async def fetch_by_id(session, anime_id: int, force: bool = False) -> Anime | None:
    """Récupère une fiche par id (depuis le cache si récent, sinon AniList)."""
    obj = session.get(Anime, anime_id)
    if obj is not None and not force:
        age = (utcnow() - obj.cached_at).days if obj.cached_at else 999
        if age < config.ANIME_CACHE_TTL_DAYS:
            return obj
    try:
        data = await _post(_BY_ID_QUERY, {"id": anime_id})
    except Exception:
        return obj  # on garde le cache si AniList est indisponible
    m = data.get("Media")
    if not m:
        return obj
    obj = upsert_anime(session, _media_to_dict(m))
    session.commit()
    return obj
