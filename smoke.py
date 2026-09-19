"""Test de fumée : vérifie routes publiques + connectées + POST (note, tier, ajout)."""
import base64
import json

from itsdangerous import TimestampSigner
from fastapi.testclient import TestClient

from app import config, tiers
from app.db import SessionLocal, init_db
from app.main import app
from app.models import Anime, Entry, User

init_db()
UID = "123456789012345678"
s = SessionLocal()
if not s.get(User, UID):
    s.add(User(id=UID, username="tester", global_name="Tester", avatar="", is_public=True))
    s.commit()
tiers.ensure_default_tiers(s, UID)
s.close()

signer = TimestampSigner(config.SESSION_SECRET)
sess = {"user": {"id": UID, "username": "tester", "global_name": "Tester", "avatar": ""},
        "csrf": "testcsrf"}
cookie = signer.sign(base64.b64encode(json.dumps(sess).encode())).decode()

client = TestClient(app, follow_redirects=False)
client.cookies.set("session", cookie)

fails = 0
def check(method, url, ok=(200,), **kw):
    global fails
    r = getattr(client, method)(url, **kw)
    status = "OK " if r.status_code in ok else "!! "
    if r.status_code not in ok:
        fails += 1
    print(f"{status}{method.upper():4} {url} -> {r.status_code}")
    return r

# --- Pages publiques / connectées ---
for u in ["/", "/healthz", "/users", "/profile", "/list", "/tierlist",
          "/ranking?scope=anilist", "/ranking?scope=global", "/ranking?scope=me"]:
    check("get", u)

# --- Recherche réelle (réseau AniList) ---
r = check("get", "/search?q=frieren")
sess_db = SessionLocal()
anime = sess_db.query(Anime).first()
print("   anime en cache après recherche:", anime.id if anime else None,
      "-", (anime.english or anime.romaji) if anime else "")
sess_db.close()

if anime:
    aid = anime.id
    check("post", "/entry/add", ok=(303,),
          data={"anime_id": aid, "status": "finished", "csrf": "testcsrf"})
    sdb = SessionLocal()
    e = sdb.query(Entry).filter_by(user_id=UID, anime_id=aid).first()
    eid = e.id if e else None
    sdb.close()
    print("   entry créée:", eid)
    if eid:
        r = check("post", "/entry/rate", data={"entry_id": eid, "rating": "9.5", "csrf": "testcsrf"})
        print("   note ->", r.json())
        # vérifie l'auto-placement dans un palier 9.5 fraîchement créé
        sdb = SessionLocal()
        e = sdb.get(Entry, eid)
        t = sdb.get(type(e).tier.property.mapper.class_, e.tier_id) if e.tier_id else None
        print("   auto-tier:", (t.label, t.value) if t else None)
        # test drag & drop vers le palier valeur 8
        from app.models import Tier
        t8 = sdb.query(Tier).filter_by(user_id=UID, value=8.0).first()
        sdb.close()
        if t8:
            r = check("post", "/tier/place", json={"entry_id": eid, "tier_id": t8.id, "csrf": "testcsrf"})
            print("   place ->", r.json())
            sdb = SessionLocal()
            e = sdb.get(Entry, eid)
            print("   après place: tier_id=", e.tier_id, "rating=", e.rating)
            sdb.close()
        check("post", "/entry/remove", ok=(303,), data={"entry_id": eid, "csrf": "testcsrf"})

# --- CSRF invalide doit être rejeté ---
check("post", "/entry/add", ok=(403,),
      data={"anime_id": 1, "status": "finished", "csrf": "MAUVAIS"})

print("\nÉCHECS:", fails)
raise SystemExit(1 if fails else 0)
