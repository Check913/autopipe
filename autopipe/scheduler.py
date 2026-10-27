"""Continuous orchestration: full pipeline loop (24/7) with UTC posting windows."""
import datetime as dt
import time
import traceback

from . import analytics, fetch_media, ideas, scout, subs, tts, video
from .config import get_config, get_paths
from .upload import pinned_comment_text, upload_video
from .util import fix_proxy_env, get_logger, load_json, save_json

log = get_logger("scheduler")


def _next_window_start(now_utc, post_hours, grace_min=5):
    """Next UTC target for posting.

    A window less than grace_min minutes in the past is still honored
    (starting at 15:00-15:05 posts immediately instead of skipping a day).
    """
    candidates = []
    for h in post_hours:
        cand = now_utc.replace(hour=int(h) % 24, minute=0, second=0, microsecond=0)
        if cand > now_utc:
            candidates.append(cand)
        elif (now_utc - cand).total_seconds() <= grace_min * 60:
            candidates.append(now_utc)
    if not candidates:
        earliest = min(int(h) % 24 for h in post_hours)
        return (now_utc + dt.timedelta(days=1)).replace(
            hour=earliest, minute=0, second=0, microsecond=0)
    return min(candidates)


def run_pipeline_once(config=None, do_upload=True, idea=None):
    """Full chain: radar -> scout -> idea -> tts -> srt -> clips -> video -> upload."""
    cfg = config or get_config()
    fix_proxy_env(cfg.get("network", {}).get("proxy_mode", "direct"))
    paths = get_paths(cfg)
    log.info("=== AutoPipe: pipeline started ===")

    # 0. Radar multi-plateformes (Reddit/Hacker News/Google News/Trends/
    #    YouTube Shorts/TikTok/Instagram) : detecte les contenus viraux du web
    #    dont le pipeline doit s'inspirer. Cache TTL interne, sources isolees,
    #    jamais fatal (une panne radar ne casse pas le pipeline).
    try:
        from . import radar
        radar.run_radar(cfg)
    except Exception:
        log.warning("Radar a echoue (non fatal):\n%s", traceback.format_exc())

    # 1. Trending topics
    scout.run_scout(cfg)

    # 1.5 MODULE 2 (graft non-cassant) : arbitre predictif de tendances.
    # Croise Google Trends + saturation YouTube -> score de momentum. Les
    # sujets au-dessus de min_momentum (vague AVANT saturation) sont injectes
    # dans data/scout/latest.json pour etre generes en priorite. Non fatal.
    if cfg.get("trend", {}).get("enabled", False):
        try:
            from . import trend_arbiter
            doc = trend_arbiter.run_trend_arbiter(cfg)
            picks = [{"topic": t["query"],
                      "score": round(float(t.get("momentum") or 0.0), 3)}
                     for t in doc.get("topics", [])
                     if float(t.get("momentum") or 0.0) >=
                     float(cfg.get("trend", {}).get("min_momentum", 0.3))]
            if picks:
                scout_file = paths.scout / "latest.json"
                scout_data = load_json(scout_file, {"topics": []}) or {}
                existing = {str((t or {}).get("topic", "")).strip().lower()
                            for t in (scout_data.get("topics") or [])}
                merged = [t for t in picks
                          if t["topic"].strip().lower() not in existing]
                scout_data["topics"] = list(scout_data.get("topics") or []) + merged
                save_json(scout_file, scout_data)
                log.info("Trend Arbiter: %s sujet(s) a fort momentum injecte(s)",
                         len(merged))
        except Exception:
            log.warning("Trend Arbiter a echoue (non fatal):\n%s",
                        traceback.format_exc())

    # 1.8 Boucle retention (critique 2026-10-27) : metrique
    # averageViewPercentage (proxy Viewed vs Swiped Away) via Analytics
    # API - token SEPARÉ (yt-analytics.readonly), le token d'upload et la
    # facon de publier restent intacts. Non autorise/non fatal -> le
    # pipeline tourne comme avant (voir `autopipe analytics-auth`).
    try:
        analytics.refresh_retention(cfg)
    except Exception:
        log.warning("Refresh retention a echoue (non fatal):\n%s",
                    traceback.format_exc())

    # 1.9 MODULE 3 (graft non-cassant) : boucle d'auto-apprentissage.
    # Apprend les patterns gagnants des videos publiees puis ajuste la cfg
    # du lot (hooks, temperatures, directives de prompt) SANS intervention.
    # Jamais fatal : le pipeline tourne meme si l'optimizer echoue.
    try:
        from . import optimizer
        optimizer.hook_before_batch(cfg)
    except Exception:
        log.warning("Optimizer hook a echoue (non fatal):\n%s",
                    traceback.format_exc())

    # 2. Idea + script. Si remake.enabled, on part d'un Short viral de la niche
    # (Super-Remake "10%% Better"); tout echec retombe sur les idees standards.
    if idea is None:
        remake_cfg = cfg.get("remake", {})
        ideas_list = []
        if remake_cfg.get("enabled"):
            try:
                from . import remake as remake_mod
                ideas_list, _ = remake_mod.run_remake(
                    cfg, count=int(remake_cfg.get("count", 2)))
            except Exception:
                ideas_list = []
                log.warning("Super-Remake a echoue, fallback idees standard:\n%s",
                            traceback.format_exc())
            if not ideas_list:
                log.warning("Super-Remake: 0 idee valide -> fallback idees standard")
        if not ideas_list:
            ideas_list, _ = ideas.run_ideas(cfg, count=3)
        if not ideas_list:
            raise RuntimeError("No idea generated")
        idea = ideas_list[0]
    log.info("Selected idea: %s%s", idea.get("title"),
             " (Super-Remake de %s)" % idea.get("source_short_title")
             if idea.get("source_short_id") else "")

    out_dir = paths.output / str(idea["id"])
    out_dir.mkdir(parents=True, exist_ok=True)

    # 3. Voice
    audio = out_dir / "voice.mp3"
    timings = out_dir / "timings.json"
    tts.synthesize(idea["script"], audio, timings,
                   cfg["tts"].get("voice"), cfg["tts"].get("rate"), cfg["tts"].get("volume"))

    # 4. Subtitles
    srt = out_dir / "subs.srt"
    from .subs import build_srt_from_timings
    build_srt_from_timings(timings, srt, cfg)

    # 5. Stock clips
    kws = [idea.get("source_topic", "")] + [str(t) for t in (idea.get("tags") or [])[:2]]
    clips = fetch_media.run_fetch(kws, cfg)

    # 6. Assembly
    final = video.run_video(idea, audio, timings, clips, cfg)

    result = {"idea": idea["id"], "title": idea.get("title"), "video": str(final)}

    # 7. Upload + stats
    if do_upload:
        comment = pinned_comment_text(idea, cfg)
        video_id = upload_video(final, idea["title"], idea["description"],
                                idea.get("tags", []), cfg, pinned_comment=comment)
        result["youtube_id"] = video_id
        try:
            analytics.record([video_id], cfg)
        except Exception as exc:
            log.warning("Initial stats unavailable: %s", exc)
        # V2 : rattacher l'id YouTube a la paire A/B de hook (si le sujet
        # en a une) pour que summarize() puisse joindre variant -> retention.
        if idea.get("pair_id"):
            try:
                from .hook_ab import attach_video
                attach_video(idea["pair_id"], video_id, cfg)
            except Exception as exc:
                log.warning("Hook A/B attach impossible: %s", exc)

    save_json(paths.data / "last_run.json", result)
    log.info("=== AutoPipe: pipeline finished: %s ===", result)
    return result


def run_loop(config=None, once=False):
    """Permanent loop.

    If [upload] post_hours_utc is set (e.g. [15, 21] = 15:00 and 21:00 UTC,
    peak US audience hours), uploads happen at those UTC windows only.
    Otherwise one run every scheduler.interval_hours.
    """
    cfg = config or get_config()
    paths = get_paths(cfg)
    state_file = paths.root / cfg["scheduler"].get("state_file", "data/scheduler_state.json")
    interval = int(cfg["scheduler"].get("interval_hours", 4)) * 3600
    # Frequence de publication CONSERVEE a l'identique (decision du
    # 2026-10-27) : daily_limit + post_hours_utc inchanges.
    # posts_per_day reste disponible mais vaut 0 par defaut (0 -> jamais
    # utilise : c'est daily_limit qui gere le quota, comme avant).
    posts_per_day = int(cfg["upload"].get("posts_per_day", 0))
    daily_limit = (posts_per_day if posts_per_day > 0
                   else int(cfg["upload"].get("daily_limit", 6)))
    try:
        post_hours = [int(h) % 24 for h in cfg["upload"].get("post_hours_utc", [])]
    except (TypeError, ValueError):
        post_hours = []
        log.warning("Invalid upload.post_hours_utc -> UTC windows disabled")

    while True:
        state = load_json(state_file, {}) or {}
        today = dt.date.today().isoformat()
        if state.get("day") != today:
            state = {"day": today, "uploads": 0}
        can_upload = state.get("uploads", 0) < daily_limit
        if not can_upload:
            log.info("Daily upload quota reached (%s/%s)", state.get("uploads"), daily_limit)
            if not once:
                # Fix boucle serree (2026-10-27) : quota atteint + fenetres
                # horaires -> l'ancien code relancait run_pipeline_once
                # (ideas+LLM+tts) SANS upload et SANS sleep, en boucle
                # rapprochee. On dort jusqu'a la premiere fenetre de demain.
                now = dt.datetime.now(dt.timezone.utc)
                if post_hours:
                    target = (now.replace(hour=0, minute=0, second=0,
                                          microsecond=0)
                              + dt.timedelta(days=1, hours=min(post_hours)))
                else:
                    target = now + dt.timedelta(seconds=interval)
                wait = max(0, int((target - now).total_seconds()))
                log.info("Quota atteint -> reprise a %s UTC (attente %s)",
                         target.strftime("%Y-%m-%d %H:%M"),
                         dt.timedelta(seconds=wait))
                time.sleep(wait)
                continue

        if can_upload and post_hours and not once:
            now = dt.datetime.now(dt.timezone.utc)
            target = _next_window_start(now, post_hours)
            wait = max(0, int((target - now).total_seconds()))
            log.info("Next US posting window: %s UTC (waiting %s)",
                     target.strftime("%Y-%m-%d %H:%M"), dt.timedelta(seconds=wait))
            time.sleep(wait)

        failed = False
        try:
            result = run_pipeline_once(cfg, do_upload=can_upload)
            if can_upload and result.get("youtube_id"):
                state["uploads"] = state.get("uploads", 0) + 1
                save_json(state_file, state)
        except Exception:
            failed = True
            log.error("Pipeline failed:\n%s", traceback.format_exc())

        if once:
            break
        if failed:
            # Avant ce correctif, un echec pendant la fenetre de grace (5 min)
            # relançait le pipeline toutes les ~30 s en boucle serree.
            log.info("Pipeline failed -> retry in 5 min (no tight loop)")
            time.sleep(300)
        elif not post_hours:
            log.info("Next run in %s h (Ctrl+C to stop)", interval // 3600)
            time.sleep(interval)
