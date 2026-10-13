"""Modèles SQLAlchemy."""
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint, Index)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Statuts possibles d'un anime dans la liste d'un utilisateur.
STATUS_TO_WATCH = "to_watch"
STATUS_WATCHING = "watching"
STATUS_FINISHED = "finished"
STATUSES = (STATUS_TO_WATCH, STATUS_WATCHING, STATUS_FINISHED)
STATUS_LABELS = {
    STATUS_TO_WATCH: "À écouter",
    STATUS_WATCHING: "En cours",
    STATUS_FINISHED: "Fini",
}


class User(Base):
    __tablename__ = "users"
    # id = identifiant Discord (chaîne) pour éviter tout souci d'entier 64 bits.
    id = Column(String(32), primary_key=True)
    username = Column(String(64), nullable=False, default="")
    global_name = Column(String(64), nullable=False, default="")
    avatar = Column(String(64), nullable=False, default="")
    # 0 = pas de photo perso (on utilise l'avatar Discord) ; > 0 = version de la
    # photo uploadée (sert aussi de cache-buster dans l'URL).
    custom_avatar = Column(Integer, nullable=False, default=0)
    is_public = Column(Boolean, nullable=False, default=False)
    # Top 3 animes préférés (ids d'anime racine), affichés sur le profil.
    top1_id = Column(Integer, nullable=True)
    top2_id = Column(Integer, nullable=True)
    top3_id = Column(Integer, nullable=True)
    # Top 3 personnages préférés (ids de personnage Kitsu, cf. table Character).
    char1_id = Column(Integer, nullable=True)
    char2_id = Column(Integer, nullable=True)
    char3_id = Column(Integer, nullable=True)
    # Jeton d'API perso (secret) : sert à l'extension navigateur pour envoyer
    # l'activité (POST /api/watch) sans dépendre du cookie de session. None = pas
    # encore généré. On stocke le token en clair (usage perso, faible enjeu) ; il est
    # régénérable à volonté depuis le profil.
    api_token = Column(String(64), nullable=True)
    # Compte « pseudo + mot de passe » (sans Discord). id = "l" + hex aléatoire (jamais
    # numérique, donc aucune collision avec un id Discord). login_key = pseudo en
    # minuscules (unicité insensible à la casse, index unique créé dans db._migrate).
    login_key = Column(String(32), nullable=True)
    password_hash = Column(String(255), nullable=True)  # Argon2id, jamais le mdp en clair
    created_at = Column(DateTime(timezone=True), default=utcnow)
    last_login = Column(DateTime(timezone=True), default=utcnow)

    entries = relationship("Entry", back_populates="user", cascade="all, delete-orphan")
    tiers = relationship("Tier", back_populates="user", cascade="all, delete-orphan")
    watch_events = relationship("WatchEvent", back_populates="user",
                                cascade="all, delete-orphan")
    login_sessions = relationship("LoginSession", cascade="all, delete-orphan")

    @property
    def display_name(self) -> str:
        return self.global_name or self.username or "Utilisateur"


class LoginSession(Base):
    """Session ouverte d'un compte pseudo/mdp, gardée côté serveur.

    Le cookie (signé) ne suffit pas seul : sans cette table, un cookie copié resterait
    valable après la déconnexion. Ici, se déconnecter supprime la ligne ; changer de
    mot de passe supprime toutes les lignes du compte. On ne stocke que le SHA-256 du
    jeton du cookie, jamais le jeton lui-même.
    """
    __tablename__ = "login_sessions"
    id = Column(String(64), primary_key=True)
    user_id = Column(String(32), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)


class Anime(Base):
    """Cache local d'une fiche AniList."""
    __tablename__ = "anime"
    id = Column(Integer, primary_key=True)  # id AniList
    romaji = Column(String(400), default="")
    english = Column(String(400), default="")
    native = Column(String(400), default="")
    synonyms = Column(Text, default="")       # séparés par "\n"
    search_blob = Column(Text, default="")    # tous les titres en minuscules, pour la recherche locale
    cover_url = Column(String(500), default="")
    banner_url = Column(String(500), default="")
    color = Column(String(16), default="")
    genres = Column(Text, default="")         # séparés par ","
    fmt = Column(String(32), default="")
    episodes = Column(Integer, default=0)
    season_year = Column(Integer, default=0)
    average_score = Column(Integer, default=0)  # score du fournisseur (0-100)
    popularity = Column(Integer, default=0)     # userCount Kitsu (tri du catalogue)
    description = Column(Text, default="")
    cached_at = Column(DateTime(timezone=True), default=utcnow)
    # Regroupement par franchise : id de l'anime "racine" (saison 1, le plus ancien
    # de la chaîne préquelle/suite). 0 = pas encore regroupé ; == id pour un anime
    # racine ou autonome. Toutes les entrées/notes se posent sur la racine.
    root_id = Column(Integer, default=0, index=True)
    # Clé de regroupement par franchise (titre normalisé, cf. grouping.py).
    group_key = Column(String(200), default="", index=True)

    @property
    def title(self) -> str:
        return self.english or self.romaji or self.native or f"Anime #{self.id}"

    @property
    def genre_list(self) -> list[str]:
        return [g for g in (self.genres or "").split(",") if g]


class Character(Base):
    """Cache local d'un personnage Kitsu.

    On ne stocke QUE l'URL de l'image (hébergée sur le CDN Kitsu, media.kitsu.app) :
    aucune image n'est enregistrée sur le disque du Pi, le navigateur les charge
    directement depuis Kitsu (autorisé par la CSP img-src). L'id canonique est l'id
    Kitsu du personnage.
    """
    __tablename__ = "characters"
    id = Column(Integer, primary_key=True)  # id Kitsu du personnage
    name = Column(String(300), default="")
    image_url = Column(String(500), default="")
    cached_at = Column(DateTime(timezone=True), default=utcnow)


class Entry(Base):
    """Un anime dans la liste d'un utilisateur (statut + note + commentaire + tier)."""
    __tablename__ = "entries"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    anime_id = Column(Integer, ForeignKey("anime.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(16), nullable=False, default=STATUS_TO_WATCH)
    rating = Column(Float, nullable=True)          # 0..10 par pas de 0.5, ou None
    comment = Column(Text, nullable=False, default="")
    tier_id = Column(Integer, ForeignKey("tiers.id", ondelete="SET NULL"), nullable=True)
    tier_pos = Column(Float, nullable=False, default=0)  # ordre manuel dans le palier
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "anime_id", name="uq_user_anime"),
        Index("ix_entries_user_status", "user_id", "status"),
        Index("ix_entries_anime", "anime_id"),
    )

    user = relationship("User", back_populates="entries")
    anime = relationship("Anime")
    tier = relationship("Tier", foreign_keys=[tier_id])


class Board(Base):
    """Une tier list nommée. Chaque utilisateur a une liste « de base » (is_default,
    auto-remplie par les notes) et peut créer d'autres listes custom (placement manuel)."""
    __tablename__ = "boards"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(60), nullable=False, default="Ma tier list")
    is_default = Column(Boolean, nullable=False, default=False)
    position = Column(Integer, nullable=False, default=0)  # ordre des onglets
    # "anime" (tier list d'animes, comportement historique) ou "character" (tier list
    # de personnages, placement via CharPlacement). Les deux familles sont séparées.
    kind = Column(String(16), nullable=False, default="anime")
    created_at = Column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_boards_user", "user_id", "position"),)


class Tier(Base):
    """Un palier d'une tier list. Rattaché à un board (et à son utilisateur)."""
    __tablename__ = "tiers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    board_id = Column(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=True, index=True)
    label = Column(String(24), nullable=False, default="")
    value = Column(Float, nullable=True)   # note associée (10, 9.5, ...) ou None (S/A/B custom)
    position = Column(Integer, nullable=False, default=0)  # ordre d'affichage (0 = tout en haut)

    __table_args__ = (Index("ix_tiers_user", "user_id", "position"),)

    user = relationship("User", back_populates="tiers")


class Placement(Base):
    """Position d'un anime dans une tier list CUSTOM (les listes de base utilisent
    Entry.tier_id / Entry.tier_pos). Une ligne par (board, entrée)."""
    __tablename__ = "placements"
    id = Column(Integer, primary_key=True, autoincrement=True)
    board_id = Column(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False)
    entry_id = Column(Integer, ForeignKey("entries.id", ondelete="CASCADE"), nullable=False)
    tier_id = Column(Integer, ForeignKey("tiers.id", ondelete="SET NULL"), nullable=True)
    pos = Column(Float, nullable=False, default=0)  # ordre dans le palier

    __table_args__ = (
        UniqueConstraint("board_id", "entry_id", name="uq_board_entry"),
        Index("ix_placements_board", "board_id"),
    )


class CharPlacement(Base):
    """Position d'un personnage dans une tier list de PERSONNAGES (board kind='character').

    Les tier lists de personnages n'ont pas de « notes » : chaque personnage ajouté au
    board est une ligne ici (tier_id = NULL tant qu'il est « à classer »). Une ligne par
    (board, personnage).
    """
    __tablename__ = "char_placements"
    id = Column(Integer, primary_key=True, autoincrement=True)
    board_id = Column(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False)
    char_id = Column(Integer, ForeignKey("characters.id", ondelete="CASCADE"), nullable=False)
    tier_id = Column(Integer, ForeignKey("tiers.id", ondelete="SET NULL"), nullable=True)
    pos = Column(Float, nullable=False, default=0)  # ordre dans le palier

    __table_args__ = (
        UniqueConstraint("board_id", "char_id", name="uq_board_char"),
        Index("ix_char_placements_board", "board_id"),
    )

    character = relationship("Character")


class WatchEvent(Base):
    """Un épisode regardé, à un instant donné. Alimente la heatmap d'activité du
    profil (une case par jour, intensité = nombre d'épisodes du jour).

    Source : l'extension navigateur (crunchyroll / anime-sama) ou une saisie manuelle
    sur le site. `anime_id` pointe sur l'anime racine quand on a réussi à le résoudre
    dans le catalogue ; sinon on garde uniquement `raw_title` (le titre brut vu par
    l'extension). `day` est la date locale « YYYY-MM-DD » (fuseau config.WATCH_TZ),
    pré-calculée pour grouper la heatmap sans refaire la conversion de fuseau en SQL.
    """
    __tablename__ = "watch_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    anime_id = Column(Integer, ForeignKey("anime.id", ondelete="SET NULL"), nullable=True)
    raw_title = Column(String(400), nullable=False, default="")
    episode = Column(Integer, nullable=True)               # n° d'épisode si connu
    source = Column(String(32), nullable=False, default="manual")  # crunchyroll/anime-sama/manual
    watched_at = Column(DateTime(timezone=True), default=utcnow)
    day = Column(String(10), nullable=False, default="")   # "YYYY-MM-DD" locale
    created_at = Column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_watch_user_day", "user_id", "day"),
        Index("ix_watch_user_at", "user_id", "watched_at"),
    )

    user = relationship("User", back_populates="watch_events")
    anime = relationship("Anime")
