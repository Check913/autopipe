"""Utilitaires partages : logging, JSON, client LLM, helpers ffmpeg/ffprobe."""
import functools
import json
import logging
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_configured = False


def setup_logging(level="INFO", logfile=None):
    """Configure le logging global (console + fichier optionnel)."""
    global _configured
    if _configured:
        return
    # Console Windows en cp1252: les emojis des titres YouTube (ex: \U0001F525)
    # provoquaient un "--- Logging error ---" UnicodeEncodeError a chaque run
    # (observe le 2026-10-26 00:41). 'replace' degrade proprement en '?' sans
    # jamais interrompre le logging.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    handlers = [logging.StreamHandler(sys.stdout)]
    if logfile:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format=LOG_FORMAT,
        handlers=handlers,
    )
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _configured = True


def get_logger(name="autopipe"):
    return logging.getLogger(name)


def load_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        get_logger().warning("JSON invalide %s: %s", p, exc)
        return default


def save_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def slugify(text, maxlen=60):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:maxlen] or "video"


def as_script_text(value):
    """Convertit un champ script LLM en texte brut multi-lignes (contrat TTS).

    Cause racine observee le 2026-10-26 00:37: gemini-2.5-flash renvoie le
    script sous forme de tableau JSON de lignes au lieu d'une string. edge-tts
    exige un str -> "text must be str" apres 5 tentatives TTS inutiles.
    Accepte: str, liste de lignes (ou de segments {text/line/sentence}),
    dict de lignes (cles numeriques triees, sinon ordre d'insertion).
    """
    if value is None:
        return ""
    if isinstance(value, dict):
        keys = list(value.keys())
        if keys and all(str(k).lstrip("-").isdigit() for k in keys):
            items = [value[k] for k in sorted(keys, key=lambda k: int(k))]
        else:
            items = list(value.values())
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return str(value).strip()
    lines = []
    for item in items:
        if isinstance(item, dict):
            item = (item.get("text") or item.get("line") or item.get("sentence")
                    or item.get("script") or json.dumps(item, ensure_ascii=False))
        lines.append(str(item).strip())
    return "\n".join(x for x in lines if x)


def retry(times=3, delay=2.0, exceptions=(Exception,)):
    """Decorateur de retry avec backoff lineaire."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last = None
            for attempt in range(1, times + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:
                    last = exc
                    get_logger().warning("Echec %s (tentative %s/%s): %s", fn.__name__, attempt, times, exc)
                    if attempt < times:
                        time.sleep(delay * attempt)
            raise last
        return wrapper
    return deco


def run_ffmpeg(args, cwd=None):
    """Lance ffmpeg et leve une erreur claire en cas d'echec."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + [str(a) for a in args]
    log = get_logger("ffmpeg")
    log.debug("ffmpeg %s", " ".join(cmd[3:10]))
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg a echoue (%s): %s" % (proc.returncode, proc.stderr[-2000:]))
    return proc


def ffprobe_json(path):
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError("ffprobe a echoue: %s" % proc.stderr[-1000:])
    return json.loads(proc.stdout)


def media_duration(path):
    """Duree en secondes d'un fichier audio/video."""
    info = ffprobe_json(path)
    return float(info["format"]["duration"])


class LLMClient:
    """Client minimal pour une API compatible OpenAI (/chat/completions)."""

    def __init__(self, base_url="", api_key="", model="", timeout=90):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or "gpt-4o-mini"
        self.timeout = timeout

    @property
    def enabled(self):
        return bool(self.base_url)

    def chat(self, system, user, temperature=0.8, max_tokens=4000, json_mode=False):
        import requests
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = requests.post(self.base_url + "/chat/completions", json=payload,
                             headers=headers, timeout=self.timeout)
        if resp.status_code >= 400:
            # Log le corps de l'erreur (ex: "model ... no longer available",
            # cle invalide) au lieu d'un simple "404 Client Error" illisible.
            snippet = (resp.text or "").strip().replace("\n", " ")[:300]
            raise RuntimeError("LLM HTTP %s sur %s: %s" %
                               (resp.status_code, self.base_url + "/chat/completions", snippet))
        return resp.json()["choices"][0]["message"]["content"]

    def chat_json(self, system, user, **kw):
        raw = self.chat(system, user, json_mode=True, **kw).strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        return json.loads(raw)
# --- Correctif proxy: variables d'environnement mal formees ---

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
               "http_proxy", "https_proxy", "all_proxy")


def fix_proxy_env(mode="direct"):
    """Neutralise ou repare les variables proxy avant tout appel reseau.

    Cause racine observee (log 2026-10-23 20:42-21:05): la session du bot
    avait HTTPS_PROXY=https://127.0.0.1:59832 avec un mauvais schema.
    urllib3 tentait alors une poignee de main TLS vers un proxy HTTP pur,
    d'ou SSLError('WRONG_VERSION_NUMBER') puis ProxyError('Your proxy appears
    to only use HTTP and not HTTPS') sur Gemini, Pixabay, OAuth
    (oauth2.googleapis.com) et l'upload YouTube (httplib2).

    Modes:
      direct -> supprime toutes les variables proxy et force NO_PROXY='*'
                (connexion directe, fonctionne selon les diagnostics curl).
      fix    -> reecrit https:// en http:// pour les proxys locaux mal
                declares (le port parle HTTP, pas HTTPS).
      keep   -> ne modifie rien.
    """
    log = get_logger("network")
    if mode == "keep":
        log.info("Mode proxy 'keep': variables d'environnement conservees")
        return mode
    if mode == "direct":
        neutralisees = []
        for var in _PROXY_VARS:
            val = os.environ.pop(var, None)
            if val:
                neutralisees.append("%s=%s" % (var, val))
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        if neutralisees:
            log.info("Mode proxy 'direct': variables neutralisees: %s",
                     ", ".join(neutralisees))
        else:
            log.info("Mode proxy 'direct': aucune variable proxy declaree")
        return mode
    if mode == "fix":
        corrigees = []
        for var in _PROXY_VARS:
            val = os.environ.get(var)
            if not val:
                continue
            if val.lower().startswith("https://"):
                new = "http://" + val[len("https://"):]
                os.environ[var] = new
                corrigees.append("%s -> %s" % (var, new))
        if corrigees:
            log.info("Mode proxy 'fix': schemas https:// corriges en http://: %s",
                     "; ".join(corrigees))
        else:
            log.info("Mode proxy 'fix': rien a corriger")
        return mode
    log.warning("Mode proxy inconnu '%s' (attendu: direct/fix/keep) -> 'direct'", mode)
    return fix_proxy_env("direct")