"""Tests engagement_value (engagedViews/views) + checkpoint ASS."""
from autopipe.analytics import engagement_value
from autopipe.config import get_paths
from autopipe.subs import write_checkpoint_ass


def test_engagement_value_engaged_ratio():
    assert engagement_value({"engaged_ratio": 75.0}) == 75.0


def test_engagement_value_repli_avg_view_pct():
    assert engagement_value({"avg_view_pct": 60.0}) == 60.0


def test_engagement_value_vide():
    assert engagement_value({}) == 0.0
    assert engagement_value(None) == 0.0
    assert engagement_value({"engaged_ratio": "pas un nombre"}) == 0.0


def test_write_checkpoint_ass_ecrit(make_cfg, tmp_path):
    out = tmp_path / "ckpt.ass"
    res = write_checkpoint_ass("STAY - IT GETS WILD", out, 15.0)
    assert res == out and out.exists()
    content = out.read_text(encoding="utf-8")
    assert "STAY" in content
    assert "Alignment" in content and "2" in content
    # Fenetre de diffusion : 15.0 s -> 17.5 s (0:00:15.00 -> 0:00:17.50).
    assert "0:00:15.00" in content and "0:00:17.50" in content


def test_write_checkpoint_ass_refus_trop_tot(make_cfg, tmp_path):
    out = tmp_path / "ckpt.ass"
    assert write_checkpoint_ass("STAY", out, 1.0) is None
    assert not out.exists()


def test_write_checkpoint_ass_refus_texte_vide(make_cfg, tmp_path):
    out = tmp_path / "ckpt.ass"
    assert write_checkpoint_ass("   ", out, 15.0) is None
    assert write_checkpoint_ass(None, out, 15.0) is None


def test_paths_expose_hook_ab(make_cfg):
    paths = get_paths(make_cfg())
    assert paths.hook_ab.name == "hook_ab"
    assert paths.hook_ab.parent == paths.data
    assert paths.hook_ab.exists()