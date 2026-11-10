"""AI layer (phase E4, v0.7.0).

Gateway-agnostic LLM endpoints backed by an OpenAI-chat-style completion API
(config/ai.json: base_url, model, api_key_env, timeout/temperature/max_tokens
and the three prompt instructions):

- POST /ai/rfp-scan          text -> requirements / scope matrix + gaps
- POST /ai/scenario-suggest  recap -> suggested what-if overlays + rationale
- POST /ai/bid-memo          recap + assumptions -> narrative memo

Degradation contract: when config/ai.json is missing, api_key_env is unset
or empty, or the gateway is unreachable / refuses / returns non-200, the
endpoints return HTTP 503 {"detail": "ai gateway unreachable", "schema":
{...}} where `schema` documents that endpoint's request/response shape. A
200 response whose content does not parse to the documented JSON shape
returns 502 with the same body shape (different detail).

The transport is isolated in _http_post_json / _chat so tests can mock the
gateway without network IO; no test may call paid APIs.
"""

import json
import os

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_MODULE_DIR)
DEFAULT_AI_PATH = os.path.join(_REPO_ROOT, "config", "ai.json")

SCHEMAS = {
    "rfp_scan": {
        "request": {"text": "RFP text"},
        "response": {
            "requirements": [{"id": "R1", "category": "coverage|erces|power|testing|...",
                              "text": "requirement text",
                              "source_excerpt": "verbatim quote"}],
            "scope_matrix": [{"requirement_id": "R1", "in_scope": True,
                              "catalog_zones": ["E", "G", "H"], "note": "..."}],
            "gaps": [{"id": "G1", "text": "open question",
                      "reason": "why the RFP is ambiguous / silent"}],
            "assumptions": ["assumption stated to the owner"],
        },
    },
    "scenario_suggest": {
        "request": {"estimate_id": 1, "recap": {"...": "estimate recap object"}},
        "response": {"suggestions": [{"name": "scenario name",
                                      "rationale": "why, grounded in the recap",
                                      "overlay": {"access_overrides": {"location_id": "rope"}},
                                      "impact_hint": "direction + driver"}]},
    },
    "bid_memo": {
        "request": {"estimate_id": 1, "recap": {"...": "estimate recap object"},
                    "assumptions": [{"text": "...", "status": "open"}]},
        "response": {"memo": "narrative memo (markdown/plain)",
                     "highlights": ["the numbers a reviewer must see"]},
    },
}

_REQUIRED_KEYS = {
    "rfp_scan": ("requirements", "scope_matrix", "gaps"),
    "scenario_suggest": ("suggestions",),
    "bid_memo": ("memo",),
}


class AiUnavailable(Exception):
    """No config / no key / gateway unreachable (API maps to 503)."""


class AiResponseError(Exception):
    """Gateway answered but the payload does not fit the contract (502)."""


def load_ai_config(path=None):
    p = path or os.environ.get("ROMTOOL_AI") or DEFAULT_AI_PATH
    if not os.path.exists(p):
        raise AiUnavailable("no ai gateway configured (config/ai.json missing)")
    with open(p, "r", encoding="utf-8") as fh:
        cfg = json.load(fh) or {}
    for k in ("base_url", "model"):
        if not cfg.get(k):
            raise AiUnavailable("ai config is missing %r" % k)
    key_env = cfg.get("api_key_env")
    if key_env:
        if not os.environ.get(key_env, "").strip():
            raise AiUnavailable("api key env var %r is not set" % key_env)
        cfg["api_key"] = os.environ[key_env].strip()
    else:
        cfg["api_key"] = None
    cfg.setdefault("timeout_seconds", 30)
    cfg.setdefault("temperature", 0.2)
    cfg.setdefault("max_tokens", 1600)
    cfg["_path"] = p
    return cfg


# --------------------------------------------------------------- transport

def _http_post_json(url, payload, api_key=None, timeout=30.0):
    """Single place where the network happens. Tests monkeypatch this."""
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if resp.status != 200:
                raise AiUnavailable("gateway HTTP %s" % resp.status)
    except urllib.error.HTTPError as exc:
        raise AiUnavailable("gateway HTTP %s" % exc.code) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise AiUnavailable("gateway unreachable: %s" % type(exc).__name__) from exc
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AiResponseError("gateway returned non-JSON body") from exc


def _extract_content(gw_response):
    if not isinstance(gw_response, dict):
        raise AiResponseError("gateway body is not an object")
    choices = gw_response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AiResponseError("gateway body has no choices[]")
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise AiResponseError("gateway message has no content")
    return content.strip()


def _content_to_json(content):
    """Expect strict JSON; tolerate markdown fences around it."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except ValueError:
        raise AiResponseError("gateway content is not the documented JSON") from None


def _chat(cfg, endpoint_key, user_text):
    """Send one completion for an endpoint; returns the parsed result dict."""
    prompts = cfg.get("prompts") or {}
    messages = [
        {"role": "system",
         "content": prompts.get("system") or
         "You are an estimating assistant. Answer with strict JSON only."},
    ]
    instruction = prompts.get("%s_instruction" % endpoint_key)
    if instruction:
        messages.append({"role": "system", "content": instruction})
    messages.append({"role": "user", "content": user_text})
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = {
        "model": cfg["model"], "messages": messages,
        "temperature": cfg["temperature"], "max_tokens": cfg["max_tokens"],
    }
    gw = _http_post_json(url, body, api_key=cfg.get("api_key"),
                         timeout=float(cfg["timeout_seconds"]))
    content = _extract_content(gw)
    result = _content_to_json(content)
    if not isinstance(result, dict):
        raise AiResponseError("gateway content is not a JSON object")
    missing = [k for k in _REQUIRED_KEYS.get(endpoint_key, ())
               if k not in result]
    if missing:
        raise AiResponseError("gateway content missing keys: %s"
                              % ", ".join(missing))
    return result


# ----------------------------------------------------------- endpoint logic

def rfp_scan(text, cfg=None):
    """RFP text -> requirements / scope matrix + gaps (structured JSON)."""
    cfg = cfg or load_ai_config()
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    result = _chat(cfg, "rfp_scan", text)
    return {"result": result, "meta": _meta(cfg)}


def scenario_suggest(recap, cfg=None):
    """Recap -> suggested what-if overlays with rationale."""
    cfg = cfg or load_ai_config()
    if not isinstance(recap, dict):
        raise ValueError("recap must be an object")
    user = json.dumps({"recap": _trim_recap(recap)}, indent=1)
    result = _chat(cfg, "scenario_suggest", user)
    return {"result": result, "meta": _meta(cfg)}


def bid_memo(recap, assumptions, cfg=None):
    """Recap + assumption register rows -> narrative memo."""
    cfg = cfg or load_ai_config()
    if not isinstance(recap, dict):
        raise ValueError("recap must be an object")
    assumptions = assumptions or []
    user = json.dumps({"recap": _trim_recap(recap), "assumptions": assumptions},
                      indent=1)
    result = _chat(cfg, "bid_memo", user)
    return {"result": result, "meta": _meta(cfg)}


def _meta(cfg):
    return {"model": cfg["model"], "gateway": cfg["base_url"],
            "config": cfg["_path"]}


def _trim_recap(recap):
    """Keep the recap lean for the prompt: totals, money lines, zone rollups,
    crew, quality mix - never the full line list or the raw definition."""
    keys = ("name", "totals", "cost", "money_lines", "zone_rollups", "crew",
            "commercial", "warnings", "schedule")
    out = {k: recap[k] for k in keys if k in recap}
    lines = recap.get("lines") or []
    kinds = {}
    for l in lines:
        q = l.get("source_quality")
        kinds[q] = kinds.get(q, 0) + 1
    out["line_quality_mix"] = kinds
    out["n_lines"] = len(lines)
    return out
