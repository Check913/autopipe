"""Super-Remake : Shorts viraux -> analyse -> remake 100% original ("10% Better").

Pipeline du module :
  1. discover_hits()   : scrape les Shorts les plus vus de la niche (yt-dlp, sans cle API).
  2. analyze_hit()     : metadonnees + sous-titres du hit -> Core Hook / formule psychologique / rythme.
  3. build_remake_idea(): le moteur "10% Better" (LLM) regenere un script TOTALEMENT original
                          base sur la MEME formule psychologique, sans reutiliser une seule phrase.
  4. enforce_originality(): garde anti-duplication (similarite Jaccard) avant sauvegarde.

Le JSON produit respecte le contrat des idees AutoPipe (id, title, hook, script, comment,
description, tags, duration_target, source_topic, generator) + provenance (source_short_*),
donc tts/subs/video/upload fonctionnent sans modification.
"""
import datetime as dt
import json
import re
import traceback
from collections import Counter

from yt_dlp import YoutubeDL

from .config import get_config, get_paths
from .util import LLMClient, as_script_text, get_logger, load_json, save_json, slugify

log = get_logger("remake")


# --------------------------------------------------------------------------
# Constantes du moteur "10% Better"
# --------------------------------------------------------------------------

HARD_PROMPT = (
    "You are the 10%-Better Engine: an elite US YouTube Shorts ghostwriter. "
    "You receive the decoded structure of a Short that already went viral "
    "(core hook, psychological formula, pacing, subject) and you build a "
    "COMPLETELY ORIGINAL new Short that applies the same psychological formula "
    "with stronger execution. "
    "ABSOLUTE RULES: "
    "(1) NEVER copy or paraphrase the original text: no reused sentence, no "
    "reused title, no reused catch-phrase. Write every sentence from zero. "
    "(2) OPEN with a shock question or a brutal claim in the very first words. "
    "(3) ULTRA-SHORT sentences, one idea per line, spoken rhythm 150-170 wpm, "
    "85-120 words total (30-45 seconds). "
    "(3b) PATTERN INTERRUPTS: no flat stretch longer than 10 seconds - every "
    "7-10 seconds of script (about every 2-3 lines) insert a micro-turn: a "
    "shock question, a reversal, or a change of visual focus for the editor. "
    "(3c) HOOK CALIBRATION: the hook/opening must ride the strongest detected "
    "psychological trigger (negative curiosity, urgency, mystery, shock "
    "number) and, when available, answer one real unanswered viewer question. "
    "(4) FACTS: better documented than the original - concrete and checkable, "
    "no invented statistics. "
    "(5) LOOP ENDING: the last line flows back into the opening line. "
    "(6) Add ONE upgrade the original did not have: a sharper fact, a "
    "counter-intuitive twist, or a stronger visual cue for the editor. "
    "(7) No subscribe begging, no channel intro, no filler words. "
    "You reply ONLY with valid JSON with keys: title (max 60 chars, curiosity "
    "gap, keyword in the first 3 words), hook (max 60 chars, on-screen text, "
    "question preferred), script (a SINGLE multi-line plain-text STRING, "
    "never an array: ONE ultra-short sentence per line, 6-12 lines), comment "
    "(one short pinned question), description "
    "(2-3 lines ending with 3 hashtags), tags (list of 8-12 lowercase "
    "keywords), duration_target (int seconds)."
)

STOPWORDS = frozenset((
    "a", "about", "above", "after", "again", "all", "am", "an", "and", "any",
    "are", "arent", "as", "at", "be", "because", "been", "before", "being",
    "below", "between", "both", "but", "by", "cant", "could", "did", "do",
    "does", "doing", "dont", "down", "during", "each", "even", "every", "few",
    "for", "from", "further", "had", "has", "have", "having", "he", "her",
    "here", "hers", "him", "his", "how", "i", "if", "in", "into", "is", "it",
    "its", "ive", "just", "like", "me", "more", "most", "my", "no", "nor",
    "not", "now", "of", "off", "on", "once", "only", "or", "other", "our",
    "out", "over", "own", "same", "she", "should", "so", "some", "such",
    "than", "that", "the", "their", "them", "then", "there", "these", "they",
    "this", "those", "through", "to", "too", "under", "until", "up", "very",
    "was", "we", "were", "what", "when", "where", "which", "while", "who",
    "whom", "why", "will", "with", "won", "would", "you", "your", "youre",
    # vocabulaire YouTube inutile pour l'analyse de sujet
    "shorts", "short", "video", "videos", "subscribe", "channel", "watch",
    "today", "new", "best", "top", "part", "full", "hd", "official",
))

_TOKEN_RE = re.compile(r"[a-z0-9']+")
_YT_ID_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})")


# --------------------------------------------------------------------------
# Outils
# --------------------------------------------------------------------------

def _video_id_from_url(url):
    m = _YT_ID_RE.search(str(url or ""))
    return m.group(1) if m else None


def _fmt_views(views):
    try:
        v = int(views or 0)
    except (TypeError, ValueError):
        return "0"
    if v >= 1000000:
        return "%.1fM" % (v / 1000000.0)
    if v >= 1000:
        return "%dk" % (v // 1000)
    return str(v)


def _ydl_opts(cfg=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 2,
        "geo_bypass": True,
    }
    # yt-dlp lit le proxy systeme (registre Windows) et ignore fix_proxy_env :
    # cause du bot-check "Sign in to confirm you're not a bot". En mode
    # "direct", on force la connexion directe (proxy "" ; ydl_extra peut
    # toujours la redefinir explicitement).
    if str(((cfg or {}).get("network") or {}).get("proxy_mode", "direct")) == "direct":
        opts["proxy"] = ""
    # Extra options via [remake].ydl_extra (dict TOML), sans ecraser la base.
    for key, value in ((cfg or {}).get("remake", {}).get("ydl_extra") or {}).items():
        opts[str(key)] = value
    return opts


def _token_set(text):
    return set(_TOKEN_RE.findall(str(text or "").lower())) - STOPWORDS


def _jaccard(set_a, set_b):
    """Similarite Jaccard sur ensembles de mots (0.0 = aucun recouvrement)."""
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return float(inter) / float(union) if union else 0.0


# --------------------------------------------------------------------------
# Etape 1 : decouverte des Shorts viraux de la niche
# --------------------------------------------------------------------------

def discover_hits(cfg=None, keywords=None, limit=None):
    """Scrape les Shorts les plus vus des mots-cles de la niche.

    Retourne un dict {date, region, language, keywords, min_views, hits,
    total_found, rejected} sauvegarde dans data/remake/hits_latest.json.
    """
    cfg = cfg or get_config()
    paths = get_paths(cfg)
    language = str(cfg["general"].get("language", "en"))
    region = str(cfg["general"].get("region", "US"))
    min_views = max(1, int(cfg["remake"].get("min_views", 500000)))
    max_duration = max(1, int(cfg["remake"].get("max_duration", 75)))
    limit = max(1, int(limit or cfg["remake"].get("limit", 5)))
    per_query = max(5, int(cfg["remake"].get("search_per_keyword", 10)))
    words = list(keywords or cfg["remake"].get("keywords")
                 or cfg["scout"].get("keywords") or ["amazing facts"])

    queries = []
    for kw in words:
        base = " ".join(str(kw).split())
        if not base:
            continue
        queries.append("ytsearch%d:%s shorts" % (per_query, base))
        queries.append("ytsearch%d:%s #shorts" % (per_query, base))
    if not queries:
        queries = ["ytsearch%d:amazing facts shorts" % per_query]

    raw = []
    opts = _ydl_opts(cfg)
    opts["extract_flat"] = "in_playlist"
    with YoutubeDL(opts) as ydl:
        for query in queries:
            try:
                info = ydl.extract_info(query, download=False)
            except Exception as exc:
                log.warning("Recherche '%s' echouee: %s", query, exc)
                continue
            for entry in (info or {}).get("entries") or []:
                if not entry:
                    continue
                vid = _video_id_from_url(entry.get("url") or entry.get("webpage_url")) \
                    or str(entry.get("id") or "")
                if not vid:
                    continue
                raw.append({
                    "id": vid,
                    "title": (entry.get("title") or "").strip(),
                    "channel": entry.get("channel") or entry.get("uploader") or "",
                    "views": int(entry.get("view_count") or 0),
                    "duration": float(entry.get("duration") or 0.0),
                    "url": entry.get("url") or entry.get("webpage_url")
                    or "https://www.youtube.com/watch?v=%s" % vid,
                    "query": query,
                })

    # Dedup par identifiant video.
    seen, hits = set(), []
    for h in raw:
        if h["id"] in seen:
            continue
        seen.add(h["id"])
        hits.append(h)
    total_found = len(hits)

    # Fusion des hits video du radar multi-plateformes (YouTube recherche
    # niche + TikTok/Instagram tags). Vues inconnues (TikTok/Instagram) : 0
    # -> l'hydratation ci-dessous recupere les vraies metadonnees.
    try:
        from .radar import radar_video_hits
        before = len(hits)
        for h in radar_video_hits(cfg):
            if h["id"] in seen:
                continue
            seen.add(h["id"])
            hits.append(h)
        if len(hits) > before:
            log.info("Radar: +%s hits video externes fusionnes", len(hits) - before)
    except Exception as exc:
        log.warning("Fusion hits radar ignoree: %s", exc)

    # Les resultats de recherche "flat" n'exposent pas toujours view_count :
    # si aucune vue n'est connue, on recupere les vraies metadonnees des
    # candidats plausibles avant d'appliquer le filtre viral.
    unknown = [h for h in hits if h["views"] == 0]  # recherche flat + radar
    if unknown:
        hydrate = int(cfg["remake"].get("hydrate_count", 15))
        cands = [h for h in unknown
                 if h["duration"] <= max_duration][:hydrate]
        hydrate_opts = _ydl_opts(cfg)
        hydrate_opts["skip_download"] = True
        with YoutubeDL(hydrate_opts) as ydl:
            for h in cands:
                try:
                    # URL directe (TikTok/Instagram/YouTube) sinon watch?v= reconstruit.
                    target = h.get("url") or "https://www.youtube.com/watch?v=%s" % h["id"]
                    info = ydl.extract_info(target, download=False) or {}
                except Exception as exc:
                    log.debug("Hydratation %s echouee: %s", h["id"], exc)
                    continue
                h["views"] = int(info.get("view_count") or 0)
                h["duration"] = float(info.get("duration") or h["duration"])
                if info.get("title"):
                    h["title"] = info["title"].strip()
        log.info("Vues inconnues (recherche flat / radar) -> %s candidats hydrates",
                 len(cands))

    # Filtre viral : vues minimales + vraie duree Short.
    filtered = [h for h in hits if h["views"] >= min_views and h["duration"] <= max_duration]
    if not filtered:
        # Filet de securite : on garde les Shorts les plus vus pour que l'analyse
        # reste possible meme si la niche est calme aujourd'hui.
        shorts = sorted([h for h in hits if h["duration"] <= max_duration],
                        key=lambda x: x["views"], reverse=True)
        filtered = shorts[:10]
        log.warning("Aucun Short >= %s vues: filet de securite, %s Shorts les plus vus conserves",
                    min_views, len(filtered))
    filtered.sort(key=lambda x: x["views"], reverse=True)
    top = filtered[:limit]

    out = {"date": dt.datetime.now().isoformat(), "region": region,
           "language": language, "keywords": words, "min_views": min_views,
           "max_duration": max_duration, "total_found": total_found,
           "rejected": total_found - len(filtered), "hits": top}
    save_json(paths.remake / "hits_latest.json", out)
    log.info("Super-Remake scout: %s Shorts retenus sur %s (seuil %s vues, <= %ss)",
             len(top), total_found, _fmt_views(min_views), max_duration)
    for h in top:
        log.info("  HIT %s vues | %ss | %s", _fmt_views(h["views"]),
                 int(h["duration"]), (h["title"] or "?")[:70])
    return out


# --------------------------------------------------------------------------
# Etape 2 : recuperation des sous-titres (transcript) + analyse structurelle
# --------------------------------------------------------------------------

def _clean_vtt(raw):
    """Extrait le texte parlé d'un fichier VTT/SRV (enleve timestamps, entetes, doublons)."""
    lines_out = []
    seen = set()
    for line in str(raw or "").splitlines():
        s = line.strip()
        if (not s or s.startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE", "Region"))
                or "-->" in s or s.isdigit()
                or s.startswith(("<", "{"))):
            continue
        s = re.sub(r"<[^>]+>", "", s)          # balises VTT <c>, <00:00:00.000>
        s = re.sub(r"^\s*[-\[\]]+\s*", "", s)  # puces et indications speaker
        s = s.strip()
        if s and s not in seen:
            seen.add(s)
            lines_out.append(s)
    return " ".join(lines_out)


def _fetch_caption(url):
    import requests
    resp = requests.get(url, timeout=20,
                        headers={"User-Agent": "Mozilla/5.0 (AutoPipe Super-Remake)"})
    if resp.status_code != 200:
        raise RuntimeError("caption HTTP %s" % resp.status_code)
    return _clean_vtt(resp.text)


def _pick_subtitles(info):
    """Cherche une piste de sous-titres EN (manuelle puis auto) -> texte ou ''."""
    for key in ("subtitles", "automatic_captions"):
        tracks = info.get(key) or {}
        langs = [k for k in tracks if str(k).lower().startswith("en")] or list(tracks)
        for lang in langs:
            for fmt in tracks.get(lang) or []:
                url = fmt.get("url")
                if not url:
                    continue
                try:
                    text = _fetch_caption(url)
                except Exception as exc:
                    log.debug("Piste %s/%s echouee: %s", key, lang, exc)
                    continue
                if text:
                    return text, "%s:%s" % (key, lang)
    return "", ""


def _first_sentences(text, n=3, max_chars=160):
    """Les n premieres phrases du transcript = le hook des 3 premieres secondes."""
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    parts = [p for p in parts if p] or [text]
    picked = " ".join(parts[:max(1, int(n))])
    if len(picked) > max_chars:
        cut = picked[:max_chars]
        if " " in cut:
            cut = cut[: cut.rfind(" ")]
        picked = cut.strip()
    return picked


def _keywords_from_text(text, n=8):
    """Mots-cles par frequence, stopwords enleves (base de la definition de sujet)."""
    tokens = [t for t in _TOKEN_RE.findall(str(text or "").lower())
              if len(t) >= 4 and t not in STOPWORDS]
    return [w for w, _c in Counter(tokens).most_common(max(1, int(n)))]


def _retention_payload(hit, cfg):
    """Comment Sentiment & Retention Scraper (couche 1 yt-dlp, couche 2 innertube).

    Retourne None si desactive ou si les deux couches echouent (mode degrade).
    """
    rcfg = cfg["remake"]
    if not rcfg.get("comments_enabled", True):
        return None
    try:
        from . import retention as _retention
        return _retention.analyze_hit_retention(
            str(hit.get("id") or ""),
            hit.get("title") or "",
            int(float(hit.get("duration") or 0.0)),
            int(rcfg.get("max_comments", 60)),
            buckets=int(rcfg.get("retention_buckets", 20)),
            interval=tuple(rcfg.get("interrupt_interval") or (7, 10)),
        )
    except Exception as exc:
        log.warning("retention scrape impossible pour %s (%s)",
                    hit.get("id"), exc)
        return None


def analyze_hit(url_or_id, cfg=None, llm=None):
    """Metadonnees + transcript d'un Short, puis decorticage de sa structure.

    Remplit hit['analysis'] = {core_hook, psychological_formula, pacing, subject,
    rewrite_directive, generator}. Le LLM (10%-Better Engine) decode la formule ;
    sinon une analyse heuristique prend le relais (jamais de blocage).
    """
    cfg = cfg or get_config()
    raw = str(url_or_id or "").strip()
    vid = _video_id_from_url(url_or_id) or raw
    # YouTube -> URL watch reconstruite ; sinon (TikTok/Instagram/radar) on
    # passe l'URL brute a yt-dlp, qui gere nativement ces plateformes.
    url = raw if raw.startswith("http") else "https://www.youtube.com/watch?v=%s" % vid
    opts = _ydl_opts(cfg)
    opts["skip_download"] = True
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    info = info or {}

    subs_text, subs_source = _pick_subtitles(info)
    description = str(info.get("description") or "").strip()
    hook = _first_sentences(subs_text or description,
                            int(cfg["remake"].get("hook_sentences", 3)),
                            int(cfg["remake"].get("hook_chars", 160)))
    text_for_kw = " ".join(x for x in (info.get("title"), description, subs_text) if x)

    hit = {
        "id": str(info.get("id") or vid),
        "url": info.get("webpage_url") or url,
        "title": (info.get("title") or "").strip(),
        "channel": info.get("channel") or info.get("uploader") or "",
        "views": int(info.get("view_count") or 0),
        "duration": float(info.get("duration") or 0.0),
        "likes": int(info.get("like_count") or 0),
        "captions_found": bool(subs_text),
        "caption_source": subs_source,
        "captions_excerpt": subs_text[:1200],
        "description_excerpt": description[:400],
        "hook_excerpt": hook,
        "keywords": _keywords_from_text(text_for_kw,
                                        int(cfg["remake"].get("keywords_from_text", 8))),
    }
    hit["analysis"] = analyze_structure(hit, llm=llm)
    retention_data = _retention_payload(hit, cfg)
    if retention_data:
        hit["retention"] = retention_data
        m = retention_data.get("matrix") or {}
        hit["analysis"]["retention"] = {
            "source": retention_data.get("source"),
            "n_comments": len(retention_data.get("comments") or []),
            "kinds": m.get("kinds") or {},
            "bucket_s": m.get("bucket_s"),
            "unlocated": m.get("unlocated"),
            "density": m.get("density") or [],
            "spikes_s": m.get("spikes") or [],
            "interrupts": (retention_data.get("interrupts") or [])[:20],
            "hook_triggers": ((retention_data.get("hook_profile") or {})
                              .get("triggers")) or {},
            "gaps": retention_data.get("gaps") or [],
        }
        log.info("Retention %s: %s commentaires (%s) | pics=%s | gaps=%s",
                 vid, len(retention_data.get("comments") or []),
                 retention_data.get("source") or "aucun",
                 (m.get("spikes") or [])[:3],
                 len(retention_data.get("gaps") or []))
    log.info("Hit %s analyse: captions=%s | sujet=%s | core_hook=%s",
             vid, subs_source or "aucune",
             (hit["analysis"].get("subject") or "?")[:50],
             (hit["analysis"].get("core_hook") or "?")[:70])
    return hit


def analyze_structure(hit, llm=None):
    """Decortique POURQUOI le hit a marche (Core Hook + formule psychologique)."""
    a = {}
    if llm is not None and llm.enabled:
        try:
            # Pas de max_tokens serre : avec response_format json_object,
            # gemini-2.5-flash "pense" et un budget trop court tronque le JSON.
            a = llm.chat_json(HARD_PROMPT, _analyze_user_prompt(hit),
                              temperature=0.6)
        except Exception as exc:
            log.warning("Analyse LLM impossible pour %s (%s) -> heuristique",
                        hit.get("id"), exc)
    if not a.get("core_hook") or not a.get("subject"):
        a = _heuristic_analysis(hit)
    a["pacing"] = a.get("pacing") if isinstance(a.get("pacing"), dict) else {}
    a["psychological_formula"] = [str(x) for x in (a.get("psychological_formula") or [])]
    a["rewrite_directive"] = str(a.get("rewrite_directive") or
                                 "Same formula, stronger execution, brand new sentences.")
    a["generator"] = a.get("generator") or ("llm:%s" % (llm.model if llm else "heuristic"))
    return a


def _analyze_user_prompt(hit):
    ret = (hit.get("analysis") or {}).get("retention") or {}
    if ret.get("n_comments"):
        gaps = "\n".join("- %s" % (g.get("text") or "")[:120]
                         for g in (ret.get("gaps") or [])[:5]) or "- none"
        ret_block = (
            "COMMENT RETENTION SIGNALS (real viewers' reactions):\n"
            "Comments analyzed: %s (questions=%s, reactions=%s, unlocated=%s)\n"
            "Reaction spikes (seconds): %s\n"
            "Top unanswered viewer questions (holes in the racket):\n%s\n\n"
            % (ret.get("n_comments"),
               (ret.get("kinds") or {}).get("question", 0),
               (ret.get("kinds") or {}).get("reaction", 0),
               ret.get("unlocated") or 0,
               ret.get("spikes_s") or "none", gaps)
        )
    else:
        ret_block = ("COMMENT RETENTION SIGNALS: none available (degraded "
                     "mode - decode from transcript only).\n\n")
    return (
        "ORIGINAL SHORT (already viral, do NOT copy):\n"
        "Title: %s\nViews: %s | Duration: %.1fs | Likes: %s\n"
        "Channel: %s\n\n"
        "OBSERVED OPENING (first seconds of the transcript):\n%s\n\n"
        "%s"
        "DECODE WHY IT WORKED: the Core Hook (why people did not swipe in the "
        "first 3 seconds), the psychological formula (list of 3-5 levers: "
        "curiosity gap, shock, open loop, stakes, payoff...), the pacing "
        "(JSON: words_per_minute, sentence_style, estimated_hook_seconds) and "
        "the SUBJECT in 5-12 words. Reply ONLY with valid JSON with keys: "
        "core_hook, psychological_formula (list), pacing (object), subject, "
        "rewrite_directive."
        % (hit.get("title") or "?", _fmt_views(hit.get("views")),
           float(hit.get("duration") or 0.0), hit.get("likes") or 0,
           hit.get("channel") or "?", hit.get("hook_excerpt") or "(inconnu)",
           ret_block)
    )


def _heuristic_analysis(hit):
    """Analyse de secours sans LLM (formule virale standard des Shorts)."""
    kw = list(hit.get("keywords") or [])
    subject = ", ".join(kw[:3]) if kw else (hit.get("title") or "this topic")[:60]
    title = (hit.get("title") or "").strip()
    return {
        "core_hook": ("Open information gap in the first words: '%s'" % title)[:200],
        "psychological_formula": [
            "Curiosity gap: the opening promises a payoff the viewer must see",
            "Fast payoff: a surprising fact lands within the first seconds",
            "Stakes: the viewer loses something by scrolling away",
            "Loop ending: the last line circles back to the opening claim",
        ],
        "pacing": {"words_per_minute": 155,
                   "sentence_style": "ultra-short spoken sentences, one idea per line",
                   "estimated_hook_seconds": 4},
        "subject": subject,
        "rewrite_directive": "Keep the curiosity gap and the fast payoff, document "
                             "the facts, rewrite every sentence from scratch.",
        "generator": "heuristic",
    }


# --------------------------------------------------------------------------
# Etape 3 : moteur de reecriture amelioree ("10% Better")
# --------------------------------------------------------------------------

def _rewrite_user_prompt(analysis, niche, language, cfg=None):
    a = analysis.get("analysis") or {}
    fmt = "\n".join("- %s" % x for x in (a.get("psychological_formula") or [])) or "- (heuristique)"
    pacing = json.dumps(a.get("pacing") or {})[:200]
    ret = a.get("retention") or {}
    bucket_s = ret.get("bucket_s") or 0
    density = ret.get("density") or []
    density_s = " ".join(str(d) for d in density) \
        if density else "- no comment data (transcript-only mode)"
    interrupts = ret.get("interrupts") or []
    if interrupts:
        interrupt_plan = "\n".join(
            "- at ~%ss: %s%s" % (x.get("s"), x.get("type"),
                                 " (reaction-spike zone)"
                                 if x.get("align_spike") else "")
            for x in interrupts[:12])
    else:
        interrupt_plan = ("- no data: use the default cadence (a micro-turn "
                          "every 7-10 seconds)")
    gaps_block = "\n".join("- %s" % (g.get("text") or "")[:120]
                           for g in (ret.get("gaps") or [])[:5]) \
        or "- none available"
    triggers = ret.get("hook_triggers") or {}
    triggers_s = ", ".join("%s (%s hits)" % (k, v) for k, v in
                           sorted(triggers.items(), key=lambda kv: -kv[1])[:4]) \
        or "none detected (default: negative curiosity + shock number)"
    prompt = (
        "VIRAL SHORT THAT ALREADY WORKED (analysis of why it worked)\n"
        "Title: %s\nViews: %s | Duration: %.1fs\n"
        "Core hook: %s\nPacing: %s\n"
        "Psychological formula:\n%s\nSubject: %s\n\n"
        "Channel niche: %s\nOutput language: %s\n\n"
        "RETENTION MATRIX (second-by-second comment reaction density, %ss "
        "buckets; spikes mark where viewers reacted strongest):\n%s\n"
        "PATTERN INTERRUPT PLAN (force a micro-twist, a shock question or a "
        "visual focus change at each mark):\n%s\n"
        "UNANSWERED VIEWER QUESTIONS (the new script must answer at least one "
        "of these to capture maximum engagement):\n%s\n"
        "HOOK FREQUENCY MATCHER (psychological triggers detected in the "
        "original's opening and comments -> calibrate the new hook on the "
        "strongest one):\n%s\n\n"
        "Build a NEW Short on the same psychological formula but on an UPGRADED "
        "version of this subject (10%% Better): more shocking open, tighter "
        "pacing, better documented facts, stronger loop ending. Take the SUBJECT "
        "and the PATTERN only. Do NOT reuse phrases, titles or sentences from "
        "the original video: every sentence must be a fresh, original formulation."
        % (analysis.get("title") or "?", _fmt_views(analysis.get("views")),
           float(analysis.get("duration") or 0.0),
           a.get("core_hook") or "?", pacing, fmt,
           a.get("subject") or "?", niche or "", language or "en",
           bucket_s, density_s, interrupt_plan, gaps_block, triggers_s)
    )
    # MODULE 3 (graft non-cassant) : directive de style apprise par la boucle
    # d'auto-optimisation (ex. "hooks en question -> +X% d'engagement"),
    # posee par optimizer.apply_config sur la cfg du lot. Sans cfg ou sans
    # directive, le prompt est STRICTEMENT identique au comportement historique.
    if cfg:
        directive = (cfg.get("remake", {}) or {}).get("style_directive") \
            or (cfg.get("ideas", {}) or {}).get("style_directive")
        if directive:
            prompt += "\n\nSTYLE CALIBRATION (learned by auto-optimizer, follow it):\n%s" % directive
    return prompt


def _normalize_idea(data, defaults_source, cfg, analysis):
    """Normalise le JSON LLM vers le contrat d'idee AutoPipe + provenance remake."""
    for key in ("title", "hook", "script", "description", "tags"):
        if not data.get(key):
            raise RuntimeError("Remake LLM incomplet: cle '%s' absente" % key)
    data["title"] = str(data["title"]).strip()[:60]
    data["hook"] = str(data["hook"]).strip()[:60]
    # gemini-2.5-flash renvoie parfois le script en tableau JSON de lignes
    # (observe le 2026-10-26): edge-tts exige un str -> normalisation ici.
    data["script"] = as_script_text(data["script"])
    if not data["script"]:
        raise RuntimeError("Remake LLM incomplet: script vide apres normalisation")
    if not data.get("comment"):
        data["comment"] = defaults_source.COMMENT_TEMPLATES[0]
    if not isinstance(data["tags"], list):
        data["tags"] = [str(data["tags"])]
    data["tags"] = [str(t).strip().lower() for t in data["tags"]][:12]
    a = analysis.get("analysis") or {}
    subject = " ".join(str(a.get("subject") or analysis.get("title") or "").split())
    data["duration_target"] = int(data.get("duration_target") or 40)
    data["source_topic"] = subject
    data["source_short_id"] = analysis["id"]
    data["source_short_url"] = analysis.get("url")
    data["source_short_title"] = analysis.get("title")
    data["source_short_views"] = analysis.get("views")
    data["analysis"] = a
    return data


def build_remake_idea(analysis, cfg, llm):
    """10% Better Engine : regenere une idee ORIGINALE depuis la structure analysee."""
    from . import ideas as defaults_source
    data = llm.chat_json(
        HARD_PROMPT,
        _rewrite_user_prompt(analysis, cfg["general"].get("niche", ""),
                             cfg["general"].get("language", "en"), cfg=cfg),
        temperature=float(cfg["remake"].get("temperature", 0.85)),
    )
    data = _normalize_idea(data, defaults_source, cfg, analysis)
    data["generator"] = "remake:" + str(cfg["llm"].get("model", ""))
    return data


def _fallback_remake_idea(analysis, cfg):
    """Remake de secours sans LLM : formule appliquee, phrases nouvelles."""
    from . import ideas as defaults_source
    a = analysis.get("analysis") or {}
    subject = " ".join(str(a.get("subject") or analysis.get("title") or "").split()) or "this topic"
    kw = list(analysis.get("keywords") or [])
    tags = ["shorts"] + [k for k in kw[:4] if len(k) > 3] + ["facts", "mindblowing"]
    title = defaults_source._curiosity_title("What Really Happens With %s" % subject)[:60]
    hook = ("Why does %s break every rule?" % subject)[:60]
    script = (
        "Why does %s break every rule?\n"
        "Nobody warned you about this part.\n"
        "First fact: the pattern was there the whole time.\n"
        "Second fact: the numbers move the opposite way.\n"
        "Third fact: one detail changes the whole picture.\n"
        "Now the twist: it only works when nobody is watching.\n"
        "And that is exactly why %s breaks every rule."
    ) % (subject, subject)
    return {
        "title": title,
        "hook": hook,
        "script": script,
        "comment": defaults_source.COMMENT_TEMPLATES[0],
        "description": "%s\nThe documented version, in 30 seconds.\n#shorts #facts #didyouknow" % title,
        "tags": tags[:12],
        "duration_target": 40,
        "source_topic": subject,
        "source_short_id": analysis["id"],
        "source_short_url": analysis.get("url"),
        "source_short_title": analysis.get("title"),
        "source_short_views": analysis.get("views"),
        "analysis": a,
        "generator": "remake:fallback",
    }


# --------------------------------------------------------------------------
# Etape 4 : optimisation anti-duplication (unicite totale)
# --------------------------------------------------------------------------

def originality_report(idea, original_title, original_text=None):
    """Similarite (Jaccard) du remake vs l'original: titre + texte reellement dit."""
    title_sim = _jaccard(_token_set(idea.get("title", "")), _token_set(original_title))
    script_sim = _jaccard(_token_set(idea.get("script", "")), _token_set(original_text))
    return {
        "title_jaccard": round(title_sim, 3),
        "script_jaccard": round(script_sim, 3),
        "max_jaccard": round(max(title_sim, script_sim), 3),
        "passed": max(title_sim, script_sim) <= 0.35,
    }


def enforce_originality(idea, original_title, original_text=None, cfg=None,
                        threshold=None):
    """Mesure l'originalite et refuse le remake trop proche de l'original.

    Aucune phrase n'est recopiee par construction (prompt interdiction) : ce
    garde est le filet final avant sauvegarde/upload. Retourne (idea, passed).
    """
    cfg = cfg or get_config()
    if threshold is None:
        threshold = float(cfg["remake"].get("max_jaccard", 0.35))
    report = originality_report(idea, original_title, original_text)
    report["passed"] = report["max_jaccard"] <= threshold
    idea["originality"] = report
    return idea, report["passed"]


# --------------------------------------------------------------------------
# Orchestration : run_remake (equivalent de run_ideas pour le scheduler)
# --------------------------------------------------------------------------

def _already_remade_ids(paths):
    """Shorts deja remakes dans les runs precedents (anti-doublon)."""
    done = set()
    if not paths.remake.exists():
        return done
    for path in paths.remake.glob("remake_*.json"):
        data = load_json(path, {})
        if data.get("source_short_id"):
            done.add(data["source_short_id"])
    return done


def run_remake(config=None, count=None):
    """Chaine complete : hits -> analyse -> remake 10% Better -> garde d'unicite.

    Retourne (ideas, saved_paths) au meme format que ideas.run_ideas, donc le
    scheduler et le CLI consomment le resultat sans changement.
    """
    cfg = config or get_config()
    paths = get_paths(cfg)
    count = max(1, int(count or cfg["remake"].get("count", 2)))
    llm = LLMClient(base_url=cfg["llm"].get("base_url", ""),
                    api_key=cfg["llm"].get("api_key", ""),
                    model=cfg["llm"].get("model", "gpt-4o-mini"))
    if llm.enabled:
        log.info("Super-Remake: moteur 10%% Better actif (%s)", llm.model)
    else:
        log.warning("LLM non configure: remakes en mode heuristique (voir config.llm)")

    scout_data = discover_hits(cfg)

    # MODULE 1 (graft non-cassant) : analyse visuelle OpenCV des N top hits
    # (3 premieres secondes : rythme des coupes, zooms, palette, zone de
    # sous-titres). Les patterns appris sont injectes dans cfg["video"] pour
    # le rendu du lot. Toute panne (OpenCV/yt-dlp/reseau) est non fatale.
    if cfg.get("visual", {}).get("enabled", False):
        try:
            from .visual_pattern import (aggregate_patterns, analyze_top_hits,
                                         apply_to_video_cfg, save_patterns_latest)
            metrics = analyze_top_hits(cfg, hits=scout_data.get("hits", []))
            pattern = save_patterns_latest(
                cfg, aggregate_patterns(list(metrics.values())))
            if pattern and apply_to_video_cfg(cfg, pattern):
                log.info("Visual: codes visuels viraux injectes dans cfg['video']")
        except Exception:
            log.warning("Module visuel ignore (non fatal):\n%s",
                        traceback.format_exc())

    done_ids = _already_remade_ids(paths)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    ideas, saved = [], []
    for hit in scout_data.get("hits", []):
        if len(ideas) >= count:
            break
        if hit["id"] in done_ids:
            log.info("Short %s deja remake -> ignore", hit["id"])
            continue
        try:
            analysis = analyze_hit(hit["url"], cfg, llm=llm)
        except Exception as exc:
            log.warning("Analyse impossible pour %s: %s", hit["url"], exc)
            continue
        # La duree vue en recherche flat peut manquer ou mentir: on re-verifie
        # avec les vraies metadonnees avant de remaker.
        if float(analysis.get("duration") or 0.0) > float(scout_data.get("max_duration", 75)):
            log.info("Short %s trop long (%.0fs) -> ignore", analysis["id"],
                     float(analysis.get("duration") or 0.0))
            continue

        original_text = " ".join(x for x in (analysis.get("captions_excerpt"),
                                             analysis.get("description_excerpt")) if x)
        try:
            idea = build_remake_idea(analysis, cfg, llm) if llm.enabled \
                else _fallback_remake_idea(analysis, cfg)
        except Exception as exc:
            log.warning("Remake LLM echoue pour %s (%s) -> heuristique",
                        analysis["id"], exc)
            idea = _fallback_remake_idea(analysis, cfg)

        idea, passed = enforce_originality(idea, analysis.get("title"),
                                           original_text, cfg)
        if not passed:
            log.warning("REJETE (anti-duplication): jaccard=%s > seuil pour le remake de %s",
                        idea["originality"]["max_jaccard"], analysis.get("title")[:60])
            continue

        idea["score"] = hit.get("views")
        idea["id"] = "%s-remake-%02d" % (stamp, len(ideas) + 1)
        path = paths.remake / ("remake_%s_%s.json" % (idea["id"], slugify(idea["title"], 40)))
        save_json(path, idea)
        saved.append(path)
        ideas.append(idea)
        log.info("Super-Remake OK [%s] originalite max=%s | %s",
                 idea["id"], idea["originality"]["max_jaccard"], idea["title"])

    save_json(paths.remake / "remakes_latest.json",
              {"date": stamp, "remakes": [str(p) for p in saved]})
    if not ideas:
        log.warning("Aucun Super-Remake valide: rien ne sera produit cette iteration")
    else:
        log.info("Super-Remakes generes: %s", len(ideas))
    return ideas, saved


def load_latest_remake(config=None):
    """Derniere idee remake valide (fallback scheduler)."""
    cfg = config or get_config()
    paths = get_paths(cfg)
    latest = load_json(paths.remake / "remakes_latest.json", {})
    for name in reversed(latest.get("remakes", [])):
        idea = load_json(paths.remake / name)
        if idea:
            return idea
    return None