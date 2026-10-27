"""Tests V3 (2026-10-27) : alignement mots/duree, specificite des hooks,
taxonomie commune hook_type, checkpoint en % de la duree."""
import sys

sys.path.insert(0, ".")

from autopipe import identity, optimizer  # noqa: E402


def test_script_word_budget_short_calibre_13s():
    """13 s a 145-165 wpm -> 31-36 mots (avant V3 : 50-85 = 19-32 s)."""
    lo, hi = identity.script_word_budget("short")
    assert lo <= hi
    assert 25 <= lo <= 40
    assert 30 <= hi <= 45


def test_script_word_budget_long_calibre_60s():
    lo, hi = identity.script_word_budget("long")
    assert lo <= hi
    assert 135 <= lo <= 155
    assert 145 <= hi <= 175


def test_script_word_budget_respecte_profil_cfg(make_cfg):
    cfg = make_cfg()
    cfg.setdefault("ideas", {})["duration_profiles"] = {
        "short": {"target": 13, "min": 12, "max": 18},
        "long": {"target": 55, "min": 45, "max": 60},
    }
    lo55, hi55 = identity.script_word_budget("long", cfg=cfg)
    lo60, hi60 = identity.script_word_budget("long")
    # Une cible 55 s doit donner un budget plus petit qu'une cible 60 s.
    assert hi55 < hi60


def test_default_hook_specifique_pas_vague():
    """Le hook par defaut doit nommer un chiffre/une erreur, jamais un
    hook vague style 'you won't believe' (x4 retention perdue)."""
    VAGUE = ("you won't believe", "you wont believe", "this will shock you",
             "wait for it", "insane")
    for topic in ("deep sea creatures", "the moon landing", "honey"):
        hook = identity._default_hook(topic)
        low = hook.lower()
        assert hook and len(hook) <= 60
        assert not any(v in low for v in VAGUE)


def test_hook_variants_familles_diverses():
    """Les variantes de hook ne doivent pas toutes etre de la meme famille."""
    variants = identity.hook_variants("deep sea creatures", "")
    assert len(variants) >= 2
    fams = {v.get("family") for v in variants}
    assert len(fams) >= 2


def test_optimizer_hook_type_taxonomie_commune(make_cfg):
    """optimizer._hook_family doit classer la negation comme identity
    (avant V3 : regexes locales -> 'statement')."""
    assert optimizer._hook_family("Nobody warned you about the trench") \
        == identity.hook_family("Nobody warned you about the trench") \
        == "negation"
    assert optimizer._hook_family("3 facts about honey") == "number"
    assert optimizer._hook_family("Why do octopuses escape?") == "question"


def test_features_from_remake_utilise_hook_family(tmp_path, make_cfg):
    """_features_from_remake doit exposer hook_type selon la taxonomie
    identity (famille negation distingquee)."""
    import json
    p = tmp_path / "idea.json"
    p.write_text(json.dumps({
        "id": "t1", "title": "3 Things About Honey Nobody Says",
        "hook": "Nobody warned you about honey",
        "script": "x", "score": 10, "duration_target": 13,
    }), encoding="utf-8")
    make_cfg()
    feats = optimizer._features_from_remake(p)
    assert feats["hook_type"] == "negation"