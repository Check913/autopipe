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
    "identity": {
        # Identite de chaine (2026-10-27) : gate de niche + bloc injecte dans
        # tous les prompts LLM. L'algo YouTube ne cataloge la chaine que si
        # la niche est stable (critique : sujets heterogenes inclassables).
        "name": "",
        "niche": "",              # vide -> general.niche
        "audience": "",
        "promise": "",
        "tone": "",
        "keywords": [],           # themes on-niche (gate scout/ideas)
        "anti_topics": [],       # themes interdits (gate scout/ideas)
        # V2 : soft score par defaut. strict=True n'est conserve que pour qui
        # veut un gate dur explicite - a petit volume il rejette trop de
        # sujets et tue la cadence (bug observe : keywords=[] + strict ->
        # 100 % des tendances rejetees).
        "strict": False,
        "off_niche_penalty": 1.5,  # malus rank_score des sujets hors niche
    },
    "ideas": {
        # Calibrage Shorts 2026 (critique AP Dhillon) : titres <= 50 car.
        # concrets, hooks <= 60 car., scripts 50-85 mots (18-32 s) - sweet
        # spot retention 15-35 s, hooks forts des les 2 premieres secondes.
        "title_max_chars": 50,
        "hook_max_chars": 60,
        "banned_title_words": [],
        "script_words_min": 50,
        "script_words_max": 85,
        "duration_min": 15,
        "duration_max": 32,
        "duration_default": 24,
        # V2 : deux formats, pas un milieu - 13 s ultra-dense OU 60 s
        # narration complete. density 1-2 -> short, 3+ -> long.
        "duration_profiles": {
            "short": {"target": 13, "min": 12, "max": 18},
            "long": {"target": 60, "min": 50, "max": 60},
        },
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
        # V3 kill switch : stoppe la propagation d'une famille/niche morte.
        "kill_switch": True,
        "kill_family_pct": 30.0,   # 3 Shorts < 30 % dans une famille -> ban
        "kill_niche_pct": 40.0,    # 5 Shorts consecutifs < 40 % -> alerte niche
        "kill_ban_shorts": 20,     # duree du ban (en Shorts publies analyses)
        "kill_niche_shorts": 10,   # duree de la pause de niche
        # V3 baseline : verrouille les `baseline_size` premiers Shorts publies
        # ; aucun ajustement numerique tant que ce n'est pas verrouille.
        "baseline_size": 10,
        # V3 patterns actifs : retention moyenne > active_min_pct sur
        # >= active_min_n Shorts -> force dans les `active_shorts` suivants.
        "active_min_pct": 70.0,
        "active_min_n": 3,
        "active_shorts": 5,
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
              "hook_overlay": True, "hook_duration": 3.0, "hook_font_size": 84,
              # V2 : checkpoint mi-video (texte de rappel affiche a N s) pour
              # retenir les swipes sur les formats 60 s.
              "checkpoint_enabled": True,
              # V3 : checkpoints FIXES aux moments qui comptent - la decision
              # de swipe se joue a 1,4-2,3 s (hook), le premier beat tient a
              # 8 s, le payoff arrive dans le dernier tiers. L'ancien
              # checkpoint_at_pct unique (0.45 = 27 s sur un 60 s) ne
              # testait qu'un moment de la courbe : a 27 s, qui reste est
              # deja accroche. Le dernier tiers est ajoute automatiquement
              # sur les formats longs (60 s) ; sur 13 s il tomberait a
              # ~10 s, trop proche du checkpoint 8 s -> deduplique.
              "checkpoint_times_s": [2.0, 8.0],
              "checkpoint_text": "STAY - #3 IS WILD"},
    "upload": {
        "privacy": "public",
        "category_id": "24",
        "made_for_kids": False,
        "client_secrets": "client_secret.json",
        "token_file": "token.json",
        "daily_limit": 6,
        # posts_per_day : 0 = comportement historique conserve (daily_limit,
        # nombre de Shorts/jour actuel de la chaine, volontairement intact).
        "posts_per_day": 0,
        "post_hours_utc": [15, 21],
        "auto_comment": True,
        "comment_template": "",
    },
    "analytics": {
        # V2/V3 : vrai proxy de retention = engagedViews/views. Une "engaged
        # view" Shorts = une lecture qui continue apres la premiere frame
        # (doc YouTube, revision du 31/03/2025) - PAS "actif a 30 s".
        # Croise avec averageViewPercentage. Cible : >= 70 %.
        "engaged_target_pct": 70,
        # V3 loop rate : vues / viewers uniques - valide le "fin rejoint
        # debut". > 100 % = les viewers re-regardent (loop OK) ; < 80 % =
        # le loop ne prend pas. uniqueViewers est Studio-only via API
        # (verifie 2026-10-27) : le code TENTE la metrique et replie
        # proprement si Google la refuse.
        "loop_target_pct": 100,
        "loop_min_pct": 80,
    },
    "hook_ab": {
        # V2 : A/B de hook - deux variantes par sujet, tracker
        # variant_id -> retention (data/hook_ab/assignments.jsonl).
        "enabled": True,
    },
    "research": {
        # MODULE 4 (2026-10-29) : benchmark quantitatif continu de la niche.
        # Scraping yt-dlp (sans cle) des Shorts qui gagnent + canaux rivaux
        # -> chiffres reels (durees, patterns de titres, velocite de vues)
        # injectes dans les prompts. Differe du radar (sujets viraux a
        # remaker) : ici on mesure COMMENT les gagnants gagnent.
        "enabled": True,
        "keywords": [],           # vide -> scout.keywords
        "competitors": [],        # handles (@x) ou urls de canaux a espionner
        "search_per_keyword": 15, # resultats flat par mot-cle
        "competitor_max": 20,     # Shorts recents analyses par concurrent
        "min_views": 100000,      # seuil "gagnant" pour l'analyse
        "top_n": 20,              # taille de l'echantillon gagnant analyse
        "cache_hours": 12,        # re-scrape apres ce delai
        "pacing_seconds": 0.6,    # pause entre requetes (respect des cibles)
    },
    "monetization": {
        # MODULE 5 (2026-10-29) : suivi YPP (Programme Partenaire YouTube)
        # + ETA + revenus estimes. Sources : channels.list (abonnes),
        # registre (vues Shorts 90 j), Analytics API (heures visionnage,
        # si le token yt-analytics.readonly est present).
        "enabled": True,
        "check_hours": 12,        # frequence du check (cache progress.json)
        "target_subs": 1000,
        "target_shorts_views_90d": 10000000,
        "target_watch_hours_12m": 4000,
        "rpm_shorts_usd": 0.08,   # RPM Shorts estime ($ / 1000 vues)
    },
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
        self.hook_ab = self.data / "hook_ab"
        self.registry = self.data / "registry"
        self.research = self.data / "research"
        self.monetization = self.data / "monetization"
        self.audio_cache = self.cache / "audio"
        self.video_cache = self.cache / "pexels"

    def ensure(self):
        for d in (self.data, self.output, self.cache, self.scout,
                  self.ideas, self.remake, self.radar, self.analytics,
                  self.visual, self.trend, self.optimizer, self.hook_ab,
                  self.registry, self.research, self.monetization,
                  self.audio_cache, self.video_cache):
            d.mkdir(parents=True, exist_ok=True)
        return self


def get_paths(cfg=None, root=None):
    cfg = cfg or get_config()
    return Paths(cfg, root=root).ensure()
