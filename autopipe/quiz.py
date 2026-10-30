"""Pivot quiz/trivia (2026-10-30) : des idees quiz au lieu de faits passives.

Pourquoi : 344 vues en 90 jours ne peuvent pas atteindre le YPP vite ; un quiz
interactif (question -> compte a rebours -> reponse) retient mieux et coute
moins a produire que les faits "amazing". Revelation de la reponse DANS LA
MEME VIDEO (pas de chaine cross-video). Hooks A/B quiz-specific (familles
question / challenge) via hook_ab.assign — les gabarits identity.hook_variants
cassent la grammaire d'un hook quiz. Cartes ASS rendues par subs.
write_quiz_card_ass (question puis reponse), checkpoints generiques ignores
en mode quiz.

Chaque idea porte idea["quiz"]["items"] = rounds complets {question, options,
answer_index, explanation, category} rendus par video.py ; le scheduler
renvoie idea_full jusqu'a la publication puis quiz.on_published met a jour
data/quiz/state.json : 'used' = questions deja publiees (dedup longue duree),
'generated' = rounds pris par les builds pre-gen non encore publies,
'videos' = journal des uploads.
"""
import datetime as dt
import json
import re

from .config import get_paths
from .hook_ab import assign as ab_assign
from .identity import duration_plan, script_word_budget
from .util import as_script_text, get_logger, load_json, save_json

log = get_logger("quiz")

# rounds=3 / countdown=5 : budget parole long (145-165 mots pour 60 s,
# cf. identity.script_word_budget) -> hook + 3 rounds + outro ~ 165 mots.
# 5 rounds donnaient ~80 s de voix, hors cible Short.
DEFAULTS = {
    "enabled": True,
    "rounds": 3,
    "countdown_s": 5,
    "categories": ["general", "geography", "science", "history", "movies"],
    "hooks_a": [
        "How many of these can you get right?",
        "Can you beat the average score?",
        "Are you smarter than most people?",
        "How high can you score?",
    ],
    # hooks_b sans chiffre de secondes : le compte a rebours reel est
    # configurable (countdown_s), un hook ne doit pas le contredire.
    "hooks_b": [
        "Every second counts. No cheating.",
        "Most people fail question 4.",
        "Beat the average score if you can.",
        "Your time starts now.",
    ],
}

_STATE_MAX_USED = 300
_STATE_MAX_GENERATED = 24

_COUNTDOWN_WORDS = ("Ten.", "Nine.", "Eight.", "Seven.", "Six.", "Five.",
                    "Four.", "Three.", "Two.", "One.")

_TITLES = (
    "{n} Question Quiz. Can You Score {n}/{n}?",
    "Only 1% Score {n}/{n} On This Quiz",
    "Can You Beat This {n} Question Quiz?",
    "{n} Questions. {cd} Seconds Each. No Cheating.",
    "Most People Fail This {n} Question Quiz",
    "This Quiz Gets Harder Every Question",
    "If You Score {n}/{n} You Are Above Average",
    "{n} Quick Questions. No Cheating Allowed.",
)

ROUNDS_SYSTEM = ("You write general-knowledge quiz rounds for short videos. "
                 "You answer with valid JSON only.")
SCRIPT_SYSTEM = ("You write natural spoken scripts for YouTube Shorts quiz "
                 "videos. You answer with valid JSON only.")

# Banque de secours : 18 rounds verifiables, rotates par seq quand le LLM
# est indisponible. Chaque round = {question, options, answer_index,
# explanation, category}.
FALLBACK_ROUNDS = (
    {"category": "geography",
     "question": "Which US state is the largest by land area?",
     "options": ["Texas", "Alaska", "California", "Montana"],
     "answer_index": 1,
     "explanation": "Alaska is more than twice the size of Texas."},
    {"category": "science",
     "question": "Which planet has the shortest day?",
     "options": ["Mercury", "Venus", "Jupiter", "Mars"],
     "answer_index": 2,
     "explanation": "Jupiter spins once in under ten hours."},
    {"category": "science",
     "question": "What color is a polar bear's skin?",
     "options": ["White", "Pink", "Black", "Gray"],
     "answer_index": 2,
     "explanation": "Black skin absorbs heat under the white fur."},
    {"category": "history",
     "question": "Which country invented tea?",
     "options": ["India", "Japan", "England", "China"],
     "answer_index": 3,
     "explanation": "Tea was first brewed in ancient China."},
    {"category": "science",
     "question": "How many hearts does an octopus have?",
     "options": ["One", "Two", "Three", "Four"],
     "answer_index": 2,
     "explanation": "Two pump blood to the gills, one to the body."},
    {"category": "geography",
     "question": "What is the largest island in the world?",
     "options": ["Borneo", "Madagascar", "Greenland", "Sumatra"],
     "answer_index": 2,
     "explanation": "Australia is a continent, so Greenland wins."},
    {"category": "science",
     "question": "Which metal is liquid at room temperature?",
     "options": ["Iron", "Mercury", "Lead", "Tin"],
     "answer_index": 1,
     "explanation": "Mercury stays liquid even below zero."},
    {"category": "movies",
     "question": "Which film won the first Best Animated Feature Oscar?",
     "options": ["Toy Story", "Shrek", "Spirited Away", "Finding Nemo"],
     "answer_index": 1,
     "explanation": "Shrek beat the field in 2002."},
    {"category": "geography",
     "question": "Which letter is missing from all US state names?",
     "options": ["Q", "Z", "X", "J"],
     "answer_index": 0,
     "explanation": "No state name contains the letter Q."},
    {"category": "science",
     "question": "How long can a giraffe's tongue be?",
     "options": ["10 cm", "20 cm", "35 cm", "50 cm"],
     "answer_index": 3,
     "explanation": "Up to fifty centimeters, almost twenty inches."},
    {"category": "geography",
     "question": "Which country has the most time zones?",
     "options": ["Russia", "USA", "France", "China"],
     "answer_index": 2,
     "explanation": "France has twelve zones counting its territories."},
    {"category": "science",
     "question": "Which bird can fly backwards?",
     "options": ["Owl", "Hummingbird", "Eagle", "Penguin"],
     "answer_index": 1,
     "explanation": "Hummingbirds can hover and reverse."},
    {"category": "science",
     "question": "Which is the only flying mammal?",
     "options": ["Squirrel", "Bat", "Sugar glider", "Opossum"],
     "answer_index": 1,
     "explanation": "Bats have true powered flight."},
    {"category": "science",
     "question": "How many bones does a shark have?",
     "options": ["206", "100", "25", "Zero"],
     "answer_index": 3,
     "explanation": "A shark's skeleton is all cartilage."},
    {"category": "general",
     "question": "Which language has the most native speakers?",
     "options": ["English", "Spanish", "Mandarin", "Hindi"],
     "answer_index": 2,
     "explanation": "Mandarin Chinese tops the list."},
    {"category": "general",
     "question": "How much does the Eiffel Tower grow in summer?",
     "options": ["It shrinks", "5 cm", "15 cm", "1 meter"],
     "answer_index": 2,
     "explanation": "Heat expands the iron by about fifteen centimeters."},
    {"category": "general",
     "question": "Which fruit has more vitamin C than an orange?",
     "options": ["Apple", "Banana", "Kiwi", "Grape"],
     "answer_index": 2,
     "explanation": "Kiwis pack about double the vitamin C."},
    {"category": "science",
     "question": "Which planet would float in water?",
     "options": ["Earth", "Saturn", "Neptune", "Mars"],
     "answer_index": 1,
     "explanation": "Saturn is less dense than water."},
)


def _quiz_cfg(cfg):
    """Fusionne les valeurs [quiz] du config avec les defaults du module."""
    merged = dict(DEFAULTS)
    merged.update(cfg.get("quiz") or {})
    return merged


def _dir(cfg):
    d = get_paths(cfg).data / "quiz"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path(cfg):
    return _dir(cfg) / "state.json"


def _load_state(cfg):
    state = load_json(_state_path(cfg)) or {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("seq", 0)
    state.setdefault("used", [])
    state.setdefault("generated", [])
    state.setdefault("videos", [])
    return state


def _save_state(cfg, state):
    _dir(cfg)
    save_json(_state_path(cfg), state)


def _norm(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _round_key(item):
    return _norm(item.get("question"))


def _normalize_round(item, categories):
    """Valide et normalise un round (LLM ou banque) ; None si invalide."""
    if not isinstance(item, dict):
        return None
    question = str(item.get("question") or "").strip()
    raw_opts = item.get("options")
    if not question or not isinstance(raw_opts, list):
        return None
    options = [str(o).strip() for o in raw_opts if str(o or "").strip()]
    if len(options) < 3:
        return None
    try:
        idx = int(item.get("answer_index"))
    except (TypeError, ValueError):
        return None
    if idx < 0 or idx >= len(options):
        return None
    category = str(item.get("category") or "").strip().lower()
    if category not in categories:
        category = "general"
    return {
        "question": question,
        "options": options[:4],
        "answer_index": min(idx, 3),
        "explanation": str(item.get("explanation") or "").strip(),
        "category": category,
    }


def _bank_fill(state, cfg, count, rounds, exclude_keys):
    """Comble avec la banque de secours, rotation deterministe par seq.

    exclude_keys (used ∪ generated) est respecte au premier appel ; un second
    appel avec set() vide garantit que la banque repond toujours, meme si
    toutes ses questions sont deja passees.
    """
    taken = {_round_key(r) for r in rounds}
    cats = [str(c).lower() for c in
            (_quiz_cfg(cfg).get("categories") or DEFAULTS["categories"])]
    n = len(FALLBACK_ROUNDS)
    offset = int(state.get("seq") or 0)
    out = []
    for i in range(n):
        if len(out) >= count:
            break
        cand = _normalize_round(FALLBACK_ROUNDS[(offset + i) % n], cats)
        if not cand:
            continue
        key = _round_key(cand)
        if key in taken or key in exclude_keys:
            continue
        taken.add(key)
        out.append(cand)
    return out


def next_rounds(llm, cfg, count=None):
    """Rounds quiz pour une video : LLM d'abord, banque de secours sinon.

    Dedup contre data/quiz/state.json : 'used' (deja publiees) et 'generated'
    (prises par les builds pre-gen pas encore publiees). Incrémente 'seq' et
    journalise dans 'generated' (cap _STATE_MAX_GENERATED).
    """
    qcfg = _quiz_cfg(cfg)
    try:
        want = int(count or qcfg.get("rounds") or DEFAULTS["rounds"])
    except (TypeError, ValueError):
        want = DEFAULTS["rounds"]
    want = max(1, min(want, 8))
    cats = [str(c).lower() for c in
            (qcfg.get("categories") or DEFAULTS["categories"])]
    state = _load_state(cfg)
    exclude_keys = {_norm(q) for q in state.get("used", []) if q}
    exclude_keys |= {_norm(q) for q in state.get("generated", []) if q}
    rounds = []
    if llm is not None and getattr(llm, "enabled", False):
        user = (
            "Write %d general-knowledge quiz rounds for a short video. "
            "Allowed categories: %s. Each round is an object: "
            "{\"question\": str, \"options\": [exactly 4 short answers], "
            "\"answer_index\": int 0-3, \"explanation\": one short sentence}. "
            "Questions must be answerable in 10 seconds, fun and factual. "
            "Return only JSON: {\"rounds\": [...]}"
        ) % (want + 2, ", ".join(cats))
        try:
            data = llm.chat_json(ROUNDS_SYSTEM, user)
        except Exception as exc:
            log.warning("quiz: rounds LLM indisponible (%s)", exc)
            data = None
        raw = data.get("rounds") if isinstance(data, dict) else None
        for item in (raw or [])[: want * 3]:
            if len(rounds) >= want:
                break
            cand = _normalize_round(item, cats)
            if not cand:
                continue
            key = _round_key(cand)
            if key in exclude_keys or key in {_round_key(r) for r in rounds}:
                continue
            rounds.append(cand)
    if len(rounds) < want:
        rounds.extend(_bank_fill(state, cfg, want - len(rounds), rounds,
                                 exclude_keys))
    if len(rounds) < want:
        rounds.extend(_bank_fill(state, cfg, want - len(rounds), rounds, set()))
    new_keys = [_round_key(r) for r in rounds]
    state["generated"] = (list(state.get("generated", []))
                          + new_keys)[-_STATE_MAX_GENERATED:]
    state["seq"] = int(state.get("seq") or 0) + 1
    _save_state(cfg, state)
    return rounds


def hook_variants(category, cfg):
    """Deux variantes quiz pour l'A/B : famille 'question' vs 'challenge'.

    Rotation deterministe (somme d'octets de la categorie, plus seq pour B)
    pour varier sans hasard. Clippées a [ideas].hook_max_chars (60).
    """
    qcfg = _quiz_cfg(cfg)
    max_chars = int((cfg.get("ideas") or {}).get("hook_max_chars") or 60)
    state = _load_state(cfg)
    seq = int(state.get("seq") or 0)
    cat = str(category or "general")
    hooks_a = [str(h) for h in (qcfg.get("hooks_a") or DEFAULTS["hooks_a"])]
    hooks_b = [str(h) for h in (qcfg.get("hooks_b") or DEFAULTS["hooks_b"])]
    ia = sum(ord(c) for c in cat) % len(hooks_a)
    ib = (sum(ord(c) for c in cat) + seq) % len(hooks_b)
    return [
        {"variant": "A", "family": "question", "hook": hooks_a[ia][:max_chars]},
        {"variant": "B", "family": "challenge", "hook": hooks_b[ib][:max_chars]},
    ]


def _countdown_words(seconds):
    try:
        cd = int(seconds)
    except (TypeError, ValueError):
        cd = DEFAULTS["countdown_s"]
    cd = max(1, min(10, cd))
    return " ".join(_COUNTDOWN_WORDS[-cd:])


def local_script(rounds, hook, cfg):
    """Script deterministe : hook, question + options, compte a rebours
    (secondes epellees), revelation, outro score en commentaire."""
    qcfg = _quiz_cfg(cfg)
    try:
        cd_plain = int(qcfg.get("countdown_s") or DEFAULTS["countdown_s"])
    except (TypeError, ValueError):
        cd_plain = DEFAULTS["countdown_s"]
    cd_plain = max(1, min(10, cd_plain))
    cd = _countdown_words(cd_plain)
    letters = ("A", "B", "C", "D")
    lines = [str(hook or "").strip()]
    for i, r in enumerate(rounds, 1):
        opts = r["options"]
        opts_text = " ".join("%s: %s" % (letters[j], o)
                             for j, o in enumerate(opts))
        lines.append("Question %d: %s %s" % (i, r["question"], opts_text))
        lines.append("Lock in your answer, your %d seconds start now: %s"
                     % (cd_plain, cd))
        correct = opts[r["answer_index"]]
        letter = letters[min(r["answer_index"], len(letters) - 1)]
        lines.append("Time's up! The answer is %s: %s. %s"
                     % (letter, correct, r.get("explanation") or ""))
    n = len(rounds)
    lines.append("That was all %d questions." % n)
    lines.append("Comment your score out of %d, and subscribe for a new quiz "
                 "every day." % n)
    return "\n".join(lines)


def _llm_script(llm, rounds, hook, cfg):
    """Script LLM sous contrat strict : premiere ligne = hook verbatim,
    budget de mots [script_word_budget x 75% plancher], et chaque question +
    bonne reponse doivent etre presentes (normalisees). Sinon None."""
    if llm is None or not getattr(llm, "enabled", False):
        return None
    qcfg = _quiz_cfg(cfg)
    lo, _hi = script_word_budget("long", cfg=cfg)
    floor = int(lo * 0.75)
    try:
        cd_plain = int(qcfg.get("countdown_s") or DEFAULTS["countdown_s"])
    except (TypeError, ValueError):
        cd_plain = DEFAULTS["countdown_s"]
    cd_plain = max(1, min(10, cd_plain))
    payload = {
        "hook": str(hook or ""),
        "countdown_s": cd_plain,
        "total": len(rounds),
        "rounds": [{"question": r["question"], "options": r["options"],
                    "answer_index": r["answer_index"],
                    "explanation": r.get("explanation", "")}
                   for r in rounds],
    }
    user = (
        "Write the spoken script of a quiz short. The FIRST LINE must be the "
        "hook verbatim. For each round: read the question and the options "
        "A/B/C, tell viewers they have %(cd)d seconds to lock in, then "
        "reveal the answer with a one-line explanation. Close by asking "
        "viewers to comment their score out of %(total)d and to subscribe. "
        "Rounds JSON: %(json)s"
    ) % {"cd": cd_plain, "total": len(rounds),
         "json": json.dumps(payload, ensure_ascii=False)}
    try:
        data = llm.chat_json(SCRIPT_SYSTEM, user)
    except Exception as exc:
        log.warning("quiz: script LLM indisponible (%s)", exc)
        return None
    text = as_script_text((data or {}).get("script")) \
        if isinstance(data, dict) else ""
    stripped = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not stripped:
        return None
    rest = [ln for ln in stripped if _norm(ln) != _norm(hook)]
    final = "\n".join([str(hook or "").strip()] + rest)
    if len(final.split()) < floor:
        log.warning("quiz: script LLM hors budget (%s mots < %s)",
                    len(final.split()), floor)
        return None
    flat = _norm(final)
    for r in rounds:
        if _round_key(r) not in flat:
            log.warning("quiz: script LLM sans la question %r",
                        r["question"][:40])
            return None
        if _norm(r["options"][r["answer_index"]]) not in flat:
            log.warning("quiz: script LLM sans la bonne reponse")
            return None
    return final


def build_idea(llm, cfg):
    """Une idee quiz complete : titre, hook A/B quiz, rounds, script.

    LLM optionnel (None ou non configure -> banque de secours). L'idea
    renvoyee est prete pour la pipeline standard : duration_format long
    (density 4), quiz.items requis par video.py, source_topic dedup
    'quiz:<categorie>'.
    """
    qcfg = _quiz_cfg(cfg)
    state = _load_state(cfg)
    cats = [str(c).lower() for c in
            (qcfg.get("categories") or DEFAULTS["categories"])]
    try:
        cd_plain = int(qcfg.get("countdown_s") or DEFAULTS["countdown_s"])
    except (TypeError, ValueError):
        cd_plain = DEFAULTS["countdown_s"]
    cd_plain = max(1, min(10, cd_plain))
    cursor = int(state.get("seq") or 0) + len(state.get("videos") or [])
    category = cats[cursor % len(cats)]
    rounds = next_rounds(llm, cfg)
    n = len(rounds)
    variants = hook_variants(category, cfg)
    try:
        ab = ab_assign("quiz:%s" % category, variants, cfg=cfg)
    except Exception as exc:
        ab = None
        log.warning("quiz: hook A/B indisponible (%s)", exc)
    hook = str((ab or {}).get("hook") or variants[0]["hook"]).strip()
    script = _llm_script(llm, rounds, hook, cfg) \
        or local_script(rounds, hook, cfg)
    plan = duration_plan(4, cfg=cfg)
    title = _TITLES[cursor % len(_TITLES)].format(n=n, cd=cd_plain)[:50]
    idea = {
        "topic": "quiz",
        "source_topic": "quiz:%s" % category,
        "title": title,
        "hook": hook,
        "density": 4,
        "duration_format": plan["format"],
        "duration_target": plan["target"],
        "duration_range": [plan["min"], plan["max"]],
        "script": script,
        "generator": ("quiz:llm"
                      if llm is not None and getattr(llm, "enabled", False)
                      else "quiz:bank"),
        "quiz": {
            "enabled": True,
            "rounds": n,
            "countdown_s": cd_plain,
            "cards": True,
            "items": rounds,
        },
        "comment": ("Score check: how many did you get right out of %d? "
                    "Comment your score and which question tricked you." % n),
        "description": ("Quick %s quiz: %d questions, %d seconds each. "
                        "Comment your score out of %d! "
                        "#shorts #quiz #trivia #quiztime #generalknowledge"
                        % (category, n, cd_plain, n)),
        "tags": ["quiz", "trivia", "quiz time", "quiz questions",
                 "general knowledge", "test your knowledge", category],
    }
    if ab:
        idea["pair_id"] = ab["pair_id"]
        idea["hook_variant"] = ab.get("variant")
        idea["hook_variants"] = variants
    return idea


def on_published(idea, video_id, cfg):
    """Apres upload : journalise la video et verrouille ses questions.

    Idempotent par video_id (True la premiere fois, False ensuite) pour que
    les deux chemins de publication du scheduler (run_pipeline_once et
    publish_pending_video) puissent l'appeler sans double compte.
    """
    vid = str(video_id or "").strip()
    if not vid:
        return False
    state = _load_state(cfg)
    for v in state.get("videos", []):
        if str((v or {}).get("video_id") or "") == vid:
            return False
    state["videos"] = list(state.get("videos", [])) + [{
        "video_id": vid,
        "idea_id": (idea or {}).get("id"),
        "title": (idea or {}).get("title"),
        "ts": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }]
    items = ((idea or {}).get("quiz") or {}).get("items") or []
    used = list(state.get("used", []))
    for r in items:
        if isinstance(r, dict) and r.get("question"):
            used.append(_round_key(r))
    state["used"] = used[-_STATE_MAX_USED:]
    _save_state(cfg, state)
    return True
