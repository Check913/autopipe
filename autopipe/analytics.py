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
    print("%-15s %8s %7s %8s  %s" % ("ID", "VUES", "LIKES", "COMM.", "TITRE"))
    for r in rows[:20]:
        print("%-15s %8s %7s %8s  %s" % (
            r["id"], r.get("views", 0), r.get("likes", 0),
            r.get("comments", 0), (r.get("title") or "")[:60]))
    total = sum(r.get("views", 0) for r in rows)
    print("Total vues: %s sur %s videos" % (total, len(rows)))
    return rows
