# -*- coding: utf-8 -*-
"""MAX (2026-10-31) : moteur de metadonnees + regles Shorts verifiees 2025-2026.

Centralise TOUT ce que la recherche web (notes 2026-10-31) a valide, pour
que ni upload.py ni quiz.py ni scheduler.py n'aient a re-coder une regle :

1. Limites API reelles (verifiees, doc YouTube) :
   - snippet.description = 5000 OCTETS (pas caracteres) : un emoji = 4 octets,
     un accent = 2 -> troncature PAR OCTETS, jamais par caracteres.
     (l'ancien code tronquait a 4900 caracteres : fausse securite)
   - snippet.tags = 500 caracteres AU TOTAL, guillemets inclus
     (format API "tag1","tag2" -> chaque tag coute len+2 quotes+1 virgule).
   - hashtags : seuil d'invalidation = 60 (PAS 15, mythe 2023-2024) ;
     > 60 hashtags -> TOUS ignores. 3 hashtags max affiches au-dessus du
     titre -> les 3 premiers comptent, la niche d'abord, #shorts en dernier.
2. title_score : familles mesurees (etude 2026, vitesses d'acces a 10K vues)
   - direct-statement 16.3% / you-focused 14.8% / number-led 13.3%
     (median le plus eleve : 1 392 vues) / question 12.7% / curiosity 10.8%.
3. Fenetres US intelligentes : publish = pic_audience - lead (1h30), pics
   par jour de semaine (etudes SocialPilot 301K videos + Buffer 1.8M).
4. Gates de retention (boucle d'auto-amelioration) : KILL_HOOK <60%,
   TIGHTEN_EDIT 60-70, ADD_LOOP 70-85, SCALE >=85, REPLAY_LOOP si loop>100.

Purement stdlib (aucun import googleapiclient) -> testable hors ligne.
"""
import datetime as dt
import re
import unicodedata
import zlib

log_title = "metas"  # module tag (logger branche par les appelants si besoin)

# ---------------------------------------------------------------------------
# 1) LIMITES API : octets, tags, hashtags
# ---------------------------------------------------------------------------

DESC_MAX_BYTES = 5000       # doc YouTube : 5000 OCTETS
TAGS_TOTAL_BUDGET = 500     # 500 caracteres, guillemets inclus
TAG_MAX_CHARS = 100         # par tag individuel
TAGS_MAX_COUNT = 30         # garde API (au-dela -> 400 invalid)
HASHTAG_INVALID_LIMIT = 60  # seuil OFFICIEL d'invalidation totale
HASHTAG_MAX = 5             # 3 affiches au-dessus du titre + marge securite

_HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")


def byte_len(text):
    """Longueur UTF-8 reelle (la limite API compte des OCTETS)."""
    return len(str(text or "").encode("utf-8"))


def truncate_bytes(text, max_bytes=DESC_MAX_BYTES, suffix=""):
    """Tronque proprement a `max_bytes` OCTETS UTF-8 sans couper un caractere.

    Un emoji (4 octets) ou un accent (2 octets) n'est jamais laisse a moitie
    (source du bug API 400 invalidDescription du guide 2023 : troncature
    caracteres vs octets). `suffix` (ex " ...") est ajoute seulement si une
    troncature a eu lieu, SANS depasser la limite.
    """
    text = str(text or "")
    if byte_len(text) <= max_bytes:
        return text
    budget = max(0, max_bytes - byte_len(suffix))
    raw = text.encode("utf-8")[:budget]
    # Recul jusqu'a un debut de caractere UTF-8 valide (bit de continuation).
    while raw and (raw[-1] & 0xC0) == 0x80:
        raw = raw[:-1]
    out = raw.decode("utf-8", errors="ignore").rstrip()
    if suffix and out:
        out = out.rstrip() + suffix
    return out


def _strip_controls(text):
    """Retire les caracteres interdits par l'API tags (< >) + quotes internes."""
    return str(text or "").replace("<", "").replace(">", "")\
        .replace('"', "'").strip()


def fit_tags(tags, budget=TAGS_TOTAL_BUDGET, max_tags=TAGS_MAX_COUNT):
    """Liste de tags sous le budget API de 500 caracteres AU TOTAL.

    Le cout reel d'un tag dans le payload API = len(tag) + 2 guillemets
    + 1 virgule (+ espace separateur) : on compte len+4 par tag, marge
    conservatrice. Deduplique (insensible a la casse), trim, garde l'ordre
    (les premiers tags pesent le plus), borne chaque tag a 100 caracteres.
    """
    out, seen, used = [], set(), 0
    for raw in (tags or []):
        tag = _strip_controls(raw)[:TAG_MAX_CHARS]
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        cost = len(tag) + 4  # "tag", + espace
        if used + cost > budget or len(out) >= max_tags:
            break
        seen.add(key)
        out.append(tag)
        used += cost
    return out


def split_hashtags(text):
    """Tous les #tags valides (alnum + underscore, sans espace) d'un texte."""
    return _HASHTAG_RE.findall(str(text or ""))


def count_hashtags(*texts):
    """Total de hashtags titre+description (seuil d'invalidation = 60)."""
    return sum(len(split_hashtags(t)) for t in texts)


def build_hashtags(base, extra=None, limit=HASHTAG_MAX):
    """Chaine hashtags niche-first, dedupliquee, sous le seuil 60.

    Regles encodees (recherche 2026-10-31) :
      - > 60 hashtags dans la video -> TOUS invalides (seuil officiel 60,
        PAS 15) ; on plafonne a 5 : les 3 premiers sont affiches au-dessus
        du titre, donc ce sont EUX qui travaillent.
      - #shorts n'apporte plus de distribution (consensus 2026) : optionnel,
        toujours EN DERNIER, jamais a la place d'un hashtag de niche.
      - un hashtag = lettres/chiffres/underscore uniquement.
    """
    def _clean(word):
        w = str(word or "").strip().lstrip("#")
        # Accents -> lettre de base (unicodedata) : "générale" -> "generale",
        # jamais "gnrale" (le re brut supprimait le caractere accentue entier).
        w = "".join(c for c in unicodedata.normalize("NFKD", w)
                    if not unicodedata.combining(c))
        w = re.sub(r"[^A-Za-z0-9_]", "", w)
        return w

    niche, shorts_tag = [], None
    for cand in [base] + list(extra or []):
        w = _clean(cand)
        if not w:
            continue
        if w.lower() == "shorts":
            shorts_tag = "#shorts"   # toujours EN DERNIER, jamais avant la niche
            continue
        if w.lower() in {n[1:].lower() for n in niche}:
            continue
        niche.append("#" + w)
        if len(niche) >= max(1, limit):
            break
    if shorts_tag:
        niche.append(shorts_tag)
    return " ".join(niche)


def build_description(body, hashtags, max_bytes=DESC_MAX_BYTES):
    """Description finale : corps + bloc hashtags, troncature PAR OCTETS.

    Le hashtag invalide TOUTE la chaine s'ils sont >60 au total : on garantit
    ici le nombre, et upload.py garantit le budget octets via cette fonction.
    """
    body = str(body or "").rstrip()
    tags = str(hashtags or "").strip()
    parts = [p for p in (body, tags) if p]
    return truncate_bytes("\n\n".join(parts), max_bytes)


# ---------------------------------------------------------------------------
# 2) TITRES : familles mesurees + score + anti-repetition
# ---------------------------------------------------------------------------

# Vitesses d'acces a 10K vues par famille de titre (etude 2026, % de videos).
# number-led : median le plus haut (1 392 vues) -> bonus median.
TITLE_FAMILY_SPEED = {
    "statement": 16.3,   # direct-statement : atteint 10K le plus vite
    "you": 14.8,         # you-focused (2e personne)
    "number": 13.3,      # number-led, median le plus eleve
    "question": 12.7,
    "curiosity": 10.8,   # curiosity-gap : le plus lent
}
_TITLE_VISIBLE = 55
# 2e personne EN + FR (le bot ecrit des titres FR/EN mixtes : "Ton cerveau",
# "Tu as 5 secondes", "You will fail"...).
_YOU_RE = re.compile(
    r"\b(you|your|you're|youre|tu|ton|ta|tes|te|vous|votre|vos)\b", re.I)
_CURIOSITY_RE = re.compile(
    r"\b(why|reason|secret|truth|nobody|what happens|personne|pourquoi|"
    r"impossible|incroyable|jamais|seul)\b", re.I)
_NUMBER_RE = re.compile(r"\d")


def title_family(title):
    """Famille d'un titre (question / you / number / curiosity / statement).

    Ordre = priorite de detection : chiffre en TETE -> number-led ; finit
    par '?' -> question ; 2e personne -> you ; curiosite -> curiosity ;
    sinon statement (affirmation directe, famille la plus rapide mesuree).
    """
    t = str(title or "").strip()
    if not t:
        return "statement"
    if _NUMBER_RE.match(t):
        return "number"
    if t.rstrip().endswith("?"):
        return "question"
    if _YOU_RE.search(t):
        return "you"
    if _CURIOSITY_RE.search(t):
        return "curiosity"
    return "statement"


def title_score(title, family_bonus=None):
    """Score de titre 0..100 : famille mesuree + regles de forme 2026.

    - famille (vitesse 10K mesuree) ~ moitie du score
    - chiffre present, mot-cle de niche dans les 30 premiers caracteres
    - longueur : sweet spot 20-55 (zone visible du feed), penalite > 55
    - CAPS : 1-2 mots max (au-dela : effet spam mobile)
    - emojis en debut : penalise (mange la zone visible)
    - family_bonus : {famille: delta relatif appris par l'optimizer}
    """
    t = str(title or "").strip()
    if not t:
        return 0.0
    fam = title_family(t)
    base = TITLE_FAMILY_SPEED.get(fam, 12.0) / 16.3 * 50.0
    bonus = float((family_bonus or {}).get(fam, 0.0))
    base += max(-0.3, min(0.3, bonus)) * 50.0  # +/-15 pts max, borne
    s = base
    if _NUMBER_RE.search(t):
        s += 6.0
    niche_word = re.search(r"(quiz|trivia|question|\biq\b|\btest\b|culture)",
                           t, re.I)
    if niche_word and niche_word.start() < 30:
        s += 5.0
    caps = sum(1 for w in t.split() if w.isupper() and len(w) > 2)
    s -= 4.0 * max(0, caps - 2)
    if len(t) > _TITLE_VISIBLE:
        s -= min(12.0, (len(t) - _TITLE_VISIBLE) * 0.8)
    elif 20 <= len(t) <= _TITLE_VISIBLE:
        s += 5.0
    if re.match(r"^[\U0001F300-\U0001FAFF\U00002600-\U000027BF]", t):
        s -= 5.0
    if re.search(r"\b(subscribe|sub4sub)\b", t, re.I):
        s -= 15.0
    return round(max(0.0, s), 2)


def title_ok(title):
    """Contrat API : 8-100 caracteres, pas de < > (invalid)."""
    t = str(title or "")
    return 8 <= len(t) <= 100 and "<" not in t and ">" not in t


def pick_title(candidates, recent=None, family_bonus=None, seq=0):
    """Meilleur titre DISPONIBLE : score + anti-repetition deterministe.

    - exclus les titres deja utilises recemment (`recent`, insensible casse)
    - score = title_score + family_bonus appris
    - tie-break deterministe leger (crc32 + seq) : deux titres ex-aequo
      alternent sans jamais devenir aleatoires (reproductible en test).
    Renvoie (titre, famille, score) ; fallback = premier candidat valide.
    """
    recent_l = {str(t).strip().lower() for t in (recent or []) if t}
    best, best_key = None, None
    fallback = None
    for cand in (candidates or []):
        t = str(cand or "").strip()
        if not title_ok(t):
            continue
        if fallback is None:
            fallback = t        # 1er valide = secours, MAIS aussi candidat
        if t.lower() in recent_l:
            continue
        fam = title_family(t)
        score = title_score(t, family_bonus)
        jitter = ((zlib.crc32(t.encode("utf-8")) + int(seq or 0)) % 97) / 1000.0
        key = (score + jitter, t)
        if best_key is None or key > best_key:
            best, best_key = t, key
    if best is None:
        for cand in (candidates or []):
            t = str(cand or "").strip()
            if title_ok(t):
                return t, title_family(t), title_score(t, family_bonus)
        return (candidates[0][:100] if candidates else ""), "statement", 0.0
    return best, title_family(best), title_score(best, family_bonus)


# ---------------------------------------------------------------------------
# 3) FENETRES US INTELLIGENTES : publish = pic_audience - lead (1h30)
# ---------------------------------------------------------------------------

# Pics d'audience US heure LOCALE (ET), par jour de semaine (Mon=0).
# Etudes : SocialPilot (301K videos), Buffer (1.8M) - notes section 5.1.
US_PEAKS = {
    0: [(12, 15)],                       # Mon
    1: [(14, 17)],                       # Tue
    2: [(12, 15)],                       # Wed
    3: [(12, 15)],                       # Thu
    4: [(12, 15), (16, 19), (19, 21)],   # Fri (meilleur jour, pic 16-19)
    5: [(11, 14)],                       # Sat (week-end : mid-day peak)
    6: [(11, 14)],                       # Sun
}


def _et_tz():
    """Fuseau US Eastern ; fallback offset fixe -5 si tzdata absent."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return dt.timezone(dt.timedelta(hours=-5), "EST")


def publish_instants(cfg, utc_ref=None):
    """Instants de publication UTC (datetime) pour ~48 h autour de `utc_ref`.

    publish = debut_du_pic_ET - lead (defaut 90 min, config
    [upload].smart_lead_min). Fenetre passee de plus de 6 h = ecartee
    (inutile de publier pour un pic deja termine).
    """
    cfg = cfg or {}
    up = cfg.get("upload") or {}
    try:
        lead_min = int(up.get("smart_lead_min", 90))
    except (TypeError, ValueError):
        lead_min = 90
    lead = dt.timedelta(minutes=lead_min)
    tz_et = _et_tz()
    ref = utc_ref or dt.datetime.now(dt.timezone.utc)
    out = []
    for offset in (-1, 0, 1, 2):
        et_date = (ref.astimezone(tz_et) + dt.timedelta(days=offset)).date()
        for start_h, _end_h in US_PEAKS.get(et_date.weekday(), []):
            start_et = dt.datetime(et_date.year, et_date.month, et_date.day,
                                   start_h, 0, tzinfo=tz_et)
            pub_utc = (start_et - lead).astimezone(dt.timezone.utc)
            if pub_utc < ref - dt.timedelta(hours=6):
                continue
            out.append(pub_utc)
    return sorted(set(out))


def smart_post_hours(cfg, utc_now=None):
    """Heures UTC (int) a publier POUR LE JOUR UTC de `utc_now`.

    Renvoie [] si aucun pic ne tombe ce jour-la (le scheduler retombe alors
    sur post_hours_utc statiques). Deduplique : deux pics peuvent donner la
    meme heure UTC apres conversion ET->UTC.
    """
    ref = utc_now or dt.datetime.now(dt.timezone.utc)
    target = ref.date()
    hours = set()
    for pub in publish_instants(cfg, ref):
        if pub.date() == target:
            hours.add(pub.hour)
    return sorted(hours)


def next_smart_instant(cfg, utc_now=None):
    """Prochain instant de publication intelligent (UTC datetime)."""
    ref = utc_now or dt.datetime.now(dt.timezone.utc)
    instants = [p for p in publish_instants(cfg, ref) if p >= ref]
    return instants[0] if instants else None


# ---------------------------------------------------------------------------
# 4) GATES DE RETENTION : boucle d'auto-amelioration mesurable
# ---------------------------------------------------------------------------

# Seuils engaged-view ratio (%%) -> gate. Revision officielle du 31/03/2025 :
# "viewed vs swiped away" = "Stayed to watch" ; engagedViews/views est le
# vrai ratio restes-vs-swipes.
GATE_KILL = 60.0
GATE_TIGHTEN = 70.0
GATE_SCALE = 85.0

_DIRECTIVES = {
    "KILL_HOOK": (
        "CRITICAL retention gate KILL_HOOK: engaged-view ratio %.0f%% is "
        "below 60%% - viewers swipe in the first seconds. REBUILD the hook: "
        "the single most surprising element must hit in the first 2 "
        "seconds, on-screen text must read in under 2 seconds, and the "
        "first spoken line must NOT restate the title."),
    "TIGHTEN_EDIT": (
        "Retention gate TIGHTEN_EDIT: engaged-view ratio %.0f%% sits in the "
        "60-70%% band. Tighten the edit: remove every pause between beats, "
        "cut one question/fact, keep each segment under 4 seconds, and "
        "move the payoff earlier."),
    "ADD_LOOP": (
        "Retention gate ADD_LOOP: engaged-view ratio %.0f%% is good "
        "(70-85%%). To break into the top tier, strengthen the LOOP: tease "
        "the payoff in the first line, end where the video starts so the "
        "viewer re-watches (loop ending), and add a mid-video checkpoint "
        "restating the stakes."),
    "SCALE": (
        "Retention gate SCALE: engaged-view ratio %.0f%% is elite (>=85%%). "
        "Keep the exact structure and increase output frequency - the "
        "format is proven, scale it."),
}


def retention_gate(engaged_ratio, loop_rate=None):
    """Gate pour une video : {gate, directive, engaged_ratio, loop_rate}.

    engaged_ratio = engagedViews/views (vrai "Stayed to watch"). loop_rate
    > 100 -> REPLAY_LOOP : les viewers re-regardent, la boucle prend.
    """
    ratio = float(engaged_ratio or 0.0)
    if ratio < GATE_KILL:
        gate = "KILL_HOOK"
    elif ratio < GATE_TIGHTEN:
        gate = "TIGHTEN_EDIT"
    elif ratio < GATE_SCALE:
        gate = "ADD_LOOP"
    else:
        gate = "SCALE"
    directive = _DIRECTIVES[gate] % ratio
    try:
        lr = float(loop_rate) if loop_rate is not None else None
    except (TypeError, ValueError):
        lr = None
    if lr is not None and lr > 100.0:
        directive += (" REPLAY_LOOP confirmed: viewers rewatch (%.0f%% loop "
                      "rate) - the ending-to-beginning loop WORKS, protect "
                      "it in every future script." % lr)
    elif lr is not None and lr < 80.0:
        directive += (" Loop check FAILED (%.0f%% < 80%%): viewers do NOT "
                      "rewatch - end the video on an unresolved beat that "
                      "the first line answers." % lr)
    return {"gate": gate, "directive": directive,
            "engaged_ratio": round(ratio, 1), "loop_rate": lr}


def gates_summary(rows):
    """Resume des gates sur plusieurs videos (rows analytics retention).

    Renvoie {counts: {gate: n}, mean_engaged, gates_directive} : la
    directive agregee est celle du pire gate majoritaire (la boucle corrige
    d'abord ce qui tue, ensuite ce qui peut scaler).
    """
    rows = rows or []
    counts, vals, worst = {}, [], None
    for r in rows:
        if not isinstance(r, dict):
            continue
        g = retention_gate(r.get("engaged_ratio"), r.get("loop_rate"))
        counts[g["gate"]] = counts.get(g["gate"], 0) + 1
        vals.append(g["engaged_ratio"])
        if worst is None or g["engaged_ratio"] < worst["engaged_ratio"]:
            worst = g
    mean = round(sum(vals) / len(vals), 1) if vals else 0.0
    # Priorite : le gate le plus grave present dans les donnees commande la
    # directive aggregate (KILL_HOOK > TIGHTEN_EDIT > ADD_LOOP > SCALE).
    priority = ("KILL_HOOK", "TIGHTEN_EDIT", "ADD_LOOP", "SCALE")
    top = next((g for g in priority if counts.get(g)), None)
    directive = ""
    if worst and top:
        directive = retention_gate(worst["engaged_ratio"],
                                   _mean_loop(rows))["directive"]
    return {"counts": counts, "mean_engaged": mean, "gates_directive": directive,
            "n": len(vals)}


def _mean_loop(rows):
    loops = [r.get("loop_rate") for r in rows
             if isinstance(r, dict) and r.get("loop_rate") is not None]
    try:
        return (sum(float(x) for x in loops) / len(loops)) if loops else None
    except (TypeError, ValueError):
        return None


def gates_directive_from_file(cfg):
    """Directive gates depuis data/analytics/retention_latest.json ('' si rien).

    Branchee dans optimizer.apply_config : s'applique des la 1re video
    analysee, comme hook_directive, mais avec des seuils actionnables par
    palier au lieu d'un seul message.
    """
    from .config import get_paths
    from .util import load_json
    try:
        doc = load_json(get_paths(cfg).analytics / "retention_latest.json") or {}
    except Exception:
        return ""
    rows = doc.get("rows") or []
    if not rows:
        return ""
    return gates_summary(rows).get("gates_directive") or ""


# ---------------------------------------------------------------------------
# 5) Aides descriptions : corps + CTA question ouverte (levier commentaires)
# ---------------------------------------------------------------------------

def open_question_cta(total, series="quiz"):
    """CTA question FERMEE pour la niche quiz (notes 4.4 : levier commentaires
    le plus cite) : reponse facile -> volume de commentaires maximal."""
    return "Score check: how many did you get out of %d? 0 is a valid answer." % total
