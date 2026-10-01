"""Source des fiches d'anime + cache local.

Fournisseur : **Kitsu** (https://kitsu.io/api), gratuite et sans clé, infrastructure
indépendante d'AniList/MyAnimeList (tous deux en panne au moment du déploiement).

Kitsu expose un dictionnaire de titres multilingue (en, en_jp = romaji, ja_jp = natif,
et souvent fr_fr / it_it / pt_br…), l'affiche, la bannière, les catégories (genres),
la note moyenne et le synopsis. On normalise tout vers le même dict puis on met en
cache (table Anime). L'id canonique est l'id Kitsu.

Le code fournisseur est isolé ici : on peut rebrancher AniList/Jikan plus tard sans
toucher au reste de l'app.
"""
import asyncio
import time
from datetime import timezone

import httpx

from . import config, grouping
from .models import Anime, utcnow


def _cache_age_days(obj) -> int:
    """Âge du cache en jours, robuste au fait que SQLite relit des dates naïves."""
    if not obj or not obj.cached_at:
        return 999
    cached = obj.cached_at
    if cached.tzinfo is None:
        cached = cached.replace(tzinfo=timezone.utc)
    return (utcnow() - cached).days

KITSU = "https://kitsu.io/api/edge"
_HEADERS = {"Accept": "application/vnd.api+json",
            "User-Agent": "anime.dwa.gold/1.0"}
# NB : « categories » DOIT figurer dans les champs, sinon JSON:API retire le lien
# vers les catégories (relationships.categories.data) même avec include=categories.
_FIELDS_ANIME = ("titles,canonicalTitle,abbreviatedTitles,posterImage,coverImage,"
                 "averageRating,userCount,episodeCount,startDate,subtype,synopsis,categories")

# Titres "principaux" traités à part ; le reste du dict titres devient synonyme.
_MAIN_KEYS = {"en", "en_jp", "ja_jp"}

_last_call = 0.0
_MIN_INTERVAL = 0.4


async def _get(path: str, params: dict | None = None) -> dict:
    global _last_call
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        await asyncio.sleep(wait)
    async with httpx.AsyncClient(timeout=15, headers=_HEADERS) as c:
        for _ in range(3):
            _last_call = time.monotonic()
            r = await c.get(f"{KITSU}{path}", params=params)
            if r.status_code in (429, 502, 503, 504):
                await asyncio.sleep(1.0)
                continue
            r.raise_for_status()
            return r.json()
    r.raise_for_status()
    return {}


def _year(start_date: str | None) -> int:
    if start_date and len(start_date) >= 4 and start_date[:4].isdigit():
        return int(start_date[:4])
    return 0


def _media_to_dict(item: dict, cat_names: dict) -> dict:
    a = item.get("attributes") or {}
    titles = a.get("titles") or {}
    romaji = titles.get("en_jp") or a.get("canonicalTitle") or ""
    english = titles.get("en") or ""
    native = titles.get("ja_jp") or ""
    synonyms = list(a.get("abbreviatedTitles") or [])
    synonyms += [v for k, v in titles.items() if k not in _MAIN_KEYS and v]
    synonyms = list(dict.fromkeys(s for s in synonyms if s))
    all_titles = [romaji, english, native, *synonyms]
    search_blob = "\n".join(t.lower() for t in all_titles if t)

    poster = a.get("posterImage") or {}
    cover = a.get("coverImage") or {}
    rating = a.get("averageRating")
    try:
        score = int(round(float(rating))) if rating else 0  # 0-100
    except (TypeError, ValueError):
        score = 0

    rel = (((item.get("relationships") or {}).get("categories") or {}).get("data")) or []
    genres = [cat_names[r["id"]] for r in rel if r.get("id") in cat_names]

    return {
        "id": int(item["id"]),
        "romaji": romaji,
        "english": english,
        "native": native,
        "synonyms": "\n".join(synonyms),
        "search_blob": search_blob,
        "cover_url": poster.get("large") or poster.get("medium") or poster.get("original") or "",
        "color": "",
        "banner_url": cover.get("large") or cover.get("original") or "",
        "genres": ",".join(genres),
        "fmt": (a.get("subtype") or "").upper(),
        "episodes": a.get("episodeCount") or 0,
        "season_year": _year(a.get("startDate")),
        "average_score": score,
        "popularity": a.get("userCount") or 0,
        "description": (a.get("synopsis") or "").strip(),
        "group_key": grouping.group_key(romaji, english, native, int(item["id"])),
    }


def _category_names(payload: dict) -> dict:
    return {c["id"]: (c.get("attributes") or {}).get("title", "")
            for c in payload.get("included", []) if c.get("type") == "categories"}


def upsert_anime(session, data: dict) -> Anime:
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


async def search(session, term: str, per_page: int = 20) -> list[Anime]:
    term = (term or "").strip()
    if not term:
        return []
    params = {
        "filter[text]": term,
        "page[limit]": min(per_page, 20),  # Kitsu : 20 max par page
        "include": "categories",
        "fields[anime]": _FIELDS_ANIME,
        "fields[categories]": "title",
    }
    payload = await _get("/anime", params)
    cat_names = _category_names(payload)
    out = []
    for item in payload.get("data", []):
        out.append(upsert_anime(session, _media_to_dict(item, cat_names)))
    session.commit()
    return out


async def browse(session, offset: int = 0, limit: int = 20) -> list[Anime]:
    """Page du catalogue Kitsu triée par popularité (userCount décroissant)."""
    params = {
        "sort": "-userCount",
        "page[limit]": min(limit, 20),
        "page[offset]": max(offset, 0),
        "include": "categories",
        "fields[anime]": _FIELDS_ANIME,
        "fields[categories]": "title",
    }
    payload = await _get("/anime", params)
    cat_names = _category_names(payload)
    out = []
    for item in payload.get("data", []):
        out.append(upsert_anime(session, _media_to_dict(item, cat_names)))
    session.commit()
    return out


async def fetch_relationships(anime_id: int, roles: set[str]) -> list[tuple[str, dict]]:
    """Relations d'un anime filtrées par rôle (prequel/sequel…).

    Retourne une liste de (role, dict_normalisé_de_l_anime_destination).
    """
    params = {
        "include": "destination",
        "fields[mediaRelationships]": "role,destination",
        "fields[anime]": _FIELDS_ANIME,
        "fields[categories]": "title",
    }
    try:
        payload = await _get(f"/anime/{anime_id}/mediaRelationships", params)
    except Exception:
        return []
    cat_names = _category_names(payload)
    dests = {it["id"]: it for it in payload.get("included", []) if it.get("type") == "anime"}
    out = []
    for rel in payload.get("data", []):
        role = (rel.get("attributes") or {}).get("role")
        if role not in roles:
            continue
        d = ((rel.get("relationships") or {}).get("destination") or {}).get("data") or {}
        if d.get("type") == "anime" and d.get("id") in dests:
            out.append((role, _media_to_dict(dests[d["id"]], cat_names)))
    return out


async def fetch_by_id(session, anime_id: int, force: bool = False) -> Anime | None:
    obj = session.get(Anime, anime_id)
    if obj is not None and not force:
        if _cache_age_days(obj) < config.ANIME_CACHE_TTL_DAYS:
            return obj
    params = {
        "include": "categories",
        "fields[anime]": _FIELDS_ANIME,
        "fields[categories]": "title",
    }
    try:
        payload = await _get(f"/anime/{anime_id}", params)
    except Exception:
        return obj  # on garde le cache si l'API est indisponible
    item = payload.get("data")
    if not item:
        return obj
    obj = upsert_anime(session, _media_to_dict(item, _category_names(payload)))
    session.commit()
    return obj
