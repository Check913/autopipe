"""Generation de sous-titres SRT a partir des timings de mots edge-tts."""
from pathlib import Path

from .util import get_logger

log = get_logger("subs")


def format_ts(seconds):
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def _flush(cur, cues):
    text = " ".join(w["text"] for w in cur).strip()
    if not text:
        return
    start = cur[0]["start"]
    end = max(cur[-1]["end"], start + 0.3)
    cues.append({"start": start, "end": end, "text": text})


def group_words(words, max_chars=42, max_lines=2, min_duration=0.7, max_gap=0.6):
    """Regroupe les mots en sous-titres lisibles (1-2 lignes)."""
    cues = []
    cur = []
    limit = max_chars * max_lines
    for w in words:
        if not w.get("text"):
            continue
        if cur and (w["start"] - cur[-1]["end"] > max_gap):
            _flush(cur, cues)
            cur = []
        cur.append(w)
        text = " ".join(x["text"] for x in cur)
        if len(text) >= limit:
            _flush(cur, cues)
            cur = []
    if cur:
        _flush(cur, cues)

    # fusion des cues trop courts avec le precedent
    merged = []
    for cue in cues:
        if merged and (cue["end"] - cue["start"] < min_duration or len(cue["text"]) < 12):
            prev = merged[-1]
            if (len(prev["text"]) + len(cue["text"]) + 1 <= limit
                    and cue["start"] - prev["end"] <= max_gap):
                prev["text"] += " " + cue["text"]
                prev["end"] = cue["end"]
                continue
        merged.append(cue)
    return merged


def write_srt(cues, path):
    lines = []
    for i, cue in enumerate(cues, 1):
        lines.append("%d\n%s --> %s\n%s\n" % (
            i, format_ts(cue["start"]), format_ts(cue["end"]), cue["text"].strip()))
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    log.info("SRT: %s cues -> %s", len(cues), path)
    return path


def build_srt_from_timings(timings_json, out_srt, cfg=None):
    from .config import get_config
    cfg = cfg or get_config()
    from .util import load_json
    words = load_json(timings_json, {}).get("words", [])
    cues = group_words(words,
                       max_chars=int(cfg["subs"].get("max_chars", 42)),
                       max_lines=int(cfg["subs"].get("max_lines", 2)),
                       min_duration=float(cfg["subs"].get("min_duration", 0.7)))
    if not cues:
        raise RuntimeError("Aucun sous-titre genere (timings vides ?)")
    return write_srt(cues, out_srt)


def safe_margin_top_px(cfg=None, height=1920):
    """Marge verticale haute (hook, align 8) : libere la bande d'UI
    YouTube du haut du Short (~234 px mesures en 2026). Surcharge via
    [subs].safe_margin_top_px ; 0 = auto."""
    cfg = cfg or {}
    h = int(height)
    v = int((cfg.get("subs") or {}).get("safe_margin_top_px", 0) or 0)
    if v > 0:
        return v
    return max(int(round(h * 0.13)), 250)


def safe_margin_bottom_px(cfg=None, height=1920):
    """Marge verticale basse (sous-titres, align 2) : reste au-dessus de
    l'UI YouTube du bas (~345 px) et de l'abonnement (~y1480-1560). Bas de
    bande texte vise : y <= 1575 sur 1080x1920. Surcharge via
    [subs].safe_margin_bottom_px ; 0 = auto."""
    cfg = cfg or {}
    h = int(height)
    v = int((cfg.get("subs") or {}).get("safe_margin_bottom_px", 0) or 0)
    if v > 0:
        return v
    return max(int(round(h * 0.18)), 200)


def checkpoint_margin_v_px(cfg=None, height=1920):
    """Marge du checkpoint (align 2) : strictement entre le bloc hook
    (haut, zone safe) et la bande sous-titres (bas), avec >= 60 px d'air
    de chaque cote et au plus 0.55*h de marge."""
    bottom = safe_margin_bottom_px(cfg, height)
    h = int(height)
    target = bottom + int(round(h * 0.075)) + 60
    return min(max(target, 0), int(round(h * 0.55)))


_ASS_HOOK_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Arial,{font_size},&H0000FFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,5,2,8,50,50,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,{start},{end},Hook,,0,0,0,,{text}
"""


def _ass_escape(text):
    # Sans regex: le motif d'echappement ASS precedent utilisait un jeu de
    # caracteres mal ferme ("unterminated character set") qui levait
    # re.error et faisait perdre l'overlay hook. On remplace simplement les
    # caracteres speciaux ASS ({, }, backslash) par des espaces.
    text = str(text)
    for ch in ("{", "}", "\\"):
        text = text.replace(ch, " ")
    return text.replace("\r", " ").replace("\n", " ")


def _ass_ts(seconds):
    seconds = max(0.0, float(seconds))
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return "%d:%02d:%02d.%02d" % (h, m, s, cs)


def write_hook_ass(hook_text, out_ass, duration=3.0, width=1080, height=1920,
                   font_size=84, wrap_chars=18, max_lines=3, margin_v=None,
                   cfg=None):
    """Ecrit un fichier ASS: texte choc jaune en haut de l'ecran (hook visuel).

    Affiche pendant les `duration` premieres secondes, gros caractere,
    contour noir: le levier anti swipe-away des Shorts.
    """
    import textwrap
    from pathlib import Path
    if hook_text is None or not str(hook_text).strip():
        return None
    raw = _ass_escape(hook_text).strip().upper()
    if not raw:
        return None
    lines = textwrap.wrap(raw, width=wrap_chars)
    if not lines:
        return None
    lines = lines[:max_lines]
    if margin_v is None:
        margin_v = safe_margin_top_px(cfg, height)
    content = _ASS_HOOK_TEMPLATE.format(
        width=int(width), height=int(height), font_size=int(font_size),
        margin_v=int(margin_v),
        start=_ass_ts(0.0), end=_ass_ts(duration), text="\\N".join(lines))
    out_ass = Path(out_ass)
    out_ass.write_text(content, encoding="utf-8")
    log.info("Hook visuel: %d lignes, %.1fs -> %s", len(lines), duration, out_ass)
    return out_ass


_ASS_CHECKPOINT_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
# Fix "texte colle" : MarginV calcule par checkpoint_margin_v_px() pour
# rester strictement entre le hook (haut, zone safe) et les sous-titres
# graves au bas d'ecran, sans chevaucher ni l'un ni l'autre.
Style: Ckpt,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,4,1,2,50,50,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,{start},{end},Ckpt,,0,0,0,,{text}
"""


def write_checkpoint_ass(text, out_ass, at_seconds, duration=2.5,
                         width=1080, height=1920, font_size=64,
                         wrap_chars=24, max_lines=2, margin_v=None,
                         cfg=None):
    """Checkpoint mi-video : rappel de curiosite affiche a `at_seconds`.

    Vise la chute de retention du milieu de Short (formats 60 s) : un texte
    court blanc (outline noir), bas de l'ecran, affiche pendant
    `duration` secondes a partir de `at_seconds`. Desactive pour les formats
    13 s (inutile a cette echelle). Renvoie None si le texte est vide ou si
    la fenetre est invalide (at_seconds trop tot/tard dans la video).
    """
    import textwrap
    from pathlib import Path
    at_seconds = float(at_seconds)
    duration = float(duration)
    # V3 : garde abaassee a 1.5 s pour autoriser le checkpoint hook a 2 s
    # (la decision de swipe se joue a 1,4-2,3 s - un checkpoint a 3 s la
    # ratait).
    if at_seconds < 1.5 or duration <= 0:
        return None
    if text is None or not str(text).strip():
        return None
    raw = _ass_escape(text).strip().upper()
    if not raw:
        return None
    lines = textwrap.wrap(raw, width=wrap_chars)[:max_lines]
    if not lines:
        return None
    if margin_v is None:
        margin_v = checkpoint_margin_v_px(cfg, height)
    content = _ASS_CHECKPOINT_TEMPLATE.format(
        width=int(width), height=int(height), font_size=int(font_size),
        margin_v=int(margin_v),
        start=_ass_ts(at_seconds), end=_ass_ts(at_seconds + duration),
        text="\\N".join(lines))
    out_ass = Path(out_ass)
    out_ass.write_text(content, encoding="utf-8")
    log.info("Checkpoint visuel: '%s' a %.1fs (%.1fs) -> %s",
             " / ".join(lines)[:60], at_seconds, duration, out_ass)
    return out_ass


_ASS_QUIZ_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Quiz,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H70000000,-1,0,0,0,100,100,0,0,3,3,0,5,40,40,30,1
Style: QuizAns,Arial,{font_size},&H0000FF00,&H000000FF,&H00000000,&H70000000,-1,0,0,0,100,100,0,0,3,3,0,5,40,40,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,{start},{end},{style},,0,0,0,,{anchor}{text}
"""

# Standard vertical concurrence (analyse Shorts viraux) : lignes courtes,
# bloc ancore a une hauteur STABLE, marge laterale confortable dans la boite.
QUIZ_ANCHOR_FRAC = 0.40  # centre vertical du bloc = 40 % de la hauteur


def write_quiz_card_ass(text_lines, out_ass, at_seconds, duration=4.0,
                        kind="question", width=1080, height=1920,
                        font_size=None, wrap_chars=24, max_lines=7,
                        anchor_y_frac=None):
    """Carte question/reponse du mode quiz : bloc CENTRE, boite noire
    semi-transparente, texte blanc (question) ou vert (reponse).

    Contrairement aux hooks/checkpoints : PAS de mise en majuscules (les
    questions et les options doivent rester lisibles telles quelles), texte
    centre (alignment 5) au lieu du bas d'ecran. Accepte une chaine ou une
    liste de lignes (question + options). Renvoie None si la fenetre est
    invalide ou si tout le texte est vide.
    """
    import textwrap
    from pathlib import Path
    at_seconds = float(at_seconds)
    duration = float(duration)
    if at_seconds < 0.5 or duration <= 0:
        return None
    style = "QuizAns" if str(kind) == "answer" else "Quiz"
    if font_size is None:
        font_size = 54 if style == "QuizAns" else 58
    if text_lines is None:
        return None
    if isinstance(text_lines, str):
        text_lines = [text_lines]
    lines = []
    for line in text_lines:
        raw = _ass_escape(line).strip()
        if not raw:
            continue
        # textwrap ne rend jamais [] pour une chaine non vide, mais le
        # fallback garde les mots tres longs intacts si ca change un jour.
        wrapped = textwrap.wrap(raw, width=wrap_chars)[:max_lines] or [raw]
        lines.extend(wrapped)
    lines = lines[:max_lines]
    if not lines:
        return None
    # Fix "texte colle" (2026-10-31) : la carte est ANCOREE a une hauteur
    # stable (an5 + \pos) au lieu d'etre centree par defaut au milieu de
    # l'ecran : un bloc de 7-9 lignes descendait jusqu'a ~y1160 et touchait
    # la 1re ligne de sous-titres (collage mesure a ~3px). Ancre a 40 % de la
    # hauteur, quel que soit le nombre de lignes : le bloc reste au-dessus de
    # la zone sous-titres (bas d'ecran) et sous le hook (haut d'ecran).
    frac = QUIZ_ANCHOR_FRAC if anchor_y_frac is None else float(anchor_y_frac)
    anchor = "{\\an5\\pos(%d,%d)}" % (int(width) // 2,
                                      int(round(int(height) * frac)))
    content = _ASS_QUIZ_TEMPLATE.format(
        width=int(width), height=int(height), font_size=int(font_size),
        start=_ass_ts(at_seconds), end=_ass_ts(at_seconds + duration),
        style=style, anchor=anchor, text="\\N".join(lines))
    out_ass = Path(out_ass)
    out_ass.write_text(content, encoding="utf-8")
    log.info("Carte quiz (%s): %d lignes a %.1fs (%.1fs) ancre y=%d -> %s",
             style, len(lines), at_seconds, duration,
             int(round(int(height) * frac)), out_ass)
    return out_ass


# -------------------------------------------------------------- sous-titres
# Pipeline natif (fix "texte colle" 2026-10-31) : l'SRT brule via
# `subtitles=...:force_style` est rendu par ffmpeg dans un espace PlayRes
# 384x288 : FontSize=14 y equivaut a ~93px reels (x1920/288) et chaque cue
# se re-wrape en 3-4 lignes -> un mur de texte qui mange le tiers bas de
# l'ecran et colle aux cartes quiz. On convertit donc l'SRT en ASS avec
# PlayRes NATIF (1080x1920) : taille, marges et contour y sont exacts.

_ASS_SUBS_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Subs,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,{outline},0,2,{margin_l},{margin_r},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def parse_srt(path):
    """Parse un SRT -> [{start, end, text}]. Parser minimal mais complet :
    index numerique ignore, timing 'hh:mm:ss,mmm --> hh:mm:ss,mmm' (espaces
    et -- ou -> acceptes), texte multi-lignes joint par espace. Fichier
    absent -> liste vide (le fallback force_style de assemble() prend le
    relais, et ffmpeg echouera alors a un endroit parlant)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        log.warning("SRT introuvable: %s", path)
        return []
    cues = []
    cur_start = cur_end = None
    cur_lines = []

    def flush():
        if cur_start is not None and cur_lines:
            cues.append({"start": cur_start, "end": cur_end,
                         "text": " ".join(cur_lines).strip()})

    for line in text.splitlines():
        line = line.strip()
        if not line:
            if cur_lines:
                flush()
                cur_start = cur_end = None
                cur_lines = []
            continue
        if "-->" in line:
            try:
                a, b = line.split("-->")
                cur_start = _srt_ts(a)
                cur_end = _srt_ts(b)
                cur_lines = []
                continue
            except (ValueError, AttributeError):
                continue
        if cur_start is None:
            continue  # ligne d'index ou texte orphelin : ignore
        cur_lines.append(line)
    if cur_lines:
        flush()
    return [c for c in cues if c["text"]]


def _srt_ts(stamp):
    """hh:mm:ss,mmm (ou .mmm, optionnellement h:mm) -> secondes."""
    stamp = stamp.strip().replace(",", ".")
    parts = stamp.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        parts = ["0"] + parts
    h, m, rest = parts[0], parts[1], parts[2]
    return int(h) * 3600 + int(m) * 60 + float(rest)


def build_subs_ass(srt_path, out_ass, cfg=None, width=1080, height=1920):
    """Convertit l'SRT en ASS grave PlayRes natif : taille, marges et
    contour EXACTS en pixels 1080x1920 (plus d'espace 384x288 ou FontSize=14
    rendait ~93px). Standard vertical : fonte ~2.7%% de la hauteur (52px en
    1920), marges laterales >= 5%%, marge basse = zone safe Shorts
    (safe_margin_bottom_px : au-dessus de l'UI YouTube, ~0.18*h), outline
    ~8%% de la fonte, cues re-wrapees a max_chars (<= 2 lignes). Retourne
    out_ass, ou None si aucune cue."""
    import textwrap
    cfg = cfg or {}
    subs_cfg = (cfg.get("subs") or {})
    cues = parse_srt(srt_path)
    if not cues:
        return None
    height = int(height)
    width = int(width)
    scale = height / 1920.0
    font_size = int(round(float(subs_cfg.get("burn_font_px", 52)) * scale))
    font_size = max(12, font_size)
    outline = max(2, int(round(font_size * 0.08)))
    margin_l = margin_r = max(int(round(width * 0.05)), 60)
    margin_v = safe_margin_bottom_px(cfg, height)
    wrap_chars = int(subs_cfg.get("max_chars", 42))
    max_lines = int(subs_cfg.get("max_lines", 2))
    events = []
    for cue in cues:
        lines = []
        for seg in str(cue["text"]).split("\n"):
            raw = _ass_escape(seg).strip()
            if raw:
                lines.extend(textwrap.wrap(raw, width=wrap_chars) or [raw])
        lines = lines[:max_lines]
        if not lines:
            continue
        events.append((cue["start"], cue["end"], "\\N".join(lines)))
    if not events:
        return None
    # pas de Dialogue en dehors de l'ordre chronologique (libass lit tout,
    # mais un tri stable evite tout re-wrapping imprevisible en bord de cue)
    events.sort(key=lambda e: (e[0], e[1]))
    body = []
    for start, end, text in events:
        body.append("Dialogue: 0,%s,%s,Subs,,0,0,0,,%s" % (
            _ass_ts(start), _ass_ts(max(end, start + 0.1)), text))
    content = _ASS_SUBS_TEMPLATE.format(
        width=width, height=height, font_size=font_size, outline=outline,
        margin_l=margin_l, margin_r=margin_r, margin_v=margin_v,
    ) + "\n".join(body) + "\n"
    out_ass = Path(out_ass)
    out_ass.write_text(content, encoding="utf-8")
    log.info("Sous-titres ASS natifs: %d cues, font %dpx, marges L%d/V%d "
             "-> %s", len(events), font_size, margin_l, margin_v, out_ass)
    return out_ass