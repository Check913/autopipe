"""Continuous orchestration: full pipeline loop (24/7) with UTC posting windows."""
import datetime as dt
import time
import traceback
from pathlib import Path

from . import analytics, fetch_media, ideas, scout, subs, tts, video
from .config import get_config, get_paths
from .upload import get_service, pinned_comment_text, upload_video
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

    # Commentaire et metadonnees calcules meme sans upload : la pre-generation
    # (publication differee a l'heure exacte) a besoin de tout pour uploader
    # plus tard sans regenerer l'idee.
    comment = pinned_comment_text(idea, cfg)
    result = {"idea": idea["id"], "title": idea.get("title"), "video": str(final),
              "description": idea.get("description", ""), "tags": idea.get("tags", []),
              "comment": comment, "pair_id": idea.get("pair_id")}

    # 7. Upload + stats
    if do_upload:
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


def _utcnow():
    return dt.datetime.now(dt.timezone.utc)


def _pending_path(paths):
    return paths.data / "pending_publish.json"


def _clear_pending(pending_file):
    try:
        pending_file.unlink(missing_ok=True)
    except OSError:
        pass


def _alert(cfg, subject, body):
    """Alerte email best-effort (jamais fatal)."""
    try:
        from .alerts import send_gmail_alert
        send_gmail_alert(cfg, subject, body)
    except Exception:
        pass


def publish_pending_video(cfg, paths, pending, pending_file=None):
    """Publie une video pre-generee EXACTEMENT a l'heure prevue.

    pending["publish_at"] : heure cible ISO (UTC). Si l'heure est dans le
    futur, l'upload demarre en PRIVE `publish_lead_s` avant la cible, puis le
    statut bascule en "public" pile a l'heure cible (YouTube date la
    publication publique de la bascule) : ponctualite a la seconde.
    Si l'heure cible est deja passee, publication directe en public le plus
    vite possible (jamais de video abandonnee). Si pending contient
    "youtube_id" (tentative precedente bloquee en prive), on rebascule
    seulement, sans re-uploader.
    """
    video_path = Path(str(pending.get("video") or ""))
    idea = pending.get("idea") or {}
    title = str(pending.get("title") or idea.get("title") or "Untitled")
    description = str(pending.get("description") or idea.get("description") or "")
    tags = pending.get("tags") or idea.get("tags") or []
    comment = pending.get("comment")
    pair_id = pending.get("pair_id") or idea.get("pair_id")
    pub_at = dt.datetime.fromisoformat(str(pending["publish_at"]))
    lead = int(cfg["scheduler"].get("publish_lead_s", 120))

    if not pending.get("youtube_id") and not video_path.exists():
        log.error("Video pre-generee introuvable: %s", video_path)
        return {"published": False, "reason": "missing_video",
                "video": str(video_path)}

    uploaded_public = False
    now = _utcnow()
    if pending.get("youtube_id"):
        video_id = str(pending["youtube_id"])
        log.info("Reprise: video %s deja uploadee en prive -> bascule public", video_id)
        # La bascule public reste PILE a l'heure cible, meme apres un
        # redemarrage entre l'upload prive et la bascule.
        if now < pub_at:
            wait = (pub_at - now).total_seconds()
            log.info("Bascule public a %s UTC (attente %s)",
                     pub_at.strftime("%H:%M:%S"), dt.timedelta(seconds=int(wait)))
            time.sleep(max(0.0, wait))
            now = _utcnow()
    elif now < pub_at:
        wait = (pub_at - now).total_seconds() - lead
        if wait > 0:
            log.info("Publication exacte a %s UTC: upload prive dans %s",
                     pub_at.strftime("%Y-%m-%d %H:%M:%S"),
                     dt.timedelta(seconds=int(wait)))
            time.sleep(wait)
        log.info("Upload prive demarre (basculera en public a %s UTC)",
                 pub_at.strftime("%H:%M:%S"))
        video_id = upload_video(video_path, title, description, tags, cfg,
                                privacy="private")
        # Point de reprise: l'id de la video privee est persiste TOUT DE SUITE,
        # pour qu'un crash pendant l'attente se resume en bascule simple
        # (jamais un re-upload / video dupliquee).
        if pending_file is not None:
            pending["youtube_id"] = video_id
            save_json(pending_file, pending)
        now = _utcnow()
        if now < pub_at:
            time.sleep(max(0.0, (pub_at - now).total_seconds()))
            now = _utcnow()
    else:
        late_min = (now - pub_at).total_seconds() / 60.0
        log.warning("Cible %s UTC depassee de %.1f min -> publication publique "
                    "immediate (en retard, mais pas annulee)",
                    pub_at.strftime("%Y-%m-%d %H:%M"), late_min)
        video_id = upload_video(video_path, title, description, tags, cfg,
                                pinned_comment=comment)
        comment = None  # deja poste par upload_video
        uploaded_public = True  # pas de bascule necessaire

    # Bascule en public a l'heure cible exacte (ou immediatement en retard).
    # En retard pur l'upload est deja public: pas de bascule a faire.
    flipped = uploaded_public
    for attempt in range(4):
        try:
            get_service(cfg).videos().update(
                part="status",
                body={"id": video_id, "status": {"privacyStatus": "public"}}
            ).execute()
            flipped = True
            break
        except Exception as exc:
            log.warning("Bascule public echouee (%s), tentative %d/4",
                        exc, attempt + 1)
            time.sleep(5)
    if not flipped:
        log.critical("Video %s reste PRIVEE: bascule impossible. Action manuelle "
                     "requise dans YouTube Studio (ou relancer le bot: la "
                     "bascule sera re tentee).", video_id)
        _alert(cfg, "AutoPipe: video NON publiee (bascule public echouee)",
               "La video %s (%s) est uploadee mais reste PRIVEE.\n"
               "Basculez-la manuellement dans YouTube Studio." % (video_id, title))
        return {"published": False, "reason": "flip_failed",
                "youtube_id": video_id, "video": str(video_path)}
    log.info("VIDEO PUBLIEE (public) a %s UTC (cible %s): https://youtu.be/%s",
             _utcnow().strftime("%H:%M:%S"), pub_at.strftime("%H:%M:%S"), video_id)

    if comment:
        try:
            from .upload import post_pinned_comment
            post_pinned_comment(video_id, comment, cfg)
        except Exception as exc:
            log.warning("Commentaire non publie: %s", exc)
    try:
        analytics.record([video_id], cfg)
    except Exception as exc:
        log.warning("Stats initiales indisponibles: %s", exc)
    if pair_id:
        try:
            from .hook_ab import attach_video
            attach_video(pair_id, video_id, cfg)
        except Exception as exc:
            log.warning("Hook A/B attach impossible: %s", exc)
    return {"published": True, "youtube_id": video_id,
            "video": str(video_path)}


def run_loop(config=None, once=False):
    """Permanent loop with EXACT-time publishing.

    With [upload] post_hours_utc (e.g. [15, 21] = 15:00 and 21:00 UTC, peak
    US audience hours), each video is published EXACTLY at the window start:
      1. PRE-GENERATION: the video (idea, TTS, clips, editing) is fully built
         `pregen_minutes` before the window, without uploading
         (data/pending_publish.json = contrat de publication, survit aux
         redemarrages du bot) ;
      2. PUBLICATION A L'HEURE EXACTE: upload prive qui demarre
         `publish_lead_s` avant la cible, puis bascule public PILE a l'heure.
    Un bot relance en retard publie la video des que possible (fallback,
    jamais annule). Sans post_hours_utc: une generation+publication par
    scheduler.interval_hours (comportement historique).
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

    pending_file = _pending_path(paths)
    lead = int(cfg["scheduler"].get("publish_lead_s", 120))
    pregen_min = int(cfg["scheduler"].get("pregen_minutes", 20))

    while True:
        state = load_json(state_file, {}) or {}
        today = dt.date.today().isoformat()
        if state.get("day") != today:
            state = {"day": today, "uploads": 0}
        can_upload = state.get("uploads", 0) < daily_limit

        pending_quota = load_json(pending_file, None)
        quota_block = not can_upload and not (isinstance(pending_quota, dict)
                                              and pending_quota.get("publish_at"))
        if quota_block:
            log.info("Daily upload quota reached (%s/%s)", state.get("uploads"), daily_limit)
            if not once:
                # Fix boucle serree (2026-10-27) : quota atteint + fenetres
                # horaires -> on dort jusqu'a la premiere fenetre de demain
                # (pas de pipeline a vide, pas de pre-generation inutile).
                now = _utcnow()
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
            # once + quota : le pipeline ci-dessous tournera sans upload.

        if once or not post_hours:
            # Mode manuel (`run`, `serve --once`) ou fenetres desactivees :
            # chaine complete immediate (comportement historique).
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
                # Avant ce correctif, un echec pendant la fenetre de grace
                # (5 min) relançait le pipeline toutes les ~30 s.
                log.info("Pipeline failed -> retry in 5 min (no tight loop)")
                time.sleep(300)
            else:
                log.info("Next run in %s h (Ctrl+C to stop)", interval // 3600)
                time.sleep(interval)
            continue

        # --- Mode fenetres horaires: pre-generation + publication a l'heure exacte ---
        now = _utcnow()
        pending = load_json(pending_file, None)
        if not isinstance(pending, dict):
            pending = None
        if pending and pending.get("publish_at"):
            try:
                pub_at = dt.datetime.fromisoformat(str(pending["publish_at"]))
            except (TypeError, ValueError):
                log.error("pending_publish.json corrompu (publish_at=%r) "
                          "-> ignore", pending.get("publish_at"))
                _clear_pending(pending_file)
                time.sleep(60)  # pas de boucle serree sur un fichier corrompu
                continue
            if now < pub_at - dt.timedelta(seconds=lead):
                wait = int((pub_at - dt.timedelta(seconds=lead) - now).total_seconds())
                log.info("Video pre-generee en attente -> publication a %s UTC "
                         "(attente %s)", pub_at.strftime("%Y-%m-%d %H:%M:%S"),
                         dt.timedelta(seconds=wait))
                time.sleep(max(1, wait))
                continue
            try:
                res = publish_pending_video(cfg, paths, pending,
                                            pending_file=pending_file)
            except Exception:
                log.error("Publication en echec:\n%s", traceback.format_exc())
                time.sleep(60)
                continue
            if res.get("published"):
                state["uploads"] = state.get("uploads", 0) + 1
                save_json(state_file, state)
                save_json(paths.data / "last_run.json", {
                    "idea": pending.get("idea_id"),
                    "title": pending.get("title"),
                    "video": pending.get("video"),
                    "youtube_id": res.get("youtube_id")})
                _clear_pending(pending_file)
            elif res.get("reason") == "flip_failed":
                # La video privee existe: on memorise son id pour rebasculer
                # sans re-uploader, et on retente bientot.
                pending["youtube_id"] = res.get("youtube_id")
                save_json(pending_file, pending)
                log.info("Bascule public en attente -> nouvelle tentative "
                         "dans 60 s")
                time.sleep(60)
            elif res.get("reason") == "missing_video":
                # Fichier disparu (nettoyage manuel...) : regeneration
                # immediate + publication (retard assume, jamais annule).
                _clear_pending(pending_file)
                _alert(cfg, "AutoPipe: video pre-generee perdue",
                       "pending_publish.json pointait vers un fichier "
                       "introuvable (%s). Regeneration + publication "
                       "immediate." % res.get("video"))
                try:
                    result = run_pipeline_once(cfg, do_upload=True)
                    if result.get("youtube_id"):
                        state["uploads"] = state.get("uploads", 0) + 1
                        save_json(state_file, state)
                except Exception:
                    log.error("Pipeline failed:\n%s", traceback.format_exc())
                    time.sleep(300)
            continue

        target = _next_window_start(now, post_hours)
        pregen_start = target - dt.timedelta(minutes=pregen_min)
        if now >= pregen_start:
            if now >= target:
                # Fenetre active sans pre-generation (bot lance en retard,
                # pregen echouee...) : generation + publication immediates.
                log.warning("Fenetre %s UTC active sans pre-generation -> "
                            "generation + publication immediates (retard)",
                            target.strftime("%Y-%m-%d %H:%M"))
                try:
                    result = run_pipeline_once(cfg, do_upload=True)
                    if result.get("youtube_id"):
                        state["uploads"] = state.get("uploads", 0) + 1
                        save_json(state_file, state)
                except Exception:
                    log.error("Pipeline failed:\n%s", traceback.format_exc())
                    time.sleep(300)
                continue
            # Pre-generation pour la fenetre a venir (sans upload).
            log.info("Pre-generation pour la fenetre %s UTC (avance: %s)",
                     target.strftime("%Y-%m-%d %H:%M"),
                     dt.timedelta(seconds=int((target - now).total_seconds())))
            try:
                result = run_pipeline_once(cfg, do_upload=False)
                pending = {
                    "publish_at": target.isoformat(),
                    "video": result.get("video"),
                    "idea_id": result.get("idea"),
                    "title": result.get("title"),
                    "description": result.get("description"),
                    "tags": result.get("tags"),
                    "comment": result.get("comment"),
                    "pair_id": result.get("pair_id"),
                    "created": now.isoformat(),
                }
                save_json(pending_file, pending)
                log.info("Video pre-generee -> publication programme a %s UTC: %s",
                         target.strftime("%Y-%m-%d %H:%M:%S"), pending["title"])
            except Exception:
                log.error("Pre-generation echouee (retry in 5 min):\n%s",
                          traceback.format_exc())
                time.sleep(300)
            continue

        wait = int((pregen_start - now).total_seconds())
        log.info("Prochaine fenetre: %s UTC (pre-generation a %s, attente %s)",
                 target.strftime("%Y-%m-%d %H:%M"),
                 pregen_start.strftime("%Y-%m-%d %H:%M"),
                 dt.timedelta(seconds=wait))
        time.sleep(max(1, wait))
