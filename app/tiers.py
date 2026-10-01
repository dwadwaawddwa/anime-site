"""Gestion des tier lists : boards, paliers par défaut, placement auto, réordonnancement.

- Chaque utilisateur a un board « de base » (is_default) auto-rempli par les notes :
  le placement y est porté par Entry.tier_id / Entry.tier_pos (comportement historique).
- Les boards custom sont manuels : le placement est porté par la table Placement.
"""
from .models import (Board, CharPlacement, Character, Entry, Placement, Tier,
                     STATUS_FINISHED)

# Paliers créés par défaut pour un nouveau board custom (S/A/B/C/D, sans note associée).
DEFAULT_CUSTOM_TIERS = ["S", "A", "B", "C", "D"]


def _label_for_value(v: float) -> str:
    return str(int(v)) if v == int(v) else str(v)


# --------------------------------------------------------------------------- #
#  Boards
# --------------------------------------------------------------------------- #
def ensure_default_board(session, user_id: str) -> Board:
    """Retourne (en le créant au besoin) le board « de base » (anime) de l'utilisateur.

    Rattache aussi au board de base les paliers historiques (board_id NULL)."""
    b = (session.query(Board)
         .filter_by(user_id=user_id, is_default=True, kind="anime")
         .order_by(Board.id).first())
    if b is None:
        b = Board(user_id=user_id, name="Ma tier list", is_default=True,
                  position=0, kind="anime")
        session.add(b)
        session.commit()
    orphans = session.query(Tier).filter_by(user_id=user_id, board_id=None).all()
    if orphans:
        for t in orphans:
            t.board_id = b.id
        session.commit()
    return b


def get_boards(session, user_id: str, kind: str = "anime") -> list[Board]:
    return (session.query(Board)
            .filter_by(user_id=user_id, kind=kind)
            .order_by(Board.position, Board.id)
            .all())


def create_custom_board(session, user_id: str, name: str) -> Board:
    """Crée un board custom en COPIANT les paliers + placements de la liste de base."""
    default = ensure_default_board(session, user_id)
    ensure_default_tiers(session, user_id)
    rows = get_boards(session, user_id)
    pos = (rows[-1].position + 1) if rows else 0
    board = Board(user_id=user_id, name=(name or "Nouvelle liste").strip()[:60] or "Nouvelle liste",
                  is_default=False, position=pos)
    session.add(board)
    session.commit()

    # Copie des paliers (on garde la correspondance ancien->nouveau palier).
    id_map: dict[int, int] = {}
    for t in get_tiers(session, default.id):
        nt = Tier(user_id=user_id, board_id=board.id, label=t.label,
                  value=t.value, position=t.position)
        session.add(nt)
        session.flush()  # pour obtenir nt.id
        id_map[t.id] = nt.id
    session.commit()

    # Copie des placements de la liste de base (Entry.tier_id ou palier de même note).
    _tier_rows, by_tier, _unranked = build_board(session, default)
    for old_tid, entries in by_tier.items():
        new_tid = id_map.get(old_tid)
        if new_tid is None:
            continue
        for i, e in enumerate(entries):
            session.add(Placement(board_id=board.id, entry_id=e.id,
                                  tier_id=new_tid, pos=float(i)))
    session.commit()
    return board


def clear_board(session, board: Board):
    """Vide un board custom : tous ses animes repartent dans « À classer »."""
    if board.is_default:
        return
    session.query(Placement).filter_by(board_id=board.id).delete()
    session.commit()


# --------------------------------------------------------------------------- #
#  Paliers
# --------------------------------------------------------------------------- #
def ensure_default_tiers(session, user_id: str) -> list[Tier]:
    """Crée les paliers 10 → 1 du board de base s'il n'en a pas encore."""
    board = ensure_default_board(session, user_id)
    existing = get_tiers(session, board.id)
    if existing:
        return existing
    tiers = []
    for i, v in enumerate(range(10, 0, -1)):  # 10, 9, ..., 1  (position 0 = en haut)
        t = Tier(user_id=user_id, board_id=board.id,
                 label=_label_for_value(v), value=float(v), position=i)
        session.add(t)
        tiers.append(t)
    session.commit()
    return tiers


def ensure_custom_tiers(session, user_id: str, board_id: int) -> list[Tier]:
    """Crée les paliers S/A/B/C/D d'un board custom s'il n'en a pas encore."""
    existing = get_tiers(session, board_id)
    if existing:
        return existing
    tiers = []
    for i, label in enumerate(DEFAULT_CUSTOM_TIERS):
        t = Tier(user_id=user_id, board_id=board_id, label=label, value=None, position=i)
        session.add(t)
        tiers.append(t)
    session.commit()
    return tiers


def get_tiers(session, board_id: int) -> list[Tier]:
    return (session.query(Tier)
            .filter_by(board_id=board_id)
            .order_by(Tier.position, Tier.id)
            .all())


def tier_with_value(session, board_id: int, value: float) -> Tier | None:
    return session.query(Tier).filter_by(board_id=board_id, value=value).first()


def ensure_tier_for_value(session, user_id: str, board_id: int, value: float) -> Tier:
    """Retourne le palier de note `value` du board, en le créant (bien inséré) au besoin.

    Sert quand on note un anime 9.5 alors qu'aucun palier 9.5 n'existe encore.
    """
    t = tier_with_value(session, board_id, value)
    if t:
        return t
    tiers = get_tiers(session, board_id)
    # on insère le nouveau palier juste avant le premier palier de valeur inférieure
    pos = len(tiers)
    for existing in tiers:
        if existing.value is not None and existing.value < value:
            pos = existing.position
            break
    for existing in tiers:
        if existing.position >= pos:
            existing.position += 1
    t = Tier(user_id=user_id, board_id=board_id,
             label=_label_for_value(value), value=value, position=pos)
    session.add(t)
    session.commit()
    return t


def auto_place(session, user_id: str, entry: Entry):
    """Place une entrée dans le palier de sa note, sur le board DE BASE uniquement.
    Appelé quand on (re)note un anime fini."""
    board = ensure_default_board(session, user_id)
    if entry.status != STATUS_FINISHED or entry.rating is None:
        entry.tier_id = None
        return
    t = ensure_tier_for_value(session, user_id, board.id, entry.rating)
    entry.tier_id = t.id


# --------------------------------------------------------------------------- #
#  Construction d'une tier list
# --------------------------------------------------------------------------- #
def build_board(session, board: Board):
    """Construit (tier_rows, by_tier, unranked) pour un board donné.

    - Board de base : placement via Entry.tier_id, sinon palier de même note.
    - Board custom : placement via la table Placement.
    """
    tier_rows = get_tiers(session, board.id)
    by_tier: dict[int, list[Entry]] = {t.id: [] for t in tier_rows}
    unranked: list[Entry] = []
    finished = (session.query(Entry)
                .filter_by(user_id=board.user_id, status=STATUS_FINISHED).all())

    if board.is_default:
        val_to_tier = {t.value: t for t in tier_rows if t.value is not None}
        for e in finished:
            tid = e.tier_id
            if tid is None and e.rating is not None and e.rating in val_to_tier:
                tid = val_to_tier[e.rating].id
            (by_tier[tid] if tid in by_tier else unranked).append(e)
        for lst in by_tier.values():
            lst.sort(key=lambda e: (e.tier_pos or 0, -(e.rating or 0)))
    else:
        placements = {p.entry_id: p for p in
                      session.query(Placement).filter_by(board_id=board.id).all()}
        for e in finished:
            p = placements.get(e.id)
            tid = p.tier_id if p else None
            (by_tier[tid] if tid in by_tier else unranked).append(e)
        for lst in by_tier.values():
            lst.sort(key=lambda e: (placements[e.id].pos if e.id in placements else 0,
                                    -(e.rating or 0)))
    return tier_rows, by_tier, unranked


# --------------------------------------------------------------------------- #
#  Tier lists de PERSONNAGES (board kind='character', placement via CharPlacement)
# --------------------------------------------------------------------------- #
def ensure_default_char_board(session, user_id: str) -> Board:
    """Retourne (en le créant au besoin) le board de personnages « de base »."""
    b = (session.query(Board)
         .filter_by(user_id=user_id, is_default=True, kind="character")
         .order_by(Board.id).first())
    if b is None:
        b = Board(user_id=user_id, name="Mes persos", is_default=True,
                  position=0, kind="character")
        session.add(b)
        session.commit()
        ensure_custom_tiers(session, user_id, b.id)  # paliers S/A/B/C/D
    return b


def create_char_board(session, user_id: str, name: str) -> Board:
    """Crée un board de personnages VIDE avec les paliers S/A/B/C/D par défaut."""
    ensure_default_char_board(session, user_id)
    rows = get_boards(session, user_id, kind="character")
    pos = (rows[-1].position + 1) if rows else 0
    board = Board(user_id=user_id,
                  name=(name or "Nouvelle liste").strip()[:60] or "Nouvelle liste",
                  is_default=False, position=pos, kind="character")
    session.add(board)
    session.commit()
    ensure_custom_tiers(session, user_id, board.id)
    return board


def clear_char_board(session, board: Board):
    """Vide un board de personnages : tous les persos repartent dans « À classer »."""
    for p in session.query(CharPlacement).filter_by(board_id=board.id).all():
        p.tier_id = None
    session.commit()


def build_char_board(session, board: Board):
    """Construit (tier_rows, by_tier, unranked) d'un board de personnages.

    Les éléments sont des objets Character (et non des Entry)."""
    tier_rows = get_tiers(session, board.id)
    by_tier: dict[int, list[Character]] = {t.id: [] for t in tier_rows}
    unranked: list[Character] = []
    placements = (session.query(CharPlacement)
                  .filter_by(board_id=board.id)
                  .order_by(CharPlacement.pos).all())
    ids = [p.char_id for p in placements]
    chars = {c.id: c for c in
             session.query(Character).filter(Character.id.in_(ids or [0])).all()}
    for p in placements:
        c = chars.get(p.char_id)
        if c is None:
            continue
        (by_tier[p.tier_id] if p.tier_id in by_tier else unranked).append(c)
    return tier_rows, by_tier, unranked
