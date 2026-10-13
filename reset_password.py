"""Réinitialise le mot de passe d'un compte pseudo (pas d'e-mail → c'est l'admin qui le fait).

Usage (sur le Pi) :  ./venv/bin/python reset_password.py <pseudo>
Génère un mot de passe temporaire aléatoire, l'affiche une seule fois, et déconnecte
toutes les sessions du compte. La personne le change ensuite depuis son profil.
"""
import secrets
import sys

from app import accounts
from app.db import SessionLocal, init_db


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    init_db()
    session = SessionLocal()
    try:
        row = accounts.find_by_pseudo(session, sys.argv[1])
        if row is None or not row.password_hash:
            print(f"Aucun compte pseudo « {sys.argv[1]} ».")
            sys.exit(2)
        temp = secrets.token_urlsafe(12)
        row.password_hash = accounts.hash_password(temp)
        accounts.revoke_all_sessions(session, row.id)
        session.commit()
        print(f"Compte « {row.username} » : mot de passe temporaire = {temp}")
        print("Toutes ses sessions ont été déconnectées. À changer depuis /profile.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
