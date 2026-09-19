"""Test ciblé de l'upload de photo de profil."""
import base64
import io
import json
import os

from itsdangerous import TimestampSigner
from fastapi.testclient import TestClient
from PIL import Image

from app import config
from app.db import SessionLocal, init_db
from app.main import app, UPLOAD_DIR
from app.models import User

init_db()
UID = "999888777666555444"
s = SessionLocal()
if not s.get(User, UID):
    s.add(User(id=UID, username="ava", global_name="Ava", avatar=""))
    s.commit()
s.close()

signer = TimestampSigner(config.SESSION_SECRET)
sess = {"user": {"id": UID, "username": "ava", "global_name": "Ava", "avatar": "",
                 "custom_avatar": 0}, "csrf": "tok"}
cookie = signer.sign(base64.b64encode(json.dumps(sess).encode())).decode()
client = TestClient(app, follow_redirects=False)
client.cookies.set("session", cookie)

fails = 0
def expect(name, cond):
    global fails
    print(("OK " if cond else "!! ") + name)
    if not cond:
        fails += 1

# 1) upload d'une vraie image PNG (dégradé)
buf = io.BytesIO()
Image.new("RGB", (500, 300), (100, 60, 200)).save(buf, "PNG")
buf.seek(0)
r = client.post("/profile/avatar", data={"csrf": "tok"},
                files={"photo": ("me.png", buf, "image/png")})
expect(f"upload PNG -> 303 (got {r.status_code})", r.status_code == 303)
path = os.path.join(UPLOAD_DIR, f"{UID}.jpg")
expect("fichier .jpg créé", os.path.exists(path))
if os.path.exists(path):
    im = Image.open(path)
    expect(f"image 256x256 JPEG (got {im.size}, {im.format})",
           im.size == (256, 256) and im.format == "JPEG")
s = SessionLocal(); row = s.get(User, UID); ver = row.custom_avatar; s.close()
expect(f"custom_avatar incrémenté (={ver})", ver == 1)

# 2) fichier non-image rejeté (400)
r = client.post("/profile/avatar", data={"csrf": "tok"},
                files={"photo": ("x.txt", io.BytesIO(b"pas une image"), "text/plain")})
expect(f"fichier texte rejeté -> 400 (got {r.status_code})", r.status_code == 400)

# 3) mauvais CSRF rejeté (403)
buf.seek(0)
r = client.post("/profile/avatar", data={"csrf": "WRONG"},
                files={"photo": ("me.png", buf, "image/png")})
expect(f"CSRF invalide -> 403 (got {r.status_code})", r.status_code == 403)

# 4) suppression -> 303, fichier supprimé, custom_avatar=0
r = client.post("/profile/avatar/remove", data={"csrf": "tok"})
expect(f"remove -> 303 (got {r.status_code})", r.status_code == 303)
expect("fichier supprimé", not os.path.exists(path))
s = SessionLocal(); row = s.get(User, UID); ver = row.custom_avatar; s.close()
expect(f"custom_avatar remis à 0 (={ver})", ver == 0)

print("\nÉCHECS:", fails)
raise SystemExit(1 if fails else 0)
