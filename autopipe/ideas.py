"""Idea + script generation (LLM if configured, otherwise local templates)."""
import datetime as dt
import re
import urllib.parse
from pathlib import Path

import requests

from .config import get_config, get_paths
from .identity import duration_clamp, filter_topics, hook_fix, identity_block, title_fix
from .scout import pick_topics
from .util import LLMClient, as_script_text, get_logger, load_json, save_json, slugify

log = get_logger("ideas")

HOOKS = [
    "What nobody tells you about",
    "The shocking truth about",
    "99% of people don't know this about",
    "Here's why everyone gets it wrong about",
    "The secret experts won't tell you about",
    "In 30 seconds you'll finally understand",
    "Stop scrolling: this changes everything about",
]

def _system_prompt(cfg=None):
    """Prompt systeme Shorts calibre 2026 (titres 50, scripts 50-85 mots).

    Remplace la constante historique SYSTEM_PROMPT (titres 60 car., scripts
    85-120 mots = 30-45 s) qui produisait le titre-article 'AP Dhillon's
    Success Hides a Global Music Shift' et des Shorts trop longs. Les
    limites viennent de [ideas] et le bloc identite de [identity] est
    injecte pour que l'algorithme puisse cataloguer la chaine.
    """
    cfg = cfg or get_config()
    ideas = cfg.get("ideas", {}) or {}
    return (
        "You are an expert US YouTube Shorts writer, vertical format, faceless. "
        "Write in natural US English, short spoken sentences (150-170 wpm). "
        "STRICT RULES: "
        "(1) title: max %(title_max)d characters, SHORT and CONCRETE, written "
        "to make someone tap: second person ('you'/'your') or a shocking "
        "question, keyword in the first 3 words, NO abstract article-style "
        "phrasing (never 'hides a shift', 'the rise of', 'an era of'). "
        "Winning examples: 'Why Everyone Is Copying This Song', "
        "'You Won't Believe What This Singer Earns', "
        "'This Habit Steals 3 Hours of Your Day'. "
        "(2) script: %(words_min)d-%(words_max)d words (%(dur_min)d-%(dur_max)d "
        "seconds). SHORTER IS BETTER: cut anything that does not build the "
        "hook or the payoff. Sentence 1 IS the hook: the most shocking fact or "
        "claim, no greeting, no intro, no 'welcome back'. "
        "(3) LOOP ENDING: the last sentence must flow back into the first "
        "sentence so the video loops seamlessly. "
        "(4) No subscribe begging; engagement comes from the pinned comment "
        "question. "
        "%(identity)s"
        "You reply ONLY with valid JSON with keys: title (max %(title_max)d "
        "chars), hook (max %(hook_max)d chars: the shocking first fact, used "
        "as on-screen text), script, comment (one short question to pin as "
        "first comment to spark replies), description (2-3 lines with 3 "
        "hashtags), tags (list of 8-12 keywords), duration_target (int "
        "seconds, between %(dur_min)d and %(dur_max)d)."
    ) % {
        "title_max": int(ideas.get("title_max_chars", 50)),
        "hook_max": int(ideas.get("hook_max_chars", 60)),
        "words_min": int(ideas.get("script_words_min", 50)),
        "words_max": int(ideas.get("script_words_max", 85)),
        "dur_min": int(ideas.get("duration_min", 15)),
        "dur_max": int(ideas.get("duration_max", 32)),
        "identity": identity_block(cfg),
    }


def _user_prompt(topic, language, niche, directive=None):
    """Prompt utilisateur + MODULE 3 : directive de style apprise.

    Sans `directive` (ou avec None), le prompt est STRICTEMENT identique au
    comportement historique (retrocompatibilite totale).
    """
    prompt = (
        "Trending topic today: %s\nChannel niche: %s\nLanguage: %s\n\n"
        "Write a viral-ready Short about it: curiosity-gap title, hook-first "
        "script whose last sentence loops back to the first, a question for "
        "the pinned comment, YouTube description with 3 hashtags, tags."
        % (topic, niche, language)
    )
    if directive:
        prompt += ("\n\nSTYLE CALIBRATION (learned from your own published "
                   "videos, follow it): %s" % directive)
    return prompt


TITLE_PATTERNS = [
    "Nobody Talks About {t}",
    "The Hidden Truth About {t}",
    "Why Is Nobody Talking About {t}?",
    "{t}: What They Never Tell You",
    "The Secret Side of {t} Nobody Shows",
]

COMMENT_TEMPLATES = [
    "Which part surprised you the most? Tell me below!",
    "Be honest: did you already know this?",
    "What should I cover next? Drop your idea in the comments!",
]


def _curiosity_title(topic, cfg=None):
    """Titre curiosity-gap -> title_fix (<= title_max_chars, non abstrait).

    Historiquement 60 car. max : c'est comme cela qu'est ne le titre-article
    critique. On passe maintenant par identity.title_fix qui coupe a 50,
    bannit les mots abstraits et reecrit si besoin en question/2e personne.
    """
    cfg = cfg or get_config()
    t = " ".join(str(topic).split())
    if t:
        t = t[0].upper() + t[1:]
    raw = TITLE_PATTERNS[len(t) % len(TITLE_PATTERNS)].format(t=t)
    return title_fix(raw, topic=t, cfg=cfg)


def _normalize_topic(topic):
    t = " ".join(str(topic).split()).lower()
    return re.sub(r"[^a-z0-9 ]", "", t).strip()


def _used_topics(paths):
    """Sujets deja traites dans les runs precedents (anti-doublon)."""
    used = set()
    if not paths.ideas.exists():
        return used
    for path in paths.ideas.glob("*.json"):
        if path.name in ("latest.json",):
            continue
        data = load_json(path, {})
        src = data.get("source_topic")
        if src:
            used.add(_normalize_topic(src))
        title = data.get("title")
        if title:
            used.add(_normalize_topic(title))
    return used


def _wiki_facts(topic):
    """2-3 phrases reelles depuis l'API resume Wikipedia (gratuit, sans cle).
    Retourne une liste de phrases ou [] si indisponible."""
    headers = {"User-Agent": "AutoPipe/2 (idea research)"}
    base = str(topic).strip()
    # Nettoyage des titres YouTube clibateaux: "X? (Explained in 5 Minutes)"
    # -> "X" ; sinon la page exacte n'existe jamais.
    cleaned = re.sub(r"\s*[(\[].*?[)\]]\s*", " ", base)
    cleaned = re.sub(r"\s*(explained?|in \d+ minutes?|top \d+|you (won'?t|will not) believe)\b.*$",
                     "", cleaned, flags=re.I).strip(" ?!.-")
    for candidate in [cleaned, base]:
        if not candidate:
            continue
        facts = _wiki_summary(candidate, headers)
        if facts:
            return facts
    # Derniere chance: recherche Wikipedia et 1er resultat.
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": cleaned or base,
                    "srlimit": 1, "format": "json"},
            timeout=8, headers=headers,
        )
        hits = resp.json().get("query", {}).get("search", []) if resp.status_code == 200 else []
        if hits:
            return _wiki_summary(hits[0]["title"], headers)
    except Exception as exc:
        log.warning("Recherche Wikipedia indisponible: %s", exc)
    return []


def _wiki_summary(title, headers):
    """Resume d'une page Wikipedia precise (phrases utilisables ou [])."""
    slug = urllib.parse.quote(str(title).strip().replace(" ", "_"), safe="")
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/%s" % slug
    try:
        resp = requests.get(url, timeout=8, headers=headers)
        if resp.status_code != 200:
            log.info("Wikipedia %s pour '%s'", resp.status_code, title[:60])
            return []
        extract = str(resp.json().get("extract") or "").strip()
        if not extract:
            return []
        sentences = re.split(r"(?<=[.!?])\s+", extract)
        return [s.strip() for s in sentences if len(s.strip()) > 30][:3]
    except Exception as exc:
        log.warning("Wikipedia indisponible pour '%s': %s", title[:60], exc)
        return []


def fallback_idea(topic, cfg=None):
    """Backup idea sans LLM, mais avec du contenu REEL (Wikipedia).
    Un script 100% template sans information fait swiper les viewers en 2 s
    et tue la distribution seed (regle YouTube oct. 2026: contenu original
    favored, contenu generique penalise)."""
    cfg = cfg or get_config()
    t = " ".join(str(topic).split())
    title = _curiosity_title(t, cfg=cfg)
    facts = _wiki_facts(t)
    if facts:
        hook = hook_fix(facts[0], topic=t, cfg=cfg)
        script = (
            "%s\n"  # phrase 1: le fait reel, accroche directe
            "That is not a guess \u2013 it is what the science actually says.\n"
            "%s\n"
            "%s\n"
            "And that is the real story of %s."
        ) % (facts[0], facts[1] if len(facts) > 1 else facts[0],
             facts[2] if len(facts) > 2 else "Most people have never heard this.", t)
        generator = "fallback+wiki"
    else:
        hook = hook_fix("Nobody talks about %s" % t, topic=t, cfg=cfg)
        script = (
            "What nobody tells you about %s.\n"
            "Here is the part almost everyone skips.\n"
            "First: the basics that actually matter.\n"
            "Second: the classic mistake that costs the most.\n"
            "Third: the simple switch that changes everything.\n"
            "And that is exactly what nobody tells you about %s."
        ) % (t, t)
        generator = "fallback"
    ideas = cfg.get("ideas", {}) or {}
    return {
        "title": title,
        "hook": hook,
        "script": script,
        "comment": COMMENT_TEMPLATES[len(t) % len(COMMENT_TEMPLATES)],
        "description": (
            "%s\nThe real facts, in 30 seconds.\n"
            "#shorts #facts #didyouknow" % title
        ),
        "tags": ["shorts", t[:40], "facts", "didyouknow", "mind blowing"],
        # Critique 2026-10-27 : 40 s etait trop long (retention en chute) ->
        # on cible la fenetre 15-32 s via identity.duration_clamp.
        "duration_target": duration_clamp(ideas.get("duration_default", 24), cfg=cfg),
        "source_topic": t,
        "generator": generator,
    }


def llm_idea(llm, topic, cfg):
    try:
        data = llm.chat_json(
            _system_prompt(cfg),
            _user_prompt(topic, cfg["general"].get("language", "en"),
                         cfg["general"].get("niche", ""),
                         directive=(cfg.get("ideas", {}) or {}).get("style_directive")),
            temperature=float(cfg["llm"].get("temperature", 0.8)),
        )
    except Exception as exc:
        log.warning("LLM failed for '%s': %s", topic[:60], exc)
        return None
    for key in ("title", "hook", "script", "description", "tags"):
        if not data.get(key):
            return None
    # Normalisation dure (critique 2026-10-27) : meme si le LLM ignore les
    # consignes, title_fix/hook_fix/duration_clamp remettent l'idee dans le
    # moule Shorts (titre <=50 non-abstrait, hook <=60, duree 15-32 s).
    data["title"] = title_fix(data["title"], topic=topic, cfg=cfg)
    data["hook"] = hook_fix(data["hook"], topic=topic, cfg=cfg)
    # Le LLM renvoie parfois le script en tableau JSON de lignes (observe
    # avec gemini-2.5-flash le 2026-10-26): edge-tts exige un str.
    data["script"] = as_script_text(data["script"])
    data["duration_target"] = duration_clamp(data.get("duration_target"), cfg=cfg)
    if not data.get("comment"):
        data["comment"] = COMMENT_TEMPLATES[0]
    if not isinstance(data["tags"], list):
        data["tags"] = [str(data["tags"])]
    data["source_topic"] = topic
    data["generator"] = "llm:" + str(cfg["llm"].get("model", ""))
    return data


def run_ideas(config=None, count=5):
    cfg = config or get_config()
    paths = get_paths(cfg)
    scout_data = load_json(paths.scout / "latest.json", {"top": []})
    # On demande plus de sujets que necessaire: le dedup inter-runs va en
    # eliminer (les 3 runs du 23/10 avaient produit 3x le meme sujet).
    candidates = pick_topics(scout_data, n=max(count * 4, 12), cfg=cfg)
    # Complement radar multi-plateformes : scout.topics inclut desormais les
    # sujets radar (fusion scout), et on complete en direct avec
    # data/radar/latest.json (Reddit/Trends/News/TikTok) si le scout etait
    # vide ou sans radar. Dedup sur sujet normalise, jamais fatal.
    try:
        from .radar import latest_radar
        radar_topics = latest_radar(cfg).get("topics") or []
    except Exception as exc:
        radar_topics = []
        log.warning("Topics radar indisponibles: %s", exc)
    extra = [t for t in list(scout_data.get("topics") or []) + radar_topics
             if isinstance(t, dict) and t.get("topic")]
    if extra:
        seen_topics = {_normalize_topic(t["topic"]) for t in candidates}
        for t in extra:
            key = _normalize_topic(t["topic"])
            if not key or key in seen_topics:
                continue
            seen_topics.add(key)
            candidates.append(t)
    # Gate de niche (critique 2026-10-27) : AP Dhillon, lingerie 475 crore,
    # Venus, Tenet et 'walking with God' dans la meme journee rendaient la
    # chaine inclassable. filter_topics garde uniquement les sujets
    # [identity].keywords et rejette [identity].anti_topics.
    candidates = filter_topics(candidates, cfg=cfg)
    used = _used_topics(paths)
    topics = [t for t in candidates
              if _normalize_topic(t["topic"]) not in used][:count]
    skipped = len(candidates) - len(topics)
    if skipped:
        log.info("Dedup: %s sujet(s) deja traite(s) ignore(s).", skipped)
    if not topics:
        log.warning("No trending data available; using niche default topics.")
        topics = [{"topic": t} for t in cfg["scout"].get("keywords", ["amazing facts"])[:count]]

    llm = LLMClient(base_url=cfg["llm"].get("base_url", ""),
                    api_key=cfg["llm"].get("api_key", ""),
                    model=cfg["llm"].get("model", "gpt-4o-mini"))
    if llm.enabled:
        log.info("LLM active: %s", llm.model)
    else:
        log.info("LLM not configured -> local templates (see config.llm)")

    ideas = []
    for t in topics:
        topic = t["topic"]
        idea = None
        if llm.enabled:
            idea = llm_idea(llm, topic, cfg)
        if not idea:
            idea = fallback_idea(topic, cfg=cfg)
        idea["score"] = t.get("score")
        ideas.append(idea)

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    saved = []
    for i, idea in enumerate(ideas, 1):
        idea["id"] = "%s-%02d" % (stamp, i)
        path = paths.ideas / ("%s-%s.json" % (idea["id"], slugify(idea["title"])))
        save_json(path, idea)
        saved.append(path)
    save_json(paths.ideas / "latest.json",
              {"date": stamp, "ideas": [str(p) for p in saved]})
    log.info("Ideas generated: %s", len(ideas))
    return ideas, saved


def load_latest_idea(config=None):
    cfg = config or get_config()
    paths = get_paths(cfg)
    latest = load_json(paths.ideas / "latest.json", {})
    for name in reversed(latest.get("ideas", [])):
        idea = load_json(paths.ideas / Path(name).name)
        if idea:
            return idea
    return None
