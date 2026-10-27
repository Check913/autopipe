"""Fixtures communes : cfg minimale isolee dans tmp_path (aucun reseau)."""
import copy

import pytest


@pytest.fixture
def make_cfg(tmp_path):
    """Cfg en memoire : data_dir/output/cache pointent vers tmp_path."""

    def _make(**over):
        cfg = {
            "general": {
                "data_dir": str(tmp_path / "data"),
                "output_dir": str(tmp_path / "output"),
                "cache_dir": str(tmp_path / "cache"),
                "log_level": "CRITICAL",
                "log_file": "",
            },
            "identity": {
                "keywords": ["amazing facts", "true story"],
                "anti_topics": ["giveaway"],
                "strict": False,
                "off_niche_penalty": 1.5,
            },
            "ideas": {"hook_max_chars": 60},
            "hook_ab": {"enabled": True},
            "scout": {"keywords": ["amazing facts"]},
        }
        cfg.update(copy.deepcopy(over))
        return cfg

    return _make