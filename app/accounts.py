"""Comptes « pseudo + mot de passe » (sans Discord, sans e-mail).

- Mots de passe hachés en Argon2id (paramètres OWASP : 19 Mio, t=2, p=1), jamais
  stockés ni journalisés en clair. Re-hachage transparent si les paramètres changent.
- Pseudo unique insensible à la casse (login_key = pseudo en minuscules).
- Pas de fuite d'existence de compte à la connexion : même message d'erreur et même
  coût de calcul (vérification contre un hash factice) quand le pseudo n'existe pas.
"""
import hashlib
import re
import secrets
from datetime import timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import func, or_

from .models import LoginSession, User, utcnow

_ph = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
# Hash d'un secret aléatoire : sert à vérifier « pour rien » quand le pseudo est
# inconnu, pour que la réponse prenne le même temps qu'avec un vrai compte.
_DUMMY_HASH = _ph.hash(secrets.token_hex(16))

PSEUDO_RE = re.compile(r"^[A-Za-z0-9_.-]{3,20}$")
PASSWORD_MIN = 8
PASSWORD_MAX = 128

RESERVED = {
    "admin", "administrator", "administrateur", "root", "system", "systeme", "mod",
    "moderator", "moderateur", "staff", "support", "help", "discord", "anime",
    "animedwa", "dwa", "null", "none", "undefined", "anonymous", "anonyme", "login",
    "logout", "register", "profile", "api", "static", "uploads",
}

# Mots de passe parmi les plus utilisés (fuites publiques) : refusés d'office.
COMMON_PASSWORDS = {
    "12345678", "123456789", "1234567890", "12345678910", "123123123", "11111111",
    "00000000", "87654321", "password", "password1", "password123", "passw0rd",
    "motdepasse", "motdepasse1", "azertyui", "azertyuiop", "qwertyui", "qwertyuiop",
    "qwerty123", "azerty123", "iloveyou", "sunshine", "princess", "football",
    "baseball", "welcome1", "admin123", "letmein1", "trustno1", "dragon123",
    "abc12345", "abcd1234", "1q2w3e4r", "1qaz2wsx", "zaq12wsx", "q1w2e3r4",
    "aaaaaaaa", "soleil123", "doudou123", "chouchou", "loulou123", "bonjour1",
    "naruto123", "onepiece", "pokemon1", "anime123", "otaku123", "jetaime1",
}


def pseudo_key(pseudo: str) -> str:
    return pseudo.strip().lower()


def check_pseudo(session, pseudo: str) -> str | None:
    """Renvoie un message d'erreur, ou None si le pseudo est valide et libre."""
    if not PSEUDO_RE.fullmatch(pseudo):
        return ("Pseudo invalide : 3 à 20 caractères, lettres sans accent, chiffres, "
                "« _ », « . » ou « - ».")
    key = pseudo_key(pseudo)
    if key in RESERVED:
        return "Ce pseudo est réservé, choisis-en un autre."
    # Libre parmi les comptes pseudo ET différent du nom d'un membre Discord
    # (empêche de se faire passer pour quelqu'un dans la liste des membres).
    taken = (session.query(User.id)
             .filter(or_(User.login_key == key,
                         func.lower(User.username) == key,
                         func.lower(User.global_name) == key))
             .first())
    if taken:
        return "Ce pseudo est déjà pris."
    return None


def check_password(password: str, pseudo: str = "") -> str | None:
    """Règles simples et efficaces (NIST 800-63B) : longueur, pas de mdp courant."""
    if len(password) < PASSWORD_MIN:
        return f"Mot de passe trop court : {PASSWORD_MIN} caractères minimum."
    if len(password) > PASSWORD_MAX:
        return f"Mot de passe trop long : {PASSWORD_MAX} caractères maximum."
    low = password.lower()
    if low in COMMON_PASSWORDS or len(set(password)) < 4:
        return "Ce mot de passe est trop courant / trop simple, choisis-en un autre."
    if pseudo and pseudo_key(pseudo) in low:
        return "Le mot de passe ne doit pas contenir ton pseudo."
    return None


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(user: User | None, password: str) -> bool:
    """Vérifie le mot de passe. Temps constant que le compte existe ou non.

    Si le hash utilise d'anciens paramètres, il est recalculé (l'appelant commit).
    """
    if user is None or not user.password_hash:
        try:
            _ph.verify(_DUMMY_HASH, password)
        except VerificationError:
            pass
        return False
    try:
        _ph.verify(user.password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    if _ph.check_needs_rehash(user.password_hash):
        user.password_hash = _ph.hash(password)
    return True


def new_local_id(session) -> str:
    """Id de compte pseudo : 'l' + 12 hex. Jamais numérique → pas de collision Discord."""
    while True:
        uid = "l" + secrets.token_hex(6)
        if session.get(User, uid) is None:
            return uid


def find_by_pseudo(session, pseudo: str) -> User | None:
    key = pseudo_key(pseudo)
    if not key or len(key) > 32:
        return None
    return session.query(User).filter(User.login_key == key).first()


# --------------------------------------------------------------------------- #
#  Sessions côté serveur (révocables) des comptes pseudo
# --------------------------------------------------------------------------- #
SESSION_MAX_AGE = timedelta(days=30)  # durée max absolue, même si on reste actif


def _sid_hash(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


def create_login_session(session, user_id: str) -> str:
    """Ouvre une session : renvoie le jeton à mettre dans le cookie (seul son hash
    est stocké). Fait au passage le ménage des sessions expirées du compte."""
    session.query(LoginSession).filter(
        LoginSession.user_id == user_id,
        LoginSession.created_at < utcnow() - SESSION_MAX_AGE).delete()
    sid = secrets.token_urlsafe(32)
    session.add(LoginSession(id=_sid_hash(sid), user_id=user_id))
    session.commit()
    return sid


def login_session_valid(session, user_id: str, sid: str | None) -> bool:
    if not sid:
        return False
    row = session.get(LoginSession, _sid_hash(sid))
    if row is None or row.user_id != user_id:
        return False
    created = row.created_at
    if created.tzinfo is None:  # SQLite relit les dates en naïf : ce sont des UTC
        created = created.replace(tzinfo=timezone.utc)
    return utcnow() - created < SESSION_MAX_AGE


def revoke_login_session(session, sid: str | None):
    if sid:
        session.query(LoginSession).filter(LoginSession.id == _sid_hash(sid)).delete()
        session.commit()


def revoke_all_sessions(session, user_id: str):
    session.query(LoginSession).filter(LoginSession.user_id == user_id).delete()
