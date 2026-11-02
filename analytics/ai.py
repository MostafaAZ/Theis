"""AI analysis of a dashboard by Claude, written for one management perspective
(FR-106, NFR-28, UC-55).

The built-in analysis in ``analytics.story`` is always shown first. When an
Anthropic API key is configured, management can ask Claude for a deeper
analysis of the same numbers: Claude receives the dashboard's KPIs, chart
series and table extracts (no personal data) and returns structured JSON that
replaces the built-in text on the page. Results are cached so repeated views
do not call the API again.

The key is read from ANTHROPIC_API_KEY, or from a file named
``anthropic_api_key.txt`` next to manage.py (kept out of git).
"""
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path

from django.conf import settings
from django.core.cache import cache

from .story import PERSPECTIVE_FOCUS, PERSPECTIVES

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
CACHE_SECONDS = 6 * 3600
KEY_FILE = Path(settings.BASE_DIR) / "anthropic_api_key.txt"

SYSTEM = """You are a senior business analyst advising the top management of Kubbar \
International, a veterinary products distributor operating in Egypt (pilot market) and \
eight other markets. You analyse data from VETREX, the company's inventory management \
system and B2B clearance platform.

Business context:
- The core problem: the purchase forecast is rounded up to supplier minimum order \
quantities (MOQ), which creates surplus stock that expires unsold and is destroyed.
- VETREX flags every batch six months before expiry. The release route is: offer to \
existing customers first, then a Sales Representative flags it as surplus, the COO \
approves release at 65% to 70% of the original price, and the batch is listed on the B2B \
platform for verified trade buyers. Write-off is the last route and needs approval by \
the General Manager and the QA Officer.
- The General Manager approves purchase plans, where the MOQ surplus is shown before \
the order is placed. Money is in Egyptian pounds (EGP).

How to write:
- Use only the numbers in the data you are given. Never invent figures, dates, products \
or causes; if the data cannot support a conclusion, say what is missing.
- Quote the key numbers (money as "1.2M EGP" or "48,052 EGP").
- Write in a consultant's voice: plain English, present tense, direct, about the company \
("Kubbar", "the company", "it"). No hedging filler, no markdown, no emojis.
- Each sentence should state a finding, what it means for the reader, or what to do.
- Keep every string short: a summary of two or three sentences, each other item one or \
two sentences."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "actions": {"type": "array", "items": {"type": "string"}},
        "charts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "insight": {"type": "string"},
                    "implication": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["id", "insight", "implication", "action"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "findings", "risks", "actions", "charts"],
    "additionalProperties": False,
}


class AIUnavailable(Exception):
    """Raised with a message the page can show when AI analysis cannot run."""


def api_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    return key


def available():
    return importlib.util.find_spec("anthropic") is not None and bool(api_key())


def payload(data, filters_text):
    """The numbers Claude sees: KPIs, chart series and short table extracts."""
    return {
        "dashboard": data["title"],
        "filters": filters_text,
        "kpis": [[str(k), str(v)] for k, v in data["kpis"]],
        "charts": [{"id": c["id"], "headline": c["headline"], "subtitle": c["subtitle"],
                    "unit": c["unit"], "labels": c["labels"],
                    "series": [{"name": s["name"], "data": [round(float(v), 2)
                                                            for v in s["data"]]}
                               for s in c["series"]]}
                   for c in data["charts"]],
        "tables": [{"title": t["title"], "headers": t["headers"],
                    "rows": [[str(v) for v in r] for r in t["rows"][:15]]}
                   for t in data["tables"][:4]],
    }


def analyse(data, filters_text, perspective):
    """Return {"summary", "findings", "risks", "actions", "charts": {id: {...}}}."""
    if not available():
        raise AIUnavailable("AI analysis is not set up: add an Anthropic API key.")
    body = payload(data, filters_text)
    label = dict(PERSPECTIVES).get(perspective, "CEO")
    request = json.dumps({"perspective": perspective, "data": body}, sort_keys=True)
    key = "ai-analysis:" + hashlib.sha256(request.encode()).hexdigest()
    cached = cache.get(key)
    if cached:
        return cached
    prompt = (f"Write the analysis for the {label} of Kubbar International. The {label} "
              f"cares most about {PERSPECTIVE_FOCUS[perspective]}.\n\n"
              f"Return: a summary of the whole dashboard; three to five findings in order "
              f"of importance to the {label}; one to three risks; two to four concrete "
              f"actions the {label} can take or ask for in VETREX; and, for every chart id "
              f"in the data, an insight (what the chart shows), an implication (what it "
              f"means for the {label}) and an action.\n\n"
              f"Dashboard data (JSON):\n{json.dumps(body, ensure_ascii=False)}")
    result = _call(prompt)
    ids = {c["id"] for c in data["charts"]}
    result["charts"] = {c["id"]: {k: c[k] for k in ("insight", "implication", "action")}
                        for c in result.get("charts", []) if c.get("id") in ids}
    result.update(source="ai", model=MODEL, perspective=perspective)
    cache.set(key, result, CACHE_SECONDS)
    return result


def _call(prompt):
    import anthropic

    client = anthropic.Anthropic(api_key=api_key(), timeout=180.0, max_retries=2)
    params = dict(
        model=MODEL, max_tokens=16000, system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": SCHEMA}})
    try:
        try:
            # Server-side fallback re-runs a declined request on another model.
            response = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
        except anthropic.BadRequestError:
            response = client.messages.create(**params)
    except anthropic.AuthenticationError:
        raise AIUnavailable("The Anthropic API key was rejected. Check the key.") from None
    except anthropic.RateLimitError:
        raise AIUnavailable("Claude is busy (rate limit). Try again in a minute.") from None
    except anthropic.APIConnectionError:
        raise AIUnavailable("Could not reach Claude. Check the internet connection.") from None
    except anthropic.APIStatusError as e:
        log.warning("Claude API error %s: %s", e.status_code, e.message)
        raise AIUnavailable(f"Claude returned an error ({e.status_code}).") from None
    if response.stop_reason == "refusal":
        raise AIUnavailable("Claude declined to analyse this data.")
    if response.stop_reason == "max_tokens":
        raise AIUnavailable("The analysis was cut short. Narrow the filters and try again.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise AIUnavailable("Claude's answer could not be read. Try again.") from None
