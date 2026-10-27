"""Suivi des performances des videos publiees (YouTube Data API)."""
import datetime as dt
import json

from .config import get_config, get_paths
from .util import get_logger, load_json, save_json

log = get_logger("analytics")


def stats_for(video_ids, cfg=None):
    from .upload import get_service
    yt = get_service(cfg)
    out = []
    ids = [str(v) for v in video_ids if v]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        resp = yt.videos().list(part="snippet,statistics,status",
                                id=",".join(chunk)).execute()
        for item in resp.get("items", []):
            st = item.get("statistics", {})
            out.append({
                "id": item["id"],
                "title": item.get("snippet", {}).get("title"),
                "published": item.get("snippet", {}).get("publishedAt"),
                "views": int(st.get("viewCount", 0)),
                "likes": int(st.get("likeCount", 0)),
                "comments": int(st.get("commentCount", 0)),
                "privacy": item.get("status", {}).get("privacyStatus"),
                "fetched": dt.datetime.now().isoformat(),
            })
    return out


def record(video_ids, cfg=None):
    cfg = cfg or get_config()
    paths = get_paths(cfg)
    rows = stats_for(video_ids, cfg)
    if not rows:
        log.warning("Aucune stat recuperee pour %s", video_ids)
        return rows
    hist = paths.analytics / "history.jsonl"
    with open(hist, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    save_json(paths.analytics / "latest.json", rows)
    log.info("Stats enregistrees: %s videos", len(rows))
    return rows


def print_summary(cfg=None):
    cfg = cfg or get_config()
    rows = load_json(get_paths(cfg).analytics / "latest.json", []) or []
    if not rows:
        print("Aucune statistique disponible (lance une publication d'abord).")
        return rows
    rows.sort(key=lambda r: r.get("views", 0), reverse=True)
    # Retention (proxy Viewed vs Swiped Away) si disponible.
    ret = {}
    try:
        rdoc = load_json(get_paths(cfg).analytics / "retention_latest.json") or {}
        ret = {str(r.get("id")): r for r in (rdoc.get("rows") or [])}
    except Exception:
        ret = {}
    if ret:
        print("%-15s %8s %7s %8s %8s  %s" % (
            "ID", "VUES", "LIKES", "COMM.", "RET%", "TITRE"))
        for r in rows[:20]:
            rrow = ret.get(str(r["id"])) or {}
            pct = rrow.get("avg_view_pct")
            pct_s = ("%.1f" % pct) if isinstance(pct, (int, float)) else "-"
            print("%-15s %8s %7s %8s %8s  %s" % (
                r["id"], r.get("views", 0), r.get("likes", 0),
                r.get("comments", 0), pct_s, (r.get("title") or "")[:60]))
    else:
        print("%-15s %8s %7s %8s  %s" % ("ID", "VUES", "LIKES", "COMM.", "TITRE"))
        for r in rows[:20]:
            print("%-15s %8s %7s %8s  %s" % (
                r["id"], r.get("views", 0), r.get("likes", 0),
                r.get("comments", 0), (r.get("title") or "")[:60]))
    total = sum(r.get("views", 0) for r in rows)
    print("Total vues: %s sur %s videos" % (total, len(rows)))
    if ret:
        pcts = [r["avg_view_pct"] for r in ret.values()
                if isinstance(r.get("avg_view_pct"), (int, float))]
        if pcts:
            mean = sum(pcts) / len(pcts)
            print("Retention moyenne (Viewed vs Swiped proxy): %.1f %% "
                  "- cible 70 %%" % mean)
            if mean < VIEWED_TARGET_PCT:
                print("Sous le seuil: le hook ne tient pas -> directive "
                      "hook 2s automatiquement injectee dans les prochains "
                      "scripts (autopipe optimize).")
    return rows


# ---------------------------------------------------------------------------
# Boucle retention "Viewed vs Swiped Away" (critique 2026-10-27)
# ---------------------------------------------------------------------------
# YouTube Analytics API expose averageViewPercentage / averageViewDuration
# (le duo le plus proche de la metrique Studio "Viewed vs Swiped Away",
# qui n'est PAS exposee via API - verifie 2026-10-27). Le seuil 70 %+
# "viewed" est la zone virale documentee (50/50 = moyen, <30 % = mort dans
# le feed). Scope DEDIE yt-analytics.readonly + token separe :
# token.json (upload) n'est JAMAIS touche -> la facon de publier reste
# exactement la meme. Jamais fatal : sans autorisation, le pipeline tourne
# comme avant (la boucle retention attend la commande analytics-auth).

YT_ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"

# Seuil Studio "Viewed vs Swiped Away" a viser (zone virale documentee).
VIEWED_TARGET_PCT = 70.0
# V2 : cible sur le VRAI ratio d'engagement engagedViews/views.
# Semantique OFFICIELLE (doc YouTube, revision du 31/03/2025) : une "view"
# Shorts compte chaque DEMARRAGE de lecture (meme un swipe instantane),
# une "engaged view" compte une lecture qui CONTINUE apres la premiere
# frame (ou un clic volontaire). Donc engagedViews/views = part des
# demarrages qui ne swipent pas immediat - le vrai proxy "Viewed vs Swiped
# Away" de Studio. engagedViews est une metrique core v2 du rapport
# "Top videos" (dimensions=video). Repli historique : averageViewPercentage.
ENGAGED_TARGET_PCT = 70.0


def _analytics_token_file(cfg):
    return get_paths(cfg).root / (cfg.get("analytics", {}) or {}).get(
        "token_file", "token_analytics.json")


def get_analytics_service(cfg=None, interactive=False):
    """Client YouTube Analytics API (scope dedie, token separe).

    Renvoie None (jamais d'exception) si non autorise : le pipeline
    continue sans la boucle retention. interactive=True (CLI
    analytics-auth uniquement) ouvre le navigateur pour autoriser.
    """
    cfg = cfg or get_config()
    token_file = _analytics_token_file(cfg)
    creds = None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        if token_file.exists():
            creds = Credentials.from_authorized_user_file(
                str(token_file), [YT_ANALYTICS_SCOPE])
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as exc:
                    log.warning("Refresh token_analytics impossible (%s)", exc)
                    creds = None
        if creds and creds.valid:
            from googleapiclient.discovery import build
            mode = cfg.get("network", {}).get("proxy_mode", "direct")
            # Meme strategie proxy que upload._build_service (mode direct
            # par defaut) mais SANS les scopes d'upload.
            import httplib2
            from google_auth_httplib2 import AuthorizedHttp
            if mode == "direct":
                proxy_info = lambda *args, **kwargs: None  # noqa: E731
            else:
                proxy_info = httplib2.proxy_info_from_environment
            http = httplib2.Http(timeout=60, proxy_info=proxy_info)
            return build("youtubeAnalytics", "v2",
                         http=AuthorizedHttp(creds, http=http),
                         cache_discovery=False)
        if interactive:
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
            secrets = get_paths(cfg).root / cfg["upload"].get(
                "client_secrets", "client_secret.json")
            if not secrets.exists():
                log.error("client_secret.json introuvable (%s)", secrets)
                return None
            flow = InstalledAppFlow.from_client_secrets_file(
                str(secrets), [YT_ANALYTICS_SCOPE])
            creds = flow.run_local_server(open_browser=True, port=0)
            token_file.write_text(creds.to_json(), encoding="utf-8")
            log.info("OAuth analytics token saved -> %s", token_file)
            import httplib2
            from google_auth_httplib2 import AuthorizedHttp
            http = httplib2.Http(timeout=60,
                                 proxy_info=(lambda *a, **k: None))  # noqa: E731
            return build("youtubeAnalytics", "v2",
                         http=AuthorizedHttp(creds, http=http),
                         cache_discovery=False)
        log.info("Boucle retention inactive: autorise une fois avec "
                 "`autopipe analytics-auth` (ne touche pas au token d'upload).")
        return None
    except ImportError as exc:
        log.warning("Dependance Google manquante pour la boucle retention: %s", exc)
        return None
    except Exception as exc:
        log.warning("Service analytics indisponible (%s) -> boucle retention "
                    "desactivee ce run", exc)
        return None


def engagement_value(row):
    """Ratio d'engagement d'une ligne retention : engaged_ratio si present,
    sinon avg_view_pct (compat anciens snapshots)."""
    if not isinstance(row, dict):
        return 0.0
    for key in ("engaged_ratio", "avg_view_pct"):
        v = row.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def retention_for(video_ids, cfg=None, days=28):
    """Retention par video : engagedViews/views (V2) + averageViewPercentage.

    Requete Analytics API reports().query, dimensions=video, dernieres
    `days` jours (les metriques Shorts mettent ~24-48 h a se stabiliser).
    engagedViews est une metrique core v2 (rapport "Top videos") ; si la
    metrique est refusee, repli sans elle et engaged_ratio retombe sur
    averageViewPercentage (champ engaged_source l'indique). Renvoie [] si
    non autorise ou erreur (jamais fatal).
    """
    cfg = cfg or get_config()
    svc = get_analytics_service(cfg)
    if svc is None:
        return []
    ids = [str(v) for v in video_ids if v]
    out = []
    end = dt.date.today().isoformat()
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    # L'API accepte plusieurs ids par filtre mais limite la taille : on
    # decoupe en paquets de 10 (suffisant : 6 uploads/jour max).
    for i in range(0, len(ids), 10):
        chunk = ids[i:i + 10]
        metrics = ("views,engagedViews,averageViewDuration,"
                   "averageViewPercentage,likes,comments,shares")
        try:
            resp = svc.reports().query(
                ids="channel==MINE",
                startDate=start, endDate=end,
                metrics=metrics,
                dimensions="video",
                filters="video==" + ",".join(chunk),
            ).execute()
        except Exception as exc:
            # engagedViews indisponible (compte non eligible, API degradee,
            # etc.) -> repli historique sans engagedViews, jamais fatal.
            log.warning("Analytics query avec engagedViews echouee (paquet "
                        "%s): %s -> repli sans engagedViews", i, exc)
            try:
                resp = svc.reports().query(
                    ids="channel==MINE",
                    startDate=start, endDate=end,
                    metrics=("views,averageViewDuration,"
                             "averageViewPercentage,likes,comments,shares"),
                    dimensions="video",
                    filters="video==" + ",".join(chunk),
                ).execute()
            except Exception as exc2:
                log.warning("Analytics query echouee (paquet %s): %s", i, exc2)
                continue
        cols = [c.get("name") for c in (resp.get("columnHeaders") or [])]
        idx = {name: pos for pos, name in enumerate(cols)}
        for row in resp.get("rows", []) or []:
            def _f(name):
                pos = idx.get(name)
                if pos is None or pos >= len(row):
                    return 0.0
                try:
                    return float(row[pos] or 0.0)
                except (TypeError, ValueError):
                    return 0.0
            views = int(_f("views"))
            engaged = int(_f("engagedViews"))
            avg_pct = round(_f("averageViewPercentage"), 1)
            if views > 0 and engaged > 0:
                # VRAI ratio "restes vs swipes" : vues qui continuent apres
                # la premiere frame / demarrages totaux. Sur Shorts, une
                # "view" compte tout demarrage (meme un swipe a 1 s) et une
                # "engaged view" une lecture qui continue - doc YouTube,
                # revision du 31/03/2025. averageViewPercentage seul ment :
                # il noie les swipes precoces dans les longues sessions des
                # viewers restes (moyenne, pas taux).
                engaged_ratio = round(100.0 * engaged / views, 1)
                engaged_source = "engagedViews"
            else:
                # Repli : averageViewPercentage (proxy historique, biais
                # favorable aux Shorts courts - a interpreter avec prudence).
                engaged_ratio = avg_pct
                engaged_source = "fallback_averageViewPercentage"
            out.append({
                "id": str(row[idx["video"]]) if idx.get("video") is not None
                      and idx["video"] < len(row) else "",
                "range_start": start,
                "range_end": end,
                "views": views,
                "engagedViews": engaged,
                "engaged_ratio": engaged_ratio,
                "engaged_source": engaged_source,
                "avg_view_duration_s": round(_f("averageViewDuration"), 1),
                # Proxy direct de "Viewed vs Swiped Away" (Studio) : un
                # Short regarde en moyenne a X % de sa duree.
                "avg_view_pct": avg_pct,
                "likes": int(_f("likes")),
                "comments": int(_f("comments")),
                "shares": int(_f("shares")),
                "viewed_target_pct": VIEWED_TARGET_PCT,
                "fetched": dt.datetime.now().isoformat(timespec="seconds"),
            })
    return out


def refresh_retention(cfg=None):
    """Rafraichit la retention de toutes les videos connues (non fatal).

    Sources d'ids: data/analytics/latest.json (record). Ecrit
    data/analytics/retention_latest.json + history_retention.jsonl,
    et calcule un diagnostic hook (cible 70 %).
    """
    cfg = cfg or get_config()
    paths = get_paths(cfg)
    latest = load_json(paths.analytics / "latest.json", []) or []
    ids = [r.get("id") for r in latest if isinstance(r, dict) and r.get("id")]
    if not ids:
        return {"status": "no_videos"}
    rows = retention_for(ids, cfg)
    if not rows:
        return {"status": "unauthorized_or_error", "n_ids": len(ids)}
    with open(paths.analytics / "history_retention.jsonl", "a",
              encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    doc = {"rows": rows, "fetched": dt.datetime.now().isoformat(),
           "viewed_target_pct": VIEWED_TARGET_PCT,
           "engaged_target_pct": float(
               (cfg.get("analytics", {}) or {}).get(
                   "engaged_target_pct", ENGAGED_TARGET_PCT))}
    save_json(paths.analytics / "retention_latest.json", doc)
    weak = [r for r in rows if engagement_value(r) <
            doc["engaged_target_pct"]]
    mean = (sum(engagement_value(r) for r in rows) / len(rows)) if rows else 0.0
    log.info("Retention rafraichie: %s videos, engagement moyen %.1f %% "
             "(cible %.0f %%) - %s sous le seuil",
             len(rows), mean, doc["engaged_target_pct"], len(weak))
    return {"status": "ok", "n": len(rows), "rows": rows,
            "weak": [r["id"] for r in weak]}


def hook_directive(cfg=None):
    """Directive de style derivee de la retention (injectee dans les prompts).

    Retourne "" si aucune donnee. Sert a boucler la metrique sur la
    generation : retention faible -> consigne hook 2 s + montage plus court.
    """
    cfg = cfg or get_config()
    doc = load_json(get_paths(cfg).analytics / "retention_latest.json") or {}
    rows = doc.get("rows") or []
    if not rows:
        return ""
    vals = [engagement_value(r) for r in rows]
    mean = sum(vals) / len(vals)
    try:
        target = float(doc.get("engaged_target_pct") or
                       (cfg.get("analytics", {}) or {}).get(
                           "engaged_target_pct", ENGAGED_TARGET_PCT))
    except (TypeError, ValueError):
        target = ENGAGED_TARGET_PCT
    if mean >= target:
        return ("Learned retention: engaged-view ratio %.0f%% "
                "(target %.0f%%, keep doing exactly this)." % (mean, target))
    parts = [
        "CRITICAL hook discipline (learned): current engaged-view ratio "
        "is only %.0f%%, target is %.0f%%." % (mean, target),
        "Front-load the single most shocking fact in the FIRST sentence so "
        "the hook lands within the first 2 seconds (viewers decide to swipe "
        "or stay in under 2 seconds).",
        "Be SPECIFIC: name the exact number, mistake, or outcome - vague "
        "hooks like 'you won't believe this' lose about 4x more viewers.",
        "The on-screen hook text must read in under 2 seconds (max 12 spoken "
        "words, 3-7 on-screen words).",
        "Cut anything that does not build the hook or the payoff.",
    ]
    if mean < 50.0:
        parts.append("Script SHORTER: aim for the low end of the duration "
                     "range; every extra second loses swipers.")
    return " ".join(parts)
