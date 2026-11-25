#!/usr/bin/env python3
"""Self-maintaining command-line client for the Gemini API (Interactions API).

  gemini.py ask    "question" [-f FILE ...] [-m MODEL[-EFFORT]] [-s SYSTEM] [-t LEVEL] [-c ID] [--yes]
  gemini.py image  "prompt"   [-o OUT] [-i IMAGE ...] [-m MODEL] [--aspect R] [--size S] [--yes]
  gemini.py check  [MODEL[-EFFORT]] [--image] [--probe] [--refresh]
  gemini.py models [--all] [--refresh]
  gemini.py doctor
  gemini.py catalog

Model aliases (neon, carbon, flash, nb, ...) are resolved against Google's live
model list for your key, newest stable version first, so new releases need no
edits. The API is the judge of effort levels: what it accepts or rejects is
remembered. Prices are not published by the API, so they live in catalog.json:
the copy bundled with the skill, or a user copy in the config directory,
whichever was checked more recently.

API key lookup order: $GEMINI_API_KEY, $GOOGLE_API_KEY, then the system keychain
(macOS Keychain or Linux Secret Service) item with service name "gemini-api-key".

Settings: ~/.config/gemini-skill/config.json, e.g. {"default_model": "neon-high"},
or the environment variables GEMINI_DEFAULT_MODEL, GEMINI_DEFAULT_IMAGE_MODEL
and GEMINI_MAX_INPUT_COST, which take precedence.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import difflib
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

__version__ = "1.0.0"

API_BASE = os.environ.get(
    "GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta"
).rstrip("/")
API_REVISION = os.environ.get("GEMINI_API_REVISION", "2026-05-20")
KEYCHAIN_SERVICE = "gemini-api-key"
SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "gemini-skill"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "gemini-skill"
CONFIG_PATH = Path(os.environ.get("GEMINI_CONFIG") or CONFIG_DIR / "config.json")
BUNDLED_CATALOG = SKILL_DIR / "catalog.json"
USER_CATALOG = CONFIG_DIR / "catalog.json"
STATE_PATH = Path(os.environ.get("GEMINI_STATE") or CACHE_DIR / "state.json")
MODEL_LIST_TTL = 24 * 3600
PRICE_STALE_DAYS = 30
MAX_INPUT_TOKENS_UNPRICED = 150_000
MAX_TEXT_FILE_BYTES = 2_000_000
BASE_EFFORT_LEVELS = ["minimal", "low", "medium", "high", "xhigh"]


def _load_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        print(f"[gemini] ignoring {CONFIG_PATH}: {err}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


CONFIG = _load_config()


def _setting(env: str, key: str, default):
    return os.environ.get(env) or CONFIG.get(key) or default


DEFAULT_TEXT_MODEL = str(_setting("GEMINI_DEFAULT_MODEL", "default_model", "hydrogen"))
DEFAULT_IMAGE_MODEL = str(_setting("GEMINI_DEFAULT_IMAGE_MODEL", "default_image_model", "nb"))
try:
    MAX_INPUT_COST = float(_setting("GEMINI_MAX_INPUT_COST", "max_input_cost", 0.50))
except ValueError:
    MAX_INPUT_COST = 0.50

_V = r"(\d+(?:\.\d+)*)"
# Alias -> pattern over live model IDs. Only exact matches count, so previews,
# TTS, live and image variants never sneak in. The highest version wins.
FAMILIES = {
    "text": {
        "carbon": rf"gemini-{_V}-carbon",
        "neon": rf"gemini-{_V}-neon",
        "argon": rf"gemini-{_V}-argon",
        "hydrogen": rf"gemini-{_V}-hydrogen",
        "flash": rf"gemini-{_V}-flash",
        "lite": rf"gemini-{_V}-flash-lite",
    },
    "image": {
        "nb": rf"gemini-nano-banana-{_V}",
        "pro": rf"gemini-{_V}-pro-image",
        "flash": rf"gemini-{_V}-flash-image",
        "lite": rf"gemini-{_V}-flash-lite-image",
    },
}
# Used only when the live list can't be fetched and nothing is cached.
FALLBACK = {
    "text": {
        "carbon": "gemini-4-carbon",
        "neon": "gemini-4-neon",
        "argon": "gemini-4-argon",
        "hydrogen": "gemini-4-hydrogen",
        "flash": "gemini-3.8-flash",
        "lite": "gemini-3.5-flash-lite",
    },
    "image": {
        "nb": "gemini-nano-banana-2.1",
        "pro": "gemini-3-pro-image",
        "flash": "gemini-3.1-flash-image",
        "lite": "gemini-3.1-flash-lite-image",
    },
}
IMAGE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
TODAY = dt.date.today()


# ---------------------------------------------------------------- utilities

def die(message: str) -> None:
    print(f"gemini: {message}", file=sys.stderr)
    sys.exit(1)


def note(message: str) -> None:
    print(f"[gemini] {message}", file=sys.stderr)


def today() -> str:
    return TODAY.isoformat()


def days_since(iso: str | None) -> int | None:
    try:
        return (TODAY - dt.date.fromisoformat(str(iso))).days
    except ValueError:
        return None


def version_key(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as err:
        note(f"could not read {path}: {err}")
        return default


def money(value: float) -> str:
    if value < 0.0001:
        return "<$0.0001"
    return f"${value:.4f}" if value < 0.01 else f"${value:.2f}"


def load_catalog() -> tuple[dict, Path | None]:
    """The most recently checked of the user's catalog and the bundled one (the user's wins a tie)."""
    override = os.environ.get("GEMINI_CATALOG")
    candidates = [Path(override)] if override else [USER_CATALOG, BUNDLED_CATALOG]
    best: dict = {}
    best_path = None
    for path in candidates:
        data = load_json(path, {})
        if not isinstance(data, dict) or not data.get("models"):
            continue
        if best_path is None or str(data.get("verified_on", "")) > str(best.get("verified_on", "")):
            best, best_path = data, path
    return best, best_path


def short_path(path: Path | None) -> str:
    if path is None:
        return "(none)"
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text


class Store:
    """catalog.json (curated, prices) plus a state file (learned, cached)."""

    def __init__(self) -> None:
        self.catalog, self.catalog_path = load_catalog()
        if not self.catalog:
            note("no catalog.json found; prices and documented effort levels are unknown")
        self.state = load_json(STATE_PATH, {})
        self.dirty = False

    def model_entry(self, model_id: str) -> dict:
        return (self.catalog.get("models") or {}).get(model_id) or {}

    def effort_levels(self) -> list[str]:
        return list(dict.fromkeys(BASE_EFFORT_LEVELS + list(self.catalog.get("effort_levels") or [])))

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True))
            tmp.replace(STATE_PATH)
            self.dirty = False
        except OSError as err:
            note(f"could not save {STATE_PATH}: {err}")


# ---------------------------------------------------------------- API access

class ApiError(Exception):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message

    def __str__(self) -> str:
        text = f"API error {self.status}: {self.message}" if self.status else self.message
        if self.status in (403, 429):
            text += (
                "\n  Hint: some models (Neon, Carbon, Argon and the image models) need a billing-enabled key;"
                " free-tier keys can use Hydrogen and Flash. A 429 can also be a rate limit."
            )
        return text


KEY_SETUP = {
    "darwin": 'security add-generic-password -a "$USER" -s gemini-api-key -w',
    "linux": 'secret-tool store --label="Gemini API key" service gemini-api-key',
}


def find_api_key() -> tuple[str, str]:
    """(key, where it came from). The key is '' if none was found."""
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = os.environ.get(var, "").strip()
        if value:
            return value, f"${var}"
    lookups = []
    if os.environ.get("GEMINI_USE_KEYCHAIN", "1") == "0":
        pass  # keychain lookup disabled (used by the tests)
    elif sys.platform == "darwin":
        lookups.append(("macOS Keychain", ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"]))
    elif sys.platform.startswith("linux"):
        lookups.append(("Secret Service (secret-tool)", ["secret-tool", "lookup", "service", KEYCHAIN_SERVICE]))
    for label, command in lookups:
        try:
            found = subprocess.run(command, capture_output=True, text=True, timeout=60)
            if found.returncode == 0 and found.stdout.strip():
                return found.stdout.strip(), label
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "", ""


def api_key() -> str:
    key, _ = find_api_key()
    if key:
        return key
    platform = "linux" if sys.platform.startswith("linux") else sys.platform
    store_hint = f", or store it in the system keychain:\n  {KEY_SETUP[platform]}" if platform in KEY_SETUP else ""
    die(f"no API key found. Set the GEMINI_API_KEY environment variable{store_hint}")
    return ""


def api_request(method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
    headers = {"x-goog-api-key": api_key(), "Api-Revision": API_REVISION}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{API_BASE}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(detail)
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                detail = parsed["error"].get("message") or detail
        except ValueError:
            pass
        raise ApiError(err.code, detail[:3000]) from None
    except urllib.error.URLError as err:
        raise ApiError(None, f"could not reach the Gemini API: {err.reason}") from None
    except TimeoutError:
        raise ApiError(None, f"no response within {timeout:.0f}s (raise --timeout)") from None
    try:
        return json.loads(payload)
    except ValueError:
        raise ApiError(None, f"API returned non-JSON: {payload[:500]}") from None


def live_models(store: Store, refresh: bool = False) -> dict | None:
    """Models available to this key, cached for a day. None if unknown."""
    cache = store.state.get("model_list") or {}
    fresh = time.time() - cache.get("fetched_at", 0) < MODEL_LIST_TTL
    if cache.get("items") and fresh and not refresh:
        return cache["items"]
    items: dict = {}
    token = None
    try:
        for _ in range(20):
            query = {"pageSize": "200", **({"pageToken": token} if token else {})}
            data = api_request("GET", "/models?" + urllib.parse.urlencode(query), timeout=30)
            for model in data.get("models") or []:
                model_id = str(model.get("name", "")).removeprefix("models/")
                if model_id:
                    items[model_id] = {
                        "display": model.get("displayName"),
                        "input_limit": model.get("inputTokenLimit"),
                        "output_limit": model.get("outputTokenLimit"),
                        "thinking": bool(model.get("thinking")),
                    }
            token = data.get("nextPageToken")
            if not token:
                break
        if not items:
            raise ApiError(None, "the model list came back empty")
    except ApiError as err:
        if cache.get("items"):
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(cache.get("fetched_at", 0)))
            note(f"could not refresh the model list ({err}); using the copy from {stamp}")
            return cache["items"]
        note(f"could not fetch the model list ({err}); using built-in model names, unverified")
        return None
    store.state["model_list"] = {"fetched_at": time.time(), "items": items}
    store.dirty = True
    return items


# ---------------------------------------------------------------- models, effort, prices

def newest(kind: str, alias: str, live: dict | None) -> str | None:
    if live is None:
        return FALLBACK[kind].get(alias)
    pattern = re.compile(FAMILIES[kind][alias])
    found = [(version_key(m.group(1)), model_id) for model_id in live if (m := pattern.fullmatch(model_id))]
    return max(found)[1] if found else None


def split_effort(spec: str, kind: str, known: set[str], store: Store) -> tuple[str, str | None]:
    """'neon-high' -> ('neon', 'high'). Only after a known alias or model, so real IDs stay whole."""
    if kind != "text":
        return spec, None
    base, sep, level = spec.rpartition("-")
    if sep and level.lower() in store.effort_levels() and (base.lower() in FAMILIES[kind] or base in known):
        return base, level.lower()
    return spec, None


def resolve(spec: str, kind: str, store: Store, live: dict | None) -> tuple[str, str | None, str | None]:
    """Returns (model_id, effort_from_spec, alias). Dies if the model isn't available."""
    families = FAMILIES[kind]
    known = set(live or {}) | set(store.catalog.get("models") or {}) | set(FALLBACK[kind].values())
    base, effort = split_effort(spec, kind, known, store)
    key = base.lower()
    if key in families:
        model_id = newest(kind, key, live)
        if not model_id:
            die(f'Google\'s live model list has no stable "{key}" model for your key. Run "gemini.py models".')
        slot = f"{kind}:{key}"
        aliases = store.state.setdefault("aliases", {})
        if aliases.get(slot) and aliases[slot] != model_id:
            note(f'"{key}" now means {model_id} (was {aliases[slot]}): Google released a newer version')
        if aliases.get(slot) != model_id:
            aliases[slot] = model_id
            store.dirty = True
        return model_id, effort, key
    if live is not None and base not in live:
        options = list(families) + [f"{a}-{lvl}" for a in families for lvl in store.effort_levels()] + list(live)
        close = difflib.get_close_matches(spec.lower(), options, n=3, cutoff=0.6)
        hint = f" Did you mean: {', '.join(close)}?" if close else ' Run "gemini.py models --all" to list them.'
        die(f'"{spec}" is not a known alias or a model in Google\'s live list for your key.{hint}')
    return base, effort, None


def learned_effort(store: Store, model_id: str) -> dict:
    entry = (store.state.get("effort") or {}).get(model_id) or {}
    return {"ok": dict(entry.get("ok") or {}), "rejected": dict(entry.get("rejected") or {})}


def record_effort(store: Store, model_id: str, level: str | None, accepted: bool) -> None:
    entry = store.state.setdefault("effort", {}).setdefault(model_id, {"ok": {}, "rejected": {}})
    key = level or "default"
    entry["ok" if accepted else "rejected"][key] = today()
    entry["rejected" if accepted else "ok"].pop(key, None)
    store.dirty = True


def usable_levels(store: Store, model_id: str) -> list[str]:
    documented = store.model_entry(model_id).get("effort") or []
    learned = learned_effort(store, model_id)
    return [
        level for level in store.effort_levels()
        if (level in documented or level in learned["ok"]) and level not in learned["rejected"]
    ]


def effort_summary(store: Store, model_id: str, level: str | None) -> tuple[str, list[str]]:
    """One phrase for the preflight line, plus warnings."""
    entry = store.model_entry(model_id)
    learned = learned_effort(store, model_id)
    if level is None:
        default = entry.get("default_effort")
        return (f"model's default effort ({default})" if default else "model's default effort"), []
    if level in learned["ok"]:
        return f"effort {level} (accepted {learned['ok'][level]})", []
    if level in learned["rejected"]:
        better = suggest_level(store, model_id, level)
        return f"effort {level} (rejected {learned['rejected'][level]})", [
            f"{model_id} rejected effort {level} on {learned['rejected'][level]}"
            + (f"; nearest level known to work: {better}" if better else "")
        ]
    documented = entry.get("effort")
    if documented is None:
        return f"effort {level} (not yet verified)", []
    if level in documented:
        return f"effort {level} (per docs)", []
    return f"effort {level} (not in docs)", [f"effort {level} isn't listed for {model_id} (docs list {', '.join(documented)}); the API decides"]


def suggest_level(store: Store, model_id: str, level: str) -> str | None:
    order = store.effort_levels()
    usable = usable_levels(store, model_id)
    if level in order:
        lower = [lvl for lvl in usable if order.index(lvl) < order.index(level)]
        if lower:
            return lower[-1]
    return usable[-1] if usable else None


def current_price(store: Store, model_id: str) -> dict | None:
    entry = store.model_entry(model_id)
    fields = ("input", "output", "per_image")
    price = {k: entry[k] for k in fields if k in entry}
    upcoming = None
    for change in sorted(entry.get("changes") or [], key=lambda c: str(c.get("from", ""))):
        if str(change.get("from", "")) <= today():
            price.update({k: change[k] for k in fields if k in change})
        elif upcoming is None:
            upcoming = change
    if not price:
        return None
    price["upcoming"] = upcoming
    return price


def price_text(price: dict | None) -> str:
    if not price:
        return "price unknown"
    if "input" in price and "output" in price:
        text = f"${price['input']:g}/${price['output']:g} per 1M tokens in/out"
    elif "per_image" in price:
        text = f"about ${price['per_image']:g} per image at 1K"
    else:
        return "price unknown"
    change = price.get("upcoming")
    if change:
        parts = [f"${change[k]:g}" for k in ("input", "output", "per_image") if k in change]
        text += f", changing to {'/'.join(parts)} on {change.get('from')}"
    return text


def catalog_warnings(store: Store, model_id: str, price: dict | None) -> list[str]:
    warnings = []
    if price is None:
        warnings.append(f"catalog.json has no price for {model_id}: refresh the catalog")
    age = days_since(store.catalog.get("verified_on"))
    if age is None:
        warnings.append("catalog.json has no verified_on date: refresh the catalog")
    elif age > PRICE_STALE_DAYS:
        warnings.append(f"prices were last checked {age} days ago: refresh the catalog")
    return warnings


def token_cost(usage: dict | None, price: dict | None) -> float | None:
    if not isinstance(usage, dict) or not price or "input" not in price:
        return None
    tokens_in = usage.get("total_input_tokens") or 0
    tokens_out = (usage.get("total_output_tokens") or 0) + (usage.get("total_thought_tokens") or 0)
    return (tokens_in * price["input"] + tokens_out * price["output"]) / 1_000_000


def usage_text(usage: dict | None) -> str:
    if not isinstance(usage, dict):
        return ""
    counts = [
        f"{label}={usage[key]}"
        for label, key in (("in", "total_input_tokens"), ("out", "total_output_tokens"), ("thought", "total_thought_tokens"))
        if usage.get(key)
    ]
    return "tokens " + " ".join(counts) if counts else ""


def cost_guard(tokens: int, price: dict | None, model_id: str, approved: bool) -> str:
    """Stops before an expensive request. Returns a short estimate for the preflight line."""
    if price and "input" in price:
        estimate = tokens * price["input"] / 1_000_000
        if estimate > MAX_INPUT_COST and not approved:
            die(
                f"estimated input cost {money(estimate)} (~{tokens:,} tokens on {model_id}) is over the "
                f"${MAX_INPUT_COST:.2f} guard. Confirm with the user, then rerun with --yes."
            )
        return f"~{tokens:,} input tokens ≈ {money(estimate)}"
    if tokens > MAX_INPUT_TOKENS_UNPRICED and not approved:
        die(f"~{tokens:,} input tokens on {model_id}, whose price is unknown. Confirm with the user, then rerun with --yes.")
    return f"~{tokens:,} input tokens"


# ---------------------------------------------------------------- content helpers

def sniff_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def image_part(path: Path) -> dict:
    if not path.is_file():
        die(f"not a file: {path}")
    data = path.read_bytes()
    mime = sniff_image_mime(data) or mimetypes.guess_type(path.name)[0]
    if not mime or not mime.startswith("image/"):
        die(f"{path} does not look like an image")
    return {"type": "image", "mime_type": mime, "data": base64.b64encode(data).decode("ascii")}


def redact(value):
    """Copy of a response with base64 payloads shortened, for --raw output."""
    if isinstance(value, dict):
        return {
            k: (
                f"<{len(v)} base64 chars>"
                if k in ("data", "signature") and isinstance(v, str) and len(v) > 200
                else redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def output_blocks(resp: dict) -> list[dict]:
    """Text and image blocks the model produced, normalised to {"type": ...}."""
    raw: list = []
    if isinstance(resp.get("steps"), list):
        for step in resp["steps"]:
            if isinstance(step, dict) and step.get("type") == "model_output":
                raw.extend(step.get("content") or [])
    elif isinstance(resp.get("outputs"), list):
        raw.extend(resp["outputs"])
    else:  # classic generateContent shape, in case the API answers with it
        for cand in resp.get("candidates") or []:
            for part in ((cand or {}).get("content") or {}).get("parts") or []:
                if not isinstance(part, dict) or part.get("thought"):
                    continue
                if isinstance(part.get("text"), str):
                    raw.append({"type": "text", "text": part["text"]})
                inline = part.get("inlineData") or part.get("inline_data")
                if isinstance(inline, dict):
                    raw.append(
                        {
                            "type": "image",
                            "data": inline.get("data"),
                            "mime_type": inline.get("mimeType") or inline.get("mime_type"),
                        }
                    )
    blocks = []
    for block in raw:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            blocks.append({"type": "text", "text": block["text"]})
        elif block.get("type") == "image":
            blocks.append(
                {
                    "type": "image",
                    "data": block.get("data"),
                    "uri": block.get("uri"),
                    "mime_type": block.get("mime_type") or block.get("mimeType"),
                }
            )
    return blocks


def read_prompt(arg: str) -> str:
    text = sys.stdin.read() if arg == "-" else arg
    if not text.strip():
        die("empty prompt")
    return text


def output_path(out: str | None, index: int, count: int, ext: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if out:
        target = Path(out).expanduser()
        if out.endswith(("/", os.sep)) or target.is_dir():
            target = target / f"gemini-{stamp}"
        elif target.suffix:
            same = {".jpg", ".jpeg"} if ext == ".jpg" else {ext}
            if target.suffix.lower() in same:
                ext = target.suffix
            else:
                note(f"the model returned {ext[1:].upper()}, so the file is saved as {ext}, not {target.suffix}")
            target = target.with_name(target.stem)
    else:
        target = Path.cwd() / f"gemini-{stamp}"
    stem = f"{target.name}-{index + 1}" if count > 1 else target.name
    candidate = target.with_name(stem + ext)
    n = 2
    while candidate.exists():  # never overwrite an existing file
        candidate = target.with_name(f"{stem}-{n}{ext}")
        n += 1
    return candidate.resolve()


def preflight(store: Store, spec: str, kind: str, level_override: str | None) -> tuple[str, str | None, dict | None, list[str], str]:
    """Resolve and check a request before any money is spent."""
    live = live_models(store)
    model_id, effort, alias = resolve(spec, kind, store, live)
    level = (level_override or "").lower() or effort
    price = current_price(store, model_id)
    warnings = catalog_warnings(store, model_id, price)
    parts = [f"{spec} → {model_id}" if spec.lower() != model_id else model_id]
    if live is None:
        warnings.append("model list unavailable, so the model name is unverified")
    if kind == "text":
        phrase, effort_warnings = effort_summary(store, model_id, level)
        parts.append(phrase)
        warnings += effort_warnings
    parts.append(price_text(price))
    return model_id, level, price, warnings, " · ".join(parts)


# ---------------------------------------------------------------- commands

def cmd_ask(args: argparse.Namespace) -> None:
    store = Store()
    prompt = read_prompt(args.prompt)
    context: list[str] = []
    images: list[dict] = []
    for name in args.file:
        if name == "-":
            if args.prompt == "-":
                die("stdin can be the prompt or a file, not both")
            context.append(f'<file path="stdin">\n{sys.stdin.read()}\n</file>')
            continue
        path = Path(name).expanduser()
        if not path.is_file():
            die(f"not a file: {name}")
        if (mimetypes.guess_type(path.name)[0] or "").startswith("image/"):
            images.append(image_part(path))
            continue
        data = path.read_bytes()
        if len(data) > MAX_TEXT_FILE_BYTES:
            die(f"{name} is over {MAX_TEXT_FILE_BYTES} bytes; send the relevant excerpt instead")
        context.append(f'<file path="{name}">\n{data.decode("utf-8", "replace")}\n</file>')
    text = "\n\n".join(context + [prompt])

    model_id, level, price, warnings, line = preflight(store, args.model, "text", args.thinking)
    estimate = cost_guard(max(1, len(text) // 4) + 1500 * len(images), price, model_id, args.yes)
    note(f"{line} · {estimate}")
    for warning in warnings:
        note(f"warning: {warning}")

    body: dict = {
        "model": model_id,
        "input": [{"type": "text", "text": text}, *images] if images else text,
    }
    if args.system:
        body["system_instruction"] = args.system
    if level:
        body["generation_config"] = {"thinking_level": level}
    if args.cont:
        body["previous_interaction_id"] = args.cont

    try:
        resp = api_request("POST", "/interactions", body, args.timeout)
    except ApiError as err:
        if err.status == 400 and level and re.search(r"thinking", err.message, re.I):
            record_effort(store, model_id, level, accepted=False)
            store.save()
            better = suggest_level(store, model_id, level)
            hint = f" Nearest level known to work: {better} (e.g. -m {model_id}-{better})." if better else ""
            die(f"{model_id} does not accept effort {level}: {err.message}{hint}")
        store.save()
        die(str(err))
    record_effort(store, model_id, level, accepted=True)
    store.save()

    if args.raw:
        print(json.dumps(redact(resp), indent=2), file=sys.stderr)
    texts = [b["text"] for b in output_blocks(resp) if b["type"] == "text"]
    if not texts:
        die("response had no text:\n" + json.dumps(redact(resp))[:3000])
    print("\n".join(texts).strip())
    usage = resp.get("usage")
    cost = token_cost(usage, price)
    tail = [usage_text(usage), f"cost ≈ {money(cost)}" if cost is not None else "cost unknown"]
    if resp.get("id"):
        tail.append(f"interaction_id={resp['id']}")
    note(" · ".join(t for t in tail if t))


def cmd_image(args: argparse.Namespace) -> None:
    store = Store()
    prompt = read_prompt(args.prompt)
    refs = [image_part(Path(p).expanduser()) for p in args.input]
    model_id, _, price, warnings, line = preflight(store, args.model, "image", None)
    if price and "per_image" in price and price["per_image"] > MAX_INPUT_COST and not args.yes:
        die(f"{model_id} costs about ${price['per_image']:g} per image. Confirm with the user, then rerun with --yes.")
    note(line)
    if args.size and args.size.upper() not in ("512", "1K"):
        warnings.append(f"the catalog price is for 1K images; {args.size} costs more")
    for warning in warnings:
        note(f"warning: {warning}")

    body: dict = {
        "model": model_id,
        "input": [{"type": "text", "text": prompt}, *refs] if refs else prompt,
    }
    fmt = {}
    if args.aspect:
        fmt["aspect_ratio"] = args.aspect
    if args.size:
        fmt["image_size"] = args.size
    if fmt:
        body["response_format"] = {"type": "image", **fmt}

    try:
        resp = api_request("POST", "/interactions", body, args.timeout)
    except ApiError as err:
        store.save()
        die(str(err))
    store.save()
    if args.raw:
        print(json.dumps(redact(resp), indent=2), file=sys.stderr)
    blocks = output_blocks(resp)
    images = [b for b in blocks if b["type"] == "image"]
    notes = [b["text"].strip() for b in blocks if b["type"] == "text" and b["text"].strip()]
    if not images:
        reason = " ".join(notes) if notes else json.dumps(redact(resp))[:3000]
        die(f"no image in the response: {reason}")
    for index, image in enumerate(images):
        if not image.get("data"):
            print(image.get("uri") or "(image block without data)")
            continue
        data = base64.b64decode(image["data"])
        mime = sniff_image_mime(data) or image.get("mime_type") or "image/png"
        path = output_path(args.out, index, len(images), IMAGE_EXT.get(mime, ".png"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(path)
    for text in notes:
        note(f"model said: {text}")
    tail = [usage_text(resp.get("usage"))]
    if price and "per_image" in price:
        tail.append(f"cost ≈ {money(price['per_image'] * len(images))}")
    if resp.get("id"):
        tail.append(f"interaction_id={resp['id']}")
    note(" · ".join(t for t in tail if t))


def cmd_check(args: argparse.Namespace) -> None:
    store = Store()
    kind = "image" if args.image else "text"
    spec = args.spec or (DEFAULT_IMAGE_MODEL if args.image else DEFAULT_TEXT_MODEL)
    live = live_models(store, refresh=args.refresh)
    model_id, effort, alias = resolve(spec, kind, store, live)
    level = effort
    info = (live or {}).get(model_id) or {}
    entry = store.model_entry(model_id)
    price = current_price(store, model_id)
    warnings = catalog_warnings(store, model_id, price)
    rows = [("request", f"{spec} → {model_id}" + (f" (newest stable {alias})" if alias else ""))]
    if live is None:
        rows.append(("available", "unverified: the model list could not be fetched"))
    else:
        limits = ""
        if info.get("input_limit"):
            limits = f" · limits {info['input_limit']:,} in / {info.get('output_limit') or 0:,} out tokens"
        rows.append(("available", f"yes, in Google's live list for your key{limits}"))
    if kind == "text":
        phrase, effort_warnings = effort_summary(store, model_id, level)
        warnings += effort_warnings
        rows.append(("effort", phrase))
        documented = entry.get("effort")
        learned = learned_effort(store, model_id)
        rows.append(("levels", (
            f"docs: {', '.join(documented) if documented else 'unknown'}"
            + (f" (default {entry['default_effort']})" if entry.get("default_effort") else "")
            + f" · accepted: {', '.join(learned['ok']) or 'none yet'}"
            + f" · rejected: {', '.join(learned['rejected']) or 'none'}"
        )))
    rows.append(("price", price_text(price) + (f" (catalog checked {store.catalog.get('verified_on')})" if price else "")))
    if entry.get("use_for"):
        rows.append(("use for", entry["use_for"]))
    for label, value in rows:
        print(f"{label:<10} {value}")
    status = 0
    if args.probe and kind == "text":
        body: dict = {"model": model_id, "input": "Reply with just: ok"}
        if level:
            body["generation_config"] = {"thinking_level": level}
        try:
            resp = api_request("POST", "/interactions", body, 120)
            record_effort(store, model_id, level, accepted=True)
            cost = token_cost(resp.get("usage"), price)
            print(f"{'probe':<10} accepted" + (f" (cost ≈ {money(cost)})" if cost is not None else ""))
        except ApiError as err:
            if err.status == 400 and level and re.search(r"thinking", err.message, re.I):
                record_effort(store, model_id, level, accepted=False)
                better = suggest_level(store, model_id, level)
                print(f"{'probe':<10} REJECTED: {err.message}" + (f" Nearest working level: {better}." if better else ""))
            else:
                print(f"{'probe':<10} failed: {err}")
            status = 1
    for warning in warnings:
        print(f"{'warning':<10} {warning}")
    store.save()
    sys.exit(status)


def cmd_models(args: argparse.Namespace) -> None:
    store = Store()
    live = live_models(store, refresh=args.refresh)
    if args.all:
        if live is None:
            die("the live model list is unavailable")
        for model_id in sorted(live):
            info = live[model_id]
            print(f"{model_id:<48} thinking={'yes' if info.get('thinking') else 'no ':<3} "
                  f"in={info.get('input_limit') or '?'} out={info.get('output_limit') or '?'}")
        store.save()
        return
    unpriced = []
    for kind, title, default in (("text", "Text models", DEFAULT_TEXT_MODEL), ("image", "Image models", DEFAULT_IMAGE_MODEL)):
        print(f"{title} (default {default})")
        for alias in FAMILIES[kind]:
            model_id = newest(kind, alias, live)
            if not model_id:
                print(f"  {alias:<9} (no stable model in the live list)")
                continue
            if current_price(store, model_id) is None:
                unpriced.append(model_id)
            entry = store.model_entry(model_id)
            details = [price_text(current_price(store, model_id))]
            if kind == "text":
                levels = entry.get("effort")
                learned = learned_effort(store, model_id)
                level_text = ", ".join(
                    lvl + ("*" if lvl == entry.get("default_effort") else "") for lvl in (levels or [])
                ) or "effort levels unknown"
                if learned["rejected"]:
                    level_text += f" (rejected: {', '.join(learned['rejected'])})"
                details.insert(0, level_text)
            print(f"  {alias:<9} {model_id:<30} {' · '.join(details)}")
            if entry.get("use_for"):
                print(f"  {'':<9} {entry['use_for']}")
        print()
    age = days_since(store.catalog.get("verified_on"))
    fetched = (store.state.get("model_list") or {}).get("fetched_at")
    print("* = model's default effort. Model names are live from Google"
          + (f" (fetched {time.strftime('%Y-%m-%d %H:%M', time.localtime(fetched))})" if fetched and live else " (unavailable)")
          + f"; prices and effort docs from {short_path(store.catalog_path)}, checked {store.catalog.get('verified_on', 'never')}"
          + (f" ({age} days ago)" if age is not None else "") + ".")
    if age is None or age > PRICE_STALE_DAYS:
        print("warning: the catalog is stale; refresh it.")
    if unpriced:
        print(f"warning: catalog.json has no price for {', '.join(unpriced)}; refresh the catalog.")
    store.save()


def cmd_doctor(args: argparse.Namespace) -> None:
    store = Store()
    key, source = find_api_key()
    platform = "linux" if sys.platform.startswith("linux") else sys.platform
    if key:
        key_text = f"found in {source}"
    else:
        key_text = "MISSING: set GEMINI_API_KEY" + (f", or run: {KEY_SETUP[platform]}" if platform in KEY_SETUP else "")
    age = days_since(store.catalog.get("verified_on"))
    rows = [
        ("version", f"gemini.py {__version__} · Python {sys.version.split()[0]} · {sys.platform}"),
        ("script", short_path(Path(__file__).resolve())),
        ("api key", key_text),
        ("config", short_path(CONFIG_PATH) + ("" if CONFIG_PATH.exists() else " (not created; built-in defaults)")),
        ("defaults", f"text {DEFAULT_TEXT_MODEL} · image {DEFAULT_IMAGE_MODEL} · cost guard ${MAX_INPUT_COST:.2f}"),
        ("catalog", f"{short_path(store.catalog_path)} · checked {store.catalog.get('verified_on', 'never')}"
                    + (f" ({age} days ago)" if age is not None else "")),
        ("state", short_path(STATE_PATH)),
    ]
    for label, value in rows:
        print(f"{label:<10} {value}")
    if not key:
        sys.exit(1)
    live = live_models(store, refresh=True)
    print(f"{'api':<10} " + (f"reachable · {len(live)} models available to your key" if live else "model list unavailable (see above)"))
    store.save()
    sys.exit(0 if live else 1)


def cmd_catalog(args: argparse.Namespace) -> None:
    """Print the path of the user's editable catalog, creating or updating it from the bundled one."""
    bundled = load_json(BUNDLED_CATALOG, {})
    user = load_json(USER_CATALOG, {})
    if not bundled and not user:
        die(f"no catalog found at {BUNDLED_CATALOG}")
    if not user or str(bundled.get("verified_on", "")) > str(user.get("verified_on", "")):
        USER_CATALOG.parent.mkdir(parents=True, exist_ok=True)
        if user:
            USER_CATALOG.with_suffix(".json.bak").write_text(json.dumps(user, indent=2) + "\n")
            note(f"the bundled catalog was newer; replaced your copy (old one kept as {short_path(USER_CATALOG.with_suffix('.json.bak'))})")
        else:
            note("created your editable copy from the bundled catalog")
        USER_CATALOG.write_text(json.dumps(bundled, indent=2) + "\n")
    print(USER_CATALOG)


def main() -> None:
    # Agents read this through pipes, which default to the locale encoding on Windows; keep output UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    parser = argparse.ArgumentParser(
        prog="gemini.py",
        description="Ask Gemini, or generate and edit images, through the Gemini API.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    text_aliases = ", ".join(FAMILIES["text"])
    image_aliases = ", ".join(FAMILIES["image"])

    ask = sub.add_parser("ask", help="ask a Gemini text model")
    ask.add_argument("prompt", help='the question, or "-" to read it from stdin')
    ask.add_argument("-f", "--file", action="append", default=[],
                     help='file to include as context; repeatable; "-" reads stdin; images are attached as images')
    ask.add_argument("-m", "--model", default=DEFAULT_TEXT_MODEL,
                     help=f"alias ({text_aliases}; newest live version is used) or a full model ID, "
                          f"optionally with an effort suffix such as neon-high; default {DEFAULT_TEXT_MODEL}")
    ask.add_argument("-s", "--system", help="system instruction")
    ask.add_argument("-t", "--thinking", help="effort level, e.g. low, medium, high, xhigh (overrides a -m suffix)")
    ask.add_argument("-c", "--continue", dest="cont", metavar="INTERACTION_ID",
                     help="continue an earlier conversation (id is printed on stderr)")
    ask.set_defaults(handler=cmd_ask)

    image = sub.add_parser("image", help="generate or edit an image")
    image.add_argument("prompt", help='what to draw or change, or "-" to read it from stdin')
    image.add_argument("-o", "--out", help="output file or directory (default: ./gemini-<time>.<ext>); never overwrites")
    image.add_argument("-i", "--input", action="append", default=[],
                       help="reference or source image to edit; repeatable (up to 14)")
    image.add_argument("-m", "--model", default=DEFAULT_IMAGE_MODEL,
                       help=f"alias ({image_aliases}) or a full model ID; default {DEFAULT_IMAGE_MODEL}")
    image.add_argument("--aspect", help="1:1, 2:3, 3:2, 3:4, 4:3, 4:5, 5:4, 9:16, 16:9, 21:9 (more on some models)")
    image.add_argument("--size", help="1K, 2K or 4K (512 on some models)")
    image.set_defaults(handler=cmd_image)

    for p in (ask, image):
        p.add_argument("--yes", action="store_true", help="the user approved a request over the cost guard")
        p.add_argument("--timeout", type=float, default=600, help="seconds to wait (default 600)")
        p.add_argument("--raw", action="store_true", help="also print the full JSON response to stderr")

    check = sub.add_parser("check", help="preflight a model and effort without sending a prompt")
    check.add_argument("spec", nargs="?", help=f"e.g. neon-high, carbon, gemini-4-neon (default {DEFAULT_TEXT_MODEL})")
    check.add_argument("--image", action="store_true", help=f"check an image model (default {DEFAULT_IMAGE_MODEL})")
    check.add_argument("--probe", action="store_true", help="send a tiny prompt to confirm the effort level live (costs a fraction of a cent)")
    check.add_argument("--refresh", action="store_true", help="refetch the live model list first")
    check.set_defaults(handler=cmd_check)

    models = sub.add_parser("models", help="list model families, effort levels and prices")
    models.add_argument("--all", action="store_true", help="every model ID in Google's live list")
    models.add_argument("--refresh", action="store_true", help="refetch the live model list first")
    models.set_defaults(handler=cmd_models)

    doctor = sub.add_parser("doctor", help="check the setup: key, config, catalog and API access")
    doctor.set_defaults(handler=cmd_doctor)

    catalog = sub.add_parser("catalog", help="print the path of your editable catalog.json, creating it if needed")
    catalog.set_defaults(handler=cmd_catalog)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
