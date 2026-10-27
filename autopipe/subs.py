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


_ASS_HOOK_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Arial,{font_size},&H0000FFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,5,2,8,50,50,190,1

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
                   font_size=84, wrap_chars=18, max_lines=3):
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
    content = _ASS_HOOK_TEMPLATE.format(
        width=int(width), height=int(height), font_size=int(font_size),
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

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Ckpt,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,4,1,2,50,50,110,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,{start},{end},Ckpt,,0,0,0,,{text}
"""


def write_checkpoint_ass(text, out_ass, at_seconds, duration=2.5,
                         width=1080, height=1920, font_size=64,
                         wrap_chars=24, max_lines=2):
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
    content = _ASS_CHECKPOINT_TEMPLATE.format(
        width=int(width), height=int(height), font_size=int(font_size),
        start=_ass_ts(at_seconds), end=_ass_ts(at_seconds + duration),
        text="\\N".join(lines))
    out_ass = Path(out_ass)
    out_ass.write_text(content, encoding="utf-8")
    log.info("Checkpoint visuel: '%s' a %.1fs (%.1fs) -> %s",
             " / ".join(lines)[:60], at_seconds, duration, out_ass)
    return out_ass