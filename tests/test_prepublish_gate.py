# -*- coding: utf-8 -*-
"""Tests du gate qualite pre-publication (autopipe.hooks.quality).

Couverture :
  * check_and_repair : repairs (emoji, prefixe compteur, titre court/long,
    description vide / sans hashtags / flood de hashtags, tags vides) et
    cas sains passant SANS aucune modification ("10,000 Facts" et
    "3 Facts" restent intacts) ;
  * violations : banned_title_words ([ideas]), anti_topics ([identity]),
    ban dur local (HARD_BAN) - evaluees sur les valeurs REPAREES ;
  * write_audit : fichier data/hooks/blocked/<ts>-<slug>.json, best-effort
    (jamais d'exception) ;
  * integration scheduler.publish_pending_video : publication BLOQUEE
    (pending purge + alerte + audit, aucun upload, aucune attente) et
    fail-open si le gate tombe en panne.

Aucun reseau. Toutes les fonctions recoivent un cfg explicite
(jamais get_config()).
"""
import datetime as dt
import re
from pathlib import Path

import autopipe.scheduler as sched
from autopipe.config import get_paths
from autopipe.hooks import quality
from autopipe.util import load_json, save_json

UTC = dt.timezone.utc
T14 = dt.datetime(2026, 10, 31, 14, 0, tzinfo=UTC)
T14_ISO = T14.isoformat()


def _clean():
    """Metadata saine de reference : ni repair, ni violation."""
    return ("3 Facts No One Tells You About Bees",
            "Bees never sleep and other facts #facts",
            ["facts"])


# ---------------------------------------------------------------------------
# check_and_repair : repairs
# ---------------------------------------------------------------------------

def test_clean_metadata_passes_unchanged(make_cfg):
    title, description, tags = _clean()
    out = quality.check_and_repair(title, description, tags, cfg=make_cfg())
    assert out[:3] == (title, description, tags)
    assert out[3] == {"repairs": [], "violations": []}


def test_number_led_titles_never_stripped(make_cfg):
    # "10,000 Facts" : la virgule de millier n'est PAS un prefixe compteur.
    title = "10,000 Facts No One Tells You"
    t, d, tg, rep = quality.check_and_repair(
        title, "Amazing facts about the ocean #facts", ["facts"],
        cfg=make_cfg())
    assert t == title
    assert not any("counter" in r for r in rep["repairs"])


def test_emoji_removed_from_title_and_description(make_cfg):
    t, d, tg, rep = quality.check_and_repair(
        "\U0001F680 Rockets launch themselves",
        "Look at this \U0001F30D #facts", ["facts"], cfg=make_cfg())
    assert t == "Rockets launch themselves"
    assert "\U0001F30D" not in d and d.startswith("Look at this")
    assert "title:emoji_removed" in rep["repairs"]
    assert "description:emoji_removed" in rep["repairs"]


def test_counter_prefixes_stripped(make_cfg):
    cases = [
        ("3. Bees never sleep at night", "Bees never sleep at night"),
        ("#5 Facts About Gold", "Facts About Gold"),
        ("Part 2: The hidden world", "The hidden world"),
    ]
    for raw, expected in cases:
        t, d, tg, rep = quality.check_and_repair(
            raw, "Bees never sleep and other facts #facts", ["facts"],
            cfg=make_cfg())
        assert t == expected, raw
        assert "title:counter_prefix_removed" in rep["repairs"], raw


def test_short_title_rebuilt_from_topic(make_cfg):
    t, d, tg, rep = quality.check_and_repair(
        "T", "Bees never sleep and other facts #facts", ["facts"],
        cfg=make_cfg())
    assert t == "3 T Facts No One Tells You"
    assert "title:rebuilt_short" in rep["repairs"]
    # Titre totalement vide -> template generique sans sujet.
    t2, _, _, rep2 = quality.check_and_repair(
        "", "Bees never sleep and other facts #facts", ["facts"],
        cfg=make_cfg())
    assert t2 == "3 Facts No One Tells You"
    assert "title:rebuilt_short" in rep2["repairs"]


def test_long_title_truncated_on_word(make_cfg):
    cfg = make_cfg(ideas={"title_max_chars": 20})
    t, d, tg, rep = quality.check_and_repair(
        "This rocket fact changes everything",
        "Amazing facts about rockets #facts", ["facts"], cfg=cfg)
    assert t == "This rocket fact"
    assert len(t) <= 20
    assert "title:truncated" in rep["repairs"]


def test_empty_description_filled_with_hashtags(make_cfg):
    title, _, tags = _clean()
    t, d, tg, rep = quality.check_and_repair(title, "", tags,
                                             cfg=make_cfg())
    assert d.startswith(title)
    assert d.rstrip().endswith("#shorts")
    assert d.count("#") == 5          # 1 niche + 3 large + #shorts final
    assert "description:empty_filled_from_title" in rep["repairs"]
    assert "description:hashtags_added" in rep["repairs"]


def test_hashtag_flood_rebuilt(make_cfg):
    flood = " ".join("#h%02d" % i for i in range(70))  # > seuil 60
    t, d, tg, rep = quality.check_and_repair(
        "3 Facts No One Tells You", flood + " Real facts", ["facts"],
        cfg=make_cfg())
    assert "description:hashtags_rebuilt" in rep["repairs"]
    assert d.split("\n\n")[0] == "Real facts"
    assert d.count("#") == 5
    assert "#h00" not in d


def test_empty_tags_rebuilt_under_budget(make_cfg):
    t, d, tg, rep = quality.check_and_repair(
        "3 Facts No One Tells You About Bees",
        "Bees never sleep and other facts #facts", [], cfg=make_cfg())
    assert tg and len(tg) >= 3
    assert "shorts" in [x.lower() for x in tg]
    assert "tags:rebuilt_empty" in rep["repairs"]


# ---------------------------------------------------------------------------
# check_and_repair : violations (evaluees sur les valeurs REPAREES)
# ---------------------------------------------------------------------------

def test_hard_ban_blocks(make_cfg):
    t, d, tg, rep = quality.check_and_repair(
        "3 Facts No One Tells You", "How to build a bomb at home #facts",
        ["facts"], cfg=make_cfg())
    assert rep["repairs"] == []
    assert rep["violations"] == ["hard_ban:description:bomb"]


def test_anti_topic_blocks_title_and_tags(make_cfg):
    cfg = make_cfg(identity={"anti_topics": ["giveaway"]})
    _, _, _, rep = quality.check_and_repair(
        "This giveaway secret changes everything",
        "Secrets nobody tells you #facts", ["facts"], cfg=cfg)
    assert rep["violations"] == ["anti_topic:giveaway"]
    # Detection aussi dans les tags seuls.
    _, _, _, rep2 = quality.check_and_repair(
        "3 Facts No One Tells You About Bees",
        "Bees never sleep and other facts #facts",
        ["giveaway tips"], cfg=cfg)
    assert rep2["violations"] == ["anti_topic:giveaway"]


def test_banned_title_words_from_ideas_block(make_cfg):
    cfg = make_cfg(ideas={"banned_title_words": ["free money"]})
    _, _, _, rep = quality.check_and_repair(
        "Free money ideas nobody tells you",
        "Bees never sleep and other facts #facts", ["facts"], cfg=cfg)
    assert rep["violations"] == ["title:banned_word:free money"]


def test_default_abstract_banned_word(make_cfg):
    _, _, _, rep = quality.check_and_repair(
        "The Untold Story of Bees",
        "Bees never sleep and other facts #facts", ["facts"],
        cfg=make_cfg())
    assert rep["violations"] == ["title:banned_word:untold"]


def test_violations_judged_on_repaired_values(make_cfg):
    # "secret" est banni mais la troncature le fait DISPARAITRE du titre
    # publie : aucune violation ne doit etre emise sur la valeur brute.
    cfg = make_cfg(ideas={"title_max_chars": 20,
                          "banned_title_words": ["secret"]})
    t, d, tg, rep = quality.check_and_repair(
        "Great ideas about secret",
        "Bees never sleep and other facts #facts", ["facts"], cfg=cfg)
    assert t == "Great ideas about"
    assert rep["violations"] == []
    assert "title:truncated" in rep["repairs"]


# ---------------------------------------------------------------------------
# write_audit
# ---------------------------------------------------------------------------

def test_write_audit_creates_blocked_file(make_cfg, tmp_path):
    path = quality.write_audit(
        "quality_gate",
        {"title": "Blocked Giveaway Video",
         "violations": ["anti_topic:giveaway"]},
        cfg=make_cfg())
    assert path is not None
    p = Path(path)
    assert p.exists()
    assert p.parent == tmp_path / "data" / "hooks" / "blocked"
    assert re.match(r"^\d{8}-\d{6}-blocked-giveaway-video\.json$", p.name)
    doc = load_json(p)
    assert doc["kind"] == "quality_gate"
    assert doc["title"] == "Blocked Giveaway Video"
    assert doc["violations"] == ["anti_topic:giveaway"]


def test_write_audit_is_best_effort():
    # cfg sans [general] -> get_paths echoue -> None, JAMAIS d'exception.
    assert quality.write_audit("quality_gate", {"title": "X"},
                               cfg={"hooks": {}}) is None


# ---------------------------------------------------------------------------
# Integration scheduler.publish_pending_video (branche fresh-upload)
# ---------------------------------------------------------------------------

def _pending_doc(title, description, tags):
    return {"publish_at": T14_ISO, "video": "out/v.mp4",
            "title": title, "description": description, "tags": tags}


def _stage(cfg, tmp_path, monkeypatch, pending, pend_file,
           utcnow=dt.datetime(2026, 10, 31, 14, 5, tzinfo=UTC)):
    """Banc d'essai scheduler : video existante, espions upload/flip/wait."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "out").mkdir(exist_ok=True)
    (tmp_path / "out" / "v.mp4").write_bytes(b"vid")
    if pend_file is not None:
        pend_file.parent.mkdir(parents=True, exist_ok=True)
        save_json(pend_file, pending)
    calls = {"upload": [], "flip": [], "wait": [], "alerts": []}
    monkeypatch.setattr(sched, "_utcnow", lambda: utcnow)
    monkeypatch.setattr(sched, "_wait_until",
                        lambda target: calls["wait"].append(target))
    monkeypatch.setattr(sched, "_flip_public_now",
                        lambda cfg_, vid: calls["flip"].append(vid))

    def fake_upload(*args, **kwargs):
        calls["upload"].append({"args": args, "kwargs": kwargs})
        return "vid_new"

    monkeypatch.setattr(sched, "upload_video", fake_upload)
    monkeypatch.setattr(
        sched, "_alert",
        lambda cfg_, subj, body: calls["alerts"].append(subj))
    monkeypatch.setattr(sched.analytics, "record", lambda *a, **k: None)
    import autopipe.registry as registry
    monkeypatch.setattr(registry, "register", lambda *a, **k: None)
    return cfg, calls


def test_scheduler_blocks_publication_and_clears_pending(
        make_cfg, tmp_path, monkeypatch):
    cfg = make_cfg(scheduler={"engage_first_hour": False},
                   identity={"anti_topics": ["giveaway"]})
    pend_file = tmp_path / "data" / "pending_publish.json"
    pending = _pending_doc("This giveaway secret changes everything",
                           "Secrets nobody tells you #facts", ["facts"])
    cfg, calls = _stage(
        cfg, tmp_path, monkeypatch, pending, pend_file,
        utcnow=dt.datetime(2026, 10, 31, 13, 40, tzinfo=UTC))

    res = sched.publish_pending_video(cfg, get_paths(cfg, root=tmp_path),
                                      pending, pending_file=pend_file)

    assert res["published"] is False
    assert res["reason"] == "quality_gate"
    assert res["violations"] == ["anti_topic:giveaway"]
    assert res["video"] == str(Path("out/v.mp4"))
    # Audit ecrit sous data/hooks/blocked/.
    assert res["audit"] is not None
    audit = Path(res["audit"])
    assert audit.exists()
    assert load_json(audit)["violations"] == ["anti_topic:giveaway"]
    # Pending purgé : rien ne sera re-publie tel quel.
    assert not pend_file.exists()
    # AUCUN upload, AUCUNE bascule, AUCUNE attente (relance immediate).
    assert calls["upload"] == []
    assert calls["flip"] == []
    assert calls["wait"] == []
    # Une seule alerte, avec le sujet du gate.
    assert len(calls["alerts"]) == 1
    assert "gate qualite" in calls["alerts"][0]


def test_scheduler_repairs_metadata_before_upload(make_cfg, tmp_path,
                                                  monkeypatch):
    cfg = make_cfg(scheduler={"engage_first_hour": False})
    pend_file = tmp_path / "data" / "pending_publish.json"
    pending = _pending_doc(
        "\U0001F680 This rocket fact changes everything",
        "Amazing facts about rockets #facts", ["facts"])
    cfg, calls = _stage(cfg, tmp_path, monkeypatch, pending, pend_file)

    res = sched.publish_pending_video(cfg, get_paths(cfg, root=tmp_path),
                                      pending, pending_file=pend_file)

    assert res == {"published": True, "youtube_id": "vid_new",
                   "video": str(Path("out/v.mp4"))}
    args = calls["upload"][0]["args"]
    assert args[1] == "This rocket fact changes everything"  # emoji parti
    assert args[2] == "Amazing facts about rockets #facts"
    assert args[3] == ["facts"]
    # Point de reprise : le pending REPARé (+ youtube_id) est persiste.
    doc = load_json(pend_file)
    assert doc["title"] == "This rocket fact changes everything"
    assert doc["youtube_id"] == "vid_new"


def test_scheduler_fail_open_when_gate_broken(make_cfg, tmp_path,
                                              monkeypatch):
    cfg = make_cfg(scheduler={"engage_first_hour": False})
    pend_file = tmp_path / "data" / "pending_publish.json"
    pending = _pending_doc(*_clean())
    cfg, calls = _stage(cfg, tmp_path, monkeypatch, pending, pend_file)

    import autopipe.hooks.quality as quality_mod

    def boom(*args, **kwargs):
        raise RuntimeError("gate en panne")

    monkeypatch.setattr(quality_mod, "check_and_repair", boom)

    res = sched.publish_pending_video(cfg, get_paths(cfg, root=tmp_path),
                                      pending, pending_file=pend_file)

    # Fail-open : la publication NORMALE continue malgre la panne du gate.
    assert res == {"published": True, "youtube_id": "vid_new",
                   "video": str(Path("out/v.mp4"))}
    assert calls["upload"]
    assert calls["alerts"] == []
