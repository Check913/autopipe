"""Tests du gate de niche soft (V2) + plan de duree dual-format."""
from autopipe.identity import (duration_plan, filter_topics, hook_family,
                               hook_variants)


TOPICS = [
    {"topic": "amazing facts about the ocean", "score": 5.0},
    {"topic": "celebrity gossip update", "score": 4.0},
    {"topic": "huge giveaway announcement", "score": 9.0},
]


def test_filter_topics_soft_garde_le_hors_niche(make_cfg):
    cfg = make_cfg()
    kept = filter_topics(TOPICS, cfg)
    subjects = [t["topic"] for t in kept]
    # L'anti-topic est rejete, mais le hors-niche simple est GARDEN.
    assert "huge giveaway announcement" not in subjects
    assert "celebrity gossip update" in subjects
    # Le on-niche passe en tete ; le hors-niche est penalise de 1.5.
    assert kept[0]["topic"] == "amazing facts about the ocean"
    off = kept[1]
    assert off["rank_score"] == 4.0 - 1.5
    assert off["identity_score"] == 0


def test_filter_topics_strict_rejette_le_hors_niche(make_cfg):
    cfg = make_cfg(identity={"keywords": ["amazing facts", "true story"],
                             "anti_topics": ["giveaway"], "strict": True,
                             "off_niche_penalty": 1.5})
    kept = filter_topics(TOPICS, cfg)
    assert [t["topic"] for t in kept] == ["amazing facts about the ocean"]


def test_duration_plan_jamais_de_milieu(make_cfg):
    cfg = make_cfg()
    assert duration_plan(1, cfg) == {"format": "short", "target": 13,
                                     "min": 12, "max": 18}
    assert duration_plan(2, cfg)["format"] == "short"
    assert duration_plan(3, cfg) == {"format": "long", "target": 60,
                                     "min": 50, "max": 60}
    assert duration_plan(5, cfg)["format"] == "long"
    # Densite invalide -> defaut securise : short.
    assert duration_plan(None, cfg)["format"] == "short"
    assert duration_plan("abc", cfg)["format"] == "short"


def test_hook_family_classification():
    assert hook_family("Why is the ocean salty?") == "question"
    assert hook_family("3 things about the ocean") == "number"
    assert hook_family("Nobody warned you about this") == "negation"
    assert hook_family("The ocean is hiding this") == "statement"
    assert hook_family("") == "statement"


def test_hook_variants_paire_ab(make_cfg):
    cfg = make_cfg()
    pair = hook_variants("the mariana trench",
                         "This trench hides something", cfg)
    assert [v["variant"] for v in pair] == ["A", "B"]
    assert all(v["hook"] for v in pair)
    assert all(v["family"] for v in pair)