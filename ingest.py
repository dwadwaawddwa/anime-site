"""Pré-ingestion du catalogue : top N animes populaires de Kitsu + genres + regroupement
par franchise (root_id). À lancer une fois (et re-lançable pour rafraîchir/étendre).

Usage : ./venv/bin/python ingest.py [nb_pages]   (20 animes par page, défaut 25 = 500)
"""
import asyncio
import sys

from app.db import SessionLocal, init_db
from app import catalog, series
from app.models import Anime


async def main(pages: int):
    init_db()
    s = SessionLocal()
    try:
        got = 0
        for p in range(pages):
            batch = await catalog.browse(s, offset=p * 20, limit=20)
            got += len(batch)
            print(f"[browse] page {p + 1}/{pages} : +{len(batch)} (cumul {got})", flush=True)
            if not batch:
                break
        total, groups = series.regroup_all(s)
        roots = s.query(Anime).filter(Anime.root_id == Anime.id).count()
        print(f"[fini] {total} animes en cache, {groups} franchises ({roots} racines).", flush=True)
    finally:
        s.close()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    asyncio.run(main(n))
