"""Tests hors-ligne du pivot quiz : rounds, script, idea, publication, cartes ASS.

Aucun reseau : LLM=None partout (banque de secours), cfg isolee dans tmp_path.
"""
import json
import re

from autopipe import quiz
from autopipe import subs
from autopipe import video
from autopipe.config import get_paths

CATS = ["general", "geography", "science", "history", "movies"]


def _make(make_cfg, **over):
    over.setdefault("quiz", {"enabled": True, "rounds": 3, "countdown_s": 5,
                             "cards": True, "categories": list(CATS)})
    return make_cfg(**over)


def _bank_rounds(count):
    cats = list(CATS)
    return [quiz._normalize_round(r, cats)
            for r in quiz.FALLBACK_ROUNDS[:count]]


def _ts_seconds(ts):
    h, m, rest = ts.split(":")
    s, cs = rest.split(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100.0


def _dialogues(ass_path):
    """[(start_s, end_s, style)] des lignes Dialogue: d'un fichier ASS."""
    out = []
    for line in ass_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("Dialogue: "):
            continue
        parts = line[len("Dialogue: "):].split(",")
        out.append((_ts_seconds(parts[1]), _ts_seconds(parts[2]), parts[3]))
    return out


# ---------------------------------------------------------------- next_rounds

def test_next_rounds_banque_et_normalisation(tmp_path, make_cfg):
    cfg = _make(make_cfg)
    rounds = quiz.next_rounds(None, cfg)
    assert len(rounds) == 3
    keys = set()
    for r in rounds:
        assert r["question"].strip()
        assert 3 <= len(r["options"]) <= 4
        assert 0 <= r["answer_index"] < len(r["options"])
        assert r["category"] in CATS
        assert isinstance(r["explanation"], str)
        keys.add(quiz._round_key(r))
    assert len(keys) == 3
    state = quiz._load_state(cfg)
    assert state["seq"] == 1
    assert len(state["generated"]) == 3
    assert (get_paths(cfg).data / "quiz" / "state.json").exists()


def test_next_rounds_dedup_generated_et_used(tmp_path, make_cfg):
    cfg = _make(make_cfg)
    first = quiz.next_rounds(None, cfg, count=5)
    first_keys = {quiz._round_key(r) for r in first}
    assert len(first_keys) == 5
    # 'generated' : un second appel ne repropose jamais les memes questions.
    second = quiz.next_rounds(None, cfg, count=6)
    assert len(second) == 6
    assert {quiz._round_key(r) for r in second}.isdisjoint(first_keys)
    # 'used' : les questions publiees sont exclues meme apres remise a zero
    # de 'generated' (dedup longue duree, pas seulement entre builds).
    quiz._save_state(cfg, {"seq": 0, "used": sorted(first_keys),
                           "generated": [], "videos": []})
    third = quiz.next_rounds(None, cfg, count=5)
    assert len(third) == 5
    assert {quiz._round_key(r) for r in third}.isdisjoint(first_keys)


def test_next_rounds_count_borne(tmp_path, make_cfg):
    cfg = _make(make_cfg)
    assert len(quiz.next_rounds(None, cfg, count=1)) == 1
    assert len(quiz.next_rounds(None, cfg, count=99)) == 8  # cap dur 8
    assert len(quiz.next_rounds(None, cfg, count=0)) == 3   # fallback cfg


# -------------------------------------------------------------- local_script

def test_local_script_structure(tmp_path, make_cfg):
    cfg = _make(make_cfg)
    rounds = [r for r in _bank_rounds(2) if r]
    hook = "How many of these can you get right?"
    script = quiz.local_script(rounds, hook, cfg)
    lines = script.splitlines()
    assert lines[0] == hook
    assert "Question 1:" in script and "Question 2:" in script
    assert "Lock in your answer, your 5 seconds start now:" in script
    assert "Five. Four. Three. Two. One." in script
    r0 = rounds[0]
    letter = "ABCD"[r0["answer_index"]]
    assert "The answer is %s: %s." % (letter, r0["options"][r0["answer_index"]]) \
        in script
    assert "That was all 2 questions." in script
    assert "Comment your score out of 2" in script
    assert "subscribe" in script


def test_local_script_countdown_clamp(tmp_path, make_cfg):
    cfg = _make(make_cfg, quiz={"enabled": True, "countdown_s": 99})
    rounds = [r for r in _bank_rounds(1) if r]
    script = quiz.local_script(rounds, "Hook here.", cfg)
    assert "your 10 seconds start now" in script  # clamp 1..10
    assert "Ten. Nine." in script


# ---------------------------------------------------------------- build_idea

def test_build_idea_offline(tmp_path, make_cfg):
    cfg = _make(make_cfg)
    idea = quiz.build_idea(None, cfg)
    q = idea["quiz"]
    assert q["enabled"] is True
    assert q["rounds"] == 3 == len(q["items"])
    assert q["countdown_s"] == 5
    assert q["cards"] is True
    for it in q["items"]:
        assert set(it) >= {"question", "options", "answer_index",
                           "explanation", "category"}
    assert idea["title"] and "quiz" in idea["title"].lower()
    assert idea["hook"] and len(idea["hook"]) <= 60
    assert idea["script"].startswith(idea["hook"])
    assert "Question 3:" in idea["script"]
    assert idea["generator"] == "quiz:bank"
    assert idea["source_topic"].startswith("quiz:")
    assert idea["duration_target"] > 0
    assert idea["duration_format"] in ("short", "long")
    # A/B quiz actif : paire rattachee, variante tracee.
    assert idea.get("pair_id")
    assert idea.get("hook_variant") in ("A", "B")


def test_build_idea_rounds_dedup_entre_ideas(tmp_path, make_cfg):
    cfg = _make(make_cfg)
    a = quiz.build_idea(None, cfg)
    b = quiz.build_idea(None, cfg)
    ka = {quiz._round_key(r) for r in a["quiz"]["items"]}
    kb = {quiz._round_key(r) for r in b["quiz"]["items"]}
    assert ka.isdisjoint(kb)


# -------------------------------------------------------------- on_published

def test_on_published_idempotent(tmp_path, make_cfg):
    cfg = _make(make_cfg)
    idea = quiz.build_idea(None, cfg)
    assert quiz.on_published(idea, "vid123", cfg) is True
    assert quiz.on_published(idea, "vid123", cfg) is False
    assert quiz.on_published(idea, "", cfg) is False
    assert quiz.on_published(idea, None, cfg) is False
    assert quiz.on_published(idea, "vid456", cfg) is True
    state = json.loads(
        (get_paths(cfg).data / "quiz" / "state.json").read_text(
            encoding="utf-8"))
    assert len(state["videos"]) == 2
    assert state["videos"][0]["video_id"] == "vid123"
    assert state["videos"][0]["idea_id"] == idea.get("id")
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                    state["videos"][0]["ts"])
    used = set(state["used"])
    assert used == {quiz._round_key(r) for r in idea["quiz"]["items"]}


def test_on_published_sans_quiz_rien_ne_locke(tmp_path, make_cfg):
    cfg = _make(make_cfg)
    idea = {"id": "x1", "title": "T", "quiz": {"enabled": True, "items": []}}
    assert quiz.on_published(idea, "vid1", cfg) is True
    state = quiz._load_state(cfg)
    assert len(state["videos"]) == 1 and state["used"] == []


# ------------------------------------------------------ write_quiz_card_ass

def test_write_quiz_card_ass_question(tmp_path):
    out = tmp_path / "q.ass"
    p = subs.write_quiz_card_ass(
        ["Question 1", "Which planet has the shortest day?",
         "A) Mercury", "B) Venus", "C) Jupiter", "D) Mars"],
        out, at_seconds=2.0, duration=4.0, kind="question")
    assert p == out and out.exists()
    content = out.read_text(encoding="utf-8")
    assert "[Events]" in content and "Dialogue: 0," in content
    assert "0:00:02.00" in content and "0:00:06.00" in content
    dial = _dialogues(out)[0]
    assert dial[2] == "Quiz"
    assert dial[0] == 2.0 and abs(dial[1] - 6.0) < 0.011
    # ligne repliee par wrap_chars (24) : fragments, pas la phrase entiere
    assert "Which planet has the" in content and "shortest day?" in content


def test_write_quiz_card_ass_answer(tmp_path):
    out = tmp_path / "a.ass"
    p = subs.write_quiz_card_ass(["Answer: C", "Jupiter"], out,
                                 at_seconds=10.5, duration=3.0, kind="answer")
    assert p is not None
    dial = _dialogues(out)[0]
    assert dial[2] == "QuizAns"
    assert abs(dial[0] - 10.5) < 0.011


def test_write_quiz_card_ass_cas_invalides(tmp_path):
    out = tmp_path / "x.ass"
    assert subs.write_quiz_card_ass("", out, at_seconds=2.0) is None
    assert subs.write_quiz_card_ass(None, out, at_seconds=2.0) is None
    assert subs.write_quiz_card_ass(["ok"], out, at_seconds=0.2) is None
    assert subs.write_quiz_card_ass(["ok"], out, at_seconds=2.0,
                                    duration=0.0) is None
    assert subs.write_quiz_card_ass(["ok"], out, at_seconds=-1.0) is None
    assert not out.exists()


def test_write_quiz_card_ass_troncature(tmp_path):
    out = tmp_path / "t.ass"
    lines = ["ligne %d" % i for i in range(10)]
    assert subs.write_quiz_card_ass(lines, out, at_seconds=1.0, duration=2.0,
                                    max_lines=3) is not None
    content = out.read_text(encoding="utf-8")
    assert content.count("\\N") == 2  # 3 lignes rendues max


# ---------------------------------------------------- _quiz_card_overlays

def _synthetic_timings(path, n_words=150, audio_dur=60.0):
    words = [{"word": "w%03d" % i, "start": round(i * audio_dur / n_words, 3)}
             for i in range(n_words)]
    path.write_text(json.dumps({"words": words}), encoding="utf-8")
    return str(path)


def test_quiz_card_overlays_fenetres(tmp_path, make_cfg):
    cfg = _make(make_cfg)
    idea = quiz.build_idea(None, cfg)
    audio_dur = 60.0
    timings = _synthetic_timings(tmp_path / "timings.json")
    out_dir = tmp_path / "cards"
    out_dir.mkdir()
    cards = video._quiz_card_overlays(idea, audio_dur, timings, out_dir, cfg)
    assert len(cards) >= 4
    by_kind = {}
    for p in cards:
        m = re.match(r"quiz_([qa])_(\d+)\.ass$", p.name)
        assert m, p.name
        by_kind.setdefault((m.group(1), int(m.group(2))), p)
    q_idx = sorted(i for (k, i) in by_kind if k == "q")
    a_idx = sorted(i for (k, i) in by_kind if k == "a")
    assert len(q_idx) == 3
    assert a_idx, "au moins une carte reponse attendue"
    prev_end = -1.0
    for i in q_idx:
        qs, qe, style = _dialogues(by_kind[("q", i)])[0]
        assert style == "Quiz"
        assert qs >= 0.5
        assert qe > qs
        assert qs > prev_end  # fenetres question dans l'ordre
        prev_end = qe
        if i in a_idx:
            a_s, a_e, a_style = _dialogues(by_kind[("a", i)])[0]
            assert a_style == "QuizAns"
            # la reponse n'apparait qu'apres la fin de la carte question
            assert a_s >= qe - 1e-6
            assert a_e > a_s  # fenetre positive
            assert a_e <= audio_dur - 0.4 + 1e-6


def test_quiz_card_overlays_desactive_et_sans_items(tmp_path, make_cfg):
    cfg = _make(make_cfg)
    idea = quiz.build_idea(None, cfg)
    timings = _synthetic_timings(tmp_path / "timings.json")
    out_dir = tmp_path / "cards"
    out_dir.mkdir()
    cfg_off = _make(make_cfg, quiz={"enabled": True, "cards": False})
    assert video._quiz_card_overlays(idea, 60.0, timings, out_dir,
                                     cfg_off) == []
    assert video._quiz_card_overlays({"script": "x"}, 60.0, timings,
                                     out_dir, cfg) == []


def test_hook_variants_quiz(tmp_path, make_cfg):
    cfg = _make(make_cfg)
    variants = quiz.hook_variants("geography", cfg)
    assert [v["variant"] for v in variants] == ["A", "B"]
    assert [v["family"] for v in variants] == ["question", "challenge"]
    for v in variants:
        assert v["hook"] and len(v["hook"]) <= 60
