"""Verrou geographique US (fail-closed) pour toute connexion YouTube.

Probleme : les runners GitHub heberges (ubuntu-latest) utilisent depuis
2026 un pool multi-cloud (Azure + AWS) : l'IP de sortie n'est plus
garantie americaine au moment de la publication. YouTube deduit le pays
d'upload de l'IP de sortie (recordingDetails deprecated depuis 2017) :
sans verrou, la video peut apparaitre publiee depuis un autre pays
(decouverte, recommandations, monetisation).

Solution : [network].upload_proxy = proxy de sortie US qui route TOUT le
trafic YouTube (get_service / apply_upload_proxy). Ce module VERIFIE le
pays effectif de cette sortie au moment exact de la publication :

  strict (defaut) : le pays de sortie doit etre PROUVE = US sinon la
                    publication est REFUSEE (mieux vaut ne rien publier
                    que publier mal localise ; l'email d'alerte part) ;
  warn            : avertissement seulement (developpement local) ;
  off             : aucun controle (deconseille).

La verification passe par les MEMES variables d'environnement que
l'upload (apply_upload_proxy / fix_proxy_env) : la sortie testee EST la
sortie qu'utilisera YouTube. Resultat mis en cache dans
cache/geo_exit.json (TTL geo_check_ttl_min) clee par sortie (proxy ou
direct). Les services geo (ip-api.com, ipinfo.io) sont interroges dans
cet ordre ; si AUCUN ne repond, on ne peut pas PROUVER la sortie ->
fail-closed.
"""

import hashlib
import json
import time
import urllib.request

from .config import get_config, get_paths
from .util import apply_upload_proxy, fix_proxy_env, get_logger

log = get_logger("geo")

DEFAULT_CHECK_URL = ("http://ip-api.com/json/?fields=status,message,"
                     "country,countryCode,query,proxy,hosting,isp")
FALLBACK_CHECK_URLS = ("https://ipinfo.io/json",)

# ipinfo.io repond "country": "US" (code court) ; ip-api.com repond
# "countryCode". Quelques noms complets au cas ou un service les renvoie.
_COUNTRY_NAMES = {"UNITED STATES": "US", "USA": "US", "US": "US"}


class GeoError(RuntimeError):
    """Sortie reseau non prouvee = US -> publication refusee (fail-closed)."""


def _egress(cfg):
    """Applique LA MEME sortie que l'upload, retourne sa description."""
    net = cfg.get("network", {}) or {}
    proxy_url = str(net.get("upload_proxy") or "").strip()
    if proxy_url:
        applied = apply_upload_proxy(proxy_url)
        return "proxy %s" % applied
    fix_proxy_env(str(net.get("proxy_mode", "direct") or "direct"))
    return "direct"


def _country_of(payload):
    """Extrait (code_pays, ip, detail) d'une reponse de service geo.

    Retourne None si le pays ne peut pas etre determine.
    """
    if not isinstance(payload, dict):
        return None
    code = payload.get("countryCode") or payload.get("country_code") \
        or payload.get("country")
    if not code:
        return None
    code = str(code).strip().upper()
    if len(code) != 2:
        code = _COUNTRY_NAMES.get(code)
        if not code:
            return None
    ip = str(payload.get("query") or payload.get("ip") or "")
    detail = str(payload.get("isp") or payload.get("org") or "")
    return code, ip, detail


def _fetch(url, timeout):
    """GET JSON via les variables d'env positionnees par _egress."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "autopipe-geo-guard/1.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def check_exit_country(cfg=None, force=False):
    """Verifie le pays de sortie effectif. NE leve PAS d'exception.

    Retourne {ok, country, ip, egress, reason, cached, mode, ...}.
    ok=None si le controle est desactive (geo_check=off).
    """
    cfg = cfg or get_config()
    net = cfg.get("network", {}) or {}
    mode = str(net.get("geo_check", "strict") or "strict").lower()
    expected = str(net.get("geo_expected_country", "US") or "US").upper()
    timeout = float(net.get("geo_check_timeout_s", 15) or 15)
    ttl_min = float(net.get("geo_check_ttl_min", 30) or 30)
    egress_desc = _egress(cfg)

    if mode == "off":
        return {"ok": None, "skipped": True, "mode": mode,
                "egress": egress_desc, "reason": "geo_check=off"}

    fingerprint = hashlib.sha1(egress_desc.encode("utf-8")).hexdigest()[:16]
    cache_file = get_paths(cfg).cache / "geo_exit.json"
    if not force:
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
            age = time.time() - float(cache.get("ts", 0))
            if cache.get("fingerprint") == fingerprint and age < ttl_min * 60:
                hit = dict(cache.get("result") or {})
                if hit.get("country") is not None:
                    hit.update({"cached": True, "egress": egress_desc,
                                "mode": mode, "expected": expected})
                    return hit
        except Exception:
            pass

    urls = [str(net.get("geo_check_url") or "").strip() or DEFAULT_CHECK_URL]
    for fallback in FALLBACK_CHECK_URLS:
        if fallback not in urls:
            urls.append(fallback)

    result = None
    last_err = None
    for url in urls:
        try:
            parsed = _country_of(_fetch(url, timeout))
            if parsed:
                country, ip, detail = parsed
                ok = (country == expected)
                result = {
                    "ok": ok, "country": country, "ip": ip, "detail": detail,
                    "egress": egress_desc, "url": url,
                    "checked_at": time.time(),
                    "reason": ("sortie US confirmee (%s)" % url) if ok
                    else ("pays de sortie %s != %s" % (country, expected)),
                }
                break
            last_err = "%s : pays absent de la reponse" % url
        except Exception as exc:
            last_err = "%s : %s" % (url, exc)
            continue

    if result is None:
        # Fail-closed : impossible de PROUVER la sortie -> on ne cache pas
        # cet echec (la prochaine tentative reinterroge en direct).
        result = {"ok": False, "country": None, "ip": None, "detail": "",
                  "egress": egress_desc, "url": None,
                  "checked_at": time.time(),
                  "reason": "verification geo impossible : %s" % last_err}
    else:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(
                {"ts": time.time(), "fingerprint": fingerprint,
                 "result": result}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    result.update({"cached": False, "mode": mode, "expected": expected})
    return result


def enforce_exit_country(cfg=None):
    """Verrou fail-closed appele juste AVANT chaque publication YouTube.

    strict -> GeoError si le pays de sortie n'est pas prouve = US ;
    warn   -> warning seulement ; off -> aucun controle.
    Retourne le dict d'etat (pour les logs/tests).
    """
    cfg = cfg or get_config()
    net = cfg.get("network", {}) or {}
    mode = str(net.get("geo_check", "strict") or "strict").lower()
    if mode == "off":
        return {"ok": None, "skipped": True, "mode": mode}
    state = check_exit_country(cfg)
    if state.get("ok"):
        log.info("Verrou geo US : OK (%s, sortie %s, pays=%s, ip=%s)",
                 "cache" if state.get("cached") else "controle live",
                 state.get("egress"), state.get("country"), state.get("ip"))
        return state
    if mode == "warn":
        log.warning("Verrou geo US : sortie NON confirmee US (%s) - "
                    "mode warn, publication continue", state.get("reason"))
        return state
    raise GeoError(
        "Sortie reseau non confirmee US -> publication refusee (fail-closed). "
        "Raison : %s. Remedes : definir [network].upload_proxy avec un proxy "
        'de sortie US, ou passer [network].geo_check = "warn" (ou "off") '
        "pour ignorer le verrou." % state.get("reason"))
