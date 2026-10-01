"""Source des personnages + cache local.

Fournisseur : **AniList** (GraphQL, gratuit sans clé) — ses images de personnages
(s4.anilist.co) sont de bien meilleure qualité que celles de Kitsu. On ne met en
cache QUE le nom + l'URL de l'image : aucune image n'est stockée sur le disque du Pi,
le navigateur les charge directement depuis le CDN AniList (autorisé par la CSP
img-src). L'id canonique d'un personnage est son id AniList.

On réutilise le helper GraphQL throttlé `anilist._post`.
"""
from datetime import timezone

from . import anilist, config
from .models import Character, utcnow

_SEARCH_Q = """
query ($q: String, $n: Int) {
  Page(perPage: $n) {
    characters(search: $q, sort: FAVOURITES_DESC) {
      id name { full } image { large medium }
    }
  }
}
"""

_BY_ID_Q = """
query ($id: Int) {
  Character(id: $id) { id name { full } image { large medium } }
}
"""

# Personnages d'un anime (recherché par titre), principaux d'abord.
_MEDIA_CHARS_Q = """
query ($s: String, $n: Int) {
  Media(search: $s, type: ANIME) {
    id title { romaji }
    characters(sort: [ROLE, FAVOURITES_DESC], perPage: $n) {
      edges { role node { id name { full } image { large medium } } }
    }
  }
}
"""


def _image_url(image: dict | None) -> str:
    if not image:
        return ""
    url = image.get("large") or image.get("medium") or ""
    # AniList renvoie une image « default.jpg » quand le perso n'a pas d'illustration.
    if not url or url.rsplit("/", 1)[-1].startswith("default"):
        return ""
    return url


def _node_to_dict(node: dict) -> dict:
    name = (node.get("name") or {}).get("full") or f"#{node['id']}"
    return {
        "id": int(node["id"]),
        "name": name.strip(),
        "image_url": _image_url(node.get("image")),
    }


def upsert(session, data: dict) -> Character:
    obj = session.get(Character, data["id"])
    if obj is None:
        obj = Character(id=data["id"])
        session.add(obj)
    obj.name = data["name"]
    obj.image_url = data["image_url"]
    obj.cached_at = utcnow()
    return obj


async def search(session, term: str, per_page: int = 20) -> list[Character]:
    term = (term or "").strip()
    if not term:
        return []
    data = await anilist._post(_SEARCH_Q, {"q": term, "n": min(per_page, 25)})
    nodes = ((data.get("Page") or {}).get("characters")) or []
    out = [upsert(session, _node_to_dict(n)) for n in nodes]
    session.commit()
    return out


async def characters_of_anime(session, title: str, per_page: int = 25) -> list[Character]:
    """Personnages d'un anime (recherché par titre sur AniList), principaux d'abord."""
    title = (title or "").strip()
    if not title:
        return []
    data = await anilist._post(_MEDIA_CHARS_Q, {"s": title, "n": min(per_page, 25)})
    media = data.get("Media") or {}
    edges = ((media.get("characters") or {}).get("edges")) or []
    out = [upsert(session, _node_to_dict(e["node"])) for e in edges if e.get("node")]
    session.commit()
    return out


def _cache_age_days(obj) -> int:
    if not obj or not obj.cached_at:
        return 999
    cached = obj.cached_at
    if cached.tzinfo is None:
        cached = cached.replace(tzinfo=timezone.utc)
    return (utcnow() - cached).days


async def fetch_by_id(session, char_id: int) -> Character | None:
    """Retourne le personnage (cache local, rafraîchi via AniList si absent/périmé)."""
    obj = session.get(Character, char_id)
    if obj is not None and _cache_age_days(obj) < config.ANIME_CACHE_TTL_DAYS:
        return obj
    try:
        data = await anilist._post(_BY_ID_Q, {"id": char_id})
    except Exception:
        return obj  # on garde le cache si l'API est indisponible
    node = data.get("Character")
    if not node:
        return obj
    obj = upsert(session, _node_to_dict(node))
    session.commit()
    return obj
