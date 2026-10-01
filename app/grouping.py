"""Normalisation de titre pour regrouper les saisons d'une même franchise.

Kitsu n'expose pas de relations préquelle/suite fiables, donc on regroupe par
**titre de base** : on retire les marqueurs de saison/partie et on compare.
On privilégie le romaji (le plus régulier : « Shingeki no Kyojin », « ... Season 2 »).

Volontairement conservateur : on ne retire QUE les marqueurs numériques de saison,
jamais les sous-titres d'arc (« Mugen Train ») ni les nombres du titre (« 86 »,
« Mob Psycho 100 »), pour éviter de fusionner des œuvres distinctes.
"""
import re

# Marqueurs de type retirés pour absorber films / OVA / specials dans la franchise.
_TYPE_RE = re.compile(
    r"\b(the\s+movie|movie|the\s+animation|gekijouban|gekijou\s*ban|film|"
    r"ova|oad|ona|specials?|recap|picture\s+drama|petit|pucchi|chibi)\b",
    re.IGNORECASE,
)

_SEASON_RE = re.compile(
    r"\b(the\s+)?final\s+season\b|"      # "(The) Final Season"
    r"\b\d+(st|nd|rd|th)\s+season\b|"     # "2nd Season"
    r"\bseason\s+\d+\b|\bsaison\s+\d+\b|"  # "Season 2" / "Saison 2"
    r"\bpart\s+\d+\b|\bcour\s+\d+\b|"      # "Part 2" / "Cour 2"
    r"\s+[ivx]{1,4}$|"                     # chiffre romain final (II, III, IV…)
    r"\s+[1-9]$",                          # n° de saison/film nu 1-9 en fin
    # NB : on ne retire qu'un chiffre 2-9 isolé en fin, ce qui protège les nombres
    # intégrés au titre (« 86 », « Mob Psycho 100 », « Steins;Gate 0 »).
    re.IGNORECASE,
)


# Séparateur de sous-titre : « Base: Sous-titre », « Base - Sous-titre », « Base (2011) »,
# « Base [Route] », « Base ~wave~ ». On exige un espace après « : » pour NE PAS casser
# « Re:Zero » ni « Steins;Gate » (deux-points/point-virgule collés).
_SUBTITLE_SEP = re.compile(r":\s|\s[–—-]\s|\s*[\[(~]")


def normalize(title: str) -> str:
    t = (title or "").lower().strip()
    # 1) on ne garde que la partie avant le premier sous-titre (regroupement agressif :
    #    films / OVA / arcs d'une même franchise retombent sur le même titre de base).
    t = _SUBTITLE_SEP.split(t, 1)[0].strip()
    # 2) on retire les marqueurs de saison résiduels
    prev = None
    while prev != t:  # répète pour « Movie 2 », « Season 3 Part 2 »…
        prev = t
        t = _TYPE_RE.sub("", t)
        t = _SEASON_RE.sub("", t).strip(" :-–—")
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return t


def group_key(romaji: str = "", english: str = "", native: str = "",
              anime_id: int | None = None) -> str:
    """Clé de regroupement d'une franchise (romaji prioritaire)."""
    for src in (romaji, english, native):
        k = normalize(src)
        if k:
            return k
    return f"#{anime_id}"  # aucun titre exploitable : reste autonome
