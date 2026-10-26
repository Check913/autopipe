"""Radar multi-plateformes : detecte les contenus viraux du web sans cle API.

Demande utilisateur (2026-10-26) : "a chaque fois qu'il est lance il scrape
tout le web (tiktok, youtube, reddit, instagram ou autre) pour reellement
obtenir des videos reellement virales ... pour trouver les videos dont il
doit s'inspirer".

Le radar agrège des sources PUBLIQUES (aucune cle requise) :
  - Reddit             JSON public r/<sub>/hot.json (upvotes + commentaires)
  - Hacker News        API Algolia publique (front page + recherche niche)
  - Google News        RSS de recherche par mots-cles de la niche
  - Google Trends      RSS des recherches qui explosent aujourd'hui
  - YouTube Shorts     multi-requetes yt-dlp + filtre recence (sp) + memoire
                       anti-repetition data/radar/history.json
  - TikTok/Instagram   pages tag publiques (best-effort, desactive par defaut)

Sortie : data/radar/latest.json
  {"date", "duration_s", "sources": {plateforme: {items, error}},
   "items": [...], "topics": [...]}
Consommateurs :
  - scout.run_scout      -> merge_radar_topics() fusionne les topics radar
  - ideas.run_ideas      -> lit scout.topics (donc aussi les topics radar)
  - remake.discover_hits -> radar_video_hits() fournit les hits video viraux
  - cli cmd_radar        -> scan manuel (`autopipe radar [--force]`)
Cache TTL : latest.json est reutilise si plus frais que radar.cache_ttl_minutes
afin de ne pas marteler les sources publiques a chaque iteration 24/7.
"""
import datetime as dt
import re
import time
import xml.etree.ElementTree as ET

from .config import get_config, get_paths
from .util import get_logger, load_json, save_json

log = get_logger("radar")

# Plateformes dont les items sont de vraies videos remakables (remake/10% Better).
VIDEO_PLATFORMS = frozenset(("youtube", "tiktok", "instagram"))

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_YT_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})")
_TT_RE = re.compile(r"/@([\w.\-]+)/video/(\d+)")
_IG_RE = re.compile(r"/(?:reel|p)/([A-Za-z0-9_-]{5,})")
_TRAFFIC_RE = re.compile(r"([\d.]+)\s*([KMB])?", re.I)

# Filtres YouTube "sp" (tries par vues + publiees recemment + type video),
# codes verifies empiriquement via yt-dlp (protobuf 08 03 12 04 08 03 10 01).
_SP_VIEWS = {
    1: "CAMSBAgCEAE%3D",     # aujourd'hui
    7: "CAMSBAgDEAE%3D",     # cette semaine (defaut)
    30: "CAMSBAgEEAE%3D",    # ce mois-ci
}

_KW_STOP = frozenset((
    "video", "videos", "shorts", "short", "watch", "subscribe", "channel",
    "today", "best", "top", "part", "full", "official", "this", "that",
    "with", "have", "from", "your", "about", "will", "what", "when", "were",
))


# --------------------------------------------------------------------------
# Petits utilitaires
# --------------------------------------------------------------------------

def _int(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _float(value):
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _clean_title(text, maxlen=80):
    """Titre propre : espaces, suffixe '- Source' (Google News), coupe mot."""
    t = " ".join(str(text or "").split())
    if t.count(" - ") == 1:
        t = t.rsplit(" - ", 1)[0]      # "Headline - CNN" -> "Headline"
    if len(t) > maxlen:
        cut = t[:maxlen]
        if " " in cut:
            cut = cut[:cut.rfind(" ")]
        t = cut.strip()
    return t


def _approx_traffic(raw):
    """'200K+' / '1.2M' / '1500' -> nombre (base du score Google Trends)."""
    m = _TRAFFIC_RE.search(str(raw or ""))
    if not m:
        return 0
    value = float(m.group(1))
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get((m.group(2) or "").upper(), 1)
    return int(value * mult)


def _http_get(url, timeout=15, headers=None):
    import requests
    h = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    resp = requests.get(url, timeout=timeout, headers=h)
    resp.raise_for_status()
    return resp


def _parse_rss(xml_text):
    """Parse un flux RSS 2.0 ou Atom minimal -> [{'title', 'link', ...}]."""
    root = ET.fromstring(xml_text)
    items = []
    for node in root.iter():
        if node.tag.split("}")[-1] not in ("item", "entry"):
            continue
        entry = {}
        for child in node:
            local = child.tag.split("}")[-1]
            if local in entry:
                continue
            if local == "link":
                # RSS 2.0 : <link>texte</link> ; Atom : <link href="..."/>.
                entry["link"] = (child.get("href") or child.text or "").strip()
            elif child.text:
                entry[local] = child.text.strip()
        items.append(entry)
    return items


def _age_minutes(date_iso):
    try:
        delta = dt.datetime.now() - dt.datetime.fromisoformat(str(date_iso))
        return delta.total_seconds() / 60.0
    except (TypeError, ValueError):
        return None


def _niche_keywords(cfg, max_kw=4):
    """Mots-cles de niche dedupliques (remake -> scout -> general.niche)."""
    words, seen = [], set()
    for src in (cfg.get("remake", {}).get("keywords") or [],
                cfg.get("scout", {}).get("keywords") or [],
                [cfg.get("general", {}).get("niche") or ""]):
        for kw in src:
            kw = " ".join(str(kw).split())
            if kw and kw.lower() not in seen:
                seen.add(kw.lower())
                words.append(kw)
    return words[:max_kw]


def _source_cfg(cfg, name):
    return cfg.get("radar", {}) or {}


# --------------------------------------------------------------------------
# Collecteurs (un par plateforme) : chaque exception reste locale
# --------------------------------------------------------------------------

def _parse_reddit_json(payload, sub):
    """Enfants JSON d'un listing hot -> items radar (upvotes + commentaires)."""
    items = []
    for child in (payload.get("data") or {}).get("children") or []:
        d = child.get("data") or {}
        title = (d.get("title") or "").strip()
        if not title or d.get("stickied") or d.get("over_18"):
            continue
        items.append({
            "id": str(d.get("id") or ""),
            "platform": "reddit",
            "title": title,
            "url": "https://www.reddit.com%s" % (d.get("permalink") or ""),
            "views": _int(d.get("ups")),          # upvotes = signal viral
            "duration": 0.0,
            "comments": _int(d.get("num_comments")),
            "author": d.get("author") or "",
            "source": "reddit:r/%s" % sub,
        })
    return items


def _parse_reddit_rss(entries, sub):
    """Entries Atom d'un flux r/<sub> -> items radar (metriques inconnues)."""
    items = []
    for entry in entries or []:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        m = re.search(r"/comments/([a-z0-9]+)", link, re.I)
        items.append({
            "id": m.group(1) if m else "",
            "platform": "reddit",
            "title": title,
            "url": link,
            "views": 0,                            # le RSS ne donne pas d'upvotes
            "duration": 0.0,
            "comments": 0,
            "author": "",
            "source": "reddit:r/%s" % sub,
        })
    return items


def _collect_reddit(cfg):
    import requests
    r = cfg.get("radar", {})
    subs = [str(s).strip().lstrip("r/").strip("/") for s in (r.get("reddit_subs") or [])
            if str(s).strip()]
    per_sub = max(5, int(r.get("reddit_per_sub", 25)))
    pacing = float(r.get("pacing_seconds", 0.4))
    timeout = float(r.get("request_timeout", 15))
    items = []
    for sub in subs[:8]:
        url = "https://www.reddit.com/r/%s/hot.json?limit=%d&raw_json=1" % (sub, per_sub)
        rss_url = "https://www.reddit.com/r/%s/hot/.rss" % sub
        got = None
        # Reddit renvoie 403 "Blocked" sur les endpoints .json sans session et
        # chaque requete compte dans son rate-limit : on tente d'abord le flux
        # Atom public r/<sub>/hot/.rss (sans metriques, sujets quand meme
        # exposes), puis .json en secours si le RSS est indisponible.
        try:
            got = _parse_reddit_rss(_parse_rss(_http_get(rss_url, timeout=timeout).text), sub)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status == 429:
                # Rate limit : on respecte Retry-After (plafonne a 15 s), un essai.
                delay = 3.0
                try:
                    delay = min(15.0, max(1.0,
                             float(exc.response.headers.get("Retry-After") or 3.0)))
                except (TypeError, ValueError):
                    pass
                log.info("Reddit r/%s RSS rate-limite, attente %.0fs puis essai",
                         sub, delay)
                time.sleep(delay)
                try:
                    got = _parse_reddit_rss(
                        _parse_rss(_http_get(rss_url, timeout=timeout).text), sub)
                except Exception as exc2:
                    log.warning("Reddit r/%s RSS indisponible apres essai (%s), "
                                "sub ignore", sub, exc2)
            else:
                log.info("Reddit r/%s RSS indisponible (%s) -> bascule .json",
                         sub, exc)
                try:
                    got = _parse_reddit_json(_http_get(url, timeout=timeout).json(), sub)
                except Exception as exc2:
                    log.warning("Reddit r/%s indisponible (%s), sub ignore",
                                sub, exc2)
        except Exception as exc:
            log.warning("Reddit r/%s RSS erreur (%s), sub ignore", sub, exc)
        if got:
            items.extend(got)
        time.sleep(pacing)
    return items


def _collect_hackernews(cfg):
    r = cfg.get("radar", {})
    per_query = max(5, int(r.get("hackernews_per_query", 30)))
    timeout = float(r.get("request_timeout", 15))
    pacing = float(r.get("pacing_seconds", 0.4))
    queries = ["front_page"]
    for kw in _niche_keywords(cfg, 2):
        queries.append(kw)
    items, seen = [], set()
    for q in queries[:3]:
        if q == "front_page":
            url = ("https://hn.algolia.com/api/v1/search?tags=front_page"
                   "&hitsPerPage=%d" % per_query)
        else:
            url = ("https://hn.algolia.com/api/v1/search?query=%s&tags=story"
                   "&hitsPerPage=%d" % (q.replace(" ", "+"), per_query))
        for hit in _http_get(url, timeout=timeout).json().get("hits") or []:
            title = (hit.get("title") or "").strip()
            if not title:
                continue
            key = _norm(title)[:60]
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "id": str(hit.get("objectID") or ""),
                "platform": "hackernews",
                "title": title,
                "url": hit.get("url") or ("https://news.ycombinator.com/item?id=%s"
                                          % hit.get("objectID")),
                "views": _int(hit.get("points")),     # points = signal viral
                "duration": 0.0,
                "comments": _int(hit.get("num_comments")),
                "author": hit.get("author") or "",
                "source": "hn:" + q,
            })
        time.sleep(pacing)
    return items


def _collect_google_news(cfg):
    r = cfg.get("radar", {})
    per_kw = max(5, int(r.get("google_news_per_keyword", 20)))
    timeout = float(r.get("request_timeout", 15))
    pacing = float(r.get("pacing_seconds", 0.4))
    items, seen = [], set()
    for kw in _niche_keywords(cfg, 3):
        url = ("https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en"
               % kw.replace(" ", "+"))
        for entry in _parse_rss(_http_get(url, timeout=timeout).text)[:per_kw]:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            key = _norm(title)[:60]
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "id": "",
                "platform": "news",
                "title": title,
                "url": entry.get("link") or "",
                "views": 0,
                "duration": 0.0,
                "comments": 0,
                "author": "",
                "source": "gnews:" + kw,
            })
        time.sleep(pacing)
    return items


def _collect_google_trends(cfg):
    r = cfg.get("radar", {})
    geo = str(r.get("google_trends_geo") or cfg.get("general", {}).get("region") or "US")
    timeout = float(r.get("request_timeout", 15))
    # Endpoint 2025+ ; l'ancien daily RSS reste en secours.
    urls = ["https://trends.google.com/trending/rss?geo=%s" % geo,
            "https://trends.google.com/trends/trendingsearches/daily/rss?geo=%s" % geo]
    last_exc = None
    for url in urls:
        try:
            entries = _parse_rss(_http_get(url, timeout=timeout).text)
            break
        except Exception as exc:
            last_exc = exc
            entries = []
    if not entries and last_exc:
        raise last_exc
    items = []
    for entry in entries:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        items.append({
            "id": "",
            "platform": "trends",
            "title": title,
            "url": "",
            "views": _approx_traffic(entry.get("approx_traffic")),  # ex: "200K+"
            "duration": 0.0,
            "comments": 0,
            "author": "",
            "source": "gtrends:%s" % geo,
        })
    return items


def _youtube_queries(cfg):
    """Requetes YouTube variees : templates {kw} croises avec les mots-cles.

    Anti-repetition : au lieu d'UNE requete fixe, on interroge plusieurs
    angles (shorts, viral, moments...) sur plusieurs mots-cles de niche.
    """
    r = cfg.get("radar", {}) or {}
    templates = [str(t).strip() for t in (r.get("youtube_queries") or [])
                 if str(t).strip()]
    if not templates:
        templates = ["{kw} shorts", "{kw} viral shorts",
                     "{kw} moments caught on camera"]
    queries, seen = [], set()
    for kw in (_niche_keywords(cfg, 3) or ["amazing facts"]):
        for tpl in templates:
            q = tpl.replace("{kw}", kw)
            if q and q.lower() not in seen:
                seen.add(q.lower())
                queries.append(q)
    return queries[:12]


def _collect_youtube(cfg):
    """YouTube : plusieurs requetes + filtre recence + tri par vues.

    Corrige le bug "toujours les memes videos a chaque lancement" :
    l'ancienne version envoyait UNE seule requete fixe "<niche> shorts"
    sans filtre de date -> YouTube renvoyait en boucle les memes megas-viraux
    historiques. Desormais :
      - radar.youtube_queries : templates {kw} x mots-cles de niche ;
      - filtre sp = tries par vues + publiees dans les N derniers jours
        (radar.youtube_recency_days) -> videos RECENTMENT virales uniquement ;
      - dedup par identifiant video entre requetes ;
      - une requete en echec n'annule jamais les autres.
    """
    r = cfg.get("radar", {})
    n = max(5, int(r.get("youtube_per_keyword", 15)))
    timeout = float(r.get("request_timeout", 15))
    pacing = float(r.get("pacing_seconds", 0.4))
    days = int(r.get("youtube_recency_days", 7) or 0)
    sp = _SP_VIEWS.get(days if days in _SP_VIEWS else (7 if days > 0 else 0), "")
    queries = _youtube_queries(cfg)
    from urllib.parse import quote
    from yt_dlp import YoutubeDL
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
            "socket_timeout": max(10, int(timeout) * 2), "retries": 2,
            "geo_bypass": True, "playlistend": n}
    items, seen = [], set()
    for qi, query in enumerate(queries):
        url = "https://www.youtube.com/results?search_query=%s" % quote(query)
        if sp:
            url += "&sp=" + sp
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            entries = (info or {}).get("entries") or []
        except Exception as exc:
            log.warning("Radar youtube requete %s/%s echoue (%s): %s",
                        qi + 1, len(queries), query, exc)
            entries = []
        for e in entries:
            if not e or not e.get("id"):
                continue
            vid = str(e.get("id"))
            if vid in seen:
                continue
            seen.add(vid)
            items.append({
                "id": vid,
                "platform": "youtube",
                "title": (e.get("title") or "").strip(),
                "url": e.get("url") or e.get("webpage_url")
                or "https://www.youtube.com/watch?v=%s" % vid,
                "views": _int(e.get("view_count")),
                "duration": _float(e.get("duration")),
                "comments": 0,
                "author": e.get("channel") or e.get("uploader") or "",
                "source": "yt:" + query,
            })
        if qi < len(queries) - 1:
            time.sleep(pacing)
    log.info("Radar youtube: %s videos uniques sur %s requetes (recence<=%sj)",
             len(items), len(queries), days or 0)
    return items


def _collect_tiktok(cfg):
    """Pages tag publiques TikTok (best-effort, souvent protegees par JS)."""
    r = cfg.get("radar", {})
    tags = [str(t).strip().lstrip("#") for t in (r.get("tiktok_tags") or [])
            if str(t).strip()]
    timeout = float(r.get("request_timeout", 15))
    pacing = float(r.get("pacing_seconds", 0.4))
    items, seen = [], set()
    for tag in tags[:5]:
        resp = _http_get("https://www.tiktok.com/tag/%s" % tag, timeout=timeout)
        for user, vid in _TT_RE.findall(resp.text):
            if vid in seen:
                continue
            seen.add(vid)
            items.append({
                "id": vid,
                "platform": "tiktok",
                "title": "TikTok #%s" % tag,   # titre reel recupere a l'hydratation
                "url": "https://www.tiktok.com/@%s/video/%s" % (user, vid),
                "views": 0,
                "duration": 0.0,
                "comments": 0,
                "author": user,
                "source": "tiktok:tag:%s" % tag,
            })
        time.sleep(pacing)
    return items


def _collect_instagram(cfg):
    """Pages tag publiques Instagram (best-effort, tres souvent bloquees)."""
    r = cfg.get("radar", {})
    tags = [str(t).strip().lstrip("#") for t in (r.get("instagram_tags") or [])
            if str(t).strip()]
    timeout = float(r.get("request_timeout", 15))
    pacing = float(r.get("pacing_seconds", 0.4))
    items, seen = [], set()
    for tag in tags[:5]:
        resp = _http_get("https://www.instagram.com/explore/tags/%s/" % tag,
                         timeout=timeout)
        for code in _IG_RE.findall(resp.text):
            if code in seen:
                continue
            seen.add(code)
            items.append({
                "id": code,
                "platform": "instagram",
                "title": "Instagram #%s" % tag,  # titre reel a l'hydratation
                "url": "https://www.instagram.com/reel/%s/" % code,
                "views": 0,
                "duration": 0.0,
                "comments": 0,
                "author": "",
                "source": "instagram:tag:%s" % tag,
            })
        time.sleep(pacing)
    return items


# --------------------------------------------------------------------------
# Score, dedup, topics
# --------------------------------------------------------------------------

def _score(it):
    """Score de viralite heuristique par plateforme (plus grand = plus viral)."""
    views = _int(it.get("views"))
    comments = _int(it.get("comments"))
    platform = it.get("platform")
    if platform == "youtube":
        dur = _float(it.get("duration"))
        if dur > 0:
            return views / max(60.0, dur)      # meme echelle que scout.score_entry
        return float(views)
    if platform == "trends":
        return float(views)                     # trafic Google Trends ("200K+")
    if platform == "news":
        return 2000.0                           # present dans l'actu du jour
    if platform == "tiktok":
        return float(views) if views > 0 else 1000.0   # inconnu -> priorise hydration
    if platform == "instagram":
        return float(views) if views > 0 else 500.0
    if platform == "reddit":
        if views > 0:
            return float(views) + 0.5 * float(comments)
        # Flux RSS "hot" sans metriques : chaque entree est par definition un
        # post chaud -> meme plancher que "news" pour rester dans les topics.
        return 2000.0
    return float(views) + 0.5 * float(comments)  # hackernews


def _dedup_key(it):
    if it.get("platform") == "youtube" and it.get("id"):
        return "yt:" + str(it["id"])
    if it.get("id") and it.get("platform") in ("tiktok", "instagram"):
        return "%s:%s" % (it["platform"], it["id"])
    return "%s:%s" % (it.get("platform") or "?", _norm(it.get("title"))[:60])


def _build_topics(items, cap=25):
    """Sujets courts prets pour ideas/remake, plateformes video priorisees."""
    ordered = sorted(items, key=lambda x: (0 if x.get("platform") in VIDEO_PLATFORMS
                                           else 1,
                                           -_float(x.get("rank") or _score(x))))
    topics, seen = [], set()
    for it in ordered:
        title = _clean_title(it.get("title"))
        if len(title) < 12:
            continue
        key = _norm(title)[:40]
        if key in seen:
            continue
        seen.add(key)
        topics.append({"topic": title, "platform": it.get("platform"),
                       "url": it.get("url"), "views": _int(it.get("views")),
                       "score": round(_float(it.get("rank") or _score(it)), 1)})
        if len(topics) >= max(1, int(cap)):
            break
    return topics


# --------------------------------------------------------------------------
# Resultats persistants + points d'entree pour scout / remake / cli
# --------------------------------------------------------------------------

def latest_radar(config=None):
    """Dernier scan radar sauvegarde (dict vide si absent)."""
    cfg = config or get_config()
    paths = get_paths(cfg)
    return load_json(paths.radar / "latest.json", {}) or {}


def _load_history(paths, days):
    """Memoire anti-repetition {cle: {first, last, n}} purgee des vieux items."""
    hist = load_json(paths.radar / "history.json", {}) or {}
    if days and days > 0:
        cutoff = (dt.datetime.now() - dt.timedelta(days=int(days))).isoformat()
        hist = {k: v for k, v in hist.items()
                if str((v or {}).get("last") or "") >= cutoff}
    return hist


def _save_history(paths, hist):
    """Sauvegarde l'historique (garde-fou : 3000 cles max, les plus recentes)."""
    hist = dict(hist or {})
    if len(hist) > 3000:
        hist = dict(sorted(hist.items(),
                           key=lambda kv: str((kv[1] or {}).get("last") or ""),
                           reverse=True)[:3000])
    save_json(paths.radar / "history.json", hist)


def run_radar(config=None, force=False):
    """Scan complet du radar (toutes sources actives) -> data/radar/latest.json.

    - force=False : reutilise le cache si plus frais que radar.cache_ttl_minutes.
    - Chaque source est protegee par try/except (une panne ne casse jamais le scan).
    """
    started = time.time()
    cfg = config or get_config()
    paths = get_paths(cfg)
    r = cfg.get("radar", {}) or {}
    out_file = paths.radar / "latest.json"
    ttl = float(r.get("cache_ttl_minutes", 20))

    if not r.get("enabled", True):
        log.info("Radar desactive ([radar] enabled = false)")
        return load_json(out_file, None) or {"items": [], "topics": [], "sources": {}}

    if not force and ttl > 0:
        cached = load_json(out_file, None)
        age = _age_minutes((cached or {}).get("date")) if cached else None
        if age is not None and age < ttl:
            log.info("Radar: cache frais (%.1f min < %.0f min) -> %s items / %s topics",
                     age, ttl, len(cached.get("items", [])),
                     len(cached.get("topics", [])))
            return cached

    pacing = float(r.get("pacing_seconds", 0.4))
    sources, items = {}, []

    def _run(name, fn):
        try:
            got = fn()
            sources[name] = {"items": len(got), "error": None}
            log.info("Radar %s: %s items", name, len(got))
            items.extend(got)
        except Exception as exc:
            sources[name] = {"items": 0, "error": str(exc)}
            log.warning("Radar %s echoue: %s", name, exc)

    if r.get("reddit_subs", []) and r.get("reddit_enabled", True):
        _run("reddit", lambda: _collect_reddit(cfg))
    if r.get("hackernews_enabled", True):
        _run("hackernews", lambda: _collect_hackernews(cfg))
    if r.get("google_news_enabled", True):
        _run("google_news", lambda: _collect_google_news(cfg))
    if r.get("google_trends_enabled", True):
        _run("google_trends", lambda: _collect_google_trends(cfg))
    if r.get("youtube_enabled", True):
        _run("youtube", lambda: _collect_youtube(cfg))
    if r.get("tiktok_enabled", False):
        _run("tiktok", lambda: _collect_tiktok(cfg))
    if r.get("instagram_enabled", False):
        _run("instagram", lambda: _collect_instagram(cfg))

    # Dedup + score + NOUVEAUTE + caps bornes (jamais plus de items_cap items).
    seen, unique = set(), []
    for it in items:
        key = _dedup_key(it)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(it)
    # Anti-repetition : un item deja vu aux scans precedents perd 20% de score
    # par passage (plancher 0.1x) ; un item JAMAIS vu est booste -> le radar
    # met en avant du NOUVEAU contenu viral au lieu de tourner en boucle.
    hist = _load_history(paths, int(r.get("history_days", 45)))
    boost = max(1.0, _float(r.get("novelty_boost", 1.5)))
    now_iso = dt.datetime.now().isoformat()
    for it in unique:
        it["score"] = round(_score(it), 1)
        key = _dedup_key(it)
        prev = hist.get(key) or {}
        n = _int(prev.get("n")) + 1
        it["novelty"] = "nouveau" if n == 1 else "vu x%d" % n
        mult = boost if n == 1 else max(0.1, 1.0 - 0.2 * min(n, 4))
        it["rank"] = round(it["score"] * mult, 1)
        hist[key] = {"first": prev.get("first") or now_iso,
                     "last": now_iso, "n": n}
    _save_history(paths, hist)
    unique.sort(key=lambda x: (_float(x.get("rank")), _score(x)), reverse=True)
    items_cap = max(1, int(r.get("items_cap", 150)))
    # Reservation video : sans elle, les upvotes Reddit/HN (score eleve) ecrasent
    # les videos FRAICHES (vues plus basses) dans la course au items_cap ->
    # remake/radar_video_hits n'auraient presque rien a consommer.
    vids = [it for it in unique if it.get("platform") in VIDEO_PLATFORMS]
    rest = [it for it in unique if it.get("platform") not in VIDEO_PLATFORMS]
    video_quota = max(0, int(r.get("video_min_items", 40)))
    vids_keep = vids[:video_quota] if video_quota else vids
    unique = (vids_keep + rest)[:items_cap]
    spare = items_cap - len(unique)
    if spare > 0:
        unique += vids[len(vids_keep):len(vids_keep) + spare]
    log.info("Radar cap: %s videos video gardees (quota %s) + %s autres",
             len(vids_keep), video_quota, len(unique) - len(vids_keep))
    topics = _build_topics(unique, int(r.get("topics_cap", 25)))

    out = {"date": dt.datetime.now().isoformat(),
           "duration_s": round(time.time() - started, 1),
           "sources": sources, "items": unique, "topics": topics}
    save_json(out_file, out)
    log.info("Radar termine: %s items / %s topics en %.1fs -> %s",
             len(unique), len(topics), out["duration_s"], out_file)
    for platform, status in sources.items():
        if status.get("error"):
            log.info("  %s: %s", platform, status["error"])
    return out


def merge_radar_topics(existing_topics, cfg=None):
    """Fusionne les topics du radar dans une liste de topics scout existante.

    Les topics radar sont AJOUTES (jamais substitues) apres les topics YouTube,
    dedup par cle normalisee, borne par radar.topics_cap.
    """
    cfg = cfg or get_config()
    cap = max(1, int(cfg.get("radar", {}).get("topics_cap", 25)))
    out = [t for t in (existing_topics or []) if t.get("topic")]
    seen = {_norm(t["topic"])[:40] for t in out}
    for t in latest_radar(cfg).get("topics", []):
        key = _norm(t.get("topic") or "")[:40]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= cap:
            break
    return out


def radar_video_hits(cfg=None, max_hits=None):
    """Hits video du radar au format compatible remake.discover_hits.

    Uniquement les plateformes video (YouTube/TikTok/Instagram) avec une URL
    exploitable par yt-dlp. Les vues inconnues (TikTok/Instagram) restent a 0 :
    l'hydratation remake les remplira avec les vraies metadonnees.
    """
    cfg = cfg or get_config()
    if max_hits is None:
        max_hits = int(cfg.get("remake", {}).get("radar_hits", 10))
    hits = []
    for it in latest_radar(cfg).get("items", []):
        if it.get("platform") not in VIDEO_PLATFORMS:
            continue
        url = str(it.get("url") or "")
        if not url.startswith("http"):
            continue
        yt = _YT_RE.search(url)
        hits.append({
            "id": yt.group(1) if yt else str(it.get("id") or url),
            "title": (it.get("title") or "").strip(),
            "channel": it.get("author") or "",
            "views": _int(it.get("views")),
            "duration": _float(it.get("duration")),
            "url": url,
            "query": "radar:%s" % it.get("platform"),
            "score": _float(it.get("rank") or it.get("score")),
        })
    hits.sort(key=lambda h: -(h.get("score") or 0.0))
    return hits[:max(1, int(max_hits))]
    # NOTE : le tri ci-dessus conserve la priorite nouveaute (rank) de run_radar.