# -*- coding: utf-8 -*-
"""Tests unitaires des gates du skill hook-generator-upgrade.

Couverture :
  * hooks.memory          : load / build / next_seq / n_with_engagement /
                            stats_by_family / used_hook_texts /
                            recent_families / consecutive_family ;
  * hooks.diversity       : tokenize / jaccard / is_dup, anti-serie de
                            famille, passes(), pick_alternative() ;
  * hooks.performance     : min_n, best_family (seuil performance_min_n) ;
  * hooks.exploration     : cycle deterministe explore_every, choose_variant ;
  * hooks.run             : orchestrateur non fatal (False en mode degrade) ;
  * prepublication_check  : gate d'activation (off / always / auto_gated).

Aucun reseau : tout tourne sur des documents en memoire + tmp_path.
Toutes les fonctions recoivent un cfg explicite (jamais get_config()).
"""
import datetime as dt

import pytest

from autopipe import hooks
from autopipe.hooks import diversity, exploration, memory, performance
from autopipe.hooks import prepublication_check
from autopipe.util import save_json


def _ts(seconds_ago=0):
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)
    return base.isoformat(timespec="seconds")


def _entry(vid, hook=None, family=None, published_at=None, views=None,
           engagement=None, engaged_source=None):
    return {
        "video_id": vid,
        "published_at": published_at,
        "hook": hook,
        "family": family,
        "views": views,
        "engagement": engagement,
        "engaged_source": engaged_source,
    }


def _doc(*entries):
    return {"publisheds": {e["video_id"]: dict(e) for e in entries}}


HOOK_A = "Why do bees never sleep at night?"
HOOK_B = "How much gold is hidden in the ocean?"
VARIANTS = [
    {"variant": "A", "hook": HOOK_A, "family": "question"},
    {"variant": "B", "hook": HOOK_B, "family": "question"},
]


# ---------------------------------------------------------------------------
# hooks.memory
# ---------------------------------------------------------------------------

def test_memory_load_missing_file(make_cfg):
    cfg = make_cfg()
    doc = memory.load(cfg)
    assert doc["publisheds"] == {}


def test_memory_build_empty_paths(make_cfg):
    cfg = make_cfg()
    doc = memory.build(cfg)
    assert doc["publisheds"] == {}
    assert doc["last_checked"] is None
    assert doc["updated"]


def test_next_seq_counts_entries(make_cfg):
    cfg = make_cfg()
    assert memory.next_seq(cfg, _doc()) == 1
    doc = _doc(_entry("v1", published_at=_ts(60)),
               _entry("v2", published_at=_ts(30)))
    assert memory.next_seq(cfg, doc) == 3


def test_n_with_engagement_counts_only_real_engaged(make_cfg):
    cfg = make_cfg()
    doc = _doc(
        _entry("a", views=100, engaged_source="engagedViews"),
        _entry("b", views=0, engaged_source="engagedViews"),
        _entry("c", views=500, engaged_source="estimated"),
        _entry("d", views=None, engaged_source=None),
    )
    assert memory.n_with_engagement(cfg, doc) == 1


def test_stats_by_family_skips_unmeasured(make_cfg):
    cfg = make_cfg()
    doc = _doc(
        _entry("q1", family="question", views=100, engagement=4.0),
        _entry("q2", family="question", views=200, engagement=6.2),
        _entry("n1", family="number", engagement=None),
        _entry("x1", views=50, engagement=9.0),  # sans famille -> ignore
    )
    stats = memory.stats_by_family(cfg, doc)
    assert set(stats) == {"question"}
    s = stats["question"]
    assert s["n"] == 2
    assert s["engagement_mean"] == pytest.approx(5.1, abs=0.05)
    assert s["views_mean"] == 150


def test_consecutive_family_counts_trailing_streak(make_cfg):
    cfg = make_cfg()
    doc = _doc(
        _entry("v1", family="number", published_at=_ts(300)),
        _entry("v2", family="question", published_at=_ts(200)),
        _entry("v3", family="question", published_at=_ts(100)),
    )
    assert memory.consecutive_family(doc, "question") == 2
    assert memory.consecutive_family(doc) == 2  # famille de la derniere
    assert memory.consecutive_family(doc, "number") == 0
    doc2 = _doc(
        _entry("v1", family="number", published_at=_ts(300)),
        _entry("v2", family="question", published_at=_ts(200)),
        _entry("v3", family="question", published_at=_ts(100)),
        _entry("v4", family="number", published_at=_ts(50)),
    )
    assert memory.consecutive_family(doc2, "number") == 1


def test_used_hook_texts_window(make_cfg):
    cfg = make_cfg()
    old = _entry("old", hook="Honey never spoils - here is why",
                 family="question", published_at=_ts(10 * 24 * 3600))
    new = _entry("new", hook="How much gold is in the ocean?",
                 family="number", published_at=_ts(60))
    doc = _doc(old, new)
    assert memory.used_hook_texts(cfg, doc, days=7) == \
        ["how much gold is in the ocean?"]
    assert memory.used_hook_texts(cfg, doc, days=30) == [
        "honey never spoils - here is why",
        "how much gold is in the ocean?",
    ]
    assert memory.recent_families(cfg, doc, days=7) == ["number"]
    assert memory.recent_families(cfg, doc, days=30) == ["question", "number"]


def test_used_hook_texts_includes_unknown_age(make_cfg):
    cfg = make_cfg()
    doc = _doc(_entry("legacy", hook="Why do bees never sleep?"))
    assert memory.used_hook_texts(cfg, doc, days=7) == \
        ["why do bees never sleep?"]
    assert memory.recent_families(cfg, doc, days=7) == []


# ---------------------------------------------------------------------------
# hooks.diversity
# ---------------------------------------------------------------------------

def test_jaccard_and_is_dup():
    a = "why do bees never sleep"
    assert diversity.jaccard(a, a) == 1.0
    assert diversity.jaccard(a, "WHY  do BEES never SLEEP") == 1.0
    assert diversity.jaccard(a, "how much gold is in the ocean") == 0.0
    assert diversity.is_dup(a, ["why do bees never sleep"], 0.7) is True
    assert diversity.is_dup(a, ["how much gold is in the ocean"],
                            0.7) is False
    assert diversity.is_dup("", ["why do bees never sleep"]) is False


def test_family_would_break_streak():
    doc = _doc(
        _entry("v1", family="question", published_at=_ts(300)),
        _entry("v2", family="question", published_at=_ts(200)),
        _entry("v3", family="question", published_at=_ts(100)),
    )
    assert diversity.family_would_break_streak(doc, "question", 3) is True
    assert diversity.family_would_break_streak(doc, "number", 3) is False
    assert diversity.family_would_break_streak(doc, "question", 4) is False
    assert diversity.family_would_break_streak(doc, "question", None) is False
    assert diversity.family_would_break_streak(doc, "question", 0) is False


def test_passes_rejects_recent_duplicate(make_cfg):
    cfg = make_cfg(hooks={"recent_days": 7, "dup_jaccard": 0.7})
    doc = _doc(_entry("v1", hook=HOOK_A, family="question",
                      published_at=_ts(60)))
    ok, reason = diversity.passes("amazing facts", HOOK_A, cfg=cfg, doc=doc)
    assert ok is False
    assert "doublon" in reason
    ok2, reason2 = diversity.passes("amazing facts", HOOK_B, cfg=cfg, doc=doc)
    assert (ok2, reason2) == (True, "")


def test_passes_rejects_family_streak(make_cfg):
    cfg = make_cfg(hooks={"max_family_consecutive": 3})
    fam = diversity.hook_family_safe(HOOK_A)
    assert fam  # famille du hook classee
    doc = _doc(*[_entry("v%d" % i, family=fam, published_at=_ts(i * 60))
                 for i in range(3)])
    ok, reason = diversity.passes("amazing facts", HOOK_A, cfg=cfg, doc=doc)
    assert ok is False
    assert "serie" in reason
    # 2 consecutives seulement : le 3e de la meme famille passe encore.
    doc2 = _doc(*[_entry("v%d" % i, family=fam, published_at=_ts(i * 60))
                  for i in range(2)])
    ok2, reason2 = diversity.passes("amazing facts", HOOK_A, cfg=cfg,
                                    doc=doc2)
    assert (ok2, reason2) == (True, "")


def test_passes_accepts_empty_history(make_cfg):
    cfg = make_cfg()
    ok, reason = diversity.passes("amazing facts", HOOK_A, cfg=cfg,
                                  doc=_doc())
    assert (ok, reason) == (True, "")


def test_pick_alternative_selects_variant_b(make_cfg):
    cfg = make_cfg(hooks={"recent_days": 7, "dup_jaccard": 0.7})
    doc = _doc(_entry("v1", hook=HOOK_A, family="question",
                      published_at=_ts(60)))
    got = diversity.pick_alternative("amazing facts", VARIANTS, cfg=cfg,
                                     doc=doc)
    assert got[0] == HOOK_B
    assert got[2] == "B"
    assert got[3]  # raison du refus de la variante A


def test_pick_alternative_keeps_proposed_when_all_fail(make_cfg):
    cfg = make_cfg(hooks={"recent_days": 7, "dup_jaccard": 0.7})
    doc = _doc(
        _entry("v1", hook=HOOK_A, family="question", published_at=_ts(60)),
        _entry("v2", hook=HOOK_B, family="question", published_at=_ts(30)),
    )
    got = diversity.pick_alternative("amazing facts", VARIANTS, cfg=cfg,
                                     doc=doc)
    assert got[0] == HOOK_A
    assert got[2] == "A"
    assert got[3]  # hook propose garde + raison expliquee


def test_pick_alternative_template_fallback(make_cfg):
    cfg = make_cfg(
        quiz={"hooks_a": ["Have you ever seen a honey spoon race?"],
              "hooks_b": ["I am the person who talks to bees"]},
        hooks={"recent_days": 7, "dup_jaccard": 0.7},
    )
    doc = _doc(
        _entry("v1", hook=HOOK_A, family="question", published_at=_ts(60)),
        _entry("v2", hook=HOOK_B, family="question", published_at=_ts(30)),
    )
    got = diversity.pick_alternative("amazing facts", VARIANTS, cfg=cfg,
                                     doc=doc)
    assert got[0] == "Have you ever seen a honey spoon race?"
    assert got[1] == "question"
    assert got[2] is None
    assert got[3]


def test_pick_alternative_no_variants(make_cfg):
    cfg = make_cfg()
    assert diversity.pick_alternative("amazing facts", [], cfg=cfg,
                                      doc=_doc()) is None
    assert diversity.pick_alternative("amazing facts", None, cfg=cfg,
                                      doc=_doc()) is None


# ---------------------------------------------------------------------------
# hooks.performance
# ---------------------------------------------------------------------------

def test_min_n_default_and_override(make_cfg):
    assert performance.min_n(make_cfg()) == 3
    assert performance.min_n(make_cfg(hooks={"performance_min_n": 5})) == 5


def test_best_family_empty_history(make_cfg):
    cfg = make_cfg()
    fam, stats = performance.best_family(cfg, _doc())
    assert fam is None
    assert stats == {}


def test_best_family_requires_min_n(make_cfg):
    cfg = make_cfg()
    doc = _doc(
        _entry("q1", family="question", engagement=9.0, views=100),
        _entry("q2", family="question", engagement=9.0, views=100),
        _entry("s1", family="statement", engagement=5.0, views=100),
        _entry("s2", family="statement", engagement=5.0, views=100),
        _entry("s3", family="statement", engagement=5.0, views=100),
    )
    fam, stats = performance.best_family(cfg, doc)
    assert fam == "statement"  # question : 2 < 3 mesures requises
    assert set(stats) == {"question", "statement"}
    fam2, _ = performance.best_family(
        make_cfg(hooks={"performance_min_n": 2}), doc)
    assert fam2 == "question"  # engagement moyen le plus eleve


# ---------------------------------------------------------------------------
# hooks.exploration
# ---------------------------------------------------------------------------

def test_is_exploration_turn_cycle(make_cfg):
    cfg = make_cfg(hooks={"explore_every": 3, "exploration_rate": 0.0})
    doc = _doc()
    assert exploration.is_exploration_turn(cfg, doc, 1) is True
    assert exploration.is_exploration_turn(cfg, doc, 2) is False
    assert exploration.is_exploration_turn(cfg, doc, 3) is False
    assert exploration.is_exploration_turn(cfg, doc, 4) is True


def test_choose_variant_follows_mode(make_cfg):
    cfg = make_cfg()
    empty = _doc()
    # Exploitation sans famille gagnante -> variante A.
    assert exploration.choose_variant(
        "amazing facts", VARIANTS, cfg=cfg, doc=empty,
        exploring=False) == (HOOK_A, "question", "A", False)
    # Exploration sans famille gagnante -> variante B (la plus loin).
    assert exploration.choose_variant(
        "amazing facts", VARIANTS, cfg=cfg, doc=empty,
        exploring=True) == (HOOK_B, "question", "B", True)
    # Famille gagnante connue : number (variantes A=question / B=number).
    variants_qn = [
        {"variant": "A", "hook": HOOK_A, "family": "question"},
        {"variant": "B", "hook": HOOK_B, "family": "number"},
    ]
    doc_num = _doc(*[_entry("n%d" % i, family="number", engagement=5.0)
                     for i in range(3)])
    # Exploitation : garde la famille gagnante -> B.
    assert exploration.choose_variant(
        "amazing facts", variants_qn, cfg=cfg, doc=doc_num,
        exploring=False) == (HOOK_B, "number", "B", False)
    # Exploration : s'eloigne de la gagnante -> revient a la question.
    assert exploration.choose_variant(
        "amazing facts", variants_qn, cfg=cfg, doc=doc_num,
        exploring=True) == (HOOK_A, "question", "A", True)
    assert exploration.choose_variant(
        "amazing facts", [], cfg=cfg, doc=empty, exploring=True) is None


# ---------------------------------------------------------------------------
# hooks.run + prepublication_check
# ---------------------------------------------------------------------------

def test_hooks_run_degraded_mode(make_cfg):
    cfg = make_cfg()
    assert hooks.run(cfg) is False
    assert hooks.run(cfg) is False  # idempotent, jamais fatal


def test_hooks_run_disabled(make_cfg):
    cfg = make_cfg(hooks={"enabled": False})
    assert hooks.run(cfg) is False


def test_is_active_modes(make_cfg):
    assert prepublication_check.is_active(make_cfg(hooks={"mode": "off"})) is False
    assert prepublication_check.is_active(make_cfg(hooks={"mode": "always"})) is True
    plain = make_cfg()
    assert prepublication_check.is_active(plain) is False
    save_json(prepublication_check.state_path(plain),
              {"activated_at": _ts(0), "n_engaged_rows_first": 1})
    assert prepublication_check.is_active(plain) is True
    disabled = make_cfg(hooks={"enabled": False})
    save_json(prepublication_check.state_path(disabled),
              {"activated_at": _ts(0)})
    assert prepublication_check.is_active(disabled) is False


def test_status_lines_never_raise(make_cfg):
    cfg = make_cfg()
    for line_fn in (performance.line, exploration.line, diversity.line):
        out = line_fn(cfg)
        assert isinstance(out, str)
        assert out.strip()
