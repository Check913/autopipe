# -*- coding: utf-8 -*-
"""Gate qualite pre-publication (skill hooks, 2026-11-01).

Dernier rempart AVANT l'upload YouTube : repare automatiquement tout ce
qui peut l'etre (emoji interdits par l'API, prefixes compteur "3."/"#5"/
"part 2", titre trop court/long, description sans hashtags, tags vides)
et BLOQUE (violations) ce qui est interdit par la monetisation YouTube ou
par l'identite de chaine (banned_title_words / anti_topics / ban dur).

Contrat :
    title, description, tags, report = check_and_repair(
        title, description, tags, cfg=cfg)
    report = {"repairs": [...], "violations": [...]}

  - violations VIDE   -> publier avec les valeurs reparees ;
  - violations non vide -> NE PAS publier : le scheduler appelle
    write_audit (data/hooks/blocked/<ts>-<slug>.json), alerte, purge le
    pending et relance immediatement le pipeline ("publier a tout prix"
    = un nouveau sujet, jamais la publication d'une metadata interdite).

Fail-open garanti : le scheduler encapsule l'appel dans try/except et
cette fonction est defensive (None/nombre -> chaine vide, jamais
d'exception volontaire : une panne du gate ne doit JAMAIS bloquer un
upload valide).
"""
import re
from datetime import datetime, timezone

from .. import identity, metas, util
from ..config import get_config, get_paths

log = util.get_logger("hooks.quality")

# Emoji + selecteurs/ZWJ : l'API YouTube metadata les refuse ou les rend
# illisibles dans les notifications push (recherche 2025-2026 : titres
# propres > titres a emojis sur CTR mobile).
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U0000FE0F\U0000200D]"
)

# Prefixes "compteur" retires du titre, UNIQUEMENT quand ce n'est PAS un
# vrai titre number-led ("3 Facts No One Tells You" reste intact) :
#   - nombre suivi d'une ponctuation ("3. ", "5) ", "7: ") ;
#   - prefixe #/n/n./n°/num ("#5 Facts", "No. 5 Facts", "n°5") ;
#   - "part|episode|video|day + nombre" ("Part 2: ...", "Day 3 ...").
# Le lookahead apres ponctuation evite de casser "10,000 Facts" (la
# virgule de millier n'est PAS suivie d'un espace).
_COUNTER_RE = re.compile(
    r"^\s*(?:"
    r"\d+\s*[\.\,\:\;\!\?\)\]]+(?=\s|$)\s*"
    r"|(?:#|n[o°]?\.?|num(?:ber)?\.?)\s*\d+\b[\.\-:]?\s*"
    r"|(?:part|ep(?:isode)?|video|day)\s*#?\s*\d+\b[\.\-:]?\s*"
    r")",
    re.IGNORECASE,
)

# Strip des hashtags quand la description en contient > 60 (seuil
# officiel YouTube : > 60 hashtags = TOUS invalides).
_HASHTAG_SUB_RE = re.compile(r"#[A-Za-z0-9_]+")

# Ban dur local (monetisation / securite YouTube), verifie sur le titre,
# la description ET les tags via identity._norm / _phrase_hit.
HARD_BAN = (
    "suicide", "self harm", "kill yourself", "bomb", "explosive",
    "porn", "nude", "naked", "sex", "escort",
    "terrorist", "nazi", "cocaine", "heroin", "meth",
)

# Hashtags de secours quand la description n'en a aucun (regles 2026 :
# 1 niche + 1 categorie + large, #shorts TOUJOURS EN DERNIER via
# metas.build_hashtags).
_DEFAULT_TAGS = ("didyouknow", "interesting", "amazingfacts", "shorts")

# Titre trop court = invisible dans les suggestions mobiles.
MIN_TITLE_BYTES = 15


def _strip_emoji(text):
    return _EMOJI_RE.sub("", str(text or ""))


def _strip_counter(title):
    return _COUNTER_RE.sub("", str(title or "")).strip()


def _topic_from(title):
    """Sujet pour le titre de secours ; garde les mots d'1 caractere
    (contrairement a identity._topic_base qui les filtre)."""
    t = re.sub(r"[^0-9A-Za-z]+", " ", str(title or "")).strip()
    t = " ".join(t.split()[:6])
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    return t


def _title_max_chars(cfg):
    try:
        return int((cfg or {}).get("ideas", {}).get("title_max_chars", 50))
    except Exception:
        return 50


def check_and_repair(title, description, tags, pending=None, cfg=None):
    """Repairs automatiques puis violations. Ne leve jamais (fail-open).

    Renvoie (title, description, tags, {"repairs": [...],
    "violations": [...]}). Les violations sont evaluees sur les valeurs
    REPAREES (on juge ce qui serait reellement publie).
    """
    try:
        cfg = cfg or get_config()
    except Exception:
        cfg = {}

    ident, banned, anti = {}, [], []
    try:
        ident = identity.load_identity(cfg) or {}
        banned = identity.banned_title_words(cfg.get("ideas")) or []
        anti = list(ident.get("anti_topics") or [])
    except Exception:
        log.debug("identity indisponible pour le gate qualite", exc_info=True)

    repairs, violations = [], []
    title = str(title or "")
    description = str(description or "")
    tags = [str(t).strip() for t in (tags or []) if str(t).strip()]

    # ---- 1) emoji (titre + description) -----------------------------------
    t2 = _strip_emoji(title)
    if t2 != title:
        repairs.append("title:emoji_removed")
        title = t2
    d2 = _strip_emoji(description)
    if d2 != description:
        repairs.append("description:emoji_removed")
        description = d2

    # ---- 2) prefixe compteur (titre uniquement) ---------------------------
    t3 = _strip_counter(title)
    if t3 != title.strip():
        repairs.append("title:counter_prefix_removed")
        title = t3

    # ---- 3) whitespace (titre uniquement ; la description garde ses
    # sauts de ligne, la structure vient de metas.build_description) --------
    t4 = " ".join(title.split())
    if t4 != title:
        repairs.append("title:whitespace_collapsed")
        title = t4

    # ---- 4) titre vide / trop court -> template number-led fiable ---------
    if len(title.encode("utf-8")) < MIN_TITLE_BYTES:
        topic = _topic_from(title)
        title = ("3 %s Facts No One Tells You" % topic) if topic \
            else "3 Facts No One Tells You"
        repairs.append("title:rebuilt_short")

    # ---- 5) titre trop long -> coupe sur mot entier -----------------------
    max_chars = _title_max_chars(cfg)
    if len(title) > max_chars:
        short = identity._shorten(title, max_chars)
        if short and short != title:
            repairs.append("title:truncated")
            title = short

    # ---- 6) description : vide -> corps = titre ; 0 hashtag -> bloc
    # hashtags ; > 60 -> strip + rebuild <= 5 --------------------------------
    body = description.strip()
    if not body:
        body = title
        repairs.append("description:empty_filled_from_title")
    n_hash = len(metas.split_hashtags(body))
    first_tag = tags[0] if tags else ""
    if n_hash == 0:
        description = metas.build_description(
            body, metas.build_hashtags(first_tag or "facts", _DEFAULT_TAGS))
        repairs.append("description:hashtags_added")
    elif n_hash > 60:
        body = " ".join(_HASHTAG_SUB_RE.sub(" ", body).split())
        description = metas.build_description(
            body, metas.build_hashtags(first_tag or "facts", _DEFAULT_TAGS))
        repairs.append("description:hashtags_rebuilt")

    # ---- 7) tags vides -> set de secours sous budget API -------------------
    if not tags:
        tags = metas.fit_tags(
            [(_topic_from(title)[:60] or "facts"), "didyouknow",
             "interesting", "shorts"])
        repairs.append("tags:rebuilt_empty")

    # ---- violations (evaluees sur les valeurs REPAREES) --------------------
    try:
        norm_title = identity._norm(title)
        norm_desc = identity._norm(description)
        norm_tags = [identity._norm(t) for t in tags]
        for w in banned:
            if identity._phrase_hit(norm_title, w):
                violations.append("title:banned_word:%s" % w)
                break
        for t in anti:
            if (identity._phrase_hit(norm_title, t)
                    or identity._phrase_hit(norm_desc, t)
                    or any(identity._phrase_hit(nt, t) for nt in norm_tags)):
                violations.append("anti_topic:%s" % t)
                break
        for w in HARD_BAN:
            hit = None
            if identity._phrase_hit(norm_title, w):
                hit = "title"
            elif identity._phrase_hit(norm_desc, w):
                hit = "description"
            elif any(identity._phrase_hit(nt, w) for nt in norm_tags):
                hit = "tags"
            if hit:
                violations.append("hard_ban:%s:%s" % (hit, w))
                break
    except Exception:
        log.debug("evaluation violations echouee (fail-open)", exc_info=True)

    return title, description, tags, {"repairs": repairs, "violations": violations}


def write_audit(kind, payload, cfg=None):
    """Audit JSON de blocage : data/hooks/blocked/<ts>-<slug>.json.

    Best-effort : un echec d'ecriture ne doit jamais masquer le blocage
    (le scheduler alerte independamment). Renvoie le chemin ou None.
    """
    try:
        cfg = cfg or get_config()
        paths = get_paths(cfg)
        d = paths.hooks / "blocked"
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        slug = util.slugify(str((payload or {}).get("title") or "")) or "video"
        path = d / ("%s-%s.json" % (ts, slug))
        doc = dict(payload or {})
        doc.setdefault("kind", kind)
        util.save_json(path, doc)
        log.info("Gate qualite: audit ecrit %s", path)
        return str(path)
    except Exception:
        log.debug("write_audit: echec (non bloquant)", exc_info=True)
        return None
