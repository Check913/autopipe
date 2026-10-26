"""YouTube trending detection via yt-dlp (no API keys)."""
import datetime as dt

from yt_dlp import YoutubeDL

from .config import get_config, get_paths
from .util import get_logger, save_json

log = get_logger("scout")


def _ydl_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 30,
        "retries": 2,
        "geo_bypass": True,
    }


def _map_entry(e, source):
    return {
        "id": e.get("id"),
        "title": e.get("title"),
        "channel": e.get("channel") or e.get("uploader"),
        "views": e.get("view_count"),
        "duration": e.get("duration"),
        "url": e.get("url") or e.get("webpage_url"),
        "source": source,
    }


def fetch_trending(region="US", count=25, lang="en"):
    opts = _ydl_opts()
    opts["playlistend"] = max(5, int(count))
    url = "https://www.youtube.com/feed/trending?gl=%s&hl=%s" % (region, lang)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = [e for e in (info or {}).get("entries") or [] if e]
    return [_map_entry(e, "trending") for e in entries]


def fetch_keyword(query, count=10):
    opts = _ydl_opts()
    opts["playlistend"] = max(5, int(count))
    url = "ytsearch%s:%s" % (int(count), query)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = [e for e in (info or {}).get("entries") or [] if e]
    return [_map_entry(e, "keyword:" + query) for e in entries]


def score_entry(e):
    """Simple score: views per second of video (relative attention)."""
    views = e.get("views") or 0
    dur = e.get("duration") or 0
    if dur and dur > 0:
        return views / max(60.0, float(dur))
    return float(views)


def pick_topics(scout_data, n=5):
    """Extracts n short topics from the best-scored titles."""
    topics = []
    seen = set()
    for e in scout_data.get("top", []):
        title = (e.get("title") or "").strip()
        if not title or len(title) < 12:
            continue
        key = title.lower()[:40]
        if key in seen:
            continue
        seen.add(key)
        topics.append({"topic": title, "url": e.get("url"), "views": e.get("views"),
                       "score": round(score_entry(e), 1)})
        if len(topics) >= n:
            break
    return topics


def run_scout(config=None, keywords=None):
    cfg = config or get_config()
    paths = get_paths(cfg)
    region = cfg["general"].get("region", "US")
    language = cfg["general"].get("language", "en")
    data = {"date": dt.datetime.now().isoformat(), "region": region,
            "language": language,
            "trending": [], "keywords": {}, "top": []}
    try:
        data["trending"] = fetch_trending(region, cfg["scout"].get("trending_count", 25),
                                          language)
        log.info("Trending: %s videos", len(data["trending"]))
    except Exception as exc:
        log.warning("Trending unavailable: %s", exc)
    for kw in keywords or cfg["scout"].get("keywords", []):
        try:
            data["keywords"][kw] = fetch_keyword(kw, 10)
            log.info("Keyword '%s': %s results", kw, len(data["keywords"][kw]))
        except Exception as exc:
            log.warning("Search '%s' failed: %s", kw, exc)
    flat = list(data["trending"]) + [e for lst in data["keywords"].values() for e in lst]
    flat.sort(key=score_entry, reverse=True)
    data["top"] = flat[:30]
    data["topics"] = pick_topics(data)
    # Fusion des topics du radar multi-plateformes (Reddit/Hacker News/
    # Google News/Trends/TikTok...) : AJOUT apres les topics YouTube, dedup,
    # jamais fatal (une panne radar ne casse pas le scout).
    try:
        from .radar import merge_radar_topics
        before = len(data["topics"])
        data["topics"] = merge_radar_topics(data["topics"], cfg)
        if len(data["topics"]) > before:
            log.info("Radar topics merges: +%s sujets externes",
                     len(data["topics"]) - before)
    except Exception as exc:
        log.warning("Fusion topics radar ignoree: %s", exc)
    out = paths.scout / "latest.json"
    save_json(out, data)
    log.info("Scout finished -> %s", out)
    return data
