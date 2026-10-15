#!/usr/bin/env python3
"""Résumé quotidien du Watchtower, rédigé par Qwen (Alibaba Cloud Model Studio).

Étapes :
  1. récupère l'état courant sur le webhook n8n (le même que celui du dashboard) ;
  2. en tire un digest SANS données personnelles (pas d'emails, pas de codes de jeu) ;
  3. demande à Qwen un court résumé en JSON ;
  4. écrit data/ai-summary.json, que le dashboard affiche.

Dépendances : aucune (bibliothèque standard uniquement).

Variables d'environnement :
  DASHSCOPE_API_KEY  (obligatoire) clé API Alibaba Model Studio, région Singapour
  QWEN_MODEL         (optionnel)   défaut : qwen3.8-flash
  QWEN_BASE_URL      (optionnel)   défaut : https://dashscope-intl.aliyuncs.com/compatible-mode/v1
  STATUS_URL         (optionnel)   défaut : webhook n8n du Watchtower
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

STATUS_URL = os.environ.get(
    "STATUS_URL",
    "https://n8n-production-9874d.up.railway.app/webhook/aventicum-watchtower-status",
)
BASE_URL = os.environ.get(
    "QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
).rstrip("/")
MODEL = os.environ.get("QWEN_MODEL", "qwen3.8-flash")
# .strip() : un espace ou un retour à la ligne collé avec la clé la ferait refuser (401)
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()

# Autres régions d'Alibaba, testées uniquement pour diagnostiquer un 401
OTHER_REGIONS = {
    "Chine (Beijing)": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "Etats-Unis": "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
}

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "ai-summary.json"

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{28,}\b")  # clés, jetons, identifiants longs

MAX_ERRORS_SENT = 8
MAX_TEXT = 220


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def redact(text):
    """Retire tout ce qui ressemble à un email ou à un jeton avant envoi au modèle."""
    text = str(text or "")
    text = EMAIL_RE.sub("[email]", text)
    text = LONG_TOKEN_RE.sub("[jeton]", text)
    return text[:MAX_TEXT]


def http_json(url, *, data=None, headers=None, timeout=45):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_status():
    return http_json(STATUS_URL, headers={"Cache-Control": "no-cache"})


def build_digest(status):
    """Digest minimal et anonymisé de l'état du Watchtower."""
    checks = []
    for c in status.get("checks") or []:
        checks.append(
            {
                "systeme": redact(c.get("checkName")),
                "statut": "ok" if c.get("status") == "ok" else "erreur",
                "detail": redact(c.get("detail")),
                "releve_a": c.get("checkedAt"),
            }
        )

    plays = status.get("plays") or []
    achats = [p for p in plays if p.get("eventType") == "achat"]
    parties = [p for p in plays if p.get("eventType") != "achat"]
    langues = {}
    for p in plays:
        lg = (p.get("langue") or "").lower()
        if lg:
            langues[lg] = langues.get(lg, 0) + 1

    errors = []
    for e in (status.get("errors") or [])[:MAX_ERRORS_SENT]:
        errors.append(
            {
                "workflow": redact(e.get("workflowName")),
                "noeud": redact(e.get("nodeName")),
                "message": redact(e.get("message")),
                "survenu_a": e.get("happenedAt"),
            }
        )

    return {
        "genere_a": status.get("generatedAt"),
        "systemes": checks,
        "activite_recente": {
            "achats": len(achats),
            "parties": len(parties),
            "langues": langues,
        },
        "incidents_recents": errors,
        "nombre_incidents_total": len(status.get("errors") or []),
    }


SYSTEM_PROMPT = (
    "Tu es l'analyste du poste de contrôle (Watchtower) d'Aventicum Quest, un jeu de piste "
    "en plein air à Avenches. Tu reçois un digest JSON de l'état technique. "
    "Réponds UNIQUEMENT par un objet JSON avec exactement ces clés :\n"
    '  "headline": une phrase de 90 caractères maximum résumant la situation ;\n'
    '  "summary": 2 à 3 phrases claires en français, ton sobre et factuel ;\n'
    '  "actions": liste de 0 à 3 actions concrètes à faire (liste vide si tout va bien).\n'
    "Règles : n'utilise que les données reçues, n'invente rien. Si un système est en erreur, "
    "dis lequel et propose une cause probable en la présentant comme une hypothèse. "
    "Signale un relevé vieux de plus de 90 minutes comme surveillance en retard. "
    "Pas d'émojis, pas de tirets cadratins."
)


def diagnose_401():
    """Aide à comprendre un 401 sans jamais afficher la clé."""
    print(
        f"Diagnostic : la clé fait {len(API_KEY)} caractères, "
        f"préfixe '{API_KEY[:5]}', serveur utilisé : {BASE_URL}",
        file=sys.stderr,
    )
    for name, base in OTHER_REGIONS.items():
        if base == BASE_URL:
            continue
        req = urllib.request.Request(
            f"{base}/models", headers={"Authorization": f"Bearer {API_KEY}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:  # réseau, délai...
            code = type(e).__name__
        verdict = "CLE ACCEPTEE ICI" if code == 200 else "refusee"
        print(f"  region {name} : HTTP {code} ({verdict})", file=sys.stderr)


def call_qwen(digest):
    url = f"{BASE_URL}/chat/completions"
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Digest du poste de contrôle (JSON) :\n"
                + json.dumps(digest, ensure_ascii=False),
            },
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 600,
        "enable_thinking": False,  # pas de raisonnement : moins cher et plus rapide
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    last_err = None
    for attempt in range(3):
        try:
            return http_json(url, data=payload, headers=headers, timeout=60)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last_err = f"HTTP {e.code}: {detail}"
            if e.code == 401:
                diagnose_401()
            # 4xx autres que 429 : inutile de réessayer (clé invalide, quota épuisé...)
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


def clean_result(raw):
    headline = str(raw.get("headline") or "").strip()[:120]
    summary = str(raw.get("summary") or "").strip()[:700]
    actions = raw.get("actions") or []
    if not isinstance(actions, list):
        actions = []
    actions = [str(a).strip()[:200] for a in actions if str(a).strip()][:3]
    if not headline or not summary:
        raise ValueError("Réponse du modèle incomplète")
    return headline, summary, actions


def write_output(obj):
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    # 1. état courant
    try:
        status = fetch_status()
    except Exception as e:  # poste injoignable : on le dit sans dépenser de tokens
        print(f"Webhook injoignable : {e}", file=sys.stderr)
        write_output(
            {
                "generatedAt": now_iso(),
                "model": None,
                "level": "unreachable",
                "headline": "Poste de contrôle injoignable",
                "summary": (
                    "Le webhook n8n du Watchtower n'a pas répondu au moment du résumé. "
                    "Le serveur n8n sur Railway est peut-être arrêté."
                ),
                "actions": ["Vérifier que le service n8n tourne sur Railway."],
            }
        )
        return 0

    digest = build_digest(status)
    checks = digest["systemes"]
    level = "incident" if any(c["statut"] == "erreur" for c in checks) else "ok"

    # 2. résumé par Qwen
    if not API_KEY:
        print("DASHSCOPE_API_KEY manquante", file=sys.stderr)
        return 1

    resp = call_qwen(digest)
    content = resp["choices"][0]["message"]["content"]
    headline, summary, actions = clean_result(parse_model_json(content))
    usage = resp.get("usage") or {}

    # 3. écriture
    write_output(
        {
            "generatedAt": now_iso(),
            "model": resp.get("model") or MODEL,
            "level": level,
            "headline": headline,
            "summary": summary,
            "actions": actions,
            "usage": {
                "input": usage.get("prompt_tokens"),
                "output": usage.get("completion_tokens"),
            },
        }
    )
    print(f"OK ({level}) : {headline}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
