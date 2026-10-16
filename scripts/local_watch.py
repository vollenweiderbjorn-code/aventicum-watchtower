#!/usr/bin/env python3
"""Agent de veille locale d'Aventicum Quest, rédigé par Qwen avec recherche web.

Chaque semaine, Qwen cherche sur le web les événements et occasions des prochaines semaines
à Avenches et dans la région, et propose où et quand promouvoir le jeu.
Écrit data/local-watch.json (affiché sur le dashboard).

Les liens affichés viennent UNIQUEMENT des résultats de recherche renvoyés par l'API,
jamais du texte écrit par le modèle, pour éviter les liens inventés.

Variables d'environnement : DASHSCOPE_API_KEY (obligatoire), QWEN_MODEL, QWEN_BASE_URL
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
MODEL = os.environ.get("QWEN_MODEL", "qwen3.8-max")
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "local-watch.json"

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]

SYSTEM_PROMPT = (
    "Tu es l'éclaireur local d'Aventicum Quest, un jeu de piste en plein air à Avenches (Vaud), "
    "sur le thème de la Rome antique, qui se joue en équipe (familles, amis, groupes) en environ "
    "120 minutes. Le créateur, Bjorn, cherche où et quand faire connaître le jeu localement. "
    "Utilise la recherche web pour trouver des événements et occasions RÉELS et à venir, dans les "
    "8 prochaines semaines, à Avenches et dans la région (Broye, Payerne, Morat, Estavayer, lac de "
    "Morat, Vaud et Fribourg proches) : marchés, fêtes, marché de Noël, événements romains ou "
    "culturels au site d'Aventicum, vacances scolaires, activités pour familles, offices du "
    "tourisme, hôtels, écoles, sociétés locales et entreprises qui pourraient offrir le jeu ou "
    "organiser une sortie d'équipe. "
    "Règles strictes : ne cite que ce que tu as réellement trouvé, ne devine aucune date. "
    "Si la date ou le lieu n'est pas confirmé par une source, écris 'date à confirmer'. "
    "Pas d'émojis, pas de tirets cadratins, français de Suisse romande.\n"
    "Réponds UNIQUEMENT par un objet JSON avec une clé \"pistes\" : liste de 4 à 6 objets avec :\n"
    '  "titre": nom de l\'événement ou de l\'occasion ;\n'
    '  "quand": date ou période, ou "date à confirmer" ;\n'
    '  "lieu": commune ou lieu ;\n'
    '  "pourquoi": pourquoi c\'est utile pour le jeu, en une phrase ;\n'
    '  "action": une action concrète pour Bjorn (qui contacter ou quoi publier), en une phrase ;\n'
    '  "priorite": "haute" | "moyenne" | "basse".'
)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_json(url, *, data=None, headers=None, timeout=120):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call_qwen(prompt):
    url = f"{BASE_URL}/chat/completions"
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 2000,
        "enable_thinking": False,
        "enable_search": True,
        "search_options": {"forced_search": True, "enable_source": True},
    }
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    last = None
    for attempt in range(3):
        try:
            return http_json(url, data=payload, headers=headers)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}"
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError) as e:
            last = str(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Appel Qwen échoué : {last}")


def parse_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


def find_sources(resp):
    """Liens réellement renvoyés par la recherche (pas ceux écrits par le modèle)."""
    info = resp.get("search_info") or {}
    if not info:
        info = ((resp.get("choices") or [{}])[0].get("message") or {}).get("search_info") or {}
    out, seen = [], set()
    for r in info.get("search_results") or []:
        u = str(r.get("url") or "").strip()
        if u.startswith("http") and u not in seen:
            seen.add(u)
            out.append({"titre": str(r.get("title") or u)[:120], "url": u, "site": str(r.get("site_name") or "")[:60]})
    return out[:10]


def clean_pistes(raw):
    out = []
    for p in (raw.get("pistes") or [])[:6]:
        if not isinstance(p, dict) or not str(p.get("titre") or "").strip():
            continue
        prio = p.get("priorite") if p.get("priorite") in ("haute", "moyenne", "basse") else "moyenne"
        out.append(
            {
                "titre": str(p.get("titre")).strip()[:120],
                "quand": str(p.get("quand") or "date à confirmer").strip()[:80],
                "lieu": str(p.get("lieu") or "").strip()[:80],
                "pourquoi": str(p.get("pourquoi") or "").strip()[:260],
                "action": str(p.get("action") or "").strip()[:260],
                "priorite": prio,
            }
        )
    if not out:
        raise ValueError("Réponse du modèle sans piste exploitable")
    return out


def main():
    if not API_KEY:
        print("DASHSCOPE_API_KEY manquante", file=sys.stderr)
        return 1
    d = datetime.now(timezone.utc)
    prompt = f"Nous sommes le {d.day} {MOIS[d.month - 1]} {d.year}. Fais la veille locale de la semaine."
    resp = call_qwen(prompt)
    sources = find_sources(resp)
    print(f"Sources de recherche renvoyées : {len(sources)}")
    if not sources:
        # sans résultat de recherche, le modèle répondrait de mémoire : on n'affiche rien
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        print("Diagnostic : clés réponse =", sorted(resp.keys()), "| clés message =", sorted(msg.keys()),
              "| modèle =", resp.get("model"), file=sys.stderr)
        print("Début de la réponse :", str(msg.get("content"))[:300].replace("\n", " "), file=sys.stderr)
        print("Aucune source renvoyée : la recherche web n'a pas fonctionné, rien n'est publié.", file=sys.stderr)
        return 1
    pistes = clean_pistes(parse_json(resp["choices"][0]["message"]["content"]))
    usage = resp.get("usage") or {}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(
            {"generatedAt": now_iso(), "model": resp.get("model") or MODEL, "pistes": pistes,
             "sources": sources,
             "usage": {"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens")}},
            ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"OK : {len(pistes)} pistes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
