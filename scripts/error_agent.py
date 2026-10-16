#!/usr/bin/env python3
"""Agent de diagnostic des erreurs d'Aventicum Quest, rédigé par Qwen.

Lit l'état du Watchtower (le même webhook que le dashboard), regroupe les incidents,
et demande à Qwen une cause probable et un correctif pour chaque problème.
Écrit data/error-report.json (affiché sur le dashboard).

Économie : si rien n'a changé depuis le dernier rapport, aucun appel au modèle
et aucun commit. Les emails et jetons sont retirés avant tout envoi à Alibaba.

Variables d'environnement : DASHSCOPE_API_KEY (obligatoire), QWEN_MODEL, QWEN_BASE_URL, STATUS_URL
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATUS_URL = os.environ.get(
    "STATUS_URL",
    "https://n8n-production-9874d.up.railway.app/webhook/aventicum-watchtower-status",
)
BASE_URL = os.environ.get(
    "QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
).rstrip("/")
MODEL = os.environ.get("QWEN_MODEL", "qwen3.8-flash")
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "error-report.json"

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{28,}\b")

# Correctifs déjà appliqués. Qwen ne doit pas relancer l'alerte pour ces erreurs anciennes.
# A compléter à la main quand un problème est réglé : {"motif": ..., "corrige_le": ..., "note": ...}
KNOWN_FIXES = [
    {
        "motif": "URLSearchParams is not defined",
        "corrige_le": "2026-10-13",
        "note": "Le code du nœud Générer Code AQUILA n'utilise plus URLSearchParams.",
    },
    {
        "motif": "Signature invalide, requête rejetée",
        "corrige_le": "2026-10-13",
        "note": "La signature Stripe est maintenant calculée sur le corps brut de la requête.",
    },
    {
        "motif": "The connection timed out (Notifier Problème)",
        "corrige_le": "2026-09-21",
        "note": "Coupure ponctuelle du service de notification, sans récidive.",
    },
]

SYSTEM_PROMPT = (
    "Tu es l'ingénieur de fiabilité d'Aventicum Quest, un jeu de piste vendu en ligne. "
    "Pile technique : paiement Stripe, automatisations n8n hébergées sur Railway, emails Brevo, "
    "tableau Google Sheets des commandes, notifications OneSignal, sites sur GitHub Pages. "
    "Tu reçois en JSON : l'état des systèmes, les incidents récents regroupés (avec leur dernière "
    "date en UTC) et la liste des correctifs déjà appliqués. "
    "Règles : base-toi uniquement sur les données reçues. Un incident dont le motif figure dans "
    "les correctifs connus et dont la dernière occurrence est ANTÉRIEURE à la date de correction "
    "est 'probablement_resolu'. Un incident identique après la date de correction est 'nouveau' "
    "et grave. Un incident sans correctif connu est 'a_surveiller' ou 'nouveau' selon son âge "
    "(moins de 48 h = nouveau). Donne la cause comme une hypothèse, et un correctif précis en "
    "une phrase que Bjorn peut appliquer dans n8n. Les achats et la livraison du code client "
    "(Stripe, Générer Code, Brevo) sont critiques. Pas d'émojis, pas de tirets cadratins.\n"
    "Réponds UNIQUEMENT par un objet JSON :\n"
    '  "severite": "ok" | "a_surveiller" | "urgent" ;\n'
    '  "titre": phrase de 90 caractères maximum ;\n'
    '  "resume": 1 à 2 phrases ;\n'
    '  "problemes": liste de 0 à 5 objets avec "probleme" (court), "statut" '
    '("nouveau" | "a_surveiller" | "probablement_resolu"), "cause" (hypothèse), "correctif".'
)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def redact(text, n=220):
    text = EMAIL_RE.sub("[email]", str(text or ""))
    return LONG_TOKEN_RE.sub("[jeton]", text)[:n]


def http_json(url, *, data=None, headers=None, timeout=60):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_digest(status):
    groups = {}
    for e in status.get("errors") or []:
        key = (redact(e.get("workflowName")), redact(e.get("nodeName")), redact(e.get("message")))
        g = groups.setdefault(key, {"fois": 0, "premiere": None, "derniere": None})
        g["fois"] += 1
        t = e.get("happenedAt")
        if t:
            g["premiere"] = min(filter(None, [g["premiere"], t]))
            g["derniere"] = max(filter(None, [g["derniere"], t]))
    incidents = [
        {"workflow": k[0], "noeud": k[1], "message": k[2], **v} for k, v in groups.items()
    ]
    incidents.sort(key=lambda x: x["derniere"] or "", reverse=True)
    systemes = [
        {
            "systeme": redact(c.get("checkName")),
            "statut": "ok" if c.get("status") == "ok" else "erreur",
            "detail": redact(c.get("detail")),
            "releve_a": c.get("checkedAt"),
        }
        for c in status.get("checks") or []
    ]
    return {
        "maintenant_utc": now_iso(),
        "systemes": systemes,
        "incidents_regroupes": incidents[:12],
        "correctifs_connus": KNOWN_FIXES,
    }


def fingerprint(digest):
    stable = {k: v for k, v in digest.items() if k != "maintenant_utc"}
    # ne pas relancer le modèle juste parce que l'heure avance : on ne regarde que les faits
    stable["systemes"] = [{k: v for k, v in s.items() if k != "releve_a"} for s in stable["systemes"]]
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:16]


def call_qwen(digest):
    url = f"{BASE_URL}/chat/completions"
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Données (JSON) :\n" + json.dumps(digest, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 1200,
        "enable_thinking": False,
    }
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    last = None
    for attempt in range(3):
        try:
            return http_json(url, data=payload, headers=headers)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError) as e:
            last = str(e)
        time.sleep(4 * (attempt + 1))
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


def clean_report(raw):
    sev = raw.get("severite") if raw.get("severite") in ("ok", "a_surveiller", "urgent") else "a_surveiller"
    probs = []
    for p in (raw.get("problemes") or [])[:5]:
        if not isinstance(p, dict):
            continue
        statut = p.get("statut") if p.get("statut") in ("nouveau", "a_surveiller", "probablement_resolu") else "a_surveiller"
        probs.append(
            {
                "probleme": str(p.get("probleme") or "").strip()[:140],
                "statut": statut,
                "cause": str(p.get("cause") or "").strip()[:300],
                "correctif": str(p.get("correctif") or "").strip()[:300],
            }
        )
    titre = str(raw.get("titre") or "").strip()[:120]
    resume = str(raw.get("resume") or "").strip()[:500]
    if not titre:
        raise ValueError("Réponse du modèle incomplète")
    return sev, titre, resume, [p for p in probs if p["probleme"]]


def main():
    try:
        status = http_json(STATUS_URL, headers={"Cache-Control": "no-cache"}, timeout=45)
    except Exception as e:
        print(f"Webhook injoignable : {e}", file=sys.stderr)
        return 0  # le résumé quotidien signale déjà ce cas

    digest = build_digest(status)
    fp = fingerprint(digest)

    try:
        old = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        old = {}
    if old.get("fingerprint") == fp:
        print("Rien de nouveau depuis le dernier rapport, aucun appel.")
        return 0

    base = {"generatedAt": now_iso(), "fingerprint": fp}
    all_ok = all(s["statut"] == "ok" for s in digest["systemes"])
    if all_ok and not digest["incidents_regroupes"]:
        out = {**base, "model": None, "severite": "ok", "titre": "Aucune erreur à signaler",
               "resume": "Tous les systèmes répondent et aucun incident n'est enregistré.", "problemes": []}
    else:
        if not API_KEY:
            print("DASHSCOPE_API_KEY manquante", file=sys.stderr)
            return 1
        resp = call_qwen(digest)
        sev, titre, resume, probs = clean_report(parse_json(resp["choices"][0]["message"]["content"]))
        usage = resp.get("usage") or {}
        out = {**base, "model": resp.get("model") or MODEL, "severite": sev, "titre": titre,
               "resume": resume, "problemes": probs,
               "usage": {"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens")}}

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OK ({out['severite']}) : {out['titre']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
