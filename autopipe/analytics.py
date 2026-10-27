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

# V3 : loop rate = vues / viewers uniques. C'est LA metrique qui valide ou
# invalide le loop "fin rejoint debut" : > 100 % = les viewers re-regardent
# (le loop fonctionne), < 80 % = le loop ne prend pas (ils ne reviennent
# pas au debut). uniqueViewers n'est PAS exposee par l'API Analytics v2
# (Studio Advanced Mode uniquement ; la metrique historique "uniques" est
# depreciee depuis 2016 - verifie 2026-10-27). On TENTE quand meme la
# requete (Google peut ouvrir la metrique sans preavis) avec repli propre,
# meme pattern que engagedViews. Jamais fatal.
LOOP_TARGET_PCT = 100.0   # au-dessus : les viewers re-regardent
LOOP_MIN_PCT = 80.0       # en-dessous : le loop ne fonctionne pas


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


def loop_thresholds(cfg=None):
    """Seuils loop rate (pct vues/viewers uniques) depuis la cfg."""
    acfg = (cfg or get_config()).get("analytics", {}) or {}
    try:
        target = float(acfg.get("loop_target_pct", LOOP_TARGET_PCT))
    except (TypeError, ValueError):
        target = LOOP_TARGET_PCT
    try:
        minimum = float(acfg.get("loop_min_pct", LOOP_MIN_PCT))
    except (TypeError, ValueError):
        minimum = LOOP_MIN_PCT
    return target, minimum


def _loop_uniques(svc, ids, start, end):
    """Tente views+uniqueViewers par video (V3 loop rate).

    Renvoie {video_id: unique_viewers} ou {} si la metrique est refusee
    (cas documente : Studio-only). Aucune exception ne remonte.
    """
    try:
        resp = svc.reports().query(
            ids="channel==MINE", startDate=start, endDate=end,
            metrics="views,uniqueViewers", dimensions="video",
            filters="video==" + ",".join(ids)).execute()
    except Exception as exc:
        log.info("uniqueViewers refusee par l'API (Studio-only, verifie "
                 "2026-10-27) -> loop rate indisponible via API: %s",
                 str(exc)[:140])
        return {}
    cols = [c.get("name") for c in (resp.get("columnHeaders") or [])]
    idx = {n: p for p, n in enumerate(cols)}
    out = {}
    for row in resp.get("rows", []) or []:
        try:
            vid = str(row[idx["video"]])
            uv = float(row[idx["uniqueViewers"]] or 0.0)
            if vid and uv > 0:
                out[vid] = int(uv)
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    if out:
        log.info("uniqueViewers DISPONIBLE pour %s videos -> loop rate "
                 "calcule directement", len(out))
    return out


def loop_value(row):
    """Loop rate d'une ligne retention (pct vues/viewers uniques).

    Renvoie None si la donnee n'existe pas (metrique API refusee).
    """
    if not isinstance(row, dict):
        return None
    v = row.get("loop_rate")
    return float(v) if isinstance(v, (int, float)) else None


def loop_verdict(rate, cfg=None):
    """ok | weak | None pour un loop rate en % (None si pas de donnee)."""
    if not isinstance(rate, (int, float)):
        return None
    target, minimum = loop_thresholds(cfg)
    if rate >= target:
        return "ok"
    if rate < minimum:
        return "weak"
    return "borderline"


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
    # V3 loop rate : une seule tentative uniqueViewers par appel (si la
    # metrique est refusee une fois, inutile de marteler l'API).
    uniques_by_id = {}
    loop_supported = True
    # L'API accepte plusieurs ids par filtre mais limite la taille : on
    # decoupe en paquets de 10 (suffisant : 6 uploads/jour max).
    for i in range(0, len(ids), 10):
        chunk = ids[i:i + 10]
        if loop_supported and not uniques_by_id:
            uniques_by_id = _loop_uniques(svc, chunk, start, end)
            if not uniques_by_id:
                loop_supported = False
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
            vid = str(row[idx["video"]]) if idx.get("video") is not None \
                  and idx["video"] < len(row) else ""
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
            # V3 loop rate : vues / viewers uniques (>100 % = re-regard).
            uv = uniques_by_id.get(vid)
            if uv:
                loop_rate = round(100.0 * views / uv, 1)
                loop_source = "uniqueViewers"
            else:
                loop_rate = None
                loop_source = "unavailable" if not loop_supported else "pending"
            out.append({
                "id": vid,
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
                "unique_viewers": uv,
                "loop_rate": loop_rate,
                "loop_source": loop_source,
                "loop_verdict": loop_verdict(loop_rate, cfg),
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
    # V3 diagnostic loop rate : la metrique qui valide le "fin rejoint
    # debut". None partout = uniqueViewers encore refusee par l'API.
    loops = [loop_value(r) for r in rows]
    measured = [v for v in loops if v is not None]
    loop_mean = (sum(measured) / len(measured)) if measured else None
    doc["loop_target_pct"], doc["loop_min_pct"] = loop_thresholds(cfg)
    doc["loop_mean"] = round(loop_mean, 1) if loop_mean is not None else None
    doc["loop_n_measured"] = len(measured)
    if loop_mean is not None:
        verdict = "ok" if loop_mean >= doc["loop_target_pct"] else (
            "weak" if loop_mean < doc["loop_min_pct"] else "borderline")
        doc["loop_verdict"] = verdict
        log.info("Loop rate moyen: %.1f %% (cible %.0f %%, min %.0f %%) -> %s",
                 loop_mean, doc["loop_target_pct"], doc["loop_min_pct"],
                 verdict)
        if verdict == "weak":
            log.warning("Loop rate < %.0f %% : le 'fin rejoint debut' ne "
                        "prend PAS - renforce la boucle (payoff tease des le "
                        "debut, derniere image = premiere image).",
                        doc["loop_min_pct"])
    else:
        log.info("Loop rate: metrique uniqueViewers indisponible via API "
                 "(Studio-only) - loop non mesure ce run")
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
    # V3 feedback loop rate : valide ou invalide le 'fin rejoint debut'.
    loops = [loop_value(r) for r in rows]
    measured = [v for v in loops if v is not None]
    if measured:
        lmean = sum(measured) / len(measured)
        ltarget, lmin = loop_thresholds(cfg)
        if lmean < lmin:
            parts.append(
                "CRITICAL loop check FAILED: viewers/unique-viewers ratio is "
                "only %.0f%% (below %.0f%%) - viewers are NOT rewatching. "
                "Close the narrative circle: the last visual/line must answer "
                "or echo the opening hook, and tease early that the payoff "
                "rewards a second watch." % (lmean, lmin))
        elif lmean >= ltarget:
            parts.append(
                "Loop check PASSED: viewers/unique-viewers ratio %.0f%% "
                "(rewatching confirmed) - keep the ending-to-beginning loop "
                "exactly this strong." % lmean)
    return " ".join(parts)
