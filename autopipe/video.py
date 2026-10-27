"""Assemblage video vertical 1080x1920 avec ffmpeg : clips + voix + sous-titres."""
import random
from pathlib import Path

from .config import get_config, get_paths
from .subs import build_srt_from_timings
from .util import get_logger, media_duration, run_ffmpeg

log = get_logger("video")


def _vf(width, height, fps):
    return ("scale=%d:%d:force_original_aspect_ratio=increase,"
            "crop=%d:%d,fps=%d,format=yuv420p" % (width, height, width, height, fps))


def normalize_clip(src, dest, width=1080, height=1920, fps=30, take=None):
    args = ["-i", str(src)]
    if take:
        args = ["-t", "%.2f" % take] + args
    args += ["-vf", _vf(width, height, fps), "-an", "-c:v", "libx264",
             "-preset", "veryfast", "-crf", "20", str(dest)]
    run_ffmpeg(args)


def fallback_background(dest, duration, width=1080, height=1920, fps=30, colors=None):
    """Fond degrade anime si aucun clip stock disponible.

    MODULE 1 : `colors` permet d'injecter la palette dominante apprise des
    Shorts viraux (cf. visual_pattern.apply_to_video_cfg). Sans palette
    fournie, le degrade historique est conserve (retrocompatible).
    """
    c0, c1 = "0x0f2027", "0x203a43"
    if colors and len(colors) >= 2:
        c0, c1 = str(colors[0]), str(colors[1])  # format "#rrggbb" accepte par ffmpeg
    src = ("gradients=s=%dx%d:d=%.2f:speed=0.03:"
           "c0=%s:c1=%s:c2=0x2c5364" % (width, height, duration, c0, c1))
    run_ffmpeg(["-f", "lavfi", "-i", src, "-r", str(fps), "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(dest)])


def build_video_track(clips, audio_dur, work_dir, width=1080, height=1920, fps=30,
                      loop_tail=0.0, colors=None):
    """Construit la piste video de duree audio a partir des clips (ou fond)."""
    work_dir = Path(work_dir)
    seg_dir = work_dir / "segs"
    seg_dir.mkdir(parents=True, exist_ok=True)
    track = work_dir / "track.mp4"

    if not clips:
        fallback_background(track, audio_dur, width, height, fps, colors=colors)
        return track

    clips = list(clips)
    random.shuffle(clips)
    segs = []
    loop_src = None
    if loop_tail > 0:
        loop_src = Path(clips[0]["path"])
        if not loop_src.exists():
            loop_src = None
    mid_dur = max(1.0, audio_dur - 2.0 * loop_tail) if loop_src else audio_dur
    per = mid_dur / len(clips)
    for i, clip in enumerate(clips):
        src = Path(clip["path"])
        if not src.exists():
            log.warning("Clip absent, ignore: %s", src)
            continue
        try:
            d = media_duration(src)
        except Exception as exc:
            log.warning("Clip illisible %s: %s", src, exc)
            continue
        take = min(d, per + 1.0)
        dest = seg_dir / ("seg%03d.mp4" % len(segs))
        normalize_clip(src, dest, width, height, fps, take=take)
        segs.append(dest)
        log.info("Segment %s/%s : %.1fs depuis %s", len(segs), len(clips), take, src.name)

    if not segs:
        fallback_background(track, audio_dur, width, height, fps, colors=colors)
        return track

    if loop_src:
        head = seg_dir / "loop_head.mp4"
        tail = seg_dir / "loop_tail.mp4"
        try:
            normalize_clip(loop_src, head, width, height, fps, take=loop_tail)
            normalize_clip(loop_src, tail, width, height, fps, take=loop_tail)
            segs = [head] + segs + [tail]
            log.info("Boucle de fin: %.2fs de re-intro en debut ET fin de video", loop_tail)
        except Exception as exc:
            log.warning("Boucle de fin ignoree: %s", exc)

    listfile = work_dir / "concat.txt"
    listfile.write_text(
        "".join("file '%s'\n" % s.resolve().as_posix() for s in segs), encoding="utf-8")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listfile),
                "-c", "copy", str(track)])
    return track


def _sub_style(cfg):
    fs = int(cfg["subs"].get("font_size", 14) or 14)
    # MODULE 1 : zone de sous-titres apprise des Shorts viraux
    # (video.subtitle_position, issue de visual_pattern). Alignement ASS :
    # 2 = bas-centre (comportement historique), 5 = milieu-centre, 8 = haut-centre.
    pos = (cfg.get("video", {}) or {}).get("subtitle_position", "bottom") or "bottom"
    if pos == "top":
        align, margin_v = 8, 60
    elif pos == "middle":
        align, margin_v = 5, 0
    else:
        align, margin_v = 2, 60
    return ("FontName=Arial,FontSize=%d,Bold=1,"
            "PrimaryColour=&H00FFFFFF&,OutlineColour=&H00000000&,"
            "Outline=2,Shadow=0,Alignment=%d,MarginV=%d" % (fs, align, margin_v))


def assemble(track, audio, srt, out_path, cfg=None, music=None, hook_ass=None,
             extra_ass=None):
    """Multiplexe piste video + voix (+ musique) + sous-titres graves."""
    cfg = cfg or get_config()
    out_path = Path(out_path)
    out_dir = out_path.parent
    # ffmpeg tourne avec cwd=out_dir et noms relatifs -> evite l'echappement Windows
    track_r = Path(track).resolve().relative_to(out_dir.resolve()).as_posix() \
        if Path(track).resolve().is_relative_to(out_dir.resolve()) else Path(track).resolve().as_posix()
    audio_r = Path(audio).resolve().relative_to(out_dir.resolve()).as_posix() \
        if Path(audio).resolve().is_relative_to(out_dir.resolve()) else Path(audio).resolve().as_posix()
    srt_r = Path(srt).resolve().relative_to(out_dir.resolve()).as_posix() \
        if Path(srt).resolve().is_relative_to(out_dir.resolve()) else Path(srt).resolve().as_posix()
    out_r = out_path.name

    voice_label = "1:a"
    music_vol = float(cfg["video"].get("music_volume", 0.15))
    inputs = ["-i", track_r, "-i", audio_r]
    afilter = "[%s]volume=1.0[voice]" % voice_label
    if music:
        inputs += ["-stream_loop", "-1", "-i", Path(music).resolve().as_posix()]
        afilter += ";[2:a]volume=%.2f[mus];[voice][mus]amix=inputs=2:duration=first:normalize=0[aout]" % music_vol
    else:
        afilter += ";[voice]anull[aout]"

    style = _sub_style(cfg)
    vfilter = "subtitles=%s:force_style='%s'" % (srt_r, style)
    if hook_ass:
        vfilter += ",subtitles=%s" % Path(hook_ass).name
    for extra in (extra_ass or []):
        if extra:
            vfilter += ",subtitles=%s" % Path(extra).name
    # MODULE 1 : zoom progressif (codes visuels viraux) APPLIQUE AVANT les
    # sous-titres, pour que le texte reste fixe pendant que l'image zoome.
    vcfg2 = cfg.get("video", {}) or {}
    zoom_kind = str(vcfg2.get("zoom_effect") or "")
    zoom_strength = float(vcfg2.get("zoom_strength", 0) or 0)
    if zoom_kind in ("in", "out") and 0 < zoom_strength <= 0.35:
        vfps = int(vcfg2.get("fps", 30) or 30)
        vw = int(vcfg2.get("width", 1080) or 1080)
        vh = int(vcfg2.get("height", 1920) or 1920)
        span = max(1, vfps * 30)  # zoom complet sur ~30 s
        if zoom_kind == "in":
            zexpr = "min(1+%.4f*on/%d,1+%.4f)" % (zoom_strength, span, zoom_strength)
        else:
            zexpr = "max(1+%.4f*(1-on/%d),1.001)" % (zoom_strength, span)
        vfilter = ("zoompan=z='%s':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                   "d=1:s=%dx%d:fps=%d," % (zexpr, vw, vh, vfps)) + vfilter
        log.info("Zoom viral %s (force %.2f) applique au rendu", zoom_kind, zoom_strength)
    filter_complex = "[0:v]%s[vout];%s" % (vfilter, afilter)
    run_ffmpeg(inputs + [
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        "-shortest",
        out_r,
    ], cwd=str(out_dir))
    return out_path


def run_video(idea, audio, timings_json, clips, config=None):
    """Pipeline video complet pour une idee -> output/<id>/video.mp4."""
    cfg = config or get_config()
    paths = get_paths(cfg)
    out_dir = paths.output / str(idea["id"])
    out_dir.mkdir(parents=True, exist_ok=True)

    # MODULE 1 (graft non-cassant) : injecte les codes visuels appris des
    # Shorts viraux dans cfg["video"] AVANT toute lecture des parametres.
    # Echec OpenCV/yt-dlp/cache vide -> degrade silencieux, rendu historique.
    try:
        from .visual_pattern import apply_to_video_cfg
        if apply_to_video_cfg(cfg):
            log.info("Visual: patterns viraux actifs pour %s", idea["id"])
    except Exception as exc:
        log.warning("Module visuel indisponible (non fatal): %s", exc)

    audio = Path(audio)
    audio_dur = media_duration(audio)
    log.info("Duree voix: %.1fs", audio_dur)

    srt = out_dir / "subs.srt"
    build_srt_from_timings(timings_json, srt, cfg)

    vcfg = cfg.get("video", {})
    loop_tail = float(vcfg.get("loop_tail", 0.7)) if vcfg.get("loop_ending", True) else 0.0
    track = build_video_track(clips, audio_dur, out_dir,
                              int(vcfg.get("width", 1080)),
                              int(vcfg.get("height", 1920)),
                              int(vcfg.get("fps", 30)),
                              loop_tail=loop_tail,
                              colors=vcfg.get("gradient_colors"))

    music_dir = paths.root / cfg["video"].get("music_dir", "assets/music")
    music = None
    if music_dir.exists():
        candidates = sorted(music_dir.glob("*.mp3"))
        if candidates:
            music = candidates[0]
            log.info("Musique de fond: %s", music.name)

    hook_ass = None
    if vcfg.get("hook_overlay", True) and idea.get("hook"):
        try:
            from .subs import write_hook_ass
            hook_ass = write_hook_ass(idea["hook"], out_dir / "hook.ass",
                                      duration=float(vcfg.get("hook_duration", 3.0)),
                                      width=int(vcfg.get("width", 1080)),
                                      height=int(vcfg.get("height", 1920)),
                                      font_size=int(vcfg.get("hook_font_size", 84)))
        except Exception as exc:
            log.warning("Hook visuel ignore: %s", exc)

    # V3 : checkpoints FIXES aux trois moments qui comptent - 2 s (le hook :
    # la decision de swipe se joue a 1,4-2,3 s), 8 s (premier beat : la
    # promesse doit tenir) et le dernier tiers (payoff, formats longs
    # uniquement). L'ancien checkpoint_at_pct unique (0.45 = 27 s sur un
    # 60 s, 6 s sur un 13 s) ne testait qu'un moment de la courbe : a 27 s,
    # un viewer encore present est DEJA accroche - le test utile est avant.
    # Deduplication : deux checkpoints a moins de 1.5 s d'ecart sont
    # fusionnes (sur un 13 s, le dernier tiers ~10 s tombe pres du 8 s).
    extra_ass = []
    is_long = (str(idea.get("duration_format") or "") == "long"
               or audio_dur > 30.0)
    if vcfg.get("checkpoint_enabled", True):
        try:
            base_times = [float(t) for t in
                          (vcfg.get("checkpoint_times_s") or [2.0, 8.0])]
        except (TypeError, ValueError):
            base_times = [2.0, 8.0]
        times = list(base_times)
        last_third = audio_dur * (2.0 / 3.0)
        if is_long and 1.5 <= last_third <= audio_dur - 3.0:
            times.append(last_third)
        # Fenetre de securite : >= 1.5 s apres le debut, >= 3 s avant la fin.
        times = sorted(t for t in times if 1.5 <= t <= audio_dur - 3.0)
        placed = []
        for t in times:
            if placed and t - placed[-1] < 1.5:
                continue
            placed.append(t)
        text = str(vcfg.get("checkpoint_text") or "STAY - IT GETS WILD")
        for k, at in enumerate(placed):
            try:
                from .subs import write_checkpoint_ass
                ckpt = write_checkpoint_ass(
                    text, out_dir / ("checkpoint_%d.ass" % k), at_seconds=at,
                    width=int(vcfg.get("width", 1080)),
                    height=int(vcfg.get("height", 1920)))
                if ckpt:
                    extra_ass.append(ckpt)
            except Exception as exc:
                log.warning("Checkpoint visuel ignore (%s s): %s", at, exc)
        if placed:
            log.info("Checkpoints places a: %s",
                     ", ".join("%.1fs" % t for t in placed))

    final = assemble(track, audio, srt, out_dir / "video.mp4", cfg, music=music,
                     hook_ass=hook_ass, extra_ass=extra_ass)
    log.info("Video finale: %s", final)
    return final
