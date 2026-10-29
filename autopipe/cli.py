"""Interface en ligne de commande AutoPipe."""
import argparse
import sys

from .config import get_config, get_paths, load_config
from .util import fix_proxy_env, get_logger, media_duration, setup_logging


def _init(verbose=False):
    cfg = load_config()
    setup_logging("DEBUG" if verbose else cfg["general"].get("log_level", "INFO"),
                  cfg["general"].get("log_file"))
    # Un proxy local mal annonce (HTTPS_PROXY=https://127.0.0.1:PORT) cassait
    # LLM, Pixabay et l'OAuth Google: on le neutralise selon network.proxy_mode.
    fix_proxy_env(cfg.get("network", {}).get("proxy_mode", "direct"))
    get_paths(cfg)
    return get_config()


def cmd_scout(a):
    from . import scout
    data = scout.run_scout(_init(a.verbose), keywords=a.keywords)
    for t in data.get("topics", [])[:5]:
        print("- %s" % t["topic"][:80])


def cmd_radar(a):
    """Scan multi-plateformes des contenus viraux (Reddit/HN/News/Trends/...)."""
    cfg = _init(a.verbose)
    from . import radar
    data = radar.run_radar(cfg, force=a.force)
    print("Radar: %s items / %s sujets en %.1fs -> %s" % (
        len(data.get("items", [])), len(data.get("topics", [])),
        data.get("duration_s", 0.0), get_paths(cfg).radar / "latest.json"))
    for platform, status in (data.get("sources") or {}).items():
        state = "OK" if not status.get("error") else "ERREUR: %s" % status["error"]
        print("  %-14s %s items  %s" % (platform, status.get("items", 0), state))
    print("\nTop sujets viraux:")
    for t in data.get("topics", [])[:10]:
        print("- [%s] %s" % (t.get("platform", "?"), str(t.get("topic", ""))[:80]))


def cmd_ideas(a):
    from . import ideas
    ideas_list, saved = ideas.run_ideas(_init(a.verbose), count=a.count)
    for i, idea in enumerate(ideas_list):
        print("[%s] %s" % (idea["id"], idea["title"]))
        print("   -> %s" % saved[i])


def cmd_remake(a):
    cfg = _init(a.verbose)
    from . import remake
    ideas_list, saved = remake.run_remake(cfg, count=a.count)
    if not ideas_list:
        print("Aucun remake valide (voir log)")
        sys.exit(1)
    for idea in ideas_list:
        print("[%s] %s" % (idea["id"], idea["title"]))
        print("   original: %s (%s vues)" % (idea.get("source_short_title", "?"),
                                             idea.get("source_short_views", 0)))
        print("   originalite max: %s" % (idea.get("originality", {}).get("max_jaccard")))
        print("   hook: %s" % idea.get("hook"))


def cmd_tts(a):
    cfg = _init(a.verbose)
    from . import tts
    from .util import load_json
    paths = get_paths(cfg)
    idea = a.idea
    if not idea:
        idea = paths.cache / "manual_script.txt"
        if not idea.exists():
            print("Donne un chemin de fichier texte: autopipe tts <fichier.txt>")
            sys.exit(1)
    text = open(idea, encoding="utf-8").read()
    out_mp3 = paths.audio_cache / "manual_voice.mp3"
    out_timings = paths.audio_cache / "manual_timings.json"
    words = tts.synthesize(text, out_mp3, out_timings,
                           cfg["tts"].get("voice"), cfg["tts"].get("rate"), cfg["tts"].get("volume"))
    print("Audio: %s (%.1fs, %s mots)" % (out_mp3, words[-1]["end"], len(words)))


def cmd_subs(a):
    cfg = _init(a.verbose)
    from .subs import build_srt_from_timings
    out = a.output or (a.timings.rsplit(".", 1)[0] + ".srt")
    build_srt_from_timings(a.timings, out, cfg)
    print("SRT: %s" % out)


def cmd_fetch(a):
    cfg = _init(a.verbose)
    from . import fetch_media
    clips = fetch_media.run_fetch(a.queries, cfg)
    print("%s clips prets" % len(clips))


def cmd_video(a):
    cfg = _init(a.verbose)
    from .util import load_json
    from . import video
    idea = load_json(a.idea)
    paths = get_paths(cfg)
    out_dir = paths.output / str(idea["id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    audio = a.audio or (out_dir / "voice.mp3")
    timings = a.timings or (out_dir / "timings.json")
    # fetch_media sauvegarde media_clips.json ; pexels_clips.json est l'ancien
    # nom, garde en fallback pour reprendre les clips deja telecharges.
    clips = load_json(paths.cache / "media_clips.json", [])
    if not clips:
        clips = load_json(paths.cache / "pexels_clips.json", []) or []
    final = video.run_video(idea, audio, timings, clips, cfg)
    print("Video: %s" % final)


def cmd_upload(a):
    cfg = _init(a.verbose)
    from .upload import upload_video
    vid = upload_video(a.file, a.title, a.description or "", a.tags or [], cfg,
                       privacy=a.privacy, pinned_comment=a.comment)
    print("https://youtu.be/%s" % vid)


def cmd_analytics(a):
    cfg = _init(a.verbose)
    from . import analytics
    if a.ids:
        analytics.record(a.ids, cfg)
    if getattr(a, "retention", False):
        # Boucle Viewed vs Swiped Away (proxy averageViewPercentage).
        # Necessite `autopipe analytics-auth` une seule fois.
        result = analytics.refresh_retention(cfg)
        status = result.get("status") if isinstance(result, dict) else None
        if status in ("unauthorized_or_error", "no_videos"):
            print("Retention indisponible (%s) - autorise avec: "
                  "autopipe analytics-auth" % status)
    analytics.print_summary(cfg)


def cmd_analytics_auth(a):
    """Autorisation Analytics API (token separe, le token d'upload est
    jamais touche) : metrique averageViewPercentage = proxy Studio
    'Viewed vs Swiped Away' cible 70 %."""
    cfg = _init(a.verbose)
    from . import analytics
    svc = analytics.get_analytics_service(cfg, interactive=True)
    if svc is None:
        print("Echec: autorisation analytics non obtenue (voir log).")
        sys.exit(1)
    print("Token analytics OK -> %s" % analytics._analytics_token_file(cfg))
    result = analytics.refresh_retention(cfg)
    print("Retention: status=%s" % result.get("status"))
    rows = result.get("rows") or []
    if rows:
        print("%-15s %8s %8s" % ("ID", "VUES", "RET%"))
        for r in rows:
            print("%-15s %8s %8s" % (r["id"], r["views"], r["avg_view_pct"]))


def cmd_visual(a):
    """MODULE 1 : analyse OpenCV des top hits viraux + application au rendu."""
    cfg = _init(a.verbose)
    from .visual_pattern import (aggregate_patterns, analyze_top_hits,
                                 apply_to_video_cfg, save_patterns_latest)
    hits = None
    if a.hits:
        from .util import load_json
        hits = (load_json(a.hits) or {}).get("hits", [])
    metrics = analyze_top_hits(cfg, hits=hits, max_videos=a.max_videos)
    if not metrics:
        print("Aucun pattern visuel extrait (OpenCV absent ou hits indisponibles)")
        sys.exit(1)
    pattern = save_patterns_latest(cfg, aggregate_patterns(list(metrics.values())))
    if apply_to_video_cfg(cfg, pattern):
        print("Patterns appliques a cfg['video'] (preview CLI, non persiste)")
    print("Pattern dominant (%s echantillons):" % pattern["n_samples"])
    print("  coupes/s: %s   intervalle: %ss" % (
        pattern["cuts_per_second"], pattern.get("cut_interval_s")))
    print("  zoom/s: %s   zone sous-titres: %s" % (
        pattern["zoom_ratio"], pattern["subtitle_zone"]))
    print("  palette: %s" % ", ".join(pattern["dominant_colors"]))
    print("  lum/contraste: %s / %s" % (pattern["brightness"], pattern["contrast"]))
    print("  detail par hit: %s" % ", ".join(sorted(metrics)))


def cmd_trend(a):
    """MODULE 2 : arbitre predictif de tendances (momentum vs saturation)."""
    cfg = _init(a.verbose)
    from . import trend_arbiter
    doc = trend_arbiter.run_trend_arbiter(cfg, max_topics=a.max_topics,
                                          fetch_saturation=not a.no_saturation)
    topics = doc.get("topics", [])
    if not topics:
        print("Aucun signal de tendance disponible (voir log)")
        sys.exit(1)
    print("Trend Arbiter: %s sujets evalues" % len(topics))
    for t in topics[:10]:
        print("  momentum=%.2f  rise=%s  saturation=%s vues  %s" % (
            t.get("momentum", 0.0), t.get("rise"), t.get("saturation_views"),
            t.get("query", "")[:60]))
    print("Snapshot: %s" % (get_paths(cfg).trend / "arbiter_latest.json"))


def cmd_optimize(a):
    """MODULE 3 : apprentissage des patterns gagnants + application cfg."""
    cfg = _init(a.verbose)
    from . import optimizer
    doc = optimizer.run_optimizer(cfg)
    print("Optimizer: status=%s  echantillons=%s/%s" % (
        doc.get("status"), doc.get("n_samples"), doc.get("min_samples")))
    for p in doc.get("patterns", []):
        print("  pattern: %s (n=%s, engagement moyen %s)" % (
            p["label"], p.get("n"), p.get("mean_engagement")))
    if doc.get("status") == "learned":
        applied = optimizer.apply_config(cfg, patterns_doc=doc)
        for line in applied:
            print("  ajustement: %s" % line)
    print("Patterns: %s" % (get_paths(cfg).optimizer / "patterns.json"))


def cmd_baseline(a):
    """V3 : etat baseline, delta, kill switch et patterns actifs (lecture)."""
    cfg = _init(a.verbose)
    from . import optimizer
    samples = optimizer._join_samples(cfg)
    bdoc = optimizer.load_json(
        optimizer._optimizer_file(cfg, "baseline.json")) or {}
    target = int(bdoc.get("target")
                 or cfg.get("optimizer", {}).get("baseline_size", 10))
    print("=== Baseline (data/optimizer/baseline.json) ===")
    if not bdoc:
        print("  absente : 0/%d Shorts de reference" % target)
        print("  -> ajustements numeriques SUSPENDUS jusqu'au verrouillage")
    elif not bdoc.get("locked"):
        print("  en cours : %d/%d Shorts" % (int(bdoc.get("n") or 0), target))
        print("  -> ajustements numeriques SUSPENDUS jusqu'au verrouillage")
    else:
        m = bdoc.get("metrics") or {}
        print("  VERROUILLEE (%d Shorts, le %s)" % (
            int(bdoc.get("n") or 0), bdoc.get("locked_at")))
        print("  engagement moyen %s%%   retention moyenne %s%%" % (
            m.get("engagement_mean"), m.get("avg_view_pct_mean")))
        for fam, mean in sorted((m.get("by_family") or {}).items()):
            print("    famille %-10s %s%%" % (fam, mean))
        delta = optimizer.baseline_delta(cfg, samples)
        if delta is None:
            print("  delta : indisponible")
        elif delta.get("status") == "pending":
            print("  delta : en attente (0/%d Shorts post-baseline)" %
                  int(delta.get("target") or target))
        else:
            d = delta.get("delta") or {}
            print("  delta post-baseline (%d/%s Shorts) :" %
                  (int(delta.get("n_after") or 0),
                   delta.get("target") or target))
            print("    engagement %+s pts   retention %+s pts" % (
                d.get("engagement_mean"), d.get("avg_view_pct_mean")))
    ks = optimizer.load_json(
        optimizer._optimizer_file(cfg, "killswitch.json")) or {}
    banned = ks.get("banned_families") or {}
    niche = ks.get("niche") or {}
    print("=== Kill switch (data/optimizer/killswitch.json) ===")
    if not banned and not niche.get("alert"):
        print("  rien de banni, niche en regle")
    else:
        for fam, info in sorted(banned.items()):
            print("  famille %s BANIE (%d Shorts restants, depuis %s)" % (
                fam, int(info.get("remaining", 0)),
                info.get("banned_at")))
        if niche.get("alert"):
            print("  niche '%s' en PAUSE : %d Shorts restants (streak %d "
                  "< %s%%)" % (niche.get("topic"),
                               int(niche.get("escape_remaining", 0)),
                               int(niche.get("streak", 0)),
                               (ks.get("thresholds") or {}).get(
                                   "niche_pct", 40.0)))
    ap = optimizer.load_json(
        optimizer._optimizer_file(cfg, "active_patterns.json")) or {}
    act = [p for p in (ap.get("active") or [])
           if int(p.get("remaining", 0)) > 0]
    print("=== Patterns actifs (data/optimizer/active_patterns.json) ===")
    if not act:
        oc = cfg.get("optimizer", {})
        print("  aucun (seuil : > %s%% sur %d Shorts)" % (
            oc.get("active_min_pct", 70.0),
            int(oc.get("active_min_n", 3))))
    else:
        for p in act:
            print("  FORCE %s=%s : %.0f%% moyen sur %d Shorts "
                  "(%d restants)" % (p.get("dimension"), p.get("value"),
                                     float(p.get("mean_avg_view_pct") or 0),
                                     int(p.get("n") or 0),
                                     int(p.get("remaining", 0))))


def cmd_hook_ab(a):
    """A/B hooks : statistiques par variante + meilleure famille."""
    cfg = _init(a.verbose)
    from . import hook_ab
    doc = hook_ab.summarize(cfg)
    status = doc.get("status", "ok")
    print("hook-ab: status=%s" % status)
    for name, st in sorted(doc.get("variants", {}).items()):
        # summarize() expose "assigned" (ideas taggees) et "measured"
        # (mesures de retention rattachees) - pas "n".
        print("  variante %s: assignees=%s  mesurees=%s  engagement moyen=%s  views moyennes=%s"
              % (name, st.get("assigned"), st.get("measured"),
                 st.get("mean_engagement"), st.get("mean_views")))
    family = hook_ab.best_family(cfg)
    if family:
        print("  meilleure famille: %s" % family)
    else:
        print("  meilleure famille: (pas encore decidable - il faut >= 3 "
              "mesures par variante et >= 5 pts d'ecart)")
    print("Summary: %s" % (get_paths(cfg).hook_ab / "summary.json"))


def cmd_research(a):
    """MODULE 4 : benchmarks quantitatifs de la niche (scrape + rapport)."""
    cfg = _init(a.verbose)
    from . import research
    if a.force:
        research.run_research(cfg, force=True)
    print(research.report(cfg))


def cmd_monetize(a):
    """MODULE 5 : suivi YPP (abonnes, vues 90 j, heures, ETA, revenus)."""
    cfg = _init(a.verbose)
    from . import monetization
    print(monetization.report(cfg))


def cmd_brain(a):
    """Memoire de publication : registre idea_id <-> youtube_id."""
    cfg = _init(a.verbose)
    from . import registry
    doc = registry.report(cfg)
    for key in ("total", "linked_to_idea", "linked_to_ab_pair",
                "first_published", "file"):
        print("%s: %s" % (key, doc.get(key)))


def cmd_run(a):
    cfg = _init(a.verbose)
    from .scheduler import run_pipeline_once
    result = run_pipeline_once(cfg, do_upload=not a.no_upload)
    print("Termine: %s" % result.get("video"))


def cmd_serve(a):
    cfg = _init(a.verbose)
    from .scheduler import run_loop
    try:
        run_loop(cfg, once=a.once)
    except KeyboardInterrupt:
        print("\nArrete proprement.")


def cmd_test_tts(a):
    cfg = _init(a.verbose)
    from . import tts
    from .subs import group_words
    mp3, words = tts.test_voice()
    cues = group_words(words)
    print("OK: %s" % mp3)
    print("Mots: %s | Cues sous-titres: %s | Duree: %.1fs" % (
        len(words), len(cues), words[-1]["end"]))


def cmd_test_video(a):
    cfg = _init(a.verbose)
    from pathlib import Path
    from . import tts, video
    from .subs import build_srt_from_timings
    paths = get_paths(cfg)
    idea = {"id": "test-video", "title": "Test AutoPipe",
            "description": "Video de test AutoPipe.", "tags": ["test", "shorts"],
            "source_topic": "test"}
    out_dir = paths.output / "test-video"
    out_dir.mkdir(parents=True, exist_ok=True)
    audio = out_dir / "voice.mp3"
    timings = out_dir / "timings.json"
    tts.synthesize("Voici une video de test generee par AutoPipe. "
                   "Si tu vois les sous-titres et le fond anime, tout fonctionne.",
                   audio, timings, cfg["tts"].get("voice"),
                   cfg["tts"].get("rate"), cfg["tts"].get("volume"))
    srt = out_dir / "subs.srt"
    build_srt_from_timings(timings, srt, cfg)
    final = video.run_video(idea, audio, timings, [], cfg)
    print("OK: %s (%.1fs)" % (final, media_duration(final)))


def cmd_check_token(a):
    """Sante du token YouTube ; alerte Gmail si une re-autorisation est requise."""
    cfg = _init(a.verbose)
    from .alerts import notify_if_action_needed, token_health
    health = token_health(cfg)
    print("Token YouTube: %s%s" % (
        health["status"],
        "" if health["status"] == "ok" else " (%s)" % health.get("reason", "")))
    _, sent = notify_if_action_needed(cfg, force=a.force, cause="reauth")
    if sent:
        print("Email d'alerte envoye (re-autorisation requise, voir reauth_247.bat).")
    if health["status"] != "ok":
        # CI : echec immediat -> notify-failure prend le relais, et on evite de
        # generer une video inuploadable (ou un flow OAuth qui bloquerait).
        sys.exit(1)


def cmd_notify_failure(a):
    """Alerte Gmail d'echec de run (appelle par GitHub Actions en if: failure())."""
    cfg = _init(a.verbose)
    from .alerts import notify_if_action_needed, token_health
    health = token_health(cfg)
    if health["status"] != "ok":
        # Echec probablement cause par le token : l'email "re-autorisation"
        # (anti-spam 24 h, envoye par check-token) couvre deja ce cas.
        _, sent = notify_if_action_needed(cfg, force=False, cause="reauth")
    else:
        _, sent = notify_if_action_needed(cfg, force=a.force, cause="runfail")
    print("Notification: %s" % ("email envoye" if sent else
          "pas d'email (Gmail non configure ou alerte deja envoyee recemment)"))


def cmd_set_country(a):
    """Force la localisation US de la page YouTube (pays de la chaine)."""
    cfg = _init(a.verbose)
    from .upload import ensure_channel_country
    result = ensure_channel_country(cfg)
    if result is None:
        print("Echec: localisation non appliquee (voir log).")
        sys.exit(1)
    state = "deja correcte" if not result["changed"] else "APPLIQUEE"
    prev = " (avant: %s)" % result["previous"] if result.get("previous") else ""
    print("Localisation de la page YouTube: %s -> %s%s" % (
        result["country"], state, prev))
    print("Effet visible sur la page chaine sous peu (cache YouTube possible).")


def cmd_setup(a):
    cfg = _init(a.verbose)
    paths = get_paths(cfg)
    import shutil
    ok = True
    def check(label, cond, hint=""):
        nonlocal ok
        print("[%s] %s %s" % ("OK " if cond else "MANQUE", label, hint if not cond else ""))
        if not cond:
            ok = False
    check("ffmpeg", bool(shutil.which("ffmpeg")), "installe avec: choco install ffmpeg")
    check("ffprobe", bool(shutil.which("ffprobe")))
    try:
        import edge_tts, yt_dlp, tomli, requests  # noqa
        check("python deps", True)
    except ImportError as e:
        check("python deps", False, "pip install -r requirements.txt (%s)" % e)
    try:
        import googleapiclient, google_auth_oauthlib  # noqa
        check("google api client", True)
    except ImportError as e:
        check("google api client", False, "pip install -r requirements.txt (%s)" % e)
    # Cle media requise selon le fournisseur EFFECTIF (cf. fetch_media.py :
    # provider "auto" = Pixabay si une cle pixabay existe, sinon Pexels).
    fm_cfg = cfg["fetch_media"]
    fm_provider = (fm_cfg.get("provider") or "auto").lower()
    if fm_provider == "pexels":
        check("cle Pexels", bool(fm_cfg.get("pexels_api_key")),
              "mettre fetch_media.pexels_api_key dans autopipe.toml (provider=pexels force)")
    elif fm_provider == "pixabay":
        check("cle Pixabay", bool(fm_cfg.get("pixabay_api_key")),
              "mettre fetch_media.pixabay_api_key dans autopipe.toml (provider=pixabay force)")
    else:
        has_media_key = bool(fm_cfg.get("pixabay_api_key") or fm_cfg.get("pexels_api_key"))
        check("cle media (Pixabay ou Pexels)", has_media_key,
              "mettre fetch_media.pixabay_api_key OU pexels_api_key dans autopipe.toml")
    check("client_secret.json", (paths.root / "client_secret.json").exists(),
          "voir README section OAuth YouTube")
    check("LLM (optionnel)", bool(cfg["llm"].get("base_url")),
          "scripts plus puissants avec un LLM configure")
    try:
        import cv2  # noqa
        check("OpenCV (Module 1 visual)", True)
    except ImportError:
        check("OpenCV (Module 1 visual)", False,
              "pip install opencv-python-headless (module visuel en mode degrade sinon)")
    print("\nStatut: %s" % ("pret" if ok else "configuration incomplette (voir ci-dessus)"))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="autopipe",
                                     description="AutoPipe : pipeline YouTube automatise")
    parser.add_argument("-v", "--verbose", action="store_true", help="log detalille")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scout", help="detecter les tendances")
    p.add_argument("-k", "--keywords", nargs="*", help="mots-cles supplementaires")
    p.set_defaults(fn=cmd_scout)

    p = sub.add_parser("radar", help="scan multi-plateformes des contenus viraux")
    p.add_argument("--force", action="store_true",
                   help="ignore le cache TTL et force un scan complet")
    p.set_defaults(fn=cmd_radar)

    p = sub.add_parser("ideas", help="generer idees + scripts")
    p.add_argument("-n", "--count", type=int, default=5)
    p.set_defaults(fn=cmd_ideas)

    p = sub.add_parser("remake", help="Super-Remake: Shorts viraux -> remakes 10%% Better")
    p.add_argument("-n", "--count", type=int, default=None,
                   help="nombre de remakes (defaut: remake.count)")
    p.set_defaults(fn=cmd_remake)

    p = sub.add_parser("tts", help="synthese vocale d'un script txt")
    p.add_argument("idea", nargs="?", help="chemin du fichier .txt du script")
    p.set_defaults(fn=cmd_tts)

    p = sub.add_parser("subs", help="generer le SRT depuis un timings.json")
    p.add_argument("timings")
    p.add_argument("-o", "--output")
    p.set_defaults(fn=cmd_subs)

    p = sub.add_parser("fetch", help="telecharger des clips Pexels")
    p.add_argument("queries", nargs="*")
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("video", help="assembler une video depuis une idee JSON")
    p.add_argument("idea", help="chemin du JSON d'idee")
    p.add_argument("--audio")
    p.add_argument("--timings")
    p.set_defaults(fn=cmd_video)

    p = sub.add_parser("upload", help="publier un fichier video")
    p.add_argument("file")
    p.add_argument("--title", required=True)
    p.add_argument("--description", default="")
    p.add_argument("--tags", nargs="*", default=[])
    p.add_argument("--privacy", default=None)
    p.add_argument("--comment", default=None,
                   help="commentaire publie sous la video apres l'upload")
    p.set_defaults(fn=cmd_upload)

    p = sub.add_parser("analytics", help="stats des videos")
    p.add_argument("--ids", nargs="*", help="IDs YouTube a rafraichir")
    p.add_argument("--retention", action="store_true",
                   help="inclure la metrique Viewed vs Swiped Away "
                        "(proxy averageViewPercentage, demande analytics-auth)")
    p.set_defaults(fn=cmd_analytics)

    p = sub.add_parser("analytics-auth",
                       help="autoriser une fois la metrique retention "
                            "(Analytics API, token separe - ne touche pas "
                            "au token d'upload)")
    p.set_defaults(fn=cmd_analytics_auth)

    p = sub.add_parser("visual", help="MODULE 1: analyser les codes visuels des tops viraux")
    p.add_argument("--hits", help="JSON de hits (defaut: data/remake/hits_latest.json via cache)")
    p.add_argument("--max-videos", type=int, default=None,
                   help="nb max de hits analyses (defaut: visual.max_videos)")
    p.set_defaults(fn=cmd_visual)

    p = sub.add_parser("trend", help="MODULE 2: scoring predictif des tendances")
    p.add_argument("--max-topics", type=int, default=None,
                   help="nb max de sujets (defaut: trend.max_topics)")
    p.add_argument("--no-saturation", action="store_true",
                   help="skip la mesure de saturation YouTube (yt-dlp)")
    p.set_defaults(fn=cmd_trend)

    p = sub.add_parser("optimize", help="MODULE 3: boucle d'auto-apprentissage analytics")
    p.set_defaults(fn=cmd_optimize)

    p = sub.add_parser("hook-ab",
                       help="A/B hooks : stats par variante + meilleure famille")
    p.set_defaults(fn=cmd_hook_ab)

    p = sub.add_parser("baseline",
                       help="V3 : etat baseline 10 Shorts, delta, kill "
                            "switch et patterns actifs (lecture seule)")
    p.set_defaults(fn=cmd_baseline)

    p = sub.add_parser("research",
                       help="MODULE 4: benchmarks chiffres de la niche (web)")
    p.add_argument("--force", action="store_true",
                   help="re-scraper tout de suite (ignore le cache TTL)")
    p.set_defaults(fn=cmd_research)

    p = sub.add_parser("monetize",
                       help="MODULE 5: suivi YPP + ETA + revenus estimes")
    p.set_defaults(fn=cmd_monetize)

    p = sub.add_parser("brain",
                       help="registre memoire: video <-> idee + rapport")
    p.set_defaults(fn=cmd_brain)

    p = sub.add_parser("run", help="pipeline complet une fois")
    p.add_argument("--no-upload", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("serve", help="mode continu 24/7")
    p.add_argument("--once", action="store_true", help="une seule iteration")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("test-tts", help="test de la voix")
    p.set_defaults(fn=cmd_test_tts)

    p = sub.add_parser("test-video", help="test d'assemblage video")
    p.set_defaults(fn=cmd_test_video)

    p = sub.add_parser("check-token",
                       help="verifier le token YouTube (+ alerte Gmail si re-autorisation requise)")
    p.add_argument("--force", action="store_true",
                   help="envoyer l'email meme si le token est valide (pour tester)")
    p.set_defaults(fn=cmd_check_token)

    p = sub.add_parser("notify-failure",
                       help="alerte Gmail d'echec de run (usage CI, if: failure)")
    p.add_argument("--force", action="store_true",
                   help="ignorer l'anti-spam de 2 h pour l'email d'echec")
    p.set_defaults(fn=cmd_notify_failure)

    p = sub.add_parser("set-country-us",
                       help="forcer la localisation US de la page YouTube (pays de la chaine)")
    p.set_defaults(fn=cmd_set_country)

    p = sub.add_parser("setup", help="verifier l'installation")
    p.set_defaults(fn=cmd_setup)

    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        print("\nInterrompu.")
        sys.exit(130)


if __name__ == "__main__":
    main()
