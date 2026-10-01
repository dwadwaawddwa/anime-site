"""Regroupement des animes par franchise (saisons).

Kitsu n'ayant pas de relations préquelle/suite fiables, on regroupe par **titre
normalisé** (`group_key`, cf. grouping.py) : tous les animes partageant la même clé
forment une franchise. La **racine** est l'anime le plus ancien ; toutes les entrées
et notes des utilisateurs se posent sur la racine → une franchise = un seul élément.
"""
from . import catalog, grouping
from .models import Anime


def _rank(a: Anime) -> tuple:
    """Ordre chronologique d'affichage des saisons : année, puis id."""
    return (a.season_year if a.season_year else 9999, a.id)


def _root_key(a: Anime) -> tuple:
    """Choix du représentant de la franchise : le plus POPULAIRE (évite qu'un
    obscur special/OVA ancien serve de tête), puis le plus ancien, puis id."""
    return (-(a.popularity or 0), a.season_year if a.season_year else 9999, a.id)


def _assign(session, members: list[Anime]) -> Anime:
    root = min(members, key=_root_key)
    for m in members:
        m.root_id = root.id
    return root


async def ensure_root(session, anime_id: int) -> Anime | None:
    """Garantit que l'anime a un root_id (regroupé par titre), et retourne la racine."""
    a = session.get(Anime, anime_id)
    if a is None:
        a = await catalog.fetch_by_id(session, anime_id)
    if a is None:
        return None
    if not a.group_key:
        a.group_key = grouping.group_key(a.romaji, a.english, a.native, a.id)
    members = session.query(Anime).filter(Anime.group_key == a.group_key).all()
    root = _assign(session, members)
    session.commit()
    return session.get(Anime, root.id)


def regroup_all(session) -> tuple[int, int]:
    """Recalcule les clés manquantes puis attribue root_id à toute la base par franchise.

    Retourne (nb_animes, nb_racines).
    """
    all_anime = session.query(Anime).all()
    groups: dict[str, list[Anime]] = {}
    for a in all_anime:
        # recalcule systématiquement (au cas où la normalisation a changé)
        a.group_key = grouping.group_key(a.romaji, a.english, a.native, a.id)
        groups.setdefault(a.group_key, []).append(a)
    for members in groups.values():
        _assign(session, members)
    session.commit()
    return len(all_anime), len(groups)


def seasons_of(session, root_id: int) -> list[Anime]:
    rows = session.query(Anime).filter(Anime.root_id == root_id).all()
    return sorted(rows, key=_rank)


def season_count(session, root_id: int) -> int:
    return session.query(Anime).filter(Anime.root_id == root_id).count()
