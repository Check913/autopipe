"""TOML configuration loading + path management."""
import copy
import os
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib

CONFIG_NAMES = ("autopipe.toml", "config.toml")

DEFAULTS = {
    "general": {
        "niche": "amazing facts",
        "language": "en",
        "region": "US",
        "data_dir": "data",
        "output_dir": "output",
        "cache_dir": "cache",
        "log_file": "autopipe.log",
        "log_level": "INFO",
    },
    "scout": {
        "trending_count": 25,
        "keywords": ["amazing facts", "true story", "science explained"],
    },
    "radar": {
        # Radar multi-plateformes (aucune cle API) : Reddit, Hacker News,
        # Google News, Google Trends, YouTube Shorts + TikTok/Instagram
        # (pages tag, desactivees par defaut). Alimente scout/ideas/remake.
        "enabled": True,
        "cache_ttl_minutes": 20,
        "items_cap": 150,
        # Reserve minimale de places pour les videos (youtube/tiktok/instagram)
        # dans items_cap, sinon Reddit/HN ecrasent les videos fraiches.
        "video_min_items": 40,
        "topics_cap": 25,
        "pacing_seconds": 0.4,
        "request_timeout": 15,
        "reddit_subs": ["interestingasfuck", "Damnthatsinteresting",
                        "BeAmazed", "interesting"],
        "reddit_per_sub": 25,
        "hackernews_enabled": True,
        "hackernews_per_query": 30,
        "google_news_enabled": True,
        "google_news_per_keyword": 20,
        "google_trends_enabled": True,
        "google_trends_geo": "",
        "youtube_enabled": True,
        "youtube_per_keyword": 15,
        # Recence YouTube (sp) : videos publiees il y a <= N jours (1/7/30).
        "youtube_recency_days": 7,
        # Requetes YouTube variees ({kw} = mot-cle de niche) -> fini les memes videos.
        "youtube_queries": ["{kw} shorts", "{kw} viral shorts",
                            "{kw} moments caught on camera"],
        # Memoire anti-repetition : un item vu aux scans precedents est decrement,
        # un inedit est booste ; oublie apres N jours (history.json).
        "history_days": 45,
        "novelty_boost": 1.5,
        "tiktok_enabled": False,
        "tiktok_tags": ["viral", "interesting"],
        "instagram_enabled": False,
        "instagram_tags": ["viral", "reels"],
    },
    "remake": {
        # Super-Remake : scrape les Shorts viraux -> analyse structurelle ->
        # regeneration 100% originale "10% Better" + garde anti-duplication.
        "enabled": False,
        "count": 2,
        "keywords": [],           # vide -> fallback sur scout.keywords
        "min_views": 500000,
        "max_duration": 75,
        "limit": 5,
        "search_per_keyword": 10,
        "hydrate_count": 15,
        "radar_hits": 10,         # hits video du radar fusionnes dans discover_hits
        "hook_sentences": 3,
        "hook_chars": 160,
        "keywords_from_text": 8,
        "temperature": 0.85,
        "max_jaccard": 0.35,      # similarite max titre/script vs l'original
        "comments_enabled": True, # scraper commentaires + matrice retention
        "max_comments": 60,       # commentaires analyses par hit viral
        "retention_buckets": 20,  # tranches seconde/s de la matrice
        "interrupt_interval": [7, 10],  # pattern interrupt toutes les 7-10 s
        "ydl_extra": {},          # options yt-dlp supplementaires (dict TOML)
    },
    "visual": {
        # Module 1 : analyse OpenCV des 3 premieres secondes des hits viraux
        # -> coupes, zoom, palette, zone sous-titres appliques au rendu.
        "enabled": True,
        "analyze_seconds": 3.0,
        "max_videos": 5,
        "download_max_height": 720,
        "cut_threshold": 0.40,
        "cache_hours": 168,       # re-analyse d'un meme hit apres 7 jours
    },
    "trend": {
        # Module 2 : arbitrage de tendance (Google Trends x saturation YouTube).
        "enabled": True,
        "geo": "",                # vide -> radar.google_trends_geo / general.region
        "max_topics": 15,
        "top_n": 5,
        "min_momentum": 0.3,      # seuil pour proposer un sujet
        "cache_hours": 6,         # duree de validite du dernier snapshot
    },
    "optimizer": {
        # Module 3 : boucle auto-apprenante (analytics -> patterns -> cfg).
        "enabled": True,
        "min_samples": 3,         # videos minimales avant tout apprentissage
        "max_delta": 0.35,        # variation max appliquee a un parametre
        "patterns_file": "",      # vide -> data/optimizer/patterns.json
    },
    "llm": {"base_url": "", "api_key": "", "model": "gpt-4o-mini", "temperature": 0.8},
    "tts": {"voice": "en-US-AndrewNeural", "rate": "+0%", "volume": "+0%"},
    "subs": {"max_chars": 42, "max_lines": 2, "min_duration": 0.7},
    "fetch_media": {
        "provider": "auto",
        "pixabay_api_key": "",
        "pexels_api_key": "",
        "clips_per_video": 6,
        "min_width": 720,
    },
    "video": {"width": 1080, "height": 1920, "fps": 30, "music_volume": 0.15,
              "music_dir": "assets/music", "loop_ending": True, "loop_tail": 0.7,
              "hook_overlay": True, "hook_duration": 3.0, "hook_font_size": 84},
    "upload": {
        "privacy": "public",
        "category_id": "24",
        "made_for_kids": False,
        "client_secrets": "client_secret.json",
        "token_file": "token.json",
        "daily_limit": 6,
        "post_hours_utc": [15, 21],
        "auto_comment": True,
        "comment_template": "",
    },
    "analytics": {},
    "network": {"proxy_mode": "direct"},
    "scheduler": {"interval_hours": 4, "state_file": "data/scheduler_state.json"},
}

_ACTIVE = None


def _deep_merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def find_config_file(root=None):
    root = Path(root or os.getcwd())
    for name in CONFIG_NAMES:
        cand = root / name
        if cand.exists():
            return cand
    return None


def load_config(path=None, root=None):
    data = {}
    file = Path(path) if path else find_config_file(root)
    if file and file.exists():
        with open(file, "rb") as fh:
            data = tomllib.load(fh)
    cfg = _deep_merge(DEFAULTS, data)
    if os.environ.get("PEXELS_API_KEY"):
        cfg["fetch_media"]["pexels_api_key"] = os.environ["PEXELS_API_KEY"]
    if os.environ.get("PIXABAY_API_KEY"):
        cfg["fetch_media"]["pixabay_api_key"] = os.environ["PIXABAY_API_KEY"]
    if os.environ.get("LLM_BASE_URL"):
        cfg["llm"]["base_url"] = os.environ["LLM_BASE_URL"]
    if os.environ.get("LLM_API_KEY"):
        cfg["llm"]["api_key"] = os.environ["LLM_API_KEY"]
    global _ACTIVE
    _ACTIVE = cfg
    return cfg


def get_config(**overrides):
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = load_config()
    cfg = copy.deepcopy(_ACTIVE)
    for dotted, value in overrides.items():
        node = cfg
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return cfg


class Paths:
    def __init__(self, cfg, root=None):
        self.root = Path(root or os.getcwd())
        self.data = self.root / cfg["general"]["data_dir"]
        self.output = self.root / cfg["general"]["output_dir"]
        self.cache = self.root / cfg["general"]["cache_dir"]
        self.scout = self.data / "scout"
        self.ideas = self.data / "ideas"
        self.remake = self.data / "remake"
        self.radar = self.data / "radar"
        self.analytics = self.data / "analytics"
        self.visual = self.data / "visual"
        self.trend = self.data / "trend"
        self.optimizer = self.data / "optimizer"
        self.audio_cache = self.cache / "audio"
        self.video_cache = self.cache / "pexels"

    def ensure(self):
        for d in (self.data, self.output, self.cache, self.scout,
                  self.ideas, self.remake, self.radar, self.analytics,
                  self.visual, self.trend, self.optimizer,
                  self.audio_cache, self.video_cache):
            d.mkdir(parents=True, exist_ok=True)
        return self


def get_paths(cfg=None, root=None):
    cfg = cfg or get_config()
    return Paths(cfg, root=root).ensure()
