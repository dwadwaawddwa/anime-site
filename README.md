# DWAnime

Site pour noter et classer ses animes → [anime.dwa.gold](https://anime.dwa.gold)

- Recherche multilingue (romaji, anglais, japonais), fiches et images tirées de Kitsu.
- Statuts (à voir / en cours / fini), notes de 0,5 à 10 en un clic, commentaires.
- Tier list en glisser-déposer, classements perso / global / Kitsu, filtre par genre.
- Saisons d'une même franchise regroupées en une seule série.
- Profils publics ou privés, photo de profil, heatmap d'activité façon GitHub.
- Connexion Discord ou compte pseudo + mot de passe (Argon2id, sans e-mail).
- API à jeton pour l'extension Chrome qui remonte les épisodes vus.
- SSO pour les sites liés (DWAnisongs, DWAblindtest).

## Stack

Python · FastAPI · Uvicorn · SQLAlchemy · SQLite · Jinja2 · JS vanilla

## Lancer en local

```bat
copy .env.example .env
python -m venv venv
venv\Scripts\pip install -r requirements.txt
run.bat
```

Puis http://127.0.0.1:8030. `ingest.py` remplit le catalogue, `reset_password.py` réinitialise un mot de passe.
