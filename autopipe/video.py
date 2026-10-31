"""Assemblage video vertical 1080x1920 avec ffmpeg : clips + voix + sous-titres."""
import random
from pathlib import Path

from .config import get_config, get_paths
from .subs import build_srt_from_timings, build_subs_ass
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

    # Remplissage (fix 2026-10-30) : la somme des segments doit couvrir la
    # duree de la voix. Cas quiz : Pixabay renvoie souvent 1-2 clips pour
    # "quiz"/"trivia" alors que la voix dure ~80-113 s -> sans remplissage,
    # la piste s'arretait a ~12 s et le mux tronquait la video ET l'audio.
    # Le concat peut repeter les fichiers : on boucle les segments jusqu'a
    # couvrir audio_dur (meme codec/params via normalize_clip).
    def _seg_dur(path):
        try:
            return float(media_duration(path))
        except Exception:
            return 0.0

    total = sum(_seg_dur(s) for s in segs)
    if total > 0 and total < audio_dur - 0.5:
        filled = []
        acc = 0.0
        while acc < audio_dur:
            for seg in segs:
                filled.append(seg)
                acc += _seg_dur(seg)
                if acc >= audio_dur:
                    break
        log.info("Remplissage boucle: %.1fs de segments x%d -> %.1fs (voix %.1fs)",
                 total, len(filled) // max(1, len(segs)), acc, audio_dur)
        segs = filled

    listfile = work_dir / "concat.txt"
    listfile.write_text(
        "".join("file '%s'\n" % s.resolve().as_posix() for s in segs), encoding="utf-8")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listfile),
                "-c", "copy", str(track)])
    return track


def _sub_style(cfg):
    """force_style de secours pour l'SRT brut (fallback si l'ASS natif
    echoue). ATTENTION (fix "texte colle") : l'SRT grave via `subtitles=`
    est rendu dans l'espace PlayRes 384x288 implicite de ffmpeg -> toute
    taille y est x6.67 (1920/288). On convertit donc les PIXELS reels en
    espace 288 au lieu d'utiliser la valeur brute."""
    scfg = cfg.get("subs", {}) or {}
    vcfg = cfg.get("video", {}) or {}
    try:
        width = int(vcfg.get("width", 1080) or 1080)
        height = int(vcfg.get("height", 1920) or 1920)
    except (TypeError, ValueError):
        width, height = 1080, 1920
    px = float(scfg.get("burn_font_px", 52) or 52)
    scale = height / 288.0          # ~6.67 pour 1080x1920
    fs288 = max(3.0, px / scale)    # 52px reels -> ~7.8 en 288-space
    outline288 = max(1.0, round(px * 0.08 / scale))
    # MODULE 1 : zone de sous-titres apprise des Shorts viraux
    # (video.subtitle_position). Alignement ASS : 2 = bas-centre (defaut),
    # 5 = milieu-centre, 8 = haut-centre. Marge en PIXELS puis convertie.
    pos = vcfg.get("subtitle_position", "bottom") or "bottom"
    # Zones safe Shorts (rech. 2026-10-31) : haut sous l'overlay UI (~234px),
    # bas au-dessus de l'overlay UI (~345px) au lieu des 3%/5.5% historiques
    # qui laissaient le texte DANS les zones UI.
    from .subs import safe_margin_bottom_px, safe_margin_top_px
    if pos == "top":
        align, margin_px = 8, safe_margin_top_px(cfg, height)
    elif pos == "middle":
        align, margin_px = 5, 0
    else:
        align, margin_px = 2, safe_margin_bottom_px(cfg, height)
    return ("FontName=Arial,FontSize=%.1f,Bold=1,"
            "PrimaryColour=&H00FFFFFF&,OutlineColour=&H00000000&,"
            "Outline=%.0f,Shadow=0,Alignment=%d,MarginV=%.0f"
            % (fs288, outline288, align, margin_px / scale))


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

    # Fix "texte colle" (2026-10-31) : on grave l'ASS NATIF (PlayRes
    # 1080x1920, tailles/marges exactes en pixels) genere depuis l'SRT ;
    # force_style n'est plus qu'un fallback si l'ASS natif echoue.
    v_w = int((cfg.get("video", {}) or {}).get("width", 1080) or 1080)
    v_h = int((cfg.get("video", {}) or {}).get("height", 1920) or 1920)
    native_ass = None
    try:
        native_ass = build_subs_ass(srt, out_dir / "subs_burn.ass", cfg,
                                    width=v_w, height=v_h)
    except Exception as exc:
        log.warning("ASS natif des sous-titres echoue, fallback SRT: %s", exc)
    if native_ass is not None:
        na = Path(native_ass).resolve()
        na_r = na.relative_to(out_dir.resolve()).as_posix() \
            if na.is_relative_to(out_dir.resolve()) else na.as_posix()
        vfilter = "subtitles=%s" % na_r
        log.info("Sous-titres graves: ASS natif PlayRes %dx%d -> %s", v_w, v_h, na_r)
    else:
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


def _quiz_fold(text):
    """Copie alignee (1 caractere -> 1 caractere) pour reperer les positions
    des questions/reponses dans le script sans casser les offsets."""
    return "".join(ch.lower() if (ch.isalnum() or ch.isspace()) else " "
                   for ch in str(text or ""))


def _quiz_pos_to_time(pos, total_chars, words, audio_dur):
    """Offset caractere -> secondes, via l'index de mot TTS equivalent.

    Les timings edge-tts donnent le rythme reel (nombres epelles, pause du
    compte a rebours) ; la fraction de caracteres du script sert d'index
    dans la liste de mots. Fallback : fraction * duree audio.
    """
    frac = min(1.0, max(0.0, pos / float(max(1, total_chars))))
    if words:
        idx = min(len(words) - 1, int(round(frac * (len(words) - 1))))
        try:
            t = float((words[idx] or {}).get("start") or 0.0)
        except (TypeError, ValueError, IndexError):
            t = 0.0
        return max(0.0, min(t, max(0.0, audio_dur)))
    return frac * max(0.0, audio_dur)


def _quiz_card_overlays(idea, audio_dur, timings_json, out_dir, cfg):
    """Cartes ASS question/reponse du mode quiz (rounds de quiz.py).

    Positionnement : l'offset de chaque question (et de sa bonne reponse)
    dans le script est converti en temps via les timings de mots edge-tts
    (fallback proportionnel sur la duree). La question s'affiche des son
    debut jusqu'a la revelation ; la reponse (carte verte) tient jusqu'a la
    question suivante. Ancre introuvable -> position au prorata des rounds.
    Aucun echec n'est fatal : la video sort sans carte plutot que sans rendu.
    """
    meta = idea.get("quiz") or {}
    items = meta.get("items") or []
    script = str(idea.get("script") or "")
    if not items or not script:
        return []
    qcfg = cfg.get("quiz") or {}
    if qcfg.get("cards") is False:
        return []
    from .subs import write_quiz_card_ass
    from .util import load_json
    words = (load_json(timings_json, {}) or {}).get("words") or []
    folded = _quiz_fold(script)
    total = max(1, len(folded))
    vcfg = cfg.get("video") or {}
    width = int(vcfg.get("width", 1080))
    height = int(vcfg.get("height", 1920))
    try:
        cd = float(qcfg.get("countdown_s") or meta.get("countdown_s") or 10)
    except (TypeError, ValueError):
        cd = 10.0

    def t_of(pos):
        return _quiz_pos_to_time(pos, total, words, audio_dur)

    marks = []  # (pos_question, pos_reponse|None) par round
    for it in items:
        if not isinstance(it, dict) or not str(it.get("question") or "").strip():
            marks.append(None)
            continue
        qpos = folded.find(_quiz_fold(it["question"]))
        apos = None
        opts = [str(o) for o in (it.get("options") or [])]
        try:
            idx = int(it.get("answer_index"))
        except (TypeError, ValueError):
            idx = 0
        correct = opts[idx] if 0 <= idx < len(opts) else ""
        if qpos >= 0 and correct:
            # Revelation, pas l'option : la bonne reponse est deja citee dans
            # la ligne de la question ("B: Saturn"). On cherche donc sa
            # DERNIERE occurrence APRES cette ligne, sinon la carte verte
            # s'afficherait pendant le compte a rebours et spoilerait la
            # reponse. Jamais repetee apres -> apos = None et la fenetre
            # degrade t_ans = t_q + cd * 0.8.
            line_end = folded.find("\n", qpos)
            if line_end < 0:
                line_end = len(folded)
            apos = folded.rfind(_quiz_fold(correct), line_end + 1)
            if apos < 0:
                apos = None
        if qpos < 0:
            # ancrage degrade : position attendue au prorata des rounds
            k = len(marks)
            qpos = int(total * (0.08 + 0.80 * k / max(1, len(items))))
            apos = None
        marks.append((qpos, apos))

    cards = []
    letters = ("A", "B", "C", "D")
    for i, mark in enumerate(marks):
        if not mark:
            continue
        it = items[i]
        opts = [str(o) for o in (it.get("options") or [])]
        try:
            idx = min(max(int(it.get("answer_index")), 0), max(len(opts) - 1, 0))
        except (TypeError, ValueError):
            idx = 0
        correct = opts[idx] if 0 <= idx < len(opts) else ""
        t_q = t_of(mark[0])
        if mark[1]:
            t_ans = t_of(mark[1])
        else:
            t_ans = t_q + cd * 0.8
        q_start = max(0.5, t_q - 0.3)
        a_start = max(q_start + 2.0, t_ans - 1.2)
        if i + 1 < len(marks) and marks[i + 1]:
            next_q = t_of(marks[i + 1][0])
        else:
            next_q = audio_dur - 2.0
        a_end = min(max(a_start + 2.0, next_q - 0.2), audio_dur - 0.5,
                    a_start + 12.0)
        if q_start >= a_start or a_start >= a_end:
            continue
        q_lines = ["Question %d" % (i + 1), str(it["question"])]
        q_lines += ["%s) %s" % (letters[j], o) for j, o in enumerate(opts[:4])]
        try:
            qa = write_quiz_card_ass(
                q_lines, out_dir / ("quiz_q_%d.ass" % i),
                at_seconds=q_start, duration=a_start - q_start,
                kind="question", width=width, height=height,
                wrap_chars=26, max_lines=9)
            if qa:
                cards.append(qa)
        except Exception as exc:
            log.warning("Carte question ignoree (round %d): %s", i + 1, exc)
        if not correct:
            continue
        a_lines = ["Answer: %s" % letters[min(idx, len(letters) - 1)], correct]
        if str(it.get("explanation") or "").strip():
            a_lines.append(str(it["explanation"]))
        try:
            aa = write_quiz_card_ass(
                a_lines, out_dir / ("quiz_a_%d.ass" % i),
                at_seconds=a_start, duration=a_end - a_start,
                kind="answer", width=width, height=height,
                wrap_chars=26, max_lines=6)
            if aa:
                cards.append(aa)
        except Exception as exc:
            log.warning("Carte reponse ignoree (round %d): %s", i + 1, exc)
    if cards:
        log.info("Mode quiz: %d cartes ASS generees pour %s", len(cards),
                 idea.get("id"))
    return cards


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
                                      font_size=int(vcfg.get("hook_font_size", 84)),
                                      cfg=cfg)
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
    quiz_meta = idea.get("quiz") if isinstance(idea.get("quiz"), dict) else {}
    is_quiz = bool(quiz_meta.get("items")) \
        and bool((cfg.get("quiz") or {}).get("enabled", True))
    is_long = (str(idea.get("duration_format") or "") == "long"
               or audio_dur > 30.0)
    if is_quiz:
        # Mode quiz : cartes question/reponse a la place des checkpoints
        # generiques (voir quiz.py : revelation dans la meme video).
        try:
            extra_ass = _quiz_card_overlays(idea, audio_dur, timings_json,
                                            out_dir, cfg)
        except Exception as exc:
            log.warning("Cartes quiz ignorees (non fatal): %s", exc)
            extra_ass = []
    elif vcfg.get("checkpoint_enabled", True):
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
                    height=int(vcfg.get("height", 1920)), cfg=cfg)
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
