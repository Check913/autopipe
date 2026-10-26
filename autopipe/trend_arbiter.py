"""Module 2 - Arbitre de tendance predictif (Google Trends x YouTube).

Croise les signaux faibles :
  - Google Trends (RSS trending searches, geo parametrable, reutilise le
    collecteur existant de autopipe.radar),
  - la saturation YouTube (vues cumulees des premiers resultats de
    recherche pour chaque sujet, via yt-dlp en mode flat).

Score de momentum = interet_montant / log(vues_existantes + 1) :
un sujet qui explose sur Trends mais encore peu couvert sur YouTube
obtient le meilleur score -> il faut generer AVANT la saturation.

Une baseline horodatee est conservee dans data/trend/baseline.json pour
mesurer la montee (delta approx_traffic vs. snapshot precedent).
Robustesse : echec reseau / API -> avertissement + dernier snapshot.
"""
import datetime as dt
import math
import re

from .config import get_config, get_paths
from .util import get_logger, load_json, save_json

log = get_logger("trend")

_TRAFFIC_RE = re.compile(r"([\d.]+)\s*([KkMm]?)")


def _parse_traffic(value):
    """'200K+' -> 200000.0 (0.0 si inconnu)."""
    m = _TRAFFIC_RE.search(str(value or ""))
    if not m:
        return 0.0
    num = float(m.group(1))
    mult = {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1.0)
    return num * mult


def _baseline_path(cfg=None):
    return get_paths(cfg).trend / "baseline.json"


def load_baseline(cfg=None):
    return load_json(_baseline_path(cfg), default={})


def save_baseline(cfg=None, baseline=None):
    save_json(_baseline_path(cfg), baseline or {})


def fetch_trends(cfg=None):
    """Sujets Google Trends -> [{query, traffic}] (reutilise radar)."""
    cfg = cfg or get_config()
    geo = str((cfg.get("trend") or {}).get("geo") or "")
    if geo:  # override optionnel du geo radar/general
        cfg = dict(cfg)
        cfg["radar"] = dict(cfg.get("radar") or {}, google_trends_geo=geo)
    try:
        from .radar import _collect_google_trends
        items = _collect_google_trends(cfg)
    except Exception as exc:
        log.warning("Google Trends indisponible: %s", exc)
        return []
    out = []
    for it in items:
        q = (it.get("title") or "").strip()
        if q:
            out.append({"query": q, "traffic": _parse_traffic(it.get("views"))})
    return out


def update_baseline(trends, cfg=None):
    """Met a jour la baseline et renvoie {query: {traffic, delta, rise}}.

    rise = (traffic - ancien) / max(ancien, 1) borne a [0, 10] : mesure de
    montee entre deux snapshots (les snapshots consecutifs sont espaces par
    le cache, cf. cache_hours).
    """
    baseline = load_baseline(cfg)
    now = dt.datetime.now().isoformat(timespec="seconds")
    for t in trends:
        q = t["query"]
        prev = baseline.get(q) or {}
        old = float(prev.get("traffic") or 0.0)
        cur = float(t.get("traffic") or 0.0)
        delta = cur - old
        rise = max(0.0, min(10.0, delta / max(old, 1.0)))
        history = list(prev.get("history") or [])
        history.append([now, cur])
        baseline[q] = {
            "traffic": cur,
            "first_seen": prev.get("first_seen") or now,
            "last_seen": now,
            "delta": delta,
            "rise": round(rise, 3),
            "history": history[-12:],  # 12 derniers snapshots
        }
    # purge des sujets non revus depuis 30 jours
    cutoff = (dt.datetime.now() - dt.timedelta(days=30)).isoformat(
        timespec="seconds")
    for q in list(baseline):
        if str(baseline[q].get("last_seen") or "") < cutoff:
            del baseline[q]
    save_baseline(cfg, baseline)
    return baseline


# ---------------------------------------------------------------------------
# Saturation YouTube + score de momentum
# ---------------------------------------------------------------------------

def _ydl_search_opts(cfg):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 20,
        "retries": 1,
    }
    if cfg.get("network", {}).get("proxy_mode", "direct") == "direct":
        opts["proxy"] = ""
    return opts


def youtube_saturation(query, cfg=None, top_n=5):
    """Vues cumulees des `top_n` premiers resultats YouTube pour `query`.

    Proxy de saturation : si les N premiers resultats totalisent deja des
    millions de vues, le sujet est couvert ; peu de vues = vague naissante.
    Renvoie 0.0 si la recherche echoue (degrade : score = rise seul).
    """
    cfg = cfg or get_config()
    try:
        import yt_dlp
        opts = _ydl_search_opts(cfg)
        opts["quiet"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info("ytsearch%d:%s" % (top_n, query),
                                    download=False)
        total = 0.0
        for e in (info or {}).get("entries") or []:
            total += float(e.get("view_count") or 0.0)
        return total
    except Exception as exc:
        log.warning("Saturation YouTube echec pour %r: %s", query, exc)
        return 0.0


def momentum_score(rise, existing_views):
    """score = interet_montant / log(vues_existantes + 1).

    rise : croissance relative du sujet sur Trends (0..10).
    existing_views : vues cumulees du top YouTube (saturation).
    Un sujet tout juste ne doit pas ecraser un sujet en explosion : le
    logarithme amortit la saturation au lieu de la diviser par zero.
    """
    denom = math.log10(max(float(existing_views or 0.0), 10.0) / 10.0 + 1.0)
    return round(float(rise or 0.0) / max(denom, 0.05), 4)


def run_trend_arbiter(cfg=None, max_topics=None, fetch_saturation=True):
    """Boucle complete : Trends -> baseline -> saturation -> classement.

    Renvoie {fetched, topics: [{query, traffic, rise, saturation_views,
    momentum}], top: [...]}. Ne leve jamais : degrade en liste vide.
    """
    cfg = cfg or get_config()
    paths = get_paths(cfg)
    tc = cfg.get("trend", {})
    max_topics = int(max_topics or tc.get("max_topics", 15))
    topics = []
    try:
        trends = fetch_trends(cfg)
    except Exception as exc:
        log.warning("fetch_trends a echoue: %s", exc)
        trends = []
    if not trends:
        return {"fetched": dt.datetime.now().isoformat(timespec="seconds"),
                "topics": [], "top": []}
    baseline = update_baseline(trends, cfg)
    seen = set()
    for t in trends:
        q = t["query"]
        if q in seen:
            continue
        seen.add(q)
        b = baseline.get(q) or {}
        rise = float(b.get("rise") or 0.0)
        sat = (youtube_saturation(q, cfg) if fetch_saturation else 0.0)
        topics.append({
            "query": q,
            "traffic": t["traffic"],
            "rise": round(rise, 3),
            "saturation_views": sat,
            "momentum": momentum_score(rise, sat),
        })
        if len(topics) >= max_topics:
            break
    topics.sort(key=lambda t: -t["momentum"])
    out = {"fetched": dt.datetime.now().isoformat(timespec="seconds"),
           "geo": tc.get("geo", ""),
           "topics": topics,
           "top": topics[: int(tc.get("top_n", 5))]}
    save_json(paths.trend / "arbiter_latest.json", out)
    return out


def suggested_keywords(cfg=None):
    """Mots-cles a fort momentum pour discover_hits / scout (liste de str).

    Lit le dernier snapshot arbiter_latest.json (cache cache_hours) et
    renvoie les sujets au-dessus du seuil min_momentum. Jamais d'exception.
    """
    try:
        cfg = cfg or get_config()
        tc = cfg.get("trend", {})
        if not tc.get("enabled", True):
            return []
        paths = get_paths(cfg)
        data = load_json(paths.trend / "arbiter_latest.json") or {}
        fetched = data.get("fetched")
        if fetched:
            age_h = (dt.datetime.now() - dt.datetime.fromisoformat(
                fetched)).total_seconds() / 3600.0
            if age_h > float(tc.get("cache_hours", 6)):
                return []
        min_m = float(tc.get("min_momentum", 0.3))
        return [t["query"] for t in data.get("topics", [])
                if float(t.get("momentum") or 0.0) >= min_m]
    except Exception as exc:
        log.warning("suggested_keywords degrade: %s", exc)
        return []
