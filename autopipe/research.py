# -*- coding: utf-8 -*-
"""MODULE 4 - Research continue : benchmark QUANTITATIF de la niche.

Demande utilisateur (2026-10-29) : "scrape le web en continue pour
s'ameliorer a chaque short ... les videos qui ont eu des succes, chaque
detail, pour produire des shorts que personne ne pourrait passer sans
s'arreter".

Positionnement vs les modules existants (pas de duplication) :
  - radar.py     : TROUVE des videos virales a remaker (sujets).
  - scout.py     : tendances YouTube generales (sujets).
  - trend_arbiter: momentum Google Trends x saturation (sujets).
  - research.py  : MESURE COMMENT les gagnants gagnent : durees exactes,
                   patterns de titres, familles de hooks, velocite de vues
                   des canaux rivaux -> chiffres injectes dans les prompts.

Sources (yt-dlp flat, aucune cle API) :
  - ytsearch{n} "{kw} #shorts" par mot-cle de niche
  - onglet /shorts des canaux rivaux (competitors dans [research])

Sorties :
  - data/research/insights.json          (benchmarks actuels, TTL cache)
  - data/research/history.jsonl          (evolution jour apres jour)
Consommateurs :
  - scheduler step 1.6   -> run_research()  (scrape, TTL)
  - scheduler step 1.95  -> inject_directive() (apres l'optimizer hook qui
    ecrase style_directive : l'ordre garantit que lecons apprises ET
    chiffres du web cohabitent dans le prompt)
  - cli `research`       -> rapport lisible
Jamais fatal : toute panne de source degrade la directive, pas le pipeline.
"""
import datetime as dt
import json
import statistics
import time
import traceback

from .config import get_config, get_paths
from .util import get_logger, load_json, save_json

log = get_logger("research")


def _ydl_opts(cfg=None):
    """Options yt-dlp (meme contrat que remake : proxy direct force)."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 30,
        "retries": 2,
        "geo_bypass": True,
    }
    # Uniquement si un mode explicite est configure (cfg None -> neutre,
    # le cfg reel du pipeline a toujours network.proxy_mode).
    mode = str(((cfg or {}).get("network") or {}).get("proxy_mode") or "")
    if mode == "direct":
        opts["proxy"] = ""
    # [research].ydl_extra : options supplementaires (dict TOML).
    for key, value in ((cfg or {}).get("research", {}).get("ydl_extra") or {}).items():
        opts[str(key)] = value
    return opts


def _entries(url, count, cfg=None):
    """Extraction flat d'une playlist/recherche (jamais d'exception)."""
    from yt_dlp import YoutubeDL
    opts = _ydl_opts(cfg)
    opts["playlistend"] = max(5, int(count))
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        out = []
        for e in (info.get("entries") or []):
            if not e:
                continue
            views = e.get("view_count")
            dur = e.get("duration")
            try:
                views = int(views) if views is not None else None
            except (TypeError, ValueError):
                views = None
            try:
                dur = float(dur) if dur is not None else None
            except (TypeError, ValueError):
                dur = None
            out.append({
                "id": e.get("id"),
                "title": str(e.get("title") or "")[:120],
                "channel": e.get("channel") or e.get("uploader") or "",
                "views": views,
                "duration": dur,
                # upload_date (YYYYMMDD) ou timestamp : selon la source,
                # l'un des deux peut manquer -> velocite calculee si dispo.
                "upload_date": e.get("upload_date") or "",
                "timestamp": e.get("timestamp"),
            })
        return out
    except Exception:
        log.warning("Research source inatteignable (%s):\n%s", url[:80],
                    traceback.format_exc())
        return []


def _collect(cfg):
    """Scrape mot-cles + concurrents. Renvoie (entries, sources_ok)."""
    rc = cfg.get("research", {}) or {}
    keywords = [str(k) for k in (rc.get("keywords") or
                                 (cfg.get("scout", {}) or {}).get("keywords") or [])
                if str(k).strip()]
    per_kw = int(rc.get("search_per_keyword", 15) or 15)
    comp_max = int(rc.get("competitor_max", 20) or 20)
    pacing = float(rc.get("pacing_seconds", 0.6) or 0.6)

    entries, sources_ok = [], 0
    for kw in keywords:
        es = _entries("ytsearch%d:%s #shorts" % (per_kw, kw), per_kw, cfg)
        if es:
            sources_ok += 1
            for e in es:
                e["kw"] = kw
        entries += es
        time.sleep(pacing)

    for comp in (rc.get("competitors") or []):
        url = _competitor_url(comp)
        if not url:
            continue
        es = _entries(url, comp_max, cfg)
        if es:
            sources_ok += 1
            for e in es:
                e["kw"] = "competitor"
        entries += es
        time.sleep(pacing)
    return entries, sources_ok


def _competitor_url(comp):
    """Normalise un concurrent (@handle, UCxxxx, url) en url /shorts."""
    c = str(comp or "").strip()
    if not c:
        return None
    if c.startswith("@"):
        return "https://www.youtube.com/%s/shorts" % c
    if c.startswith("UC"):
        return "https://www.youtube.com/channel/%s/shorts" % c
    if c.startswith("http"):
        return c if "/shorts" in c else c.rstrip("/") + "/shorts"
    return "https://www.youtube.com/@%s/shorts" % c


def _entry_ts(e):
    """Epoch (ou None) d'un entry flat : timestamp ou upload_date."""
    ts = e.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        return float(ts)
    raw = str(e.get("upload_date") or "")
    if len(raw) == 8 and raw.isdigit():
        try:
            return dt.datetime.strptime(raw, "%Y%m%d").replace(
                tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            return None
    return None


def _winners(entries, cfg):
    """Echantillon 'gagnant' : vues >= min_views, sinon top_n par vues."""
    rc = cfg.get("research", {}) or {}
    min_views = int(rc.get("min_views", 100000) or 100000)
    top_n = int(rc.get("top_n", 20) or 20)
    seen = set()
    unique = []
    for e in entries:
        eid = str(e.get("id") or "")
        if not eid or eid in seen or not e.get("views"):
            continue
        seen.add(eid)
        unique.append(e)
    winners = [e for e in unique if int(e["views"]) >= min_views]
    if not winners:
        # Petite niche au demarrage (AUCUN gagnant qualifie) : top_n par
        # vues vaut mieux que rien. Si au moins un qualifie existe, on
        # garde l'echantillon qualifie tel quel (pas de remplissage).
        winners = sorted(unique, key=lambda e: int(e["views"]),
                         reverse=True)[:top_n]
    return sorted(winners, key=lambda e: int(e["views"]), reverse=True)[:top_n]


def _pct(nums, q):
    if not nums:
        return None
    try:
        return round(float(statistics.quantiles(nums, n=100)[q - 1]), 1)
    except (statistics.StatisticsError, ValueError, IndexError):
        return round(float(sorted(nums)[min(len(nums) - 1, max(0, q - 1))]), 1)


def _median(nums):
    if not nums:
        return None
    try:
        return round(float(statistics.median(nums)), 1)
    except (statistics.StatisticsError, ValueError):
        return None


def _analyze(entries, cfg):
    """Benchmarks des gagnants (durees, titres, hooks, velocite)."""
    from .identity import hook_family
    winners = _winners(entries, cfg)
    now_ts = time.time()
    durations = [float(e["duration"]) for e in winners
                 if e.get("duration") and 0 < float(e["duration"]) <= 180]
    title_lens = [len(str(e.get("title") or "")) for e in winners
                  if e.get("title")]
    fams = {}
    for e in winners:
        if e.get("title"):
            f = hook_family(e["title"])
            fams[f] = fams.get(f, 0) + 1
    fam_total = sum(fams.values()) or 1
    velocities = []
    for e in winners:
        ts = _entry_ts(e)
        if not ts:
            continue
        age_days = max(0.5, (now_ts - ts) / 86400.0)
        velocities.append(int(e["views"]) / age_days)

    doc = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "n_scraped": len(entries),
        "n_winners": len(winners),
        "duration_median_s": _median(durations),
        "duration_p25_s": _pct(durations, 25),
        "duration_p75_s": _pct(durations, 75),
        "title_len_median": _median([float(x) for x in title_lens]),
        "pct_number_title": round(100.0 * sum(
            1 for e in winners if any(c.isdigit() for c in str(e.get("title") or ""))
        ) / (len(winners) or 1), 1),
        "pct_question_title": round(100.0 * sum(
            1 for e in winners if str(e.get("title") or "").strip().endswith("?")
        ) / (len(winners) or 1), 1),
        "hook_families_pct": {
            f: round(100.0 * n / fam_total, 1) for f, n in
            sorted(fams.items(), key=lambda kv: -kv[1])},
        "velocity_median_vpd": int(_median(velocities) or 0) if velocities else None,
        "top_titles": [{"title": e.get("title"), "views": e.get("views"),
                        "duration": e.get("duration")}
                       for e in winners[:5]],
    }
    return doc, winners


def _insights_file(cfg):
    return get_paths(cfg).research / "insights.json"


def _fresh(cfg, path):
    """True si insights.json est plus frais que research.cache_hours."""
    rc = cfg.get("research", {}) or {}
    try:
        ttl_h = float(rc.get("cache_hours", 12) or 12)
    except (TypeError, ValueError):
        ttl_h = 12.0
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        gen = dt.datetime.fromisoformat(str(raw.get("generated_at") or ""))
        age = dt.datetime.now() - gen
        return age < dt.timedelta(hours=ttl_h)
    except Exception:
        return False


def run_research(cfg=None, force=False):
    """Scrape + analyse + ecrit insights.json (TTL cache). Non fatal."""
    cfg = cfg or get_config()
    paths = get_paths(cfg)
    out = _insights_file(cfg)
    if not force and out.exists() and _fresh(cfg, out):
        log.info("Research: insights.json frais (cache) -> pas de re-scrape")
        return load_json(out) or {}
    rc = cfg.get("research", {}) or {}
    if not (rc.get("keywords") or (cfg.get("scout", {}) or {}).get("keywords")) \
            and not (rc.get("competitors") or []):
        log.info("Research: ni mots-cles ni concurrents -> desactive ce run")
        return {}
    entries, sources_ok = _collect(cfg)
    if not entries:
        old = load_json(out) if out.exists() else None
        if old:
            log.warning("Research: 0 entree scrapee -> insights conserves tels quels")
            return old
        # Premier run sans donnees : ecrire un doc degrade plutot qu'un
        # fichier vide (n_winners=0 -> aucune directive injectee).
        doc, _ = _analyze(entries, cfg)
        doc["sources_ok"] = sources_ok
        save_json(out, doc)
        return doc
    doc, winners = _analyze(entries, cfg)
    doc["sources_ok"] = sources_ok
    save_json(out, doc)
    # Historique append-only : l'evolution de la niche jour apres jour.
    try:
        with open(paths.research / "history.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
    except Exception:
        log.warning("Research history non ecrite (non fatal):\n%s",
                    traceback.format_exc())
    log.info("Research: %s entrees, %s gagnants analyses -> %s",
             doc.get("n_scraped"), doc.get("n_winners"), out)
    return doc


def research_directive(cfg=None):
    """Directive texte (EN, pour prompts LLM) issue des benchmarks reels.

    Compacte (~1-2 phrases) : les prompts LLM ont deja les lecons de
    l'optimizer ; research ajoute les CHIFFRES du marche, pas du bruit.
    Chaine vide tant qu'il n'y a pas assez de donnees.
    """
    cfg = cfg or get_config()
    doc = load_json(_insights_file(cfg)) or {}
    if int(doc.get("n_winners") or 0) < 5:
        return ""
    parts = []
    d_med, d_p25, d_p75 = (doc.get("duration_median_s"),
                           doc.get("duration_p25_s"), doc.get("duration_p75_s"))
    if d_med and d_p25 and d_p75:
        parts.append("Benchmark research (real scraped data): winning shorts "
                     "in this niche run %.0f-%.0fs (median %.0fs)"
                     % (d_p25, d_p75, d_med))
    fams = doc.get("hook_families_pct") or {}
    if fams:
        top_fam = list(fams.items())[0]
        parts.append("dominant hook family: %s (%.0f%% of winners)"
                     % (top_fam[0], top_fam[1]))
    if doc.get("pct_number_title") is not None and doc["pct_number_title"] >= 40:
        parts.append("%.0f%% of winning titles contain a number"
                     % doc["pct_number_title"])
    if doc.get("velocity_median_vpd"):
        parts.append("winners average %s views/day"
                     % _human(doc["velocity_median_vpd"]))
    top = doc.get("top_titles") or []
    if top:
        t = top[0]
        parts.append('most viral right now: "%s" (%s views)'
                     % (str(t.get("title") or "")[:60],
                        _human(int(t.get("views") or 0))))
    directive = "; ".join(parts)
    if directive:
        directive += ". Match these numbers exactly."
    return directive


def _human(n):
    """1234567 -> 1.2M (compact, prompt-friendly)."""
    n = int(n or 0)
    for unit, div in (("M", 1_000_000), ("K", 1_000)):
        if n >= div:
            return "%.1f%s" % (n / div, unit)
    return str(n)


def inject_directive(cfg=None):
    """Append la directive research a style_directive (ideas + remake).

    Appelee APRES optimizer.hook_before_batch (qui ECRASE style_directive).
    Fusion sans ecrasement : optimizer | research. Renvoie la directive.
    """
    cfg = cfg or get_config()
    directive = research_directive(cfg)
    if not directive:
        return ""
    for section in ("ideas", "remake"):
        node = cfg.setdefault(section, {})
        existing = str(node.get("style_directive") or "")
        if directive in existing:
            continue
        node["style_directive"] = (existing + " | " + directive) if existing \
            else directive
    log.info("Research directive injectee: %s", directive[:120])
    return directive


def report(cfg=None):
    """Resume lisible (CLI `research`)."""
    cfg = cfg or get_config()
    doc = load_json(_insights_file(cfg)) or {}
    if not doc:
        return "Aucun insight research (le pipeline scrape au prochain run)."
    lines = ["Research insights (%s)" % doc.get("generated_at"),
             "- entrees scrapees: %s | gagnants analyses: %s"
             % (doc.get("n_scraped"), doc.get("n_winners"))]
    if doc.get("duration_median_s"):
        lines.append("- durees gagnantes: median %.0fs (IQR %.0f-%.0fs)"
                     % (doc["duration_median_s"],
                        doc.get("duration_p25_s") or 0,
                        doc.get("duration_p75_s") or 0))
    if doc.get("hook_families_pct"):
        lines.append("- familles de hooks: %s" % doc["hook_families_pct"])
    if doc.get("velocity_median_vpd"):
        lines.append("- velocite gagnante: %s vues/jour"
                     % _human(doc["velocity_median_vpd"]))
    for t in (doc.get("top_titles") or [])[:5]:
        lines.append("  * %s - %s vues, %.0fs" % (
            str(t.get("title"))[:70], _human(int(t.get("views") or 0)),
            float(t.get("duration") or 0)))
    return "\n".join(lines)
