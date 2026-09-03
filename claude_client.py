"""
claude_client.py — a tiny, dependency-free Claude API client.

The rest of movers-build is deliberately Python-stdlib-only (no pip installs) so
the whole app stays one-command runnable. To keep that promise, this talks to
the Claude Messages API over plain urllib — the same way av_client.py talks to
Alpha Vantage — instead of pulling in the `anthropic` SDK.

Used by analyst.py's LLM brain. It is OPT-IN: nothing calls it unless you paste
an Anthropic API key into config.json under "anthropic". No key -> the analyst
uses its built-in rule-based brain instead. Shadow-only either way; this module
never places an order and never even knows what an order is.

The Anthropic API key is separate from a Claude subscription and is pay-per-use
(console.anthropic.com). Keep it in config.json only; never print it.
"""

import json
import urllib.request
import urllib.error

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-4-8"


class ClaudeError(Exception):
    """Any reason the call couldn't produce a usable answer (missing key,
    network trouble, HTTP error, empty/garbled reply). Callers catch this and
    fall back to their non-LLM path — a Claude hiccup must never break a run."""


def _key(cfg):
    a = cfg.get("anthropic") or {}
    return (a.get("api_key") or "").strip()


def have_key(cfg):
    """True only if a real key is present (not blank, not the PASTE_ placeholder)."""
    k = _key(cfg)
    return bool(k) and "PASTE_" not in k


def complete(cfg, system, user, max_tokens=1024, timeout=60):
    """One-shot text completion. Returns (text, usage) where usage is
    {"input_tokens": N, "output_tokens": N} straight from the API — the caller
    prices it. Raises ClaudeError on any problem. The key is read from
    config.json and never logged; on an HTTP error we surface the API's error
    body (which does not contain the key — the key travels in a request header,
    not the response)."""
    key = _key(cfg)
    if not key or "PASTE_" in key:
        raise ClaudeError("no Anthropic API key configured")
    model = (cfg.get("anthropic") or {}).get("model") or DEFAULT_MODEL

    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")

    req = urllib.request.Request(API_URL, data=body, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", API_VERSION)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise ClaudeError(f"HTTP {e.code} from Claude API: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ClaudeError(f"network error calling Claude API: {e}") from e

    parts = [b.get("text", "") for b in data.get("content", [])
             if b.get("type") == "text"]
    text = "".join(parts).strip()
    if not text:
        raise ClaudeError(f"empty response (stop_reason={data.get('stop_reason')})")
    u = data.get("usage") or {}
    usage = {"input_tokens": int(u.get("input_tokens") or 0),
             "output_tokens": int(u.get("output_tokens") or 0)}
    return text, usage


def price(cfg, input_tokens, output_tokens):
    """Dollar cost of a call, using the per-million-token rates in config
    (rulebook: no hardcoded numbers — if pricing or the model changes, the
    dials change). Defaults match claude-opus-4-8 list pricing."""
    a = cfg.get("anthropic") or {}
    pin = float(a.get("price_per_mtok_input", 5.00))
    pout = float(a.get("price_per_mtok_output", 25.00))
    return (input_tokens / 1e6) * pin + (output_tokens / 1e6) * pout
