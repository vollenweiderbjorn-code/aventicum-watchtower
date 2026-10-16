#!/usr/bin/env python3
"""Agent de veille locale d'Aventicum Quest, rédigé par Qwen avec recherche web.

Chaque semaine, le script lit l'agenda officiel d'Avenches (avenches.ch/agenda), puis Qwen en tire
les occasions utiles pour faire connaître le jeu (événements, publics, partenaires possibles).
Écrit data/local-watch.json (affiché sur le dashboard).

Les liens affichés viennent UNIQUEMENT des pages réellement lues, jamais du texte du modèle.
(La recherche web intégrée de Qwen ne renvoie rien sur ce modèle : on lit les sources nous-mêmes.)

Variables d'environnement : DASHSCOPE_API_KEY (obligatoire), QWEN_MODEL, QWEN_BASE_URL
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

BASE_URL = os.environ.get(
    "QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
).rstrip("/")
MODEL = os.environ.get("QWEN_MODEL", "qwen3.8-max")
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "local-watch.json"

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]

AGENDA_PAGES = [
    "https://www.avenches.ch/agenda/",
    "https://www.avenches.ch/agenda/page/2/",
    "https://www.avenches.ch/agenda/page/3/",
]

SYSTEM_PROMPT = (
    "Tu es l'éclaireur local d'Aventicum Quest, un jeu de piste en plein air à Avenches (Vaud), "
    "sur le thème de la Rome antique, qui se joue en équipe (familles, amis, groupes) en environ "
    "120 minutes. Le créateur, Bjorn, cherche où et quand faire connaître le jeu localement. "
    "Tu reçois le texte de l'agenda officiel d'Avenches (événements avec dates et liens). "
    "Choisis 4 à 6 occasions des 10 prochaines semaines où le jeu peut être promu : événements qui "
    "attirent des familles, des amis ou des groupes, événements sur la Rome antique ou le patrimoine, "
    "jeux, fêtes de fin d'année (idée de bon cadeau). Ignore les événements déjà passés. "
    "Règles strictes : n'utilise que les événements et les dates présents dans le texte reçu. "
    "N'invente aucun événement, date, lieu ou contact. Si une date manque, écris 'date à confirmer'. "
    "Pas d'émojis, pas de tirets cadratins, français de Suisse romande.\n"
    "Réponds UNIQUEMENT par un objet JSON avec une clé \"pistes\" : liste de 4 à 6 objets avec :\n"
    '  "titre": nom de l\'événement ;\n'
    '  "quand": date ou période ;\n'
    '  "lieu": commune ou lieu ;\n'
    '  "pourquoi": pourquoi c\'est utile pour le jeu, en une phrase ;\n'
    '  "action": une action concrète pour Bjorn (quoi publier, qui contacter), en une phrase ;\n'
    '  "url": l\'adresse de l\'événement, copiée telle quelle depuis le texte reçu ;\n'
    '  "priorite": "haute" | "moyenne" | "basse".'
)


class TextAndLinks(HTMLParser):
    """Extrait le texte visible et les liens d'événements d'une page."""

    def __init__(self, base):
        super().__init__()
        self.base = base
        self.skip = 0
        self.parts = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
        if tag == "a":
            href = dict(attrs).get("href") or ""
            full = urllib.parse.urljoin(self.base, href)
            if "/events/" in full and full not in self.links:
                self.links.append(full)
                self.parts.append(f" [lien: {full}] ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self.skip:
            self.skip -= 1
        if tag in ("p", "div", "li", "h1", "h2", "h3", "h4", "article", "section", "br"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip() + " ")


def fetch_agenda():
    """Texte des pages de l'agenda et liste des liens lus. Une page absente est ignorée."""
    texts, links = [], []
    for url in AGENDA_PAGES:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (aventicum-watchtower)"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                html = r.read().decode("utf-8", "replace")
        except Exception as e:
            print(f"Page ignorée {url} : {e}", file=sys.stderr)
            continue
        parser = TextAndLinks(url)
        parser.feed(html)
        text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        texts.append(f"=== {url} ===\n{text[:9000]}")
        links += [l for l in parser.links if l not in links]
    return "\n\n".join(texts), links


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
        "response_format": {"type": "json_object"},
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


def clean_pistes(raw, allowed_links):
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
                # seuls les liens réellement lus sur l'agenda sont conservés
                "url": str(p.get("url") or "").strip() if str(p.get("url") or "").strip() in allowed_links else "",
            }
        )
    if not out:
        raise ValueError("Réponse du modèle sans piste exploitable")
    return out


def main():
    if not API_KEY:
        print("DASHSCOPE_API_KEY manquante", file=sys.stderr)
        return 1
    agenda, links = fetch_agenda()
    print(f"Agenda lu : {len(agenda)} caractères, {len(links)} liens d'événements")
    if len(agenda) < 500:
        print("Agenda illisible ou vide : rien n'est publié.", file=sys.stderr)
        return 1
    d = datetime.now(timezone.utc)
    prompt = (
        f"Nous sommes le {d.day} {MOIS[d.month - 1]} {d.year}. Voici l'agenda officiel d'Avenches :\n\n"
        + agenda
    )
    resp = call_qwen(prompt)
    pistes = clean_pistes(parse_json(resp["choices"][0]["message"]["content"]), set(links))
    usage = resp.get("usage") or {}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(
            {"generatedAt": now_iso(), "model": resp.get("model") or MODEL, "source": "avenches.ch/agenda",
             "pistes": pistes,
             "usage": {"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens")}},
            ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"OK : {len(pistes)} pistes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
