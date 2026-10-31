"""Tests MODULE 6 : garde anti-demoetisation (autopipe/compliance.py).

Couverture : Jaccard + normalisation, corpus publie (merge ideas/remake,
skip latest.json, tri mtime desc, cap), originalite croisee, fatigue de
format (incl. desactivation par 0), policy guard (patterns natifs +
banned_patterns custom + regex invalide ignoree), divulgation AI
(ai_disclosure_value), gate principal (block/warn/off/fail-open) et
journaux d'audit (defects.jsonl / rejected.jsonl).
"""
import json
import os
import time

import pytest

from autopipe import compliance as C


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def cfg(make_cfg):
    """Cfg compliance + upload.altered_content (declaratif correct)."""
    return make_cfg(
        upload={"altered_content": False, "auto_comment": True},
        compliance={
            "mode": "block",
            "originality_max": 0.60,
            "script_originality_max": 0.65,
            "fatigue_max_consecutive": 3,
            "corpus_max": 120,
            "corpus_min": 3,
            "banned_patterns": [],
        },
    )


def write_corpus_file(directory, name, data, mtime=None):
    """Ecrit un JSON d'idee et (optionnel) fixe son mtime pour l'ordre."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------------------
# 1) Jaccard + tokenisation
# ---------------------------------------------------------------------------
def test_jaccard_basics():
    assert C._jaccard(set(), set()) == 0.0
    assert C._jaccard(set(), {"a", "b"}) == 0.0
    assert C._jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert C._jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)


def test_token_set_min_len_and_punctuation():
    assert C._token_set("a ab ABC? def!") == {"ab", "abc", "def"}


def test_idea_script_normalization():
    """Script liste/segments -> texte brut (contrat TTS via as_script_text)."""
    idea = {"script": ["Line one", {"text": "Line two"}]}
    text = C._idea_script(idea)
    assert "Line one" in text
    assert "Line two" in text
    assert C._idea_script({}) == ""


# ---------------------------------------------------------------------------
# 2) Corpus publie
# ---------------------------------------------------------------------------
def test_load_published_corpus_merge_and_skip_latest(make_cfg):
    cfg = make_cfg(upload={"altered_content": False},
                   compliance={"corpus_max": 120})
    paths = C.get_paths(cfg)
    write_corpus_file(paths.ideas, "idea_a.json",
                      {"title": "A", "hook": "1 fact about crabs"})
    write_corpus_file(paths.remake, "idea_b.json", {"title": "B"})
    write_corpus_file(paths.ideas, "latest.json", {"skip": True})
    corpus, total = C.load_published_corpus(cfg)
    assert total == 2                     # latest.json exclu du total
    assert len(corpus) == 2
    assert {c["_src"] for c in corpus} == {"idea_a.json", "idea_b.json"}


def test_load_published_corpus_mtime_desc_and_cap(make_cfg):
    cfg = make_cfg(upload={"altered_content": False},
                   compliance={"corpus_max": 120})
    paths = C.get_paths(cfg)
    now = time.time()
    for i in range(5):                    # idea_4 = le plus recent
        write_corpus_file(paths.ideas, "idea_%d.json" % i,
                          {"title": "t%d" % i}, mtime=now - (10 - i) * 60)
    corpus, total = C.load_published_corpus(cfg, limit=2)
    assert total == 5
    assert [c["_src"] for c in corpus] == ["idea_4.json", "idea_3.json"]
    cfg2 = make_cfg(upload={"altered_content": False},
                    compliance={"corpus_max": 1})
    corpus2, _ = C.load_published_corpus(cfg2)
    assert [c["_src"] for c in corpus2] == ["idea_4.json"]


# ---------------------------------------------------------------------------
# 3) Originalite croisee
# ---------------------------------------------------------------------------
def test_originality_report_identical_twin(make_cfg):
    cfg = make_cfg(upload={"altered_content": False}, compliance={})
    text = {"title": "Why do octopuses have three hearts?",
            "hook": "Why do octopuses have three hearts?"}
    idea = dict(text, script="Same script twice.")
    corpus = [dict(text, script="Same script twice.", _src="twin.json")]
    rep = C.originality_report(idea, corpus, cfg)
    assert rep["max_jaccard"] == 1.0
    assert rep["max_script_jaccard"] == 1.0
    assert rep["worst_match_src"] == "twin.json"
    assert rep["corpus_size"] == 1


def test_originality_report_below_thresholds(make_cfg):
    cfg = make_cfg(upload={"altered_content": False}, compliance={})
    corpus = [{"title": "Zebras eat grass all day",
               "hook": "10 zebra facts", "_src": "z.json"}]
    idea = {"title": "Why do octopuses have three hearts?",
            "hook": "Did you know this about octopuses?",
            "script": "Octopuses have three hearts and blue blood."}
    rep = C.originality_report(idea, corpus, cfg)
    assert rep["max_jaccard"] < rep["threshold"]
    assert rep["max_script_jaccard"] < rep["threshold_script"]


# ---------------------------------------------------------------------------
# 4) Fatigue de format
# ---------------------------------------------------------------------------
def test_format_fatigue_report_three_consecutive(make_cfg):
    cfg = make_cfg(upload={"altered_content": False}, compliance={})
    corpus = [{"hook": "3 octopus facts"}, {"hook": "7 squid myths"},
              {"hook": "9 shark numbers"}, {"hook": "Why do eels exist?"}]
    rep = C.format_fatigue_report({"hook": "12 things about crabs"},
                                  corpus, cfg)
    assert rep["hook_family"] == "number"
    assert rep["consecutive_same_family"] == 3
    assert rep["fatigued"] is True


def test_format_fatigue_report_run_broken_by_other_family(make_cfg):
    cfg = make_cfg(upload={"altered_content": False}, compliance={})
    corpus = [{"hook": "3 octopus facts"}, {"hook": "Why do eels exist?"},
              {"hook": "7 squid myths"}]
    rep = C.format_fatigue_report({"hook": "12 things about crabs"},
                                  corpus, cfg)
    assert rep["consecutive_same_family"] == 1
    assert rep["fatigued"] is False


def test_format_fatigue_report_zero_disables(make_cfg):
    """Contrat config.py : fatigue_max_consecutive = 0 -> desactive."""
    cfg = make_cfg(upload={"altered_content": False},
                   compliance={"fatigue_max_consecutive": 0})
    corpus = [{"hook": "3 facts"}, {"hook": "7 myths"}, {"hook": "9 sharks"}]
    rep = C.format_fatigue_report({"hook": "12 crabs"}, corpus, cfg)
    assert rep["fatigued"] is False


# ---------------------------------------------------------------------------
# 5) Policy guard
# ---------------------------------------------------------------------------
def test_policy_report_labels_and_clean(make_cfg):
    cfg = make_cfg(upload={"altered_content": False}, compliance={})
    dirty = {"title": "Sub for sub compilation giveaway", "hook": "",
             "script": "Smash that like and subscribe"}
    violations = C.policy_report(dirty, cfg)
    labels = {v["pattern"] for v in violations}
    assert labels == {"sub4sub", "compilation", "giveaway_bait",
                      "engagement_bait"}
    assert ("engagement_bait", "script") in \
        {(v["pattern"], v["source_field"]) for v in violations}
    clean = {"title": "Why do octopuses have three hearts?",
             "hook": "Did you know?", "script": "Octopuses rule."}
    assert C.policy_report(clean, cfg) == []


def test_policy_report_custom_banned_patterns_and_bad_regex(make_cfg):
    cfg = make_cfg(upload={"altered_content": False},
                   compliance={"banned_patterns":
                               ["forbiddenword", "(unclosed"]})
    idea = {"title": "A forbiddenword story", "hook": ""}
    labels = [v["pattern"] for v in C.policy_report(idea, cfg)]
    assert labels == ["forbiddenword"]     # regex invalide -> ignoree


# ---------------------------------------------------------------------------
# 6) Divulgation AI
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    (None, (None, False)),
    (False, (False, True)),
    (True, (True, True)),
    ("true", (True, True)),
    ("0", (False, True)),
    ("garbage", (None, False)),
])
def test_ai_disclosure_value(make_cfg, raw, expected):
    cfg = make_cfg(upload={"altered_content": raw})
    assert C.ai_disclosure_value(cfg) == expected


def test_ai_disclosure_value_missing_section(make_cfg):
    cfg = make_cfg()
    cfg.pop("upload", None)
    assert C.ai_disclosure_value(cfg) == (None, False)


# ---------------------------------------------------------------------------
# 7) Gate principal
# ---------------------------------------------------------------------------
def test_enforce_compliance_block_rejects(make_cfg):
    cfg = make_cfg(upload={"altered_content": False},
                   compliance={"corpus_min": 3})
    paths = C.get_paths(cfg)
    now = time.time()
    # Ordre mtime desc : twin.json (identique) le plus recent, puis des
    # hooks famille "number" -> la serie de fatigue est coupee a 1.
    for i, name in enumerate(["twin.json", "old1.json", "old2.json"]):
        if name == "twin.json":
            data = {"title": "Why do octopuses have three hearts?",
                    "hook": "Why do octopuses have three hearts?"}
        else:
            data = {"title": "%d facts about sharks" % (i + 1),
                    "hook": "%d shark facts" % (i + 1)}
        write_corpus_file(paths.ideas, name, data, mtime=now - (10 - i) * 60)
    idea = {"id": "idea-1", "title": "Why do octopuses have three hearts?",
            "hook": "Why do octopuses have three hearts?"}
    ok, rep = C.enforce_compliance(idea, cfg)
    assert ok is False
    assert rep["ok"] is False
    assert idea["compliance"] is rep
    assert any(r.startswith("originality:") for r in rep["reasons"])
    rows = C.rejected_ideas_recent(cfg)
    assert len(rows) == 1
    assert rows[0]["idea_id"] == "idea-1"
    assert rows[0]["reasons"] == rep["reasons"]
    assert not (paths.compliance / "defects.jsonl").exists()


def test_enforce_compliance_fatigue_rejects(make_cfg):
    cfg = make_cfg(upload={"altered_content": False},
                   compliance={"corpus_min": 3})
    paths = C.get_paths(cfg)
    now = time.time()
    hooks = ["3 facts about crabs", "7 facts about squids",
             "9 facts about eels"]
    for i, hook in enumerate(hooks):
        write_corpus_file(paths.ideas, "run_%d.json" % i,
                          {"title": "Run %d" % i, "hook": hook},
                          mtime=now - (10 - i) * 60)
    write_corpus_file(paths.ideas, "older.json",
                      {"title": "Older question", "hook": "Why do eels exist?"},
                      mtime=now - 100 * 60)
    idea = {"id": "idea-2", "title": "Fresh number idea",
            "hook": "12 facts about sharks"}
    ok, rep = C.enforce_compliance(idea, cfg)
    assert ok is False
    assert rep["reasons"] and rep["reasons"][0].startswith("fatigue:")
    assert C.rejected_ideas_recent(cfg)[-1]["idea_id"] == "idea-2"


def test_enforce_compliance_warn_keeps_idea(make_cfg):
    cfg = make_cfg(upload={"altered_content": False},
                   compliance={"mode": "warn", "corpus_min": 0})
    paths = C.get_paths(cfg)
    for i in range(3):
        write_corpus_file(paths.ideas, "run_%d.json" % i,
                          {"title": "Run %d" % i,
                           "hook": "%d facts" % (i + 1)})
    idea = {"id": "idea-3", "title": "Fat idea", "hook": "13 facts about crabs"}
    ok, rep = C.enforce_compliance(idea, cfg)
    assert ok is True                       # sauvegardee
    assert rep["ok"] is False               # mais marquee
    assert not (paths.compliance / "rejected.jsonl").exists()


def test_enforce_compliance_off_skips_checks(make_cfg):
    cfg = make_cfg(upload={"altered_content": False},
                   compliance={"mode": "off"})
    idea = {"id": "idea-4", "title": "Sub for sub compilation giveaway"}
    ok, rep = C.enforce_compliance(idea, cfg)
    assert ok is True
    assert "originality" not in rep
    assert "policy" not in rep
    assert "fatigue" not in rep


def test_enforce_compliance_fail_open_on_internal_error(make_cfg, monkeypatch):
    cfg = make_cfg(upload={"altered_content": False})
    paths = C.get_paths(cfg)

    def boom(_cfg, limit=None):
        raise RuntimeError("corpus explosion")

    monkeypatch.setattr(C, "load_published_corpus", boom)
    idea = {"id": "idea-5", "title": "Clean idea", "hook": "Clean hook"}
    ok, rep = C.enforce_compliance(idea, cfg)
    assert ok is True                       # fail-open : le pipeline vit
    assert rep["status"] == "error"
    defects = paths.compliance / "defects.jsonl"
    rows = [json.loads(l) for l in
            defects.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert rows[-1]["type"] == "INSUFFICIENT_DATA"
    assert rows[-1]["detail"]["check"] == "enforce_compliance"


def test_enforce_compliance_disclosure_reason_without_upload(make_cfg):
    cfg = make_cfg(compliance={"mode": "warn"})
    cfg.pop("upload", None)
    idea = {"id": "idea-6", "title": "Clean", "hook": "Clean hook"}
    ok, rep = C.enforce_compliance(idea, cfg)
    assert ok is True
    assert rep["ai_disclosure"] == {"contains_synthetic_media": None,
                                    "configured": False}
    assert any(r.startswith("disclosure:") for r in rep["reasons"])


def test_enforce_compliance_corpus_min_logs_defect(make_cfg):
    cfg = make_cfg(upload={"altered_content": False},
                   compliance={"corpus_min": 5})
    paths = C.get_paths(cfg)
    for i in range(2):
        write_corpus_file(paths.ideas, "few_%d.json" % i,
                          {"title": "T%d" % i, "hook": "%d facts" % (i + 1)})
    idea = {"id": "idea-7", "title": "Clean idea", "hook": "Clean hook?"}
    ok, rep = C.enforce_compliance(idea, cfg)
    assert ok is True
    assert rep["originality"]["corpus_size"] == 2
    rows = [json.loads(l) for l in
            (paths.compliance / "defects.jsonl").read_text(
                encoding="utf-8").splitlines() if l.strip()]
    assert any(r["detail"].get("check") == "originality" for r in rows)


# ---------------------------------------------------------------------------
# 8) Journaux d'audit
# ---------------------------------------------------------------------------
def test_log_defect_row(make_cfg):
    cfg = make_cfg(upload={"altered_content": False})
    paths = C.get_paths(cfg)
    C.log_defect(cfg, "TEST_TYPE", {"k": "v"})
    row = json.loads((paths.compliance / "defects.jsonl").read_text(
        encoding="utf-8").strip())
    assert row["type"] == "TEST_TYPE"
    assert row["detail"] == {"k": "v"}
    assert row["ts"]


def test_rejected_ideas_recent_limit_ordering(make_cfg):
    cfg = make_cfg(upload={"altered_content": False})
    paths = C.get_paths(cfg)
    file = paths.compliance / "rejected.jsonl"
    with file.open("a", encoding="utf-8") as fh:
        for i in range(5):
            fh.write(json.dumps({"idea_id": "r%d" % i}) + "\n")
    rows = C.rejected_ideas_recent(cfg, limit=3)
    assert [r["idea_id"] for r in rows] == ["r2", "r3", "r4"]
