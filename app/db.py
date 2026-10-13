"""Initialisation de la base et session factory."""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from . import config
from .models import Base

connect_args = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, connect_args=connect_args, future=True)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    """Active les clés étrangères et le mode WAL pour SQLite."""
    if config.DATABASE_URL.startswith("sqlite"):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def _column_names(conn, table: str) -> set[str]:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _migrate(conn):
    """Migrations légères et idempotentes pour SQLite (ADD COLUMN si manquant)."""
    if not config.DATABASE_URL.startswith("sqlite"):
        return
    adds = {
        "anime": [
            ("popularity", "INTEGER DEFAULT 0"),
            ("root_id", "INTEGER DEFAULT 0"),
            ("group_key", "VARCHAR(200) DEFAULT ''"),
        ],
        "users": [
            ("custom_avatar", "INTEGER DEFAULT 0"),
            ("top1_id", "INTEGER"),
            ("top2_id", "INTEGER"),
            ("top3_id", "INTEGER"),
            ("api_token", "VARCHAR(64)"),
            ("char1_id", "INTEGER"),
            ("char2_id", "INTEGER"),
            ("char3_id", "INTEGER"),
            ("login_key", "VARCHAR(32)"),
            ("password_hash", "VARCHAR(255)"),
        ],
        "entries": [
            ("tier_pos", "FLOAT DEFAULT 0"),
        ],
        "tiers": [
            ("board_id", "INTEGER"),
        ],
        "boards": [
            ("kind", "VARCHAR(16) DEFAULT 'anime'"),
        ],
    }
    for table, cols in adds.items():
        existing = _column_names(conn, table)
        for name, ddl in cols:
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    # index (créés après les colonnes)
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_anime_root_id ON anime(root_id)")
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_anime_group_key ON anime(group_key)")
    # pseudo unique (insensible à la casse : on stocke la clé en minuscules) ; les
    # comptes Discord ont login_key NULL, et SQLite autorise plusieurs NULL.
    conn.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_login_key ON users(login_key)")


def init_db():
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        _migrate(conn)
