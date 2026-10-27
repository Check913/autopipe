"""Module 1 - Extracteur de patterns visuels des Shorts viraux (OpenCV).

Telecharge les N premieres secondes des hits viraux identifies par le
Super-Remake / le Radar, puis extrait avec OpenCV :
  - le rythme des coupes (jump cuts) dans les premieres secondes,
  - la vitesse de zoom (flux optique radial median, en fraction de cadre/s),
  - la palette de couleurs dominante (k-means, hex + poids),
  - la zone d'affichage des sous-titres (tiers haut / milieu / bas),
  - luminosite, contraste et intensite de mouvement moyens.

Ces patterns sont convertis en parametres de montage concrets consommes
par autopipe.video (couleurs du degrade de secours, duree du hook, zoom
progressif, nombre de clips) et par le Super-Remake (directives de prompt).

Robustesse : si OpenCV est absent ou si un telechargement echoue, le
module se degrade (avertissement + skip) sans jamais casser le pipeline.
"""
import copy
import datetime as dt
import re
import statistics
from pathlib import Path

import numpy as np

from .config import get_config, get_paths
from .util import get_logger, load_json, save_json

log = get_logger("visual")

_VIDEO_ID_RE = re.compile(r"(?:v=|shorts/|youtu\.be/|embed/)([\w-]{6,})")

# Zones de sous-titres : (nom, borne_haute, borne_basse) evaluees par tiers.
_THIRDS = (("top", 0.0, 1.0 / 3.0), ("middle", 1.0 / 3.0, 2.0 / 3.0),
           ("bottom", 2.0 / 3.0, 1.0))


def extract_video_id(url):
    """Identifiant video stable pour nommer les artefacts (URL quelconque)."""
    m = _VIDEO_ID_RE.search(str(url or ""))
    if m:
        return m.group(1)
    return re.sub(r"[^\w-]", "_", str(url or "hit"))[:40] or "hit"


def _cv2():
    """Retourne le module cv2 ou None (mode degrade sans OpenCV)."""
    try:
        import cv2
        return cv2
    except Exception:
        return None


def _ydl_opts(cfg, dest_dir):
    """Options yt-dlp : petit format + neutralisation du proxy systeme.

    En proxy_mode="direct" on impose proxy="" (meme correctif que
    remake._ydl_opts) sinon yt-dlp echoue sur le proxy du registre Windows.
    """
    max_h = max(240, int(cfg.get("visual", {}).get("download_max_height", 720)))
    # Fix 2026-10-27 : YouTube ne sert plus de flux progressif mp4 unique
    # pour la plupart des Shorts (SABR/DASH) -> "best[ext=mp4]" renvoyait
    # "Requested format is not available". On passe par des flux separes
    # (video+audio) fusionnes par ffmpeg, avec replis progressives.
    opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": str(Path(dest_dir) / "%(id)s.%(ext)s"),
        "format": ("bv*[height<=%d][ext=mp4]+ba[ext=m4a]/"
                   "b[height<=%d][ext=mp4]/b[height<=%d]/"
                   "bv*[height<=%d]+ba/bv*+ba/b") % (
                       max_h, max_h, max_h, max_h),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "overwrites": True,
    }
    if cfg.get("network", {}).get("proxy_mode", "direct") == "direct":
        opts["proxy"] = ""
    return opts


def download_first_seconds(url, dest_dir, cfg=None, seconds=None):
    """Telecharge les `seconds` premieres secondes d'un Short -> chemin ou None.

    Utilise download_ranges de yt-dlp (coupe cote serveur si possible) avec
    repli sur un telechargement complet (les Shorts font <= 75 s).
    """
    if _cv2() is None:
        log.warning("OpenCV absent -> Module 1 (visual) desactive")
        return None
    cfg = cfg or get_config()
    seconds = float(seconds or cfg.get("visual", {}).get("analyze_seconds", 3.0))
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in dest_dir.iterdir()}

    def _ranges(_info, _ydl):
        return [{"start_time": 0.0, "end_time": seconds}]

    opts = _ydl_opts(cfg, dest_dir)
    opts.update({"download_ranges": _ranges, "force_keyframes_at_cuts": True})
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        log.warning("Segment 0-%.1fs echec (%s) -> telechargement complet",
                    seconds, exc)
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL(_ydl_opts(cfg, dest_dir)) as ydl:
                ydl.download([url])
        except Exception as exc2:
            log.warning("Telechargement impossible pour %s: %s", url, exc2)
            return None
    exts = (".mp4", ".webm", ".mkv", ".mov")
    new = [p for p in dest_dir.iterdir()
           if p.name not in before and p.suffix.lower() in exts
           and not p.name.endswith(".part")]
    if not new:
        return None
    return max(new, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# Analyse OpenCV
# ---------------------------------------------------------------------------

def _read_frames(cv2, path, fps_target=10.0, max_frames=60):
    """Echantillonne uniformement jusqu'a max_frames cadres (RGB numpy)."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return [], 0.0
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(src_fps / fps_target)))
    frames, idx = [], 0
    while len(frames) < max_frames:
        ok = cap.grab()
        if not ok:
            break
        if idx % step == 0:
            ok, frame = cap.read()
            if not ok:
                break
            frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        idx += 1
    cap.release()
    return frames, src_fps


def _hist_vec(cv2, frame):
    """Vecteur de histogramme HSV reduit (30 teintes) pour detecter les coupes."""
    small = cv2.resize(frame, (160, 284))
    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)
    hist = cv2.calcHist([hsv], [0], None, [30], [0, 180])
    cv2.normalize(hist, hist)
    return hist.flatten()


def cut_times(frames, threshold=0.40):
    """Indices (en s) des coupes franches : distance chi2 entre histograms.

    Une chute brutale de similarite entre cadres consecutifs = jump cut.
    """
    cuts = []
    prev = None
    for i, f in enumerate(frames):
        h = _hist_vec(_cv2(), f)
        if prev is not None:
            d = 0.5 * float(np.sum((prev - h) ** 2 / (prev + h + 1e-9)))
            if d > threshold:
                cuts.append(round(i * 0.1, 2))  # echantillonnage a 10 fps
        prev = h
    return cuts


def zoom_ratio(frames, cv2):
    """Vitesse de zoom median : composante radiale du flux optique (frac/s).

    >0.06 = zoom-in rapide typique des hooks viraux ; ~0 = plan fixe.
    """
    if len(frames) < 2:
        return 0.0
    grays = [cv2.cvtColor(cv2.resize(f, (160, 284)), cv2.COLOR_RGB2GRAY)
             for f in frames]
    radials = []
    cy, cx = 142.0, 80.0
    for a, b in zip(grays, grays[1:]):
        flow = cv2.calcOpticalFlowFarneback(
            a, b, None, 0.5, 2, 15, 2, 5, 1.2, 0)
        fx, fy = flow[..., 0], flow[..., 1]
        yy, xx = np.mgrid[0:284, 0:160]
        rad = (xx - cx) * fx + (yy - cy) * fy
        radials.append(float(np.median(rad)) / max(1.0, np.hypot(cx, cy)))
    return round(statistics.median(radials), 4)


def extract_palette(frames, cv2, k=4):
    """Palette dominante k-means -> liste [{hex, weight}] triee par poids."""
    if not frames:
        return []
    small = cv2.resize(frames[len(frames) // 2], (80, 142))
    pixels = small.reshape(-1, 3).astype(np.float32)
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    _ret, labels, centers = cv2.kmeans(
        pixels, k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.flatten(), minlength=k)
    order = np.argsort(-counts)
    out = []
    for i in order:
        r, g, b = (int(round(c)) for c in centers[i])
        out.append({"hex": "#%02x%02x%02x" % (r, g, b),
                    "weight": round(float(counts[i]) / counts.sum(), 3)})
    return out


def subtitle_zone(frames, cv2):
    """Zone (tiers vertical) ou se concentrent les gros textes de sous-titres.

    Heuristique : contours a fort gradient dans la bande horizontale centrale
    (lettres), ponderee par la zone. Renvoie le nom du tier dominant.
    """
    scores = {name: 0.0 for name, _, _ in _THIRDS}
    for f in frames[-6:] or frames:
        small = cv2.resize(f, (160, 284))
        gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 80, 180)
        band = edges[70:214, 8:152]  # bande utile (evite bords/UI)
        col = band.sum(axis=1) / 255.0
        total = float(col.sum()) or 1.0
        for name, top, bottom in _THIRDS:
            lo, hi = int(top * 144), int(bottom * 144)
            scores[name] += float(col[lo:hi].sum()) / total
    n = max(1, len(frames[-6:] or frames))
    best = max(scores, key=scores.get)
    return {"zone": best, "confidence": round(scores[best] / n, 3)}


def analyze_file(path, cfg=None):
    """Analyse complete d'un fichier video -> dict de metriques visuelles."""
    cv2 = _cv2()
    if cv2 is None:
        return None
    frames, src_fps = _read_frames(cv2, path)
    if not frames:
        return None
    cuts = cut_times(frames, threshold=float(
        cfg.get("visual", {}).get("cut_threshold", 0.40)) if cfg else 0.40)
    zoom = zoom_ratio(frames, cv2)
    palette = extract_palette(frames, cv2)
    sub = subtitle_zone(frames, cv2)
    gray_mid = cv2.cvtColor(cv2.resize(
        frames[len(frames) // 2], (160, 284)), cv2.COLOR_RGB2GRAY)
    return {
        "duration_analyzed_s": round(len(frames) / 10.0, 2),
        "cuts": cuts,
        "cuts_per_second": round(len(cuts) / max(0.1, len(frames) / 10.0), 2),
        "zoom_ratio": zoom,
        "palette": palette,
        "subtitle": sub,
        "brightness": round(float(gray_mid.mean()) / 255.0, 3),
        "contrast": round(float(gray_mid.std()) / 255.0, 3),
    }


def analyze_hit_visual(url, cfg=None, hit=None):
    """Pipeline complet pour 1 hit : download 3s + analyse + JSON dans data/visual."""
    cv2 = _cv2()
    if cv2 is None:
        return None
    cfg = cfg or get_config()
    paths = get_paths(cfg)
    vdir = paths.visual
    vid = extract_video_id(url)
    out_path = vdir / ("visual_%s.json" % vid)
    cached = load_json(out_path)
    max_age = float(cfg.get("visual", {}).get("cache_hours", 168))
    if cached and (dt.datetime.now() - dt.datetime.fromisoformat(
            cached.get("fetched", "2000-01-01T00:00:00"))).total_seconds() < max_age * 3600:
        return cached
    local = download_first_seconds(url, vdir / "src", cfg=cfg)
    if not local:
        return cached  # repli : cache eventuel, sinon None
    metrics = analyze_file(local, cfg=cfg)
    if metrics is None:
        return cached
    out = {
        "id": vid,
        "url": url,
        "title": (hit or {}).get("title", ""),
        "views": (hit or {}).get("views", 0),
        "fetched": dt.datetime.now().isoformat(timespec="seconds"),
        **metrics,
    }
    save_json(out_path, out)
    try:
        local.unlink()  # on ne garde pas la video source
    except OSError:
        pass
    return out


def analyze_top_hits(cfg=None, hits=None, max_videos=None):
    """Analyse les N meilleurs hits (par vues) -> {id: metriques}.

    `hits` : liste [{id, url, title, views, duration}] (ex. discover_hits).
    """
    cv2 = _cv2()
    if cv2 is None:
        return {}
    cfg = cfg or get_config()
    max_videos = int(max_videos or cfg.get("visual", {}).get("max_videos", 5))
    if not hits:
        try:
            from .remake import discover_hits
            hits = discover_hits(cfg).get("hits", [])
        except Exception as exc:
            log.warning("discover_hits indisponible: %s", exc)
            return {}
    ranked = sorted(hits, key=lambda h: int(h.get("views") or 0), reverse=True)
    results = {}
    for h in ranked[:max_videos]:
        url = h.get("url") or "https://www.youtube.com/watch?v=%s" % h["id"]
        try:
            m = analyze_hit_visual(url, cfg=cfg, hit=h)
        except Exception as exc:
            log.warning("Analyse visuelle %s echouee: %s", h.get("id"), exc)
            m = None
        if m:
            results[h["id"]] = m
    return results


# ---------------------------------------------------------------------------
# Application des patterns au moteur de rendu (graft non-cassant)
# ---------------------------------------------------------------------------

def aggregate_patterns(metrics_list):
    """Moyenne les metriques de plusieurs hits -> pattern dominant."""
    ok = [m for m in metrics_list if m]
    if not ok:
        return None
    n = len(ok)
    all_cuts = [c for m in ok for c in m.get("cuts", [])]
    cps = [m.get("cuts_per_second", 0.0) for m in ok]
    zooms = [m.get("zoom_ratio", 0.0) for m in ok]
    zones = {}
    for m in ok:
        z = (m.get("subtitle") or {}).get("zone", "middle")
        zones[z] = zones.get(z, 0) + 1
    weights = {}
    for m in ok:
        for p in m.get("palette", []):
            weights[p["hex"]] = weights.get(p["hex"], 0.0) + p["weight"]
    top_colors = sorted(weights.items(), key=lambda kv: -kv[1])[:2]
    return {
        "n_samples": n,
        "cuts_per_second": round(sum(cps) / n, 2),
        "cut_interval_s": (round(statistics.median(
            [b - a for a, b in zip(all_cuts, all_cuts[1:])]), 2)
            if len(all_cuts) > 1 else None),
        "zoom_ratio": round(sum(zooms) / n, 4),
        "subtitle_zone": max(zones, key=zones.get),
        "dominant_colors": [c for c, _ in top_colors],
        "brightness": round(sum(m.get("brightness", 0.5) for m in ok) / n, 3),
        "contrast": round(sum(m.get("contrast", 0.2) for m in ok) / n, 3),
    }


def apply_to_video_cfg(cfg, patterns=None):
    """Injecte les patterns viraux dans cfg["video"] (mutation en place).

    Non-cassant : ne touche que les cles deja utilisees par video.py
    (hook_duration, hook_font_size, gradient_colors, clips_per_video) et
    seulement si [visual] enabled. Renvoie le pattern applique ou None.
    """
    if not cfg.get("visual", {}).get("enabled", False):
        return None
    if patterns is None:
        paths = get_paths(cfg)
        patterns = load_json(paths.visual / "patterns_latest.json")
    if not patterns:
        return None
    v = cfg.setdefault("video", {})
    cps = float(patterns.get("cuts_per_second") or 0.0)
    if cps > 0:
        # duree moyenne entre coupes -> duree cible d'un clip.
        # NB: la cle consommee par fetch_media.run_fetch est
        # cfg["fetch_media"]["clips_per_video"] (et non video.*)
        interval = 1.0 / max(0.2, cps)
        cfg.setdefault("fetch_media", {})["clips_per_video"] = max(3, min(12, int(round(
            float(v.get("target_duration", 35) or 35) / interval))))
        v["hook_duration"] = max(1.5, min(4.0, round(interval * 1.5, 1)))
    zoom = float(patterns.get("zoom_ratio") or 0.0)
    if zoom > 0.05:  # zoom viral detecte -> zoom progressif active
        v["zoom_effect"] = "in"
        v["zoom_strength"] = round(min(0.35, zoom * 4.0), 2)
    colors = patterns.get("dominant_colors") or []
    if len(colors) >= 2:
        v["gradient_colors"] = [colors[0], colors[1]]
    zone = patterns.get("subtitle_zone")
    if zone == "top":
        v["subtitle_position"] = "top"
    elif zone == "bottom":
        v["subtitle_position"] = "bottom"
    else:
        v["subtitle_position"] = "middle"
    log.info("Visual: patterns appliques au rendu (cps=%s zoom=%s zone=%s)",
             cps, zoom, zone)
    return patterns


def save_patterns_latest(cfg=None, patterns=None):
    """Sauvegarde le pattern agrege comme reference courante."""
    cfg = cfg or get_config()
    paths = get_paths(cfg)
    if patterns:
        save_json(paths.visual / "patterns_latest.json", patterns)
    return patterns
