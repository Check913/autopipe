# -*- coding: utf-8 -*-
"""MODULE 5 - Monetization : suivi YPP, ETA et revenus estimes.

Demande utilisateur (2026-10-29) : le bot doit suivre ou il en est du
Programme Partenaire YouTube (YPP) sans intervention humaine.

Sources (aucun cout quota important, aucune cle supplementaire) :
  - channels.list(mine=true)   -> abonnes, vues totales (upload.get_service,
    scopes youtube.upload/readonly/force-ssl deja obtenus).
  - registre + videos.list     -> vues cumulees des Shorts des 90 derniers
    jours (regle YPP "10M vues Shorts / 90 jours").
  - youtubeAnalytics v2        -> heures visionnage 12 derniers mois
    (regle YPP "4000 heures / 12 mois"). Necessite le token
    yt-analytics.readonly (autopipe analytics-auth) : sinon None, non fatal.

Sorties :
  - data/monetization/progress.json   (dernier etat, TTL cache check_hours)
  - data/monetization/history.jsonl   (snapshots append-only -> velocite)
Consommateurs :
  - scheduler step 1.97 -> run_check() (chaque run, TTL)
  - cli `monetize`      -> report() lisible
Jamais fatal : une API indisponible degrade le rapport, pas le pipeline.
"""
import datetime as dt
import json
import math
import traceback

from .config import get_config, get_paths
from .util import get_logger, load_json, save_json

log = get_logger("monetization")

# Regles YPP officielles (monetisation complete, routes 2026) :
#   1000 abonnes ET (4000 h de visionnage / 12 mois OU 10M vues Shorts / 90 j).
WATCH_HOURS_ROUTE = "watch_hours"
SHORTS_VIEWS_ROUTE = "shorts_views"


def _progress_file(cfg):
    return get_paths(cfg).monetization / "progress.json"


def _history_file(cfg):
    return get_paths(cfg).monetization / "history.jsonl"


def _parse_iso(value):
    """RFC3339/ISO -> datetime naive UTC (mélange 'Z' et ISO locaux)."""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def _fresh(cfg, path):
    """True si progress.json est plus frais que monetization.check_hours."""
    mc = cfg.get("monetization", {}) or {}
    try:
        ttl_h = float(mc.get("check_hours", 12) or 12)
    except (TypeError, ValueError):
        ttl_h = 12.0
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        gen = dt.datetime.fromisoformat(str(raw.get("generated_at") or ""))
        return dt.datetime.now() - gen < dt.timedelta(hours=ttl_h)
    except Exception:
        return False


def _channel_stats(cfg):
    """{subs, total_views, channel_created_at, ...} via channels.list."""
    from .upload import get_service
    yt = get_service(cfg)
    resp = yt.channels().list(
        part="snippet,statistics,contentDetails", mine="true").execute()
    items = resp.get("items") or []
    if not items:
        raise RuntimeError("channels.list(mine=true) -> aucune chaine")
    item = items[0]
    st = item.get("statistics", {}) or {}
    return {
        "channel_id": item.get("id"),
        "title": item.get("snippet", {}).get("title"),
        "subs": int(st.get("subscriberCount", 0) or 0),
        "hidden_subs": bool(st.get("hiddenSubscriberCount")),
        "total_views": int(st.get("viewCount", 0) or 0),
        "video_count": int(st.get("videoCount", 0) or 0),
        "channel_created_at": item.get("snippet", {}).get("publishedAt"),
    }


def _views_90d(cfg):
    """(vues, n_videos) des Shorts publies dans les 90 derniers jours.

    Registre = liste des ids, videos.list = vues reelles actuelles (l'API
    renvoie les vues TOTALES, mais un Short a moins de 90 jours d'age ne peut
    pas avoir de vues en dehors de la fenetre -> cumul direct valable).
    """
    from .analytics import stats_for
    from .registry import load as registry_load
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=90)
    doc = registry_load(cfg)
    recent = []
    for vid, entry in doc["videos"].items():
        pub = _parse_iso(entry.get("published_at"))
        if pub is None:
            continue
        if pub >= cutoff:
            recent.append(vid)
    if not recent:
        return 0, 0
    rows = stats_for(recent, cfg)
    views = sum(int(r.get("views") or 0) for r in rows)
    return views, len(rows)


def _watch_hours_12m(cfg):
    """Heures visionnage 12 derniers mois via Analytics API (None si KO)."""
    try:
        from .analytics import get_analytics_service
        svc = get_analytics_service(cfg)
        if svc is None:
            log.info("Monetization: pas de token yt-analytics.readonly "
                     "-> watch hours ignorees (autorisez avec `analytics-auth`)")
            return None
        end = dt.date.today()
        start = end - dt.timedelta(days=365)
        resp = svc.reports().query(
            ids="channel==MINE",
            startDate=start.isoformat(),
            endDate=end.isoformat(),
            metrics="estimatedMinutesWatched",
        ).execute()
        rows = resp.get("rows") or []
        if rows:
            return round(float(rows[0][0]) / 60.0, 1)
    except Exception:
        log.warning("Watch hours indisponibles (non fatal):\n%s",
                    traceback.format_exc())
    return None


def _pct(current, target):
    if not target:
        return None
    return round(100.0 * float(current or 0) / float(target), 2)


def _eta_days(current, target, gain_per_day):
    """Jours restants (None si inconnu ou deja atteint)."""
    current = float(current or 0)
    if target and current >= target:
        return 0
    gain = float(gain_per_day or 0)
    if gain <= 0:
        return None
    return int(math.ceil((float(target) - current) / gain))


def _delta_per_day(prev_value, cur_value, days):
    try:
        gain = (float(cur_value) - float(prev_value)) / days
        return round(gain, 3) if gain > 0 else 0.0
    except (TypeError, ValueError):
        return None


def run_check(cfg=None, force=False):
    """Mesure l'etat YPP + ETA + revenus. Ecrit progress.json. Non fatal.

    Retourne le doc progress (dict), tel quel pour le scheduler/CLI.
    """
    cfg = cfg or get_config()
    mc = cfg.get("monetization", {}) or {}
    out = _progress_file(cfg)
    previous = load_json(out, {}) or {}
    if not force and previous and _fresh(cfg, out):
        log.info("Monetization: progress.json frais (cache) -> pas de re-check")
        return previous
    now = dt.datetime.now()
    doc = {
        "generated_at": now.isoformat(timespec="seconds"),
        "errors": [],
        "warnings": [],
    }
    target_subs = int(mc.get("target_subs", 1000) or 1000)
    target_views = int(mc.get("target_shorts_views_90d", 10_000_000) or 10_000_000)
    target_hours = float(mc.get("target_watch_hours_12m", 4000) or 4000)
    rpm = float(mc.get("rpm_shorts_usd", 0.08) or 0.08)
    doc["targets"] = {
        "subs": target_subs,
        "shorts_views_90d": target_views,
        "watch_hours_12m": target_hours,
        "rpm_shorts_usd": rpm,
    }

    # --- Abonnes (channels.list) -------------------------------------------
    subs = None
    try:
        stats = _channel_stats(cfg)
        doc["channel"] = {k: stats[k] for k in
                          ("channel_id", "title", "channel_created_at",
                           "hidden_subs", "video_count")}
        subs = stats["subs"]
        doc["total_views"] = stats["total_views"]
        if stats["hidden_subs"]:
            doc["warnings"].append("nombre d'abonnes masque par la chaine")
    except Exception:
        doc["errors"].append("channels.list indisponible")
        log.warning("Monetization: channels.list a echoue (non fatal):\n%s",
                    traceback.format_exc())
    doc["subs"] = subs

    # --- Velocites (delta vs snapshot precedent) ---------------------------
    vel_subs = vel_views = vel_hours = None
    prev_views = prev_hours = None
    snap_days = None
    if previous and previous.get("generated_at"):
        try:
            prev_ts = dt.datetime.fromisoformat(
                str(previous.get("generated_at") or ""))
            days = (now - prev_ts).total_seconds() / 86400.0
            if days >= 0.1:
                snap_days = days
                if subs is not None and isinstance(previous.get("subs"), int):
                    vel_subs = _delta_per_day(previous["subs"], subs, days)
                if previous.get("shorts_views_90d") is not None:
                    prev_views = float(previous["shorts_views_90d"])
                if previous.get("watch_hours_12m") is not None:
                    prev_hours = float(previous["watch_hours_12m"])
        except (TypeError, ValueError):
            pass

    # --- Vues Shorts 90 jours (registre + videos.list) ---------------------
    views = n_shorts = None
    try:
        views, n_shorts = _views_90d(cfg)
    except Exception:
        doc["errors"].append("vues 90j indisponibles (registre/API)")
        log.warning("Monetization: _views_90d a echoue (non fatal):\n%s",
                    traceback.format_exc())
    doc["shorts_views_90d"] = views
    doc["n_shorts_90d"] = n_shorts
    if views is not None and snap_days and prev_views is not None:
        vel_views = _delta_per_day(prev_views, views, snap_days)

    # --- Heures visionnage 12 mois (Analytics API, optionnel) --------------
    hours = _watch_hours_12m(cfg)
    doc["watch_hours_12m"] = hours
    if hours is not None and snap_days and prev_hours is not None:
        vel_hours = _delta_per_day(prev_hours, hours, snap_days)

    # --- Eligibilite YPP (1000 subs ET (4000h OU 10M vues)) ----------------
    checks = {}
    checks["subs"] = {"current": subs, "target": target_subs,
                      "ok": (subs or 0) >= target_subs,
                      "pct": _pct(subs, target_subs)}
    route_views = {"current": views, "target": target_views,
                   "ok": (views or 0) >= target_views,
                   "pct": _pct(views, target_views)}
    route_hours = {"current": hours, "target": target_hours,
                   "ok": (hours or 0) >= target_hours,
                   "pct": _pct(hours, target_hours)}
    routes = [WATCH_HOURS_ROUTE, SHORTS_VIEWS_ROUTE]
    if (hours is None) and (views is not None):
        routes = [SHORTS_VIEWS_ROUTE]
    doc["ypp"] = {
        "checks": {"subs": checks["subs"],
                   WATCH_HOURS_ROUTE: route_hours,
                   SHORTS_VIEWS_ROUTE: route_views},
        "routes_available": routes,
        "eligible": checks["subs"]["ok"] and any(
            r["ok"] for r in (route_hours, route_views)),
        "requirement": "1000 abonnes ET (4000 h visionnage / 12 mois "
                       "OU 10M vues Shorts / 90 jours)",
    }

    # --- ETA (jours restants) ----------------------------------------------
    eta = {}
    eta["subs"] = _eta_days(subs, target_subs, vel_subs)
    if views is not None:
        best_views_target = target_views
        eta["shorts_views_90d"] = _eta_days(views, best_views_target, vel_views)
    else:
        eta["shorts_views_90d"] = None
    if hours is not None:
        eta["watch_hours_12m"] = _eta_days(hours, target_hours, vel_hours)
    else:
        eta["watch_hours_12m"] = None
    doc["velocities_per_day"] = {
        "subs": vel_subs,
        "shorts_views_90d": vel_views,
        "watch_hours_12m": vel_hours,
    }
    doc["eta_days"] = eta

    # --- Revenus estimes (RPM Shorts, proche de la realite mais estimation) -
    # YPP monétise en continu ; avant le passage, seulement une projection
    # "si eligibility aujourd'hui". Base : vues/jour des 90 derniers jours.
    est = None
    if views is not None:
        views_per_day = views / 90.0
        est = round(views_per_day * 30.0 * rpm / 1000.0, 2)
    doc["est_monthly_revenue_usd"] = est
    doc["revenue_note"] = ("estimation %s USD/mois (RPM Shorts %s USD/1000 "
                           "vues) — payable uniquement une fois le YPP actif"
                           % (est, rpm))

    # --- Alerte quand le statut flip vers eligible --------------------------
    try:
        if doc["ypp"]["eligible"] and not previous.get("ypp", {}).get("eligible"):
            from .scheduler import _alert
            _alert(cfg, "AutoPipe : YPP ELIGIBLE",
                   "Le canal remplit les conditions YPP (%s). Passez le "
                   "monetisation dans YouTube Studio." % doc.get("channel", {})
                   .get("title", "?"))
    except Exception:
        pass

    # --- Historique append-only ---------------------------------------------
    snapshot = {
        "generated_at": doc["generated_at"],
        "subs": doc["subs"],
        "shorts_views_90d": doc["shorts_views_90d"],
        "watch_hours_12m": doc["watch_hours_12m"],
        "eligible": doc["ypp"]["eligible"],
    }
    try:
        with open(_history_file(cfg), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    except Exception:
        log.warning("Monetization history non ecrite (non fatal):\n%s",
                    traceback.format_exc())

    save_json(out, doc)
    log.info("Monetization: subs=%s, vues90j=%s, heures12m=%s, eligible=%s",
             doc["subs"], doc["shorts_views_90d"], doc["watch_hours_12m"],
             doc["ypp"]["eligible"])
    return doc


def report(cfg=None):
    """Resume lisible (CLI `monetize`)."""
    cfg = cfg or get_config()
    doc = run_check(cfg) or {}
    if not doc:
        return "Monetization: aucun etat disponible."
    ypp = doc.get("ypp") or {}
    checks = ypp.get("checks") or {}
    eta = doc.get("eta_days") or {}
    lines = ["Monetization / YPP (%s)" % doc.get("generated_at"),
             "- abonnes: %s / %s (%s%%) — ETA %s j" % (
                 doc.get("subs"), checks.get("subs", {}).get("target"),
                 checks.get("subs", {}).get("pct"),
                 eta.get("subs") if eta.get("subs") is not None else "?")]
    sv = checks.get(SHORTS_VIEWS_ROUTE) or {}
    lines.append("- vues Shorts 90 j: %s / %s (%s%%) — ETA %s j (%s Shorts)"
                 % (sv.get("current"), sv.get("target"), sv.get("pct"),
                    eta.get(SHORTS_VIEWS_ROUTE)
                    if eta.get(SHORTS_VIEWS_ROUTE) is not None else "?",
                    doc.get("n_shorts_90d")))
    wh = checks.get(WATCH_HOURS_ROUTE) or {}
    lines.append("- heures visionnage 12 m: %s / %s (%s%%) — ETA %s j%s" % (
        wh.get("current"), wh.get("target"), wh.get("pct"),
        eta.get(WATCH_HOURS_ROUTE)
        if eta.get(WATCH_HOURS_ROUTE) is not None else "?",
        "" if wh.get("current") is not None
        else "  [token yt-analytics.readonly requis: autopipe analytics-auth]"))
    lines.append("- ELIGIBLE YPP : %s" %
                 ("OUI" if ypp.get("eligible") else "pas encore"))
    if doc.get("est_monthly_revenue_usd") is not None:
        lines.append("- revenus estimes une fois YPP actif : %s"
                     % doc.get("revenue_note"))
    for w in doc.get("warnings") or []:
        lines.append("- attention: %s" % w)
    return "\n".join(lines)
