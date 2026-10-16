#!/usr/bin/env python3
"""Agent contenu marketing d'Aventicum Quest, rédigé par Qwen.

Chaque semaine : 5 idées de posts Instagram/Facebook prêtes à copier,
écrites dans data/content-ideas.json (affichées sur le Watchtower).

Dépendances : aucune (bibliothèque standard uniquement).

Variables d'environnement :
  DASHSCOPE_API_KEY  (obligatoire) clé API Alibaba Model Studio, région Singapour
  QWEN_MODEL         (optionnel)   défaut : qwen3.8-max
  QWEN_BASE_URL      (optionnel)   défaut : https://dashscope-intl.aliyuncs.com/compatible-mode/v1
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = os.environ.get(
    "QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
).rstrip("/")
MODEL = os.environ.get("QWEN_MODEL", "qwen3.8-max")  # 1 appel par semaine : on prend le modèle précis
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "content-ideas.json"

PLATFORMS = ("Instagram", "Facebook")
MAX_POSTS = 5

MOIS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]

SYSTEM_PROMPT = (
    "Tu es le responsable contenu d'Aventicum Quest, un jeu de piste en plein air à Avenches "
    "(Vaud, Suisse), sur le thème de la Rome antique (Aventicum était la capitale de l'Helvétie "
    "romaine). Le créateur, Bjorn, est un jeune entrepreneur local qui gère tout seul. "
    "Faits que tu peux utiliser, et AUCUN autre : 8 stations, environ 120 minutes, départ place "
    "de l'Église (Hôtel de Ville), en plein air, à faire en équipe, un code par commande valable "
    "12 mois, adulte 12 CHF, enfant 8 CHF (5 à 17 ans, gratuit sous 5 ans), forfait famille "
    "35 CHF (jusqu'à 5 personnes), disponible en plusieurs langues.\n"
    "Les posts sont écrits à la première personne, avec la voix de Bjorn ('je', 'on'), jamais "
    "en parlant de lui à la troisième personne. Ne raconte JAMAIS une scène ou une histoire "
    "comme si elle était réelle (pas de 'une équipe de quatre amis a terminé en deux heures', "
    "pas d'anecdote client, pas de météo ou de détail de terrain que tu ne peux pas connaître). "
    "N'invente pas d'horaire de fin de parcours ni d'objet fourni aux joueurs. Reste sur les "
    "faits ci-dessus, la curiosité, l'envie de jouer et de partager.\n"
    "Tu proposes des idées de posts pour Instagram et Facebook, en français de Suisse romande, "
    "ton chaleureux, simple et local, pas de ton publicitaire criard. "
    "Règles strictes : pas d'émojis, pas de tirets cadratins, pas de formules creuses "
    "('plongez dans', 'une expérience unique', 'incontournable'). N'invente aucun chiffre, "
    "aucun avis client, aucune date de lancement, aucun partenariat, aucun fait historique "
    "précis dont tu n'es pas certain. Chaque post doit donner une raison concrète de jouer ou "
    "de partager. Varie les angles : coulisses de la création, famille, histoire romaine "
    "générale, défi entre amis, cadeau, vie locale d'Avenches.\n"
    "Réponds UNIQUEMENT par un objet JSON avec une clé \"posts\" : liste de 5 objets, chacun avec :\n"
    '  "titre": 4 à 8 mots décrivant l\'idée ;\n'
    '  "plateforme": "Instagram" ou "Facebook" ;\n'
    '  "texte": la légende prête à publier (200 caractères minimum, 450 maximum) ;\n'
    '  "hashtags": liste de 3 à 6 hashtags sans espaces ;\n'
    '  "visuel": une phrase décrivant la photo ou la vidéo à prendre ;\n'
    '  "moment": le meilleur jour ou moment pour publier (ex. "dimanche en fin de matinée").'
)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_json(url, *, data=None, headers=None, timeout=90):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def previous_titles():
    """Titres des semaines précédentes, pour que le modèle ne se répète pas."""
    try:
        old = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    titles = list(old.get("history") or [])
    titles += [p.get("titre", "") for p in old.get("posts") or []]
    return [t for t in titles if t][-20:]


def call_qwen(user_prompt):
    url = f"{BASE_URL}/chat/completions"
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.8,
        "max_tokens": 1800,
        "enable_thinking": False,
    }
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    last_err = None
    for attempt in range(3):
        try:
            return http_json(url, data=payload, headers=headers)
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = str(e)
        time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"Appel Qwen échoué : {last_err}")


def parse_model_json(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


def clean_hashtag(h):
    h = re.sub(r"\s+", "", str(h or "")).lstrip("#")
    h = re.sub(r"[^\w]", "", h)
    return ("#" + h) if h else ""


def clean_posts(raw):
    posts = []
    for p in (raw.get("posts") or [])[:MAX_POSTS]:
        if not isinstance(p, dict):
            continue
        texte = str(p.get("texte") or "").strip()[:600]
        if len(texte) < 40:
            continue
        plat = str(p.get("plateforme") or "").strip()
        plat = plat if plat in PLATFORMS else "Instagram"
        tags = [clean_hashtag(h) for h in (p.get("hashtags") or [])]
        tags = [t for t in tags if t][:6]
        posts.append(
            {
                "titre": str(p.get("titre") or "Idée de post").strip()[:80],
                "plateforme": plat,
                "texte": texte,
                "hashtags": tags,
                "visuel": str(p.get("visuel") or "").strip()[:200],
                "moment": str(p.get("moment") or "").strip()[:80],
            }
        )
    if not posts:
        raise ValueError("Réponse du modèle sans post exploitable")
    return posts


def main():
    if not API_KEY:
        print("DASHSCOPE_API_KEY manquante", file=sys.stderr)
        return 1

    today = datetime.now(timezone.utc)
    prompt = (
        f"Nous sommes en {MOIS[today.month - 1]} {today.year}. "
        "Propose les 5 idées de posts de la semaine."
    )
    old = previous_titles()
    if old:
        prompt += (
            " Évite de refaire ces idées déjà proposées : "
            + " ; ".join(old)
            + "."
        )

    resp = call_qwen(prompt)
    posts = clean_posts(parse_model_json(resp["choices"][0]["message"]["content"]))
    usage = resp.get("usage") or {}

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(
            {
                "generatedAt": now_iso(),
                "model": resp.get("model") or MODEL,
                "posts": posts,
                "history": old[-15:],
                "usage": {
                    "input": usage.get("prompt_tokens"),
                    "output": usage.get("completion_tokens"),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"OK : {len(posts)} idées")
    return 0


if __name__ == "__main__":
    sys.exit(main())
