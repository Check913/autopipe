"""Stock video clip downloads via the Pixabay or Pexels API."""
import shutil

import requests

from .config import get_config, get_paths
from .util import get_logger, save_json

log = get_logger("fetch_media")
PEXELS_SEARCH = "https://api.pexels.com/videos/search"
PIXABAY_SEARCH = "https://pixabay.com/api/videos/"

# Mots qui rendent une recherche stock inutilisable. Exemple reel du log du
# 2026-10-23: "Ariana Grande - true story (lyric visualizer)" doit devenir
# "ariana grande true story", sinon 0 clip ou clips hors sujet.
GENERIC_WORDS = {"shorts", "short", "video", "official", "music", "lyric",
                 "visualizer", "hd", "4k", "feat"}


def _clean_query(text, max_words=4):
    """Reduit un titre/brut en quelques mots-cles propres pour la recherche stock."""
    words = []
    for word in str(text or "").split():
        letters = "".join(ch for ch in word if ch.isalnum())
        if not letters or letters.lower() in GENERIC_WORDS:
            continue
        words.append(letters.lower())
        if len(words) >= max_words:
            break
    return " ".join(words)


def _clean_queries(raw_queries, fallback=("nature", "city", "technology")):
    cleaned = []
    for q in raw_queries or []:
        cq = _clean_query(q)
        if cq and cq not in cleaned:
            cleaned.append(cq)
    return cleaned or list(fallback)


def search_pexels(query, api_key, per_page=5, orientation="portrait"):
    resp = requests.get(
        PEXELS_SEARCH,
        params={"query": query, "per_page": per_page, "orientation": orientation},
        headers={"Authorization": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("videos", [])


def search_pixabay(query, api_key, per_page=6, min_width=720):
    resp = requests.get(
        PIXABAY_SEARCH,
        params={
            "key": api_key,
            "q": query,
            "lang": "en",
            "video_type": "film",
            "safesearch": "true",
            "per_page": per_page,
            "min_width": min_width,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    # Bug corrige (diagnostic 2026-10-23) : l'API Pixabay renvoie les
    # resultats sous "hits" (comme l'API images), pas sous "videos".
    # Lire "videos" donnait toujours 0 resultat meme quand la connexion
    # marchait -> "Clips ready (pixabay): 0/6" a chaque run.
    return data.get("hits") or data.get("videos") or []


def pick_file(video, min_width=720):
    """Picks a portrait mp4 file, wide enough but not huge. (Pexels)"""
    files = [
        f for f in video.get("video_files", [])
        if f.get("file_type") == "video/mp4" and (f.get("width") or 0) >= min_width
    ]
    if not files:
        return None
    portrait = [f for f in files if (f.get("height") or 0) >= (f.get("width") or 0)]
    pool = portrait or files
    pool.sort(key=lambda f: (f.get("width") or 0) * (f.get("height") or 0))
    return pool[0]


def pick_pixabay_file(video, min_width=720):
    """Picks the smallest Pixabay rendition wide enough, portrait first.

    The Pixabay video API has no orientation filter, so portrait selection is
    done client side. When no rendition reaches min_width the largest
    available rendition is used so the pipeline still gets footage.
    """
    renditions = [
        r for r in (video.get("videos") or {}).values()
        if r.get("url") and (r.get("width") or 0) > 0
    ]
    if not renditions:
        return None
    big = [r for r in renditions if (r.get("width") or 0) >= min_width]
    if big:
        pool = list(big)
        portrait = [r for r in pool if (r.get("height") or 0) >= (r.get("width") or 0)]
        pool = portrait or pool
        pool.sort(key=lambda r: (r.get("width") or 0) * (r.get("height") or 0))
        return pool[0]
    return max(renditions, key=lambda r: (r.get("width") or 0) * (r.get("height") or 0))


def download(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            shutil.copyfileobj(r.raw, fh)
    tmp.replace(dest)


def run_fetch(queries, config=None, per_video=None):
    """Downloads up to N clips (local cache). Returns [] without an API key.

    Provider selection ([fetch_media] provider):
      - "auto" (default): Pixabay when a pixabay key is set, else Pexels
      - "pixabay" / "pexels": forces the provider
    """
    cfg = config or get_config()
    paths = get_paths(cfg)
    fm_cfg = cfg["fetch_media"]
    provider = (fm_cfg.get("provider") or "auto").strip().lower()
    pexels_key = fm_cfg.get("pexels_api_key") or ""
    pixabay_key = fm_cfg.get("pixabay_api_key") or ""
    if provider in ("", "auto"):
        provider = "pixabay" if pixabay_key else "pexels"
    if provider == "pixabay":
        if not pixabay_key:
            log.warning(
                "No Pixabay key (fetch_media.pixabay_api_key) -> gradient background used")
            return []
        return _run_fetch_pixabay(queries, cfg, paths, pixabay_key, per_video)
    if provider != "pexels":
        log.warning("Unknown fetch_media.provider %r -> auto", provider)
    if not pexels_key:
        log.warning(
            "No Pexels key (fetch_media.pexels_api_key) -> gradient background used")
        return []
    return _run_fetch_pexels(queries, cfg, paths, pexels_key, per_video)


def _run_fetch_pexels(queries, cfg, paths, api_key, per_video):
    target = per_video or int(cfg["fetch_media"].get("clips_per_video", 6))
    min_width = int(cfg["fetch_media"].get("min_width", 720))
    queries = _clean_queries(queries)
    clips = []
    seen_ids = set()
    for q in queries:
        if len(clips) >= target:
            break
        try:
            videos = search_pexels(q, api_key, per_page=6)
        except Exception as exc:
            log.warning("Pexels search '%s' failed: %s", q, exc)
            continue
        for v in videos:
            if len(clips) >= target:
                break
            if v.get("id") in seen_ids:
                continue
            f = pick_file(v, min_width)
            if not f:
                continue
            seen_ids.add(v.get("id"))
            dest = paths.video_cache / ("pexels_%s_%dx%d.mp4" % (
                v["id"], f.get("width") or 0, f.get("height") or 0))
            if not dest.exists():
                try:
                    download(f["link"], dest)
                    log.info("Clip downloaded: %s (%s)", dest.name, q)
                except Exception as exc:
                    log.warning("Download %s failed: %s", f["link"], exc)
                    continue
            clips.append({
                "path": str(dest),
                "query": q,
                "source": v.get("url"),
                "duration": v.get("duration"),
            })
    save_json(paths.cache / "media_clips.json", clips)
    log.info("Clips ready (pexels): %s/%s", len(clips), target)
    return clips


def _run_fetch_pixabay(queries, cfg, paths, api_key, per_video):
    target = per_video or int(cfg["fetch_media"].get("clips_per_video", 6))
    min_width = int(cfg["fetch_media"].get("min_width", 720))
    queries = _clean_queries(queries)
    clips = []
    seen_ids = set()
    for q in queries:
        if len(clips) >= target:
            break
        try:
            videos = search_pixabay(q, api_key, per_page=6, min_width=min_width)
        except Exception as exc:
            log.warning("Pixabay search '%s' failed: %s", q, exc)
            continue
        log.info("Pixabay '%s': %s videos", q, len(videos))
        for v in videos:
            if len(clips) >= target:
                break
            if v.get("id") in seen_ids:
                continue
            f = pick_pixabay_file(v, min_width)
            if not f:
                continue
            seen_ids.add(v.get("id"))
            dest = paths.video_cache / ("pixabay_%s_%dx%d.mp4" % (
                v["id"], f.get("width") or 0, f.get("height") or 0))
            if not dest.exists():
                try:
                    download(f["url"], dest)
                    log.info("Clip downloaded: %s (%s)", dest.name, q)
                except Exception as exc:
                    log.warning("Download %s failed: %s", f["url"], exc)
                    continue
            clips.append({
                "path": str(dest),
                "query": q,
                "source": v.get("pageURL"),
                "duration": v.get("duration"),
            })
    save_json(paths.cache / "media_clips.json", clips)
    log.info("Clips ready (pixabay): %s/%s", len(clips), target)
    return clips
