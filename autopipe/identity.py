"""Identite de chaine : gate de niche, titres courts, hooks qui accrochent.

Cree le 2026-10-27 pour repondre aux critiques sur le Short "AP Dhillon's
Success Hides a Global Music Shift" :

1. Titre style article, trop abstrait (47 caracteres, aucun "tu") ->
   title_fix() force un titre <= title_max_chars (50 par defaut), concret,
   parle au viewer (you/your), question ou chiffre, mots abstraits bannis.
2. Sujets heterogenes (musique, lingerie 475 crore, Venus, Tenet, Halloween,
   "walking with God", data science...) -> filter_topics() applique un gate
   thematique : seuls les sujets de la niche [identity] survivent ; sinon
   l'appelant retombe sur les mots-cles de niche du scout. L'algorithme
   YouTube ne peut cataloguer la chaine que si la niche est stable.
3. Chaine sans identite claire -> identity_block() est injecte dans TOUS les
   prompts LLM (ideas + remake) pour que chaque Short parle a la meme
   audience avec la meme promesse et le meme ton.

Hook visuel / hook texte : hook_fix() garantit <= hook_max_chars (60) et
une formulation conflit/curiosite (question, negation ou chiffre).
"""
import re

from .config import get_config
from .util import get_logger

log = get_logger("identity")

# Mots "titre d'article" bannis : ils decrivent un sujet au lieu de faire
# cliquer. Le titre critique "Success Hides a Global Music Shift" en
# accumulait trois ("hides", "shift", "global").
ABSTRACT_TITLE_WORDS = (
    "hides", "unveils", "shift", "era", "legacy", "journey", "remarkable",
    "fascinating", "untold", "the truth about", "everything you",
    "deep dive", "comprehensive", "analysis", "phenomenon", "evolution",
    "revolution", "landscape", "realm", "tapestry",
)


def banned_title_words(ideas_cfg=None):
    """Liste fusionnee des mots bannis (defaut + ideas.banned_title_words)."""
    extra = (ideas_cfg or {}).get("banned_title_words") or []
    return list(ABSTRACT_TITLE_WORDS) + [str(w).strip().lower()
                                         for w in extra if str(w).strip()]


def load_identity(cfg=None):
    """Charge la section [identity] avec valeurs par defaut robustes."""
    cfg = cfg or get_config()
    ident = dict(cfg.get("identity") or {})
    ident.setdefault("name", "")
    # setdefault ne suffit pas : les cles existent avec "" dans les defaults
    # de config.py -> fallback explicite si vide.
    if not str(ident.get("niche") or "").strip():
        ident["niche"] = str((cfg.get("general") or {}).get("niche")
                             or "").strip()
    ident.setdefault("audience", "")
    ident.setdefault("promise", "")
    ident.setdefault("tone", "")
    ident.setdefault("keywords", [])
    ident.setdefault("anti_topics", [])
    ident.setdefault("strict", True)
    ideas = cfg.get("ideas", {}) or {}
    ident["title_max_chars"] = int(ideas.get("title_max_chars", 50))
    ident["hook_max_chars"] = int(ideas.get("hook_max_chars", 60))
    ident["keywords"] = [str(k).strip().lower()
                         for k in (ident["keywords"] or []) if str(k).strip()]
    if not ident["keywords"]:
        # Fallback : derive les mots-cles de la niche general.niche +
        # scout.keywords, pour que le gate de niche ne rejette pas tout.
        scout_kw = [str(k).strip().lower()
                    for k in ((cfg.get("scout") or {}).get("keywords") or [])
                    if str(k).strip()]
        niche_words = [w for w in _norm(ident.get("niche")).split()
                       if len(w) >= 4]
        ident["keywords"] = sorted(set(scout_kw) | set(niche_words))
    ident["anti_topics"] = [str(k).strip().lower()
                            for k in (ident["anti_topics"] or [])
                            if str(k).strip()]
    return ident


def identity_block(cfg=None):
    """Bloc texte CHANNEL IDENTITY injecte dans les prompts systeme LLM."""
    ident = load_identity(cfg)
    lines = []
    if ident.get("name"):
        lines.append("Channel name: %s" % ident["name"])
    if ident.get("niche"):
        lines.append("Channel niche (STAY INSIDE IT, every Short): %s"
                     % ident["niche"])
    if ident.get("audience"):
        lines.append("Target audience: %s" % ident["audience"])
    if ident.get("promise"):
        lines.append("Channel promise (every Short must deliver it): %s"
                     % ident["promise"])
    if ident.get("tone"):
        lines.append("Voice/tone: %s" % ident["tone"])
    if ident.get("keywords"):
        lines.append("On-niche themes/keywords: %s"
                     % ", ".join(ident["keywords"][:25]))
    if ident.get("anti_topics"):
        lines.append("NEVER cover (off-niche, would break channel identity): "
                     "%s" % ", ".join(ident["anti_topics"][:25]))
    if not lines:
        return ""
    return "CHANNEL IDENTITY\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Gate de niche
# ---------------------------------------------------------------------------

def _norm(text):
    t = " ".join(str(text or "").split()).lower()
    return re.sub(r"[^a-z0-9 ]", " ", t)


def _phrase_hit(text, phrase, prefix=True):
    """True si `phrase` apparait dans `text` (mots entiers, tolerance prefixe)."""
    p = _norm(phrase).strip()
    if not p:
        return False
    pattern = r"(?:^| )%s%s(?: |$)" % (re.escape(p), r"\w*" if prefix else "")
    return re.search(pattern, text) is not None


def topic_score(topic_text, ident=None):
    """Score de pertinence niche d'un sujet.

    Renvoie :
      -1  sujet interdit (anti-topic)
       0  hors niche
      >0  nombre de mots-cles niche trouves (plus = mieux)
    """
    ident = ident or load_identity()
    t = _norm(topic_text)
    if not t:
        return 0
    for bad in ident["anti_topics"]:
        if _phrase_hit(t, bad):
            return -1
    hits = 0
    for kw in ident["keywords"]:
        if _phrase_hit(t, kw):
            hits += 1
    return hits


def filter_topics(topics, cfg=None):
    """Gate de niche : ne garde que les sujets on-niche, tries par pertinence.

    Chaque topic est un dict {"topic": ..., ...} (scout/radar). Les sujets
    hors niche sont ecartes (loggue, observabilite), jamais generes. Avec
    identity.strict=false, les sujets hors niche sont toleres mais places
    en queue (depriorises). Renvoie [] si rien ne passe : l'appelant doit
    retomber sur les mots-cles de niche du scout.
    """
    cfg = cfg or get_config()
    ident = load_identity(cfg)
    strict = bool(ident.get("strict", True))
    kept, dropped = [], []
    for t in topics or []:
        if not isinstance(t, dict):
            continue
        subject = str(t.get("topic") or "").strip()
        if not subject:
            continue
        s = topic_score(subject, ident)
        if s < 0:
            dropped.append((subject, "anti-topic"))
            continue
        if s == 0:
            if strict:
                dropped.append((subject, "hors niche"))
                continue
            t = dict(t)
            t["identity_score"] = 0
            kept.append(t)
            continue
        t = dict(t)
        t["identity_score"] = s
        kept.append(t)
    for subject, why in dropped[:10]:
        log.info("Gate niche: sujet ignore (%s): %s", why, subject[:70])
    if not kept:
        log.warning("Gate niche: 0/%s sujets on-niche -> l'appelant doit "
                    "utiliser les mots-cles de niche", len(topics or []))
        return []
    kept.sort(key=lambda t: (-int(t.get("identity_score") or 0),
                             -float(t.get("score") or 0)))
    return kept


# ---------------------------------------------------------------------------
# Titres & hooks calibres Shorts
# ---------------------------------------------------------------------------

_TITLE_TEMPLATES = (
    "Why {t} Is Everywhere Right Now",
    "The Real Reason {t} Blew Up",
    "Nobody Expected This From {t}",
    "{t} Just Broke the Internet",
    "How {t} Took Over So Fast",
    "Everyone Gets {t} Wrong. Here's Proof",
    "{t}: The Detail Everyone Missed",
    "You Won't Believe {t} Did This",
)


def _shorten(text, max_chars):
    """Coupe sur un mot entier, sans ponctuation residuelle."""
    text = " ".join(str(text or "").split())
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.-:;!?").strip()


def _topic_base(text):
    """Extrait un sujet court (<= 28 car.) utilisable dans un template."""
    t = " ".join(str(text or "").split())
    words = [w for w in t.split() if len(w) > 1][:6]
    t = " ".join(words)
    if len(t) > 28:
        t = t[:28]
        if " " in t:
            t = t[: t.rfind(" ")]
    t = t.strip(" ,.-:;!?\"'")
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    return t


def _has_banned(title, banned):
    t = _norm(title)
    return any(_phrase_hit(t, w, prefix=False) for w in banned if w)


def title_fix(title, topic=None, cfg=None):
    """Force un titre court, concret, oriente viewer (<= title_max_chars).

    Regles (critique 2026-10-27) :
      - max title_max_chars caracteres (50 par defaut), coupe sur mot entier ;
      - mots abstraits type article bannis ("hides", "shift", "era"...) ;
      - si le titre LLM est trop long/abstrait, reecriture a partir du SUJET
        via des templates questions/conflit ("Why {t} Is Everywhere Right
        Now", "The Real Reason {t} Blew Up"...).
    """
    cfg = cfg or get_config()
    ident = load_identity(cfg)
    ideas = cfg.get("ideas", {}) or {}
    max_chars = int(ideas.get("title_max_chars", 50))
    banned = banned_title_words(ideas)
    raw = " ".join(str(title or "").split()).strip()
    raw = raw.strip("\"“”‘’").rstrip(".").strip()
    if raw and raw[0].islower():
        raw = raw[0].upper() + raw[1:]
    if raw and len(raw) <= max_chars and not _has_banned(raw, banned):
        return raw
    # Le titre est trop long ou abstrait -> on reecrit a partir du sujet.
    base = _topic_base(topic or raw)
    cands = []
    for tpl in _TITLE_TEMPLATES:
        cand = tpl.format(t=base)
        if len(cand) <= max_chars and not _has_banned(cand, banned):
            # Priorite : question > 2e personne/chiffre > plus court.
            second = "you" in cand.lower() or "your" in cand.lower()
            number = any(c.isdigit() for c in cand)
            cands.append((0 if cand.endswith("?") else 1,
                          0 if second else 1, 0 if number else 1, len(cand), cand))
    if cands:
        return sorted(cands)[0][4]
    if raw:
        return _shorten(raw, max_chars)
    return _shorten(base or "This Changes Everything", max_chars)


def _default_hook(topic):
    base = _topic_base(topic)
    for tpl in ("Why is {t} everywhere?",
                "{t} is not what you think",
                "Nobody warned you about {t}"):
        cand = tpl.format(t=base)
        if len(cand) <= 60:
            return cand
    return _shorten("This is not what you think", 60)


def hook_fix(hook, topic=None, cfg=None):
    """Force un hook <= hook_max_chars, format conflit/curiosite.

    Le hook s'affiche a l'ecran pendant les 3 premieres secondes (video.py) :
    il doit poser une question ou un conflit, jamais un resume d'article.
    """
    cfg = cfg or get_config()
    ident = load_identity(cfg)
    max_chars = int((cfg.get("ideas", {}) or {}).get("hook_max_chars", 60))
    raw = " ".join(str(hook or "").split()).strip().strip("\"“”‘’")
    if not raw:
        raw = _default_hook(topic or "")
    return _shorten(raw, max_chars)


def duration_clamp(seconds, cfg=None):
    """Clamp la duree cible dans [duration_min, duration_max] (15-32 s par defaut).

    Critique 2026-10-27 : les Shorts de 40-60 s retiennent moins que les
    13-20 s ; recherche : sweet spot 15-35 s. On vise 15-32 s.
    """
    cfg = cfg or get_config()
    ideas = cfg.get("ideas", {}) or {}
    lo = int(ideas.get("duration_min", 15))
    hi = max(lo + 1, int(ideas.get("duration_max", 32)))
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        s = int(ideas.get("duration_default", 24))
    return max(lo, min(hi, s))