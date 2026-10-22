"""Site de notation d'anime — anime.dwa.gold.

FastAPI + SQLite. Login Discord (OAuth identify). Chaque utilisateur a sa liste,
ses notes, sa tier list. Un réglage de profil permet de rendre ses notes publiques
(comptées dans le classement global).
"""
import csv
import hashlib
import hmac
import io
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import httpx
import time

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import (accounts, catalog, characters, config, discord_api, grouping,
               security, series, tiers)
from .db import SessionLocal, init_db
from .models import (Anime, Board, CharPlacement, Character, Entry, Placement,
                     STATUS_FINISHED, STATUS_LABELS, STATUS_WATCHING, STATUSES,
                     Tier, User, WatchEvent, utcnow)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = FastAPI(title="Anime DWA")

app.add_middleware(security.SecurityHeadersMiddleware)
# CORS restreint aux extensions navigateur : l'extension (service worker MV3) envoie
# l'activité sur /api/*. Les extensions avec host_permissions contournent déjà CORS,
# mais on renvoie les en-têtes proprement pour les content scripts et le debug. Pas
# de cookies ici (auth par jeton Bearer) → allow_credentials reste False.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(chrome-extension|moz-extension)://.*$",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["authorization", "content-type"],
    allow_credentials=False,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET,
    https_only=config.COOKIE_SECURE,
    same_site="lax",
    max_age=60 * 60 * 24 * 30,
)

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
# Photos de profil uploadées (uniquement des JPEG ré-encodés par nos soins).
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["music_url"] = config.MUSIC_URL  # liens vers music.dwa.gold (vide = masqués)
templates.env.globals["blindtest_url"] = config.BLINDTEST_URL

MAX_AVATAR_BYTES = 6 * 1024 * 1024  # 6 Mo max en entrée
ASSET_V = str(int(time.time()))     # cache-buster des assets (change à chaque redémarrage)

# Compat : Starlette récent veut TemplateResponse(request, name, context). On garde
# la forme (name, context) partout et on injecte `request` depuis le contexte.
_orig_template_response = templates.TemplateResponse


def _template_response(name, context=None, **kw):
    ctx_ = context or {}
    return _orig_template_response(ctx_.get("request"), name, ctx_, **kw)


templates.TemplateResponse = _template_response


@app.on_event("startup")
def _startup():
    init_db()


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def db_session():
    return SessionLocal()


def session_user(row: User) -> dict:
    """Données de l'utilisateur gardées dans le cookie de session (signé, non chiffré :
    jamais de secret dedans)."""
    u = {
        "id": row.id,
        "username": row.username,
        "global_name": row.global_name,
        "avatar": row.avatar,
        "custom_avatar": row.custom_avatar,
    }
    if row.password_hash:
        u["auth"] = "local"
    return u


def _local_session_valid(request: Request, u: dict) -> bool:
    """Compte pseudo/mdp : la session doit encore exister côté serveur (pas de
    déconnexion / changement de mot de passe depuis). Mis en cache par requête."""
    cached = getattr(request.state, "local_ok", None)
    if cached is not None:
        return cached
    session = db_session()
    try:
        ok = accounts.login_session_valid(session, u["id"], u.get("sid"))
    finally:
        session.close()
    request.state.local_ok = ok
    return ok


def current_user(request: Request) -> dict | None:
    u = request.session.get("user")
    if u:
        if u.get("auth") == "local" and not _local_session_valid(request, u):
            request.session.clear()
            return None
        return u
    # Mode LOCAL : pas de Discord — on ouvre automatiquement la session du proprio.
    if config.LOCAL_USER_ID:
        session = db_session()
        try:
            row = session.get(User, config.LOCAL_USER_ID)
            if row is not None:
                tiers.ensure_default_tiers(session, row.id)
                u = {
                    "id": row.id,
                    "username": row.username,
                    "global_name": row.global_name,
                    "avatar": row.avatar,
                    "custom_avatar": row.custom_avatar,
                }
                request.session["user"] = u
                return u
        finally:
            session.close()
    return None


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Connexion requise")
    return u


def client_key(request: Request) -> str:
    """Clé de rate-limit : IP réelle transmise par Cloudflare si dispo."""
    return (request.headers.get("cf-connecting-ip")
            or (request.client.host if request.client else "?"))


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_hex(16)
        request.session["csrf"] = tok
    return tok


def check_csrf(request: Request, token: str | None):
    if not token or token != request.session.get("csrf"):
        raise HTTPException(status_code=403, detail="Jeton CSRF invalide")


def check_write_rate(request: Request):
    if not security.write_limiter.allow(client_key(request)):
        raise HTTPException(status_code=429, detail="Trop de requêtes, réessaie plus tard.")


def avatar_url(u: dict | User) -> str:
    if isinstance(u, dict):
        uid, av, custom = u["id"], u.get("avatar"), u.get("custom_avatar", 0)
    else:
        uid, av, custom = u.id, u.avatar, getattr(u, "custom_avatar", 0)
    if custom:  # photo perso uploadée (JPEG ré-encodé), servie depuis notre domaine
        return f"/uploads/{uid}.jpg?v={custom}"
    if not str(uid).isdigit():  # compte pseudo/mdp (pas Discord) sans photo
        return "/static/avatar-default.svg"
    if av:
        return f"https://cdn.discordapp.com/avatars/{uid}/{av}.png?size=64"
    return f"https://cdn.discordapp.com/embed/avatars/{int(uid) % 5 if str(uid).isdigit() else 0}.png"


def ctx(request: Request, **extra):
    u = current_user(request)
    base = {
        "request": request,
        "user": u,
        "csrf": csrf_token(request),
        "statuses": STATUSES,
        "status_labels": STATUS_LABELS,
        "avatar_url": avatar_url,
        "asset_v": ASSET_V,
        "is_admin": bool(u and u["id"] in config.ADMIN_BOOTSTRAP_IDS),
    }
    base.update(extra)
    return base


def get_entry(session, user_id: str, anime_id: int) -> Entry | None:
    return session.query(Entry).filter_by(user_id=user_id, anime_id=anime_id).first()


# --------------------------------------------------------------------------- #
#  Activité / heatmap (« écoutes » par jour, à la GitHub)
# --------------------------------------------------------------------------- #
try:
    _WATCH_TZ = ZoneInfo(config.WATCH_TZ)
except Exception:  # fuseau inconnu sur l'hôte -> UTC
    _WATCH_TZ = timezone.utc

# Libellés FR courts pour les étiquettes de mois de la heatmap.
_FR_MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin",
              "juil.", "août", "sept.", "oct.", "nov.", "déc."]

# Bornes (nb d'épisodes/jour) -> niveau d'intensité 0..4 de la case.
_LEVEL_BOUNDS = (1, 3, 5, 7)  # 0 ; 1-2 ; 3-4 ; 5-6 ; >=7


def local_today() -> date:
    return datetime.now(_WATCH_TZ).date()


def _fmt_local_time(dt: datetime | None) -> str:
    """Heure locale « HH:MM ». SQLite relit les dates en naïf : on les considère UTC."""
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_WATCH_TZ).strftime("%H:%M")


def local_day_str(dt: datetime | None = None) -> str:
    """Date locale « YYYY-MM-DD » d'un instant (par défaut : maintenant)."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_WATCH_TZ).date().isoformat()


def _level(count: int) -> int:
    lvl = 0
    for b in _LEVEL_BOUNDS:
        if count >= b:
            lvl += 1
    return lvl


def build_activity(session, user_id: str, weeks: int = 53) -> dict:
    """Construit les données de la heatmap : une grille de `weeks` colonnes (semaines
    commençant le lundi) x 7 lignes (lun->dim), jusqu'à aujourd'hui inclus.

    Retourne un dict prêt pour le template : weeks (liste de colonnes de 7 cases),
    month_labels (col -> libellé), et quelques totaux.
    """
    today = local_today()
    # Fin = dimanche de la semaine courante ; début = `weeks-1` semaines avant le lundi.
    end = today + timedelta(days=(6 - today.weekday()))      # dimanche courant
    start = end - timedelta(days=weeks * 7 - 1)              # lundi de la 1re colonne

    rows = (session.query(WatchEvent.day, func.count(WatchEvent.id))
            .filter(WatchEvent.user_id == user_id,
                    WatchEvent.day >= start.isoformat(),
                    WatchEvent.day <= end.isoformat())
            .group_by(WatchEvent.day).all())
    counts = {d: c for d, c in rows if d}

    cols = []
    month_labels = {}
    prev_month = None
    total = 0
    active_days = 0
    d = start
    for w in range(weeks):
        col = []
        for _ in range(7):
            iso = d.isoformat()
            c = counts.get(iso, 0)
            future = d > today
            if not future and c:
                total += c
                active_days += 1
            col.append({"date": iso, "count": c, "level": _level(c),
                        "future": future})
            d += timedelta(days=1)
        # étiquette de mois : au changement de mois, sur la 1re semaine du mois
        first_of_col = date.fromisoformat(col[0]["date"])
        if first_of_col.month != prev_month:
            month_labels[w] = _FR_MONTHS[first_of_col.month - 1]
            prev_month = first_of_col.month
        cols.append(col)

    # streak courant (jours consécutifs avec >=1 épisode, en remontant depuis aujourd'hui)
    streak = 0
    dd = today
    while counts.get(dd.isoformat(), 0) > 0:
        streak += 1
        dd -= timedelta(days=1)

    return {"weeks": cols, "month_labels": month_labels,
            "total": total, "active_days": active_days, "streak": streak,
            "start": start.isoformat(), "end": today.isoformat()}


async def resolve_anime(session, title: str) -> Anime | None:
    """Essaie de retrouver l'anime racine correspondant à un titre brut (venu de
    l'extension). Local d'abord (0 appel réseau), puis recherche Kitsu en secours."""
    t = (title or "").strip().lower()
    if not t:
        return None
    # 1) correspondance locale directe sur le blob de titres (contient souvent le
    #    titre FR/EN/romaji), la plus populaire d'abord.
    cand = (session.query(Anime).filter(Anime.search_blob.like(f"%{t}%"))
            .order_by(Anime.popularity.desc()).first())
    # 2) sinon, titre normalisé (sans marqueurs de saison ni sous-titre)
    if cand is None:
        norm = grouping.normalize(title)
        if norm:
            cand = (session.query(Anime).filter(Anime.search_blob.like(f"%{norm}%"))
                    .order_by(Anime.popularity.desc()).first())
        # 3) sinon, clé de franchise
        if cand is None:
            gk = grouping.group_key(romaji=title)
            if gk and not gk.startswith("#"):
                cand = (session.query(Anime).filter(Anime.group_key == gk)
                        .order_by(Anime.popularity.desc()).first())
    # 4) dernier recours : recherche Kitsu
    if cand is None:
        try:
            results = await catalog.search(session, title, per_page=5)
        except Exception:
            results = []
        cand = results[0] if results else None
    if cand is None:
        return None
    root = await series.ensure_root(session, cand.id)
    return root or cand


async def record_watch(session, user_id: str, title: str, episode: int | None,
                       source: str, when: datetime | None = None,
                       auto_add: bool = True, anime: Anime | None = None) -> dict:
    """Enregistre un épisode regardé : résout l'anime (sauf s'il est déjà fourni),
    crée le WatchEvent et (option) ajoute l'anime en « En cours » s'il n'est pas déjà
    dans la liste."""
    when = when or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if anime is None:
        anime = await resolve_anime(session, title)
    anime_id = anime.id if anime else None
    ev = WatchEvent(user_id=user_id, anime_id=anime_id, raw_title=(title or "")[:400],
                    episode=episode, source=source[:32] if source else "manual",
                    watched_at=when, day=local_day_str(when))
    session.add(ev)
    # Ajout auto en « En cours » (sans écraser un statut existant type « Fini »).
    if auto_add and anime_id:
        e = get_entry(session, user_id, anime_id)
        if e is None:
            session.add(Entry(user_id=user_id, anime_id=anime_id, status=STATUS_WATCHING))
    session.commit()
    return {"anime_id": anime_id,
            "matched_title": (anime.title if anime else None)}


# --------------------------------------------------------------------------- #
#  Auth Discord
# --------------------------------------------------------------------------- #
@app.get("/auth/login")
def login(request: Request):
    # Mode LOCAL : pas de Discord, la session est déjà ouverte automatiquement.
    if config.LOCAL_USER_ID:
        return RedirectResponse("/tierlist", status_code=303)
    if not config.is_configured():
        return HTMLResponse("<h1>Discord non configuré</h1>", status_code=503)
    state = secrets.token_hex(16)
    request.session["oauth_state"] = state
    return RedirectResponse(discord_api.authorize_url(state), status_code=303)


@app.get("/auth/callback")
async def callback(request: Request, code: str = "", state: str = ""):
    if not code or not state or state != request.session.get("oauth_state"):
        raise HTTPException(status_code=400, detail="État OAuth invalide")
    request.session.pop("oauth_state", None)
    token = await discord_api.exchange_code(code)
    du = await discord_api.get_current_user(token["access_token"])
    uid = str(du["id"])
    session = db_session()
    try:
        u = session.get(User, uid)
        if u is None:
            u = User(id=uid)
            session.add(u)
        u.username = du.get("username", "") or ""
        u.global_name = du.get("global_name") or du.get("username", "") or ""
        u.avatar = du.get("avatar") or ""
        u.last_login = utcnow()
        session.commit()
        tiers.ensure_default_tiers(session, uid)
        request.session["user"] = {
            "id": uid,
            "username": u.username,
            "global_name": u.global_name,
            "avatar": u.avatar,
            "custom_avatar": u.custom_avatar,
        }
    finally:
        session.close()
    # rotation de l'id de session après login (limite la fixation de session)
    request.session["csrf"] = secrets.token_hex(16)
    return RedirectResponse(_after_login(request), status_code=303)


@app.get("/logout")
def logout(request: Request):
    u = request.session.get("user")
    if u and u.get("auth") == "local":  # révoque vraiment la session côté serveur
        session = db_session()
        try:
            accounts.revoke_login_session(session, u.get("sid"))
        finally:
            session.close()
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------- #
#  SSO vers les sites satellites (music.dwa.gold, blindtest.dwa.gold) : ils délèguent
#  leur connexion à ce site
# --------------------------------------------------------------------------- #
_SSO_STATE = re.compile(r"[A-Za-z0-9_-]{16,64}")
_SSO_NEXT = re.compile(r"/sso/(music|blindtest)\?state=[A-Za-z0-9_-]{16,64}")


def _after_login(request: Request) -> str:
    """Où revenir après connexion : uniquement une route SSO connue, jamais une URL libre."""
    nxt = request.session.pop("after_login", "") or ""
    return nxt if _SSO_NEXT.fullmatch(nxt) else "/list"


@app.get("/sso/{client}")
def sso_client(request: Request, client: str, state: str = ""):
    if client not in config.SSO_CLIENTS:
        raise HTTPException(status_code=404, detail="Site inconnu")
    if not _SSO_STATE.fullmatch(state):
        raise HTTPException(status_code=400, detail="Lien de connexion invalide")
    u = current_user(request)
    if not u:
        request.session["after_login"] = f"/sso/{client}?state={state}"
        return RedirectResponse("/login", status_code=303)
    url, secret = config.SSO_CLIENTS[client]
    ts = str(int(time.time()))
    msg = f"{client}|{u['id']}|{ts}|{state}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    qs = urlencode({"uid": u["id"], "ts": ts, "state": state, "sig": sig})
    # destination fixe (config), jamais tirée de la requête → pas de redirection ouverte
    return RedirectResponse(f"{url}/auth/sso?{qs}", status_code=303)


# --------------------------------------------------------------------------- #
#  Auth pseudo + mot de passe (sans Discord, sans e-mail)
# --------------------------------------------------------------------------- #
def _auth_page(request: Request, name: str, status_code: int = 200, **extra):
    resp = templates.TemplateResponse(
        name, ctx(request, discord_ok=config.is_configured(), **extra),
        status_code=status_code)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _csrf_ok(request: Request, token: str) -> bool:
    good = request.session.get("csrf")
    return bool(token and good and secrets.compare_digest(token, good))


def _open_session(request: Request, session, row: User):
    """Repart d'une session vierge (rien de l'état anonyme n'est conservé) et
    l'enregistre côté serveur."""
    request.session.clear()
    request.session["user"] = {**session_user(row),
                               "sid": accounts.create_login_session(session, row.id)}
    request.session["csrf"] = secrets.token_hex(16)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse(_after_login(request), status_code=303)
    return _auth_page(request, "login.html")


@app.post("/login")
def login_submit(request: Request, pseudo: str = Form(""), password: str = Form(""),
                 csrf: str = Form("")):
    pseudo = pseudo.strip()[:32]

    def fail(msg: str, code: int):
        return _auth_page(request, "login.html", status_code=code, pseudo=pseudo, error=msg)

    if not _csrf_ok(request, csrf):
        return fail("Session expirée, réessaie.", 403)
    key = "acct:" + accounts.pseudo_key(pseudo)
    if (not security.login_ip_limiter.allow(client_key(request))
            or security.login_fail_limiter.blocked(key)):
        return fail("Trop de tentatives. Patiente quelques minutes avant de réessayer.", 429)
    session = db_session()
    try:
        row = accounts.find_by_pseudo(session, pseudo)
        ok = len(password) <= accounts.PASSWORD_MAX and accounts.verify_password(row, password)
        if not ok:
            security.login_fail_limiter.hit(key)
            # même message que le pseudo existe ou non (pas d'énumération de comptes)
            return fail("Pseudo ou mot de passe incorrect.", 401)
        security.login_fail_limiter.reset(key)
        row.last_login = utcnow()
        session.commit()  # inclut un éventuel re-hachage aux nouveaux paramètres
        tiers.ensure_default_tiers(session, row.id)
        nxt = _after_login(request)
        _open_session(request, session, row)
    finally:
        session.close()
    return RedirectResponse(nxt, status_code=303)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    if current_user(request):
        return RedirectResponse(_after_login(request), status_code=303)
    return _auth_page(request, "register.html")


@app.post("/register")
def register_submit(request: Request, pseudo: str = Form(""), password: str = Form(""),
                    password2: str = Form(""), website: str = Form(""),
                    csrf: str = Form("")):
    pseudo = pseudo.strip()

    def fail(msg: str, code: int = 400):
        return _auth_page(request, "register.html", status_code=code,
                          pseudo=pseudo[:32], error=msg)

    if not _csrf_ok(request, csrf):
        return fail("Session expirée, réessaie.", 403)
    check_write_rate(request)
    if website:  # pot de miel : champ caché que seuls les robots remplissent
        return fail("Inscription refusée.")
    ip = client_key(request)
    if security.register_limiter.blocked(ip):
        return fail("Trop d'inscriptions depuis ta connexion, réessaie dans une heure.", 429)
    session = db_session()
    try:
        err = accounts.check_pseudo(session, pseudo) or accounts.check_password(password, pseudo)
        if not err and password != password2:
            err = "Les deux mots de passe ne correspondent pas."
        if err:
            return fail(err)
        row = User(id=accounts.new_local_id(session), username=pseudo, global_name=pseudo,
                   avatar="", login_key=accounts.pseudo_key(pseudo),
                   password_hash=accounts.hash_password(password))
        session.add(row)
        try:
            session.commit()
        except IntegrityError:  # même pseudo créé à l'instant par quelqu'un d'autre
            session.rollback()
            return fail("Ce pseudo est déjà pris.")
        security.register_limiter.hit(ip)
        tiers.ensure_default_tiers(session, row.id)
        nxt = _after_login(request)
        _open_session(request, session, row)
    finally:
        session.close()
    return RedirectResponse(nxt, status_code=303)


@app.post("/profile/password")
def profile_password(request: Request, current: str = Form(""), new: str = Form(""),
                     new2: str = Form(""), csrf: str = Form(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    key = "pwchange:" + u["id"]
    session = db_session()
    try:
        row = session.get(User, u["id"])
        if row is None or not row.password_hash:
            raise HTTPException(status_code=400, detail="Ce compte se connecte via Discord.")
        if security.login_fail_limiter.blocked(key):
            err = "Trop de tentatives. Patiente quelques minutes."
        elif len(current) > accounts.PASSWORD_MAX or not accounts.verify_password(row, current):
            security.login_fail_limiter.hit(key)
            err = "Mot de passe actuel incorrect."
        else:
            err = accounts.check_password(new, row.username)
            if not err and new != new2:
                err = "Les deux nouveaux mots de passe ne correspondent pas."
            if not err and new == current:
                err = "Le nouveau mot de passe doit être différent de l'actuel."
        if err:
            request.session["pw_flash"] = {"ok": False, "msg": err}
            return RedirectResponse("/profile#password", status_code=303)
        security.login_fail_limiter.reset(key)
        row.password_hash = accounts.hash_password(new)
        # révoque toutes les sessions du compte ; celle-ci est rouverte juste après
        accounts.revoke_all_sessions(session, row.id)
        session.commit()
        _open_session(request, session, row)
    finally:
        session.close()
    request.session["pw_flash"] = {
        "ok": True, "msg": "Mot de passe changé. Tes autres sessions ont été déconnectées."}
    return RedirectResponse("/profile#password", status_code=303)


# --------------------------------------------------------------------------- #
#  Accueil + recherche
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", ctx(request))


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str = ""):
    items = []
    my_ids = set()
    error = None
    q = (q or "").strip()[:100]
    if q:
        if not security.search_limiter.allow(client_key(request)):
            error = "Trop de recherches, patiente une minute."
        else:
            session = db_session()
            try:
                results = await catalog.search(session, q)
                # On regroupe par franchise : une seule carte (la racine) même si la
                # recherche remonte plusieurs saisons/films. On dédoublonne d'abord par
                # clé de franchise pour n'appeler le regroupement qu'une fois par série.
                seen_keys, seen_roots = set(), set()
                for r in results:
                    if r.group_key in seen_keys:
                        continue
                    seen_keys.add(r.group_key)
                    root = await series.ensure_root(session, r.id) or r
                    if root.id in seen_roots:
                        continue
                    seen_roots.add(root.id)
                    items.append({"anime": root,
                                  "seasons": series.season_count(session, root.id)})
                u = current_user(request)
                if u:
                    my_ids = {row[0] for row in
                              session.query(Entry.anime_id).filter_by(user_id=u["id"]).all()}
            except Exception:
                error = "La base d’nimes est momentanément indisponible, réessaie."
            finally:
                session.close()
    return templates.TemplateResponse(
        "search.html", ctx(request, q=q, items=items, my_ids=my_ids, error=error))


@app.get("/anime/{anime_id}", response_class=HTMLResponse)
async def anime_detail(request: Request, anime_id: int):
    session = db_session()
    try:
        a = await catalog.fetch_by_id(session, anime_id)
        if a is None:
            raise HTTPException(status_code=404, detail="Anime introuvable")
        # On regroupe par franchise : on affiche toujours la page de la racine.
        root = await series.ensure_root(session, anime_id)
        if root and root.id != anime_id:
            return RedirectResponse(f"/anime/{root.id}", status_code=302)
        root = root or a
        seasons = series.seasons_of(session, root.id)
        entry = None
        u = current_user(request)
        if u:
            entry = get_entry(session, u["id"], root.id)
        avg, cnt = public_average(session, root.id)
        return templates.TemplateResponse(
            "anime.html",
            ctx(request, a=root, seasons=seasons, entry=entry,
                rating_steps=RATING_STEPS, pub_avg=avg, pub_count=cnt))
    finally:
        session.close()


# --------------------------------------------------------------------------- #
#  Catalogue (tout Kitsu, en cache local, regroupé par franchise)
# --------------------------------------------------------------------------- #
CATALOGUE_PER_PAGE = 24


@app.get("/catalogue", response_class=HTMLResponse)
def catalogue(request: Request, page: int = 1, genre: str = ""):
    page = max(page, 1)
    genre = (genre or "").strip()[:40]
    session = db_session()
    try:
        # Seuls les animes "racine" (représentant leur franchise) sont listés.
        q = session.query(Anime).filter(Anime.root_id == Anime.id)
        if genre:
            q = q.filter(Anime.genres.like(f"%{genre}%"))
        total = q.count()
        rows = (q.order_by(Anime.popularity.desc(), Anime.id)
                .offset((page - 1) * CATALOGUE_PER_PAGE)
                .limit(CATALOGUE_PER_PAGE).all())
        items = [{"anime": a, "seasons": series.season_count(session, a.id)} for a in rows]
        pages = max((total + CATALOGUE_PER_PAGE - 1) // CATALOGUE_PER_PAGE, 1)
        return templates.TemplateResponse(
            "catalogue.html",
            ctx(request, items=items, page=page, pages=pages, total=total,
                genre=genre, genres=all_genres(session)))
    finally:
        session.close()


# --------------------------------------------------------------------------- #
#  Liste personnelle : ajout / statut / note / commentaire / suppression
# --------------------------------------------------------------------------- #
RATING_STEPS = [round(x * 0.5, 1) for x in range(1, 21)]  # 0.5 .. 10.0


def _clamp_rating(value) -> float | None:
    try:
        v = round(float(value) * 2) / 2  # arrondi au 0.5
    except (TypeError, ValueError):
        return None
    if v < 0.5 or v > 10:
        return None
    return v


@app.get("/list", response_class=HTMLResponse)
def my_list(request: Request):
    u = require_user(request)
    session = db_session()
    try:
        entries = (session.query(Entry)
                   .filter_by(user_id=u["id"])
                   .order_by(Entry.updated_at.desc())
                   .all())
        grouped = {s: [] for s in STATUSES}
        for e in entries:
            grouped.setdefault(e.status, []).append(e)
        return templates.TemplateResponse(
            "list.html", ctx(request, grouped=grouped, rating_steps=RATING_STEPS))
    finally:
        session.close()


@app.post("/entry/add")
async def entry_add(request: Request, anime_id: int = Form(...),
                    status: str = Form(STATUS_FINISHED), csrf: str = Form(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    if status not in STATUSES:
        raise HTTPException(status_code=400, detail="Statut invalide")
    session = db_session()
    try:
        a = await catalog.fetch_by_id(session, anime_id)
        if a is None:
            raise HTTPException(status_code=404, detail="Anime introuvable")
        # On regroupe par franchise : l'entrée se pose sur l'anime racine (saison 1).
        root = await series.ensure_root(session, anime_id)
        rid = root.id if root else anime_id
        e = get_entry(session, u["id"], rid)
        if e is None:
            e = Entry(user_id=u["id"], anime_id=rid, status=status)
            session.add(e)
        else:
            e.status = status
        session.commit()
    finally:
        session.close()
    return RedirectResponse(request.headers.get("referer") or "/list", status_code=303)


def _get_own_entry(session, user_id, entry_id) -> Entry:
    e = session.get(Entry, entry_id)
    if e is None or e.user_id != user_id:
        raise HTTPException(status_code=404, detail="Entrée introuvable")
    return e


@app.post("/entry/status")
def entry_status(request: Request, entry_id: int = Form(...),
                 status: str = Form(...), csrf: str = Form(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    if status not in STATUSES:
        raise HTTPException(status_code=400, detail="Statut invalide")
    session = db_session()
    try:
        e = _get_own_entry(session, u["id"], entry_id)
        e.status = status
        if status != STATUS_FINISHED:
            e.tier_id = None
        else:
            tiers.auto_place(session, u["id"], e)
        session.commit()
    finally:
        session.close()
    return RedirectResponse(request.headers.get("referer") or "/list", status_code=303)


@app.post("/entry/rate")
def entry_rate(request: Request, entry_id: int = Form(...),
               rating: str = Form(...), csrf: str = Form(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    try:
        e = _get_own_entry(session, u["id"], entry_id)
        if rating == "" or rating == "0":
            e.rating = None
            e.tier_id = None
        else:
            r = _clamp_rating(rating)
            if r is None:
                raise HTTPException(status_code=400, detail="Note invalide")
            e.rating = r
            if e.status == STATUS_FINISHED:
                tiers.auto_place(session, u["id"], e)
        session.commit()
        return JSONResponse({"ok": True, "rating": e.rating})
    finally:
        session.close()


@app.post("/entry/comment")
def entry_comment(request: Request, entry_id: int = Form(...),
                  comment: str = Form(""), csrf: str = Form(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    try:
        e = _get_own_entry(session, u["id"], entry_id)
        e.comment = (comment or "").strip()[:2000]
        session.commit()
    finally:
        session.close()
    return RedirectResponse(request.headers.get("referer") or "/list", status_code=303)


@app.post("/entry/remove")
def entry_remove(request: Request, entry_id: int = Form(...), csrf: str = Form(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    try:
        e = _get_own_entry(session, u["id"], entry_id)
        session.delete(e)
        session.commit()
    finally:
        session.close()
    return RedirectResponse(request.headers.get("referer") or "/list", status_code=303)


# --------------------------------------------------------------------------- #
#  Tier list
# --------------------------------------------------------------------------- #
def build_tierlist(session, user_id: str):
    """Tier list « de base » d'un utilisateur (utilisée sur le profil public)."""
    board = tiers.ensure_default_board(session, user_id)
    tiers.ensure_default_tiers(session, user_id)
    return tiers.build_board(session, board)


def _own_board(session, user_id: str, board_id) -> Board | None:
    if not board_id:
        return None
    b = session.get(Board, int(board_id))
    return b if b is not None and b.user_id == user_id else None


@app.get("/tierlist", response_class=HTMLResponse)
def tierlist_page(request: Request, board: int = 0, kind: str = "anime"):
    u = require_user(request)
    session = db_session()
    try:
        # Le board demandé (s'il existe) impose son type ; sinon on suit `kind`.
        cur = _own_board(session, u["id"], board)
        kind = cur.kind if cur is not None else ("character" if kind == "character" else "anime")

        mylist = []
        if kind == "character":
            default = tiers.ensure_default_char_board(session, u["id"])
            cur = cur or default
            tier_rows, by_tier, unranked = tiers.build_char_board(session, cur)
            # Animes de la liste de l'utilisateur, pour « parcourir par anime ».
            rows = (session.query(Anime).join(Entry, Entry.anime_id == Anime.id)
                    .filter(Entry.user_id == u["id"]).all())
            mylist = sorted(rows, key=lambda a: a.title.lower())
        else:
            default = tiers.ensure_default_board(session, u["id"])
            tiers.ensure_default_tiers(session, u["id"])
            cur = cur or default
            tier_rows, by_tier, unranked = tiers.build_board(session, cur)

        return templates.TemplateResponse(
            "tierlist.html",
            ctx(request, tier_rows=tier_rows, by_tier=by_tier, unranked=unranked,
                owner=True, prof=session.get(User, u["id"]), kind=kind, mylist=mylist,
                board=cur, boards=tiers.get_boards(session, u["id"], kind=kind)))
    finally:
        session.close()


# --------------------------------------------------------------------------- #
#  Boards (plusieurs tier lists)
# --------------------------------------------------------------------------- #
@app.post("/board/new")
def board_new(request: Request, name: str = Form(""), csrf: str = Form(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    try:
        board = tiers.create_custom_board(session, u["id"], name)
        bid = board.id
    finally:
        session.close()
    return RedirectResponse(f"/tierlist?board={bid}", status_code=303)


@app.post("/charboard/new")
def charboard_new(request: Request, name: str = Form(""), csrf: str = Form(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    try:
        board = tiers.create_char_board(session, u["id"], name)
        bid = board.id
    finally:
        session.close()
    return RedirectResponse(f"/tierlist?board={bid}", status_code=303)


@app.post("/board/rename")
def board_rename(request: Request, board_id: int = Form(...),
                 name: str = Form(...), csrf: str = Form(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    try:
        b = _own_board(session, u["id"], board_id)
        if b is None:
            raise HTTPException(status_code=404, detail="Liste introuvable")
        b.name = (name or "").strip()[:60] or b.name
        session.commit()
    finally:
        session.close()
    return RedirectResponse(f"/tierlist?board={board_id}", status_code=303)


@app.post("/board/remove")
def board_remove(request: Request, board_id: int = Form(...), csrf: str = Form(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    try:
        b = _own_board(session, u["id"], board_id)
        if b is None:
            raise HTTPException(status_code=404, detail="Liste introuvable")
        if b.is_default:
            raise HTTPException(status_code=400, detail="Impossible de supprimer la liste de base")
        kind = b.kind
        # Suppression explicite : la colonne tiers.board_id ajoutée par migration n'a
        # pas de contrainte FK sur les bases existantes → pas de cascade fiable.
        if kind == "character":
            session.query(CharPlacement).filter_by(board_id=b.id).delete()
        else:
            session.query(Placement).filter_by(board_id=b.id).delete()
        session.query(Tier).filter_by(board_id=b.id).delete()
        session.delete(b)
        session.commit()
    finally:
        session.close()
    back = "/tierlist?kind=character" if kind == "character" else "/tierlist"
    return RedirectResponse(back, status_code=303)


@app.post("/board/reset")
def board_reset(request: Request, board_id: int = Form(...), csrf: str = Form(...)):
    """Vide un board custom : tout repart dans « À classer »."""
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    try:
        b = _own_board(session, u["id"], board_id)
        if b is None or b.is_default:
            raise HTTPException(status_code=400, detail="Liste invalide")
        if b.kind == "character":
            tiers.clear_char_board(session, b)
        else:
            tiers.clear_board(session, b)
    finally:
        session.close()
    return RedirectResponse(f"/tierlist?board={board_id}", status_code=303)


async def _json_body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


def _place_default(session, user_id, e, tid, order):
    """Placement sur le board de base : porté par Entry.tier_id / Entry.tier_pos.
    Déposer dans un palier chiffré met la note à jour."""
    if tid in (None, "", "unranked"):
        e.tier_id = None
    else:
        t = session.get(Tier, int(tid))
        if t is None or t.user_id != user_id:
            raise HTTPException(status_code=404, detail="Palier introuvable")
        e.tier_id = t.id
        if t.value is not None:
            e.rating = t.value
    if isinstance(order, list):
        rank = {int(eid): i for i, eid in enumerate(order)}
        for oe in (session.query(Entry)
                   .filter(Entry.user_id == user_id,
                           Entry.id.in_([int(x) for x in order] or [0])).all()):
            oe.tier_pos = rank.get(oe.id, 0)


def _place_custom(session, board, e, tid, order):
    """Placement sur un board custom : porté par la table Placement (n'affecte pas la note)."""
    # Index local des placements : la session est en autoflush=False, donc une requête
    # ne verrait pas un Placement tout juste ajouté → doublon (UNIQUE board_id, entry_id).
    known = {p.entry_id: p for p in
             session.query(Placement).filter_by(board_id=board.id).all()}

    def upsert(entry_id) -> Placement:
        p = known.get(entry_id)
        if p is None:
            p = Placement(board_id=board.id, entry_id=entry_id)
            session.add(p)
            known[entry_id] = p
        return p

    new_tid = None
    if tid not in (None, "", "unranked"):
        t = session.get(Tier, int(tid))
        if t is None or t.board_id != board.id:
            raise HTTPException(status_code=404, detail="Palier introuvable")
        new_tid = t.id
    p = upsert(e.id)
    p.tier_id = new_tid
    if isinstance(order, list):
        rank = {int(eid): i for i, eid in enumerate(order)}
        # seulement les entrées du propriétaire de la liste
        own = {eid for (eid,) in session.query(Entry.id).filter(
            Entry.user_id == board.user_id, Entry.id.in_(list(rank) or [0]))}
        for eid in rank:
            if eid not in own:
                continue
            op = upsert(eid)
            op.tier_id = new_tid
            op.pos = rank[eid]


@app.post("/tier/place")
async def tier_place(request: Request):
    u = require_user(request)
    check_write_rate(request)
    body = await _json_body(request)
    check_csrf(request, body.get("csrf"))
    session = db_session()
    try:
        e = _get_own_entry(session, u["id"], int(body.get("entry_id", 0)))
        board = _own_board(session, u["id"], body.get("board_id")) \
            or tiers.ensure_default_board(session, u["id"])
        tid = body.get("tier_id")
        order = body.get("order") or []
        if board.is_default:
            _place_default(session, u["id"], e, tid, order)
        else:
            _place_custom(session, board, e, tid, order)
        session.commit()
        return JSONResponse({"ok": True})
    finally:
        session.close()


@app.post("/tier/rename")
def tier_rename(request: Request, tier_id: int = Form(...),
                label: str = Form(...), csrf: str = Form(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    back = "/tierlist"
    try:
        t = session.get(Tier, tier_id)
        if t is None or t.user_id != u["id"]:
            raise HTTPException(status_code=404, detail="Palier introuvable")
        t.label = (label or "").strip()[:24] or t.label
        back = f"/tierlist?board={t.board_id}"
        session.commit()
    finally:
        session.close()
    return RedirectResponse(back, status_code=303)


@app.post("/tier/add")
def tier_add(request: Request, board_id: int = Form(...), value: str = Form(""),
             label: str = Form(""), csrf: str = Form(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    try:
        b = _own_board(session, u["id"], board_id)
        if b is None:
            raise HTTPException(status_code=404, detail="Liste introuvable")
        v = _clamp_rating(value) if value.strip() else None
        if v is not None:
            tiers.ensure_tier_for_value(session, u["id"], b.id, v)
        else:
            rows = tiers.get_tiers(session, b.id)
            pos = rows[-1].position + 1 if rows else 0
            t = Tier(user_id=u["id"], board_id=b.id,
                     label=(label or "Nouveau").strip()[:24] or "Nouveau",
                     value=None, position=pos)
            session.add(t)
            session.commit()
    finally:
        session.close()
    return RedirectResponse(f"/tierlist?board={board_id}", status_code=303)


@app.post("/tier/remove")
def tier_remove(request: Request, tier_id: int = Form(...), csrf: str = Form(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    back = "/tierlist"
    try:
        t = session.get(Tier, tier_id)
        if t is None or t.user_id != u["id"]:
            raise HTTPException(status_code=404, detail="Palier introuvable")
        back = f"/tierlist?board={t.board_id}"
        # les entrées de ce palier (board de base) redeviennent non classées ;
        # les placements custom sont mis à NULL par la contrainte FK (ON DELETE SET NULL).
        for e in session.query(Entry).filter_by(user_id=u["id"], tier_id=t.id).all():
            e.tier_id = None
        session.delete(t)
        session.commit()
    finally:
        session.close()
    return RedirectResponse(back, status_code=303)


# --------------------------------------------------------------------------- #
#  Personnages : recherche, ajout/retrait et placement dans une tier list perso
# --------------------------------------------------------------------------- #
@app.get("/api/characters/search")
async def api_characters_search(request: Request, q: str = ""):
    """Recherche de personnages (JSON) pour le sélecteur du top 3 et l'ajout à une
    tier list de personnages. Résultats mis en cache (nom + URL d'image AniList)."""
    require_user(request)
    q = (q or "").strip()[:100]
    if not q:
        return JSONResponse({"items": []})
    if not security.search_limiter.allow("char:" + client_key(request)):
        raise HTTPException(status_code=429, detail="Trop de recherches, patiente une minute.")
    session = db_session()
    try:
        try:
            results = await characters.search(session, q)
        except Exception:
            raise HTTPException(status_code=502, detail="Base de personnages indisponible")
        items = [{"id": c.id, "name": c.name, "image": c.image_url}
                 for c in results if c.image_url]
    finally:
        session.close()
    return JSONResponse({"items": items})


@app.get("/api/anime/{anime_id}/characters")
async def api_anime_characters(request: Request, anime_id: int):
    """Personnages d'un anime (JSON), pour « parcourir par anime » : on clique un
    anime de sa liste et on voit tous ses persos. Résolus via AniList (par titre)."""
    require_user(request)
    if not security.search_limiter.allow("char:" + client_key(request)):
        raise HTTPException(status_code=429, detail="Trop de recherches, patiente une minute.")
    session = db_session()
    try:
        a = session.get(Anime, anime_id)
        if a is None:
            raise HTTPException(status_code=404, detail="Anime introuvable")
        try:
            results = await characters.characters_of_anime(session, a.title)
        except Exception:
            raise HTTPException(status_code=502, detail="Base de personnages indisponible")
        items = [{"id": c.id, "name": c.name, "image": c.image_url}
                 for c in results if c.image_url]
    finally:
        session.close()
    return JSONResponse({"items": items})


def _own_char_board(session, user_id: str, board_id) -> Board | None:
    b = _own_board(session, user_id, board_id)
    return b if b is not None and b.kind == "character" else None


@app.post("/char/add")
async def char_add(request: Request, board_id: int = Form(...),
                   char_id: int = Form(...), csrf: str = Form(...)):
    """Ajoute un personnage à un board de personnages (dans « À classer »).

    Renvoie du JSON (`added` = True si nouvellement ajouté, False s'il y était déjà) +
    les infos du perso, pour que la page l'insère sans recharger (ajouts en série)."""
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    try:
        b = _own_char_board(session, u["id"], board_id)
        if b is None:
            raise HTTPException(status_code=404, detail="Liste introuvable")
        c = await characters.fetch_by_id(session, char_id)
        if c is None:
            raise HTTPException(status_code=404, detail="Personnage introuvable")
        exists = (session.query(CharPlacement)
                  .filter_by(board_id=b.id, char_id=c.id).first())
        added = exists is None
        if added:
            session.add(CharPlacement(board_id=b.id, char_id=c.id, tier_id=None, pos=0))
            session.commit()
        return JSONResponse({"ok": True, "added": added,
                             "char": {"id": c.id, "name": c.name, "image": c.image_url}})
    finally:
        session.close()


@app.post("/char/remove")
def char_remove(request: Request, board_id: int = Form(...),
                char_id: int = Form(...), csrf: str = Form(...)):
    """Retire un personnage d'un board de personnages."""
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    try:
        b = _own_char_board(session, u["id"], board_id)
        if b is None:
            raise HTTPException(status_code=404, detail="Liste introuvable")
        session.query(CharPlacement).filter_by(board_id=b.id, char_id=char_id).delete()
        session.commit()
    finally:
        session.close()
    return RedirectResponse(f"/tierlist?board={board_id}", status_code=303)


@app.post("/char/place")
async def char_place(request: Request):
    """Déplace un personnage dans un palier (drag & drop), avec ordre persistant."""
    u = require_user(request)
    check_write_rate(request)
    body = await _json_body(request)
    check_csrf(request, body.get("csrf"))
    session = db_session()
    try:
        b = _own_char_board(session, u["id"], body.get("board_id"))
        if b is None:
            raise HTTPException(status_code=404, detail="Liste introuvable")
        char_id = int(body.get("char_id", 0))
        tid = body.get("tier_id")
        order = body.get("order") or []

        new_tid = None
        if tid not in (None, "", "unranked"):
            t = session.get(Tier, int(tid))
            if t is None or t.board_id != b.id:
                raise HTTPException(status_code=404, detail="Palier introuvable")
            new_tid = t.id

        def get_pl(cid) -> CharPlacement | None:
            return (session.query(CharPlacement)
                    .filter_by(board_id=b.id, char_id=cid).first())

        p = get_pl(char_id)
        if p is None:
            raise HTTPException(status_code=404, detail="Personnage absent de la liste")
        p.tier_id = new_tid
        if isinstance(order, list):
            rank = {int(cid): i for i, cid in enumerate(order)}
            for cid, i in rank.items():
                op = get_pl(cid)
                if op is not None:
                    op.tier_id = new_tid
                    op.pos = i
        session.commit()
        return JSONResponse({"ok": True})
    finally:
        session.close()


# --------------------------------------------------------------------------- #
#  Classements
# --------------------------------------------------------------------------- #
def public_average(session, anime_id: int):
    row = (session.query(func.avg(Entry.rating), func.count(Entry.rating))
           .join(User, User.id == Entry.user_id)
           .filter(Entry.anime_id == anime_id, Entry.rating.isnot(None),
                   User.is_public.is_(True))
           .one())
    avg = round(row[0], 2) if row[0] is not None else None
    return avg, row[1] or 0


def all_genres(session) -> list[str]:
    genres = set()
    for (g,) in session.query(Anime.genres).filter(Anime.genres != "").all():
        for part in g.split(","):
            if part:
                genres.add(part)
    return sorted(genres)


@app.get("/ranking", response_class=HTMLResponse)
def ranking(request: Request, scope: str = "global", genre: str = ""):
    if scope not in ("me", "global", "anilist"):
        scope = "global"
    genre = (genre or "").strip()[:40]
    session = db_session()
    try:
        rows = []  # liste de dict {anime, score, count}
        if scope == "me":
            u = current_user(request)
            if u:
                q = (session.query(Entry, Anime)
                     .join(Anime, Anime.id == Entry.anime_id)
                     .filter(Entry.user_id == u["id"], Entry.rating.isnot(None)))
                if genre:
                    q = q.filter(Anime.genres.like(f"%{genre}%"))
                for e, a in q.order_by(Entry.rating.desc()).all():
                    rows.append({"anime": a, "score": e.rating, "count": None})
        elif scope == "global":
            q = (session.query(Anime,
                               func.avg(Entry.rating).label("avg"),
                               func.count(Entry.rating).label("cnt"))
                 .join(Entry, Entry.anime_id == Anime.id)
                 .join(User, User.id == Entry.user_id)
                 .filter(Entry.rating.isnot(None), User.is_public.is_(True)))
            if genre:
                q = q.filter(Anime.genres.like(f"%{genre}%"))
            q = q.group_by(Anime.id).order_by(func.avg(Entry.rating).desc())
            for a, avg, cnt in q.all():
                rows.append({"anime": a, "score": round(avg, 2), "count": cnt})
        else:  # anilist
            q = session.query(Anime).filter(Anime.average_score > 0)
            if genre:
                q = q.filter(Anime.genres.like(f"%{genre}%"))
            for a in q.order_by(Anime.average_score.desc()).limit(100).all():
                rows.append({"anime": a, "score": a.average_score / 10, "count": None})
        return templates.TemplateResponse(
            "ranking.html",
            ctx(request, rows=rows, scope=scope, genre=genre, genres=all_genres(session)))
    finally:
        session.close()


# --------------------------------------------------------------------------- #
#  Profil (public / privé) + profils publics
# --------------------------------------------------------------------------- #
def top3_anime(session, row) -> list:
    """Résout les top1/2/3 d'un utilisateur en objets Anime (dans l'ordre, sans trous)."""
    out = []
    for aid in (row.top1_id, row.top2_id, row.top3_id):
        if aid:
            a = session.get(Anime, aid)
            if a:
                out.append(a)
    return out


def top3_char(session, row) -> list:
    """Résout les char1/2/3 d'un utilisateur en objets Character (ordre, sans trous)."""
    out = []
    for cid in (row.char1_id, row.char2_id, row.char3_id):
        if cid:
            c = session.get(Character, cid)
            if c:
                out.append(c)
    return out


def user_finished(session, user_id: str):
    """Liste (anime_id, titre) des animes finis, pour le sélecteur du top 3."""
    rows = (session.query(Entry, Anime).join(Anime, Anime.id == Entry.anime_id)
            .filter(Entry.user_id == user_id, Entry.status == STATUS_FINISHED)
            .order_by(Entry.rating.desc().nullslast()).all())
    return [(a.id, a.english or a.romaji or f"#{a.id}") for _e, a in rows]


@app.get("/profile", response_class=HTMLResponse)
def profile(request: Request):
    u = require_user(request)
    session = db_session()
    try:
        row = session.get(User, u["id"])
        counts = {s: session.query(func.count(Entry.id))
                  .filter_by(user_id=u["id"], status=s).scalar() for s in STATUSES}
        rated = (session.query(func.count(Entry.id))
                 .filter(Entry.user_id == u["id"], Entry.rating.isnot(None)).scalar())
        # Animes de la liste (pour le sélecteur de saisie manuelle d'activité).
        mylist = (session.query(Anime).join(Entry, Entry.anime_id == Anime.id)
                  .filter(Entry.user_id == u["id"]).all())
        mylist = sorted(mylist, key=lambda a: a.title.lower())
        return templates.TemplateResponse(
            "profile.html", ctx(request, prof=row, counts=counts, rated=rated,
                                 is_local=bool(row.password_hash),
                                 pw_flash=request.session.pop("pw_flash", None),
                                 top3=top3_anime(session, row),
                                 char_top3=top3_char(session, row),
                                 finished=user_finished(session, u["id"]),
                                 activity=build_activity(session, u["id"]),
                                 mylist=mylist,
                                 today=local_today().isoformat()))
    finally:
        session.close()


@app.post("/profile/token")
def profile_token(request: Request, csrf: str = Form(...)):
    """(Re)génère le jeton d'API perso utilisé par l'extension navigateur."""
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    try:
        row = session.get(User, u["id"])
        row.api_token = secrets.token_urlsafe(32)
        session.commit()
    finally:
        session.close()
    return RedirectResponse("/profile", status_code=303)


@app.post("/profile/token/revoke")
def profile_token_revoke(request: Request, csrf: str = Form(...)):
    """Supprime le jeton (l'extension ne pourra plus envoyer d'activité)."""
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    try:
        row = session.get(User, u["id"])
        row.api_token = None
        session.commit()
    finally:
        session.close()
    return RedirectResponse("/profile", status_code=303)


@app.post("/watch/manual")
async def watch_manual(request: Request, anime_id: str = Form(""),
                       title: str = Form(""), count: str = Form("1"),
                       day: str = Form(""), csrf: str = Form(...)):
    """Saisie manuelle d'activité : ajoute `count` épisodes d'un anime à une date."""
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    try:
        n = max(1, min(50, int(count)))
    except (TypeError, ValueError):
        n = 1
    # Date : par défaut aujourd'hui. On pose l'instant à midi (heure locale) du jour
    # choisi pour que la conversion de fuseau retombe bien sur le bon jour.
    try:
        d = date.fromisoformat(day) if day else local_today()
    except ValueError:
        d = local_today()
    if d > local_today():
        d = local_today()
    when = datetime(d.year, d.month, d.day, 12, 0, tzinfo=_WATCH_TZ).astimezone(timezone.utc)

    session = db_session()
    try:
        anime = None
        chosen_title = (title or "").strip()
        aid = None
        try:
            aid = int(anime_id) if anime_id else None
        except ValueError:
            aid = None
        if aid:
            anime = session.get(Anime, aid)
            if anime is None:
                raise HTTPException(status_code=404, detail="Anime introuvable")
            chosen_title = anime.title
        if not chosen_title:
            raise HTTPException(status_code=400, detail="Titre ou anime requis")
        for _ in range(n):
            await record_watch(session, u["id"], chosen_title, None, "manual",
                               when=when, auto_add=True, anime=anime)
    finally:
        session.close()
    return RedirectResponse("/profile", status_code=303)


@app.get("/profile/activity/day")
def activity_day(request: Request, date: str = "", user: str = ""):
    """Détail des épisodes regardés un jour donné (JSON), pour le clic sur une case.

    `user` optionnel : consulter l'activité d'un profil PUBLIC. Par défaut, soi-même.
    """
    target = (user or "").strip()
    if target:
        session = db_session()
        try:
            row = session.get(User, target)
            if row is None or not row.is_public:
                raise HTTPException(status_code=404, detail="Profil introuvable")
        finally:
            session.close()
    else:
        u = require_user(request)
        target = u["id"]

    day = (date or "").strip()[:10]
    session = db_session()
    try:
        rows = (session.query(WatchEvent, Anime)
                .outerjoin(Anime, Anime.id == WatchEvent.anime_id)
                .filter(WatchEvent.user_id == target, WatchEvent.day == day)
                .order_by(WatchEvent.watched_at.asc()).all())
        items = []
        for ev, a in rows:
            items.append({
                "title": (a.title if a else ev.raw_title) or ev.raw_title,
                "anime_id": ev.anime_id,
                "episode": ev.episode,
                "source": ev.source,
                "time": _fmt_local_time(ev.watched_at),
            })
    finally:
        session.close()
    return JSONResponse({"date": day, "count": len(items), "items": items})


def user_by_token(session, token: str) -> User | None:
    token = (token or "").strip()
    if len(token) < 16:
        return None
    return session.query(User).filter(User.api_token == token).first()


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


@app.post("/api/watch")
async def api_watch(request: Request):
    """API pour l'extension navigateur. Auth : en-tête `Authorization: Bearer <token>`.

    Corps JSON : {"title": str, "episode": int|null, "source": "crunchyroll"|"anime-sama"}.
    Enregistre l'épisode et (auto) ajoute l'anime en « En cours ».
    """
    token = _bearer(request)
    if not security.write_limiter.allow("apiwatch:" + client_key(request)):
        raise HTTPException(status_code=429, detail="Trop de requêtes")
    session = db_session()
    try:
        user = user_by_token(session, token)
        if user is None:
            raise HTTPException(status_code=401, detail="Jeton invalide")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="JSON invalide")
        title = (body.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="Titre manquant")
        source = (body.get("source") or "extension").strip()
        ep = body.get("episode")
        try:
            ep = int(ep) if ep is not None else None
        except (TypeError, ValueError):
            ep = None
        res = await record_watch(session, user.id, title, ep, source, auto_add=True)
    finally:
        session.close()
    return JSONResponse({"ok": True, **res})


@app.get("/api/ping")
def api_ping(request: Request):
    """Vérifie qu'un jeton est valide (bouton « Tester » de l'extension)."""
    session = db_session()
    try:
        user = user_by_token(session, _bearer(request))
        if user is None:
            raise HTTPException(status_code=401, detail="Jeton invalide")
        return JSONResponse({"ok": True, "user": user.display_name})
    finally:
        session.close()


@app.post("/profile/top")
def profile_top(request: Request, top1: str = Form(""), top2: str = Form(""),
                top3: str = Form(""), csrf: str = Form(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)

    def norm(v):
        try:
            return int(v) or None
        except (TypeError, ValueError):
            return None

    session = db_session()
    try:
        row = session.get(User, u["id"])
        owned = {e.anime_id for e in
                 session.query(Entry.anime_id).filter_by(user_id=u["id"]).all()}
        vals = [norm(top1), norm(top2), norm(top3)]
        vals = [v if v in owned else None for v in vals]  # que des animes de ta liste
        row.top1_id, row.top2_id, row.top3_id = vals[0], vals[1], vals[2]
        session.commit()
    finally:
        session.close()
    return RedirectResponse("/profile", status_code=303)


@app.post("/profile/char-top")
async def profile_char_top(request: Request, char1: str = Form(""), char2: str = Form(""),
                           char3: str = Form(""), csrf: str = Form(...)):
    """Enregistre le top 3 personnages. Chaque id doit être un personnage connu de
    Kitsu : on s'assure qu'il est en cache (nom + URL d'image) avant de le retenir."""
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)

    def norm(v):
        try:
            return int(v) or None
        except (TypeError, ValueError):
            return None

    session = db_session()
    try:
        vals = []
        for v in (norm(char1), norm(char2), norm(char3)):
            if v is None:
                vals.append(None)
                continue
            c = await characters.fetch_by_id(session, v)
            vals.append(c.id if c is not None else None)
        row = session.get(User, u["id"])
        row.char1_id, row.char2_id, row.char3_id = vals[0], vals[1], vals[2]
        session.commit()
    finally:
        session.close()
    return RedirectResponse("/profile", status_code=303)


@app.get("/profile/export")
def profile_export(request: Request, fmt: str = "csv"):
    """Télécharge la liste notée de l'utilisateur (nom + note), triée par note
    décroissante. `fmt` = csv (défaut, ouvrable dans Excel) ou txt (texte propre)."""
    u = require_user(request)
    session = db_session()
    try:
        rows = (session.query(Entry, Anime)
                .join(Anime, Anime.id == Entry.anime_id)
                .filter(Entry.user_id == u["id"], Entry.rating.isnot(None))
                .order_by(Entry.rating.desc(), Anime.english).all())
        items = [(a.english or a.romaji or a.native or f"#{a.id}", e.rating)
                 for e, a in rows]
    finally:
        session.close()

    if fmt == "txt":
        lines = [f"{title} — {rating:g}/10" for title, rating in items]
        data = "\n".join(lines) + ("\n" if lines else "")
        return Response(
            content=data,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="mes-animes.txt"'},
        )

    buf = io.StringIO()
    buf.write("﻿")  # BOM UTF-8 pour qu'Excel affiche bien les accents
    writer = csv.writer(buf)
    writer.writerow(["Anime", "Note"])
    for title, rating in items:
        writer.writerow([title, f"{rating:g}"])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="mes-animes.csv"'},
    )


@app.post("/profile/public")
def profile_public(request: Request, is_public: str = Form(""), csrf: str = Form(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    try:
        row = session.get(User, u["id"])
        row.is_public = (is_public == "1")
        session.commit()
    finally:
        session.close()
    return RedirectResponse("/profile", status_code=303)


@app.post("/profile/avatar")
async def profile_avatar(request: Request, csrf: str = Form(...),
                         photo: UploadFile = File(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    data = await photo.read(MAX_AVATAR_BYTES + 1)
    if len(data) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=413, detail="Image trop lourde (6 Mo max).")
    # On ré-encode entièrement l'image via Pillow : ça neutralise tout contenu piégé
    # (SVG/HTML/polyglotte) et normalise en JPEG carré 256x256.
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img = ImageOps.fit(img, (256, 256), Image.LANCZOS)
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(status_code=400, detail="Fichier image invalide.")
    session = db_session()
    try:
        row = session.get(User, u["id"])
        row.custom_avatar = (row.custom_avatar or 0) + 1
        img.save(os.path.join(UPLOAD_DIR, f"{u['id']}.jpg"), "JPEG", quality=85)
        session.commit()
        ver = row.custom_avatar
    finally:
        session.close()
    request.session["user"] = {**u, "custom_avatar": ver}
    return RedirectResponse("/profile", status_code=303)


@app.post("/profile/avatar/remove")
def profile_avatar_remove(request: Request, csrf: str = Form(...)):
    u = require_user(request)
    check_csrf(request, csrf)
    check_write_rate(request)
    session = db_session()
    try:
        row = session.get(User, u["id"])
        row.custom_avatar = 0
        session.commit()
    finally:
        session.close()
    try:
        os.remove(os.path.join(UPLOAD_DIR, f"{u['id']}.jpg"))
    except OSError:
        pass
    request.session["user"] = {**u, "custom_avatar": 0}
    return RedirectResponse("/profile", status_code=303)


@app.get("/u/{user_id}", response_class=HTMLResponse)
def public_profile(request: Request, user_id: str):
    session = db_session()
    try:
        row = session.get(User, user_id)
        if row is None or not row.is_public:
            raise HTTPException(status_code=404, detail="Profil privé ou introuvable")
        entries = (session.query(Entry, Anime)
                   .join(Anime, Anime.id == Entry.anime_id)
                   .filter(Entry.user_id == user_id, Entry.rating.isnot(None))
                   .order_by(Entry.rating.desc()).all())
        tier_rows, by_tier, unranked = build_tierlist(session, user_id)
        # Tier list de personnages « de base » (lecture seule), si elle existe.
        char_tiers, char_by_tier = [], {}
        cb = (session.query(Board)
              .filter_by(user_id=user_id, is_default=True, kind="character").first())
        if cb is not None:
            char_tiers, char_by_tier, _unr = tiers.build_char_board(session, cb)
        return templates.TemplateResponse(
            "public_profile.html",
            ctx(request, prof=row, entries=entries, top3=top3_anime(session, row),
                char_top3=top3_char(session, row),
                tier_rows=tier_rows, by_tier=by_tier,
                char_tier_rows=char_tiers, char_by_tier=char_by_tier,
                activity=build_activity(session, user_id)))
    finally:
        session.close()


@app.get("/users", response_class=HTMLResponse)
def public_users(request: Request):
    session = db_session()
    try:
        rows = (session.query(User, func.count(Entry.id))
                .outerjoin(Entry, Entry.user_id == User.id)
                .filter(User.is_public.is_(True))
                .group_by(User.id).all())
        return templates.TemplateResponse("users.html", ctx(request, rows=rows))
    finally:
        session.close()


ALLOWED_IMG_HOSTS = {"media.kitsu.app", "media.kitsu.io", "s4.anilist.co"}


@app.get("/img")
async def img_proxy(u: str):
    """Proxy same-origin des affiches Kitsu (uniquement) — sert à l'export PNG de la
    tier list (html2canvas ne peut pas dessiner des images cross-origin sans CORS).
    Restreint aux hôtes Kitsu pour éviter tout SSRF."""
    p = urlparse(u)
    if p.scheme != "https" or p.hostname not in ALLOWED_IMG_HOSTS:
        raise HTTPException(status_code=400, detail="URL non autorisée")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(u)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Image indisponible")
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
        raise HTTPException(status_code=502, detail="Image indisponible")
    return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/healthz")
def healthz():
    return {"ok": True}
