"""Comment Sentiment & Retention Scraper + Pattern Interrupt + Hook Matcher.

Extrait les commentaires des videos virales detectees par le radar, construit
une matrice de retention seconde par seconde (pics de reaction, questions,
emotion), planifie des pattern interrupts et calibre les hooks 3 s.

Couches reseaux (jamais d'exception remontee) :
  1. yt-dlp (getcomments, proxy direct force)
  2. watch HTML -> ytInitialData -> youtubei/v1/next (continuation, sans cle)
  3. degrade : titre + transcript passe par l'appelant (aucun commentaire)
"""

import json
import math
import re
import sys
import time

import requests

TS_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def _warn(msg):
    try:
        sys.stderr.write("[retention] %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass


def _parse_count(value):
    """'1.2K' -> 1200, '3' -> 3, 12 -> 12, None -> 0."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    try:
        s = str(value).strip().replace(",", "").replace(" ", "")
        m = re.match(r"^([\d.]+)([KkMm]?)$", s)
        if not m:
            return 0
        n = float(m.group(1))
        mult = {"k": 1000, "m": 1000000, "": 1}[m.group(2).lower()]
        return int(n * mult)
    except Exception:
        return 0


def _new_session():
    s = requests.Session()
    # trust_env=False sur l'INSTANCE : neutralise le proxy du registre Windows
    s.trust_env = False
    s.proxies = {}
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
    })
    return s


def _walk(obj, visitor, depth=0, max_depth=60):
    if depth > max_depth:
        return
    try:
        items = obj.items()
    except AttributeError:
        try:
            items = enumerate(obj)
        except TypeError:
            return
    for _k, v in items:
        try:
            visitor(v)
        except Exception:
            pass
        _walk(v, visitor, depth + 1, max_depth)


# ------------------------------------------------------ couche 1 : yt-dlp

def _fetch_comments_ytdlp(video_id, max_comments):
    import yt_dlp  # lazy : evite un import lourd en tete de module

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "getcomments": True,
        # proxy "" force la connexion directe : neutralise HTTP(S)_PROXY et
        # le proxy du registre Windows (cause du bot-check observe)
        "proxy": "",
        "socket_timeout": 20,
        "retries": 1,
        "extractor_args": {"youtube": {"maxcomments": [str(max_comments)]}},
    }
    url = "https://www.youtube.com/watch?v=" + video_id
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    out = []
    for c in (info.get("comments") or []):
        text = (c.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "text": text,
            "author": c.get("author") or "",
            "like_count": int(c.get("like_count") or 0),
            "time_text": c.get("_time_text") or "",
            "channel": "ytdlp",
        })
    return out


# ------------------------------------------------- couche 2 : innertube WEB

_WEB_CONTEXT = {
    "context": {
        "client": {
            "clientName": "WEB",
            "clientVersion": "2.20250620.00.00",
            "hl": "en",
            "gl": "US",
        }
    }
}


def _comment_from_entity(payload):
    props = payload.get("properties") or {}
    content = ((props.get("content") or {}).get("content")) or ""
    author = ((payload.get("author") or {}).get("displayName")) or ""
    likes = 0
    toolbar = payload.get("toolbar") or {}
    for k, v in toolbar.items():
        if "likecount" in str(k).lower():
            likes = _parse_count(v)
            if likes:
                break
    if not likes:
        likes = _parse_count(props.get("likeCountLiked"))
    time_text = props.get("publishedTime") or ""
    if not content:
        return None
    return {
        "text": content.strip(),
        "author": author,
        "like_count": likes,
        "time_text": time_text,
        "channel": "innertube",
    }


def _extract_initial_data(html):
    m = re.search(r"var ytInitialData = ({.+?});</script>", html)
    if not m:
        m = re.search(r'window\["ytInitialData"\] = ({.+?});', html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _fetch_comments_innertube(video_id, max_comments, max_pages=3):
    s = _new_session()
    r = s.get("https://www.youtube.com/watch?v=%s&hl=en&has_verified=1"
              % video_id, timeout=25)
    if r.status_code != 200:
        _warn("watch GET %s -> HTTP %s" % (video_id, r.status_code))
        return []
    data = _extract_initial_data(r.text)
    if data is None:
        _warn("ytInitialData introuvable pour %s" % video_id)
        return []

    tokens = []

    def collect_tokens(v):
        if isinstance(v, dict) and "continuationCommand" in v:
            tok = (v.get("continuationCommand") or {}).get("token")
            if tok:
                tokens.append(tok)

    _walk(data, collect_tokens)
    if not tokens:
        _warn("aucune continuation pour %s" % video_id)
        return []

    comments, seen = [], set()
    for token in tokens[:2]:
        cur = token
        for _page in range(max_pages):
            if not cur or len(comments) >= max_comments:
                break
            body = dict(_WEB_CONTEXT)
            body["continuation"] = cur
            try:
                r2 = s.post("https://www.youtube.com/youtubei/v1/next",
                            json=body, timeout=25)
            except Exception as exc:
                _warn("next POST echec: %s" % exc)
                break
            if r2.status_code != 200:
                _warn("next POST HTTP %s" % r2.status_code)
                break
            try:
                page = r2.json()
            except Exception:
                break
            before = len(comments)
            nxt = []

            def visit(v, _seen=seen, _c=comments, _n=nxt):
                if isinstance(v, dict) and "commentEntityPayload" in v:
                    c = _comment_from_entity(v["commentEntityPayload"])
                    if c:
                        cid = ((v["commentEntityPayload"].get("properties")
                                or {}).get("commentId")) or c["text"][:64]
                        if cid not in _seen:
                            _seen.add(cid)
                            _c.append(c)
                if isinstance(v, dict) and "continuationCommand" in v:
                    tok = (v.get("continuationCommand") or {}).get("token")
                    if tok:
                        _n.append(tok)

            _walk(page, visit)
            cur = nxt[0] if nxt else None
            if len(comments) == before:
                break
            time.sleep(0.4)
        if len(comments) >= max_comments:
            break
    return comments[:max_comments]


# ------------------------------------------------------- fetch public

def fetch_comments(video_id, max_comments=60):
    """Commentaires avec fallback double couche. Retourne [] si echec total."""
    comments = []
    try:
        comments = _fetch_comments_ytdlp(video_id, max_comments)
        if comments:
            return comments
        _warn("couche yt-dlp vide pour %s -> fallback innertube" % video_id)
    except Exception as exc:
        _warn("couche yt-dlp echec (%s): %s" % (video_id, exc))
    try:
        comments = _fetch_comments_innertube(video_id, max_comments)
    except Exception as exc:
        _warn("couche innertube echec (%s): %s" % (video_id, exc))
    return comments


# ------------------------------------------ matrice de retention seconde/s

_Q_RE = re.compile(r"\?+\s*$")
_REACTION_WORDS = ("whoa", "wow", "insane", "crazy", "jumped", "scared",
                   "chute", "fou", "sursaut", "frayeur", "wtf", "omg",
                   "no way", "sick", "wild")
_QUESTION_WORDS = ("how", "why", "what", "where", "when", "comment",
                   "pourquoi", "quest", "quand", "ou est")


def classify_comment(text):
    """('question'|'reaction'|'general', sentiment -1..1) heuristique rapide."""
    t = (text or "").lower()
    if _Q_RE.search(t) or any(w in t for w in _QUESTION_WORDS):
        kind = "question"
    elif any(w in t for w in _REACTION_WORDS):
        kind = "reaction"
    else:
        kind = "general"
    pos = sum(t.count(w) for w in ("love", "amazing", "great", "best",
                                  "perfect", "genial", "parfait"))
    neg = sum(t.count(w) for w in ("hate", "bad", "worst", "boring",
                                   "fake", "stupid", "nul", "arnaque"))
    total = pos + neg
    sentiment = (pos - neg) / float(total) if total else 0.0
    return kind, round(sentiment, 2)


def parse_first_timestamp(text):
    m = TS_RE.search(text or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def build_retention_matrix(comments, duration_s, buckets=20):
    """Densite + velocite par tranche + buckets de reaction des commentaires."""
    duration_s = int(duration_s or 0)
    matrix = {"duration_s": duration_s, "buckets": buckets,
              "bucket_s": None, "density": [], "velocity": [],
              "spikes": [], "comment_timestamps": [], "unlocated": 0,
              "kinds": {"question": 0, "reaction": 0, "general": 0}}
    if duration_s <= 0 or not comments:
        return matrix
    step = max(1, duration_s // buckets)
    matrix["bucket_s"] = step
    density = [0] * buckets
    for c in comments:
        kind, _sent = classify_comment(c.get("text", ""))
        matrix["kinds"][kind] = matrix["kinds"].get(kind, 0) + 1
        ts = parse_first_timestamp(c.get("text", "") or c.get("time_text", ""))
        if ts is None:
            matrix["unlocated"] += 1
            continue
        idx = min(buckets - 1, ts // step)
        density[idx] += 1
        matrix["comment_timestamps"].append({"s": ts, "kind": kind})
    matrix["density"] = density
    vel = [0] * buckets
    for i in range(1, buckets):
        vel[i] = density[i] - density[i - 1]
    matrix["velocity"] = vel
    thr = max(density) * 0.6 if density else 0
    if thr:
        matrix["spikes"] = [i * step for i, d in enumerate(density) if d >= thr]
    return matrix


# ------------------------------------- pattern interrupts + hook matcher

def plan_pattern_interrupts(matrix, interval=(7, 10), duration_s=None):
    """Positions (en secondes) ou imposer un retournement/question choc."""
    dur = int(duration_s or matrix.get("duration_s") or 0)
    if dur <= 0:
        return []
    lo, hi = interval
    base = list(range(lo, dur, hi))
    if not base:
        return []
    spikes = set(int(s) for s in (matrix.get("spikes") or []))
    out = []
    for pos in base:
        near = any(abs(pos - s) <= hi for s in spikes)
        out.append({"s": pos, "align_spike": near,
                    "type": "reveal" if near else "question"})
    return out


_TRIGGER_LEX = {
    "curiosite_negative": ("nobody", "secret", "hidden", "no one",
                           "personne", "secret", "cache", "interdit"),
    "urgence": ("now", "before", "last", "stop", "urgent", "avant",
                "immediately", "vite"),
    "mystere": ("mystery", "truth", "revealed", "unsolved", "mystere",
                "verite", "revele", "etrange"),
    "chiffre_choc": (r"\d+%", r"\d+x", r"\$\d", r"\d+ ?(?:k|m)\b"),
}


def build_hook_profile(title, comments, sample=40):
    """Frequences lexicales des 3 premieres secondes -> triggers psychologiques."""
    text = (title or "") + " "
    for c in comments[:sample]:
        text += (c.get("text") or "") + " "
    low = text.lower()
    triggers = {}
    for name, pats in _TRIGGER_LEX.items():
        n = 0
        for p in pats:
            if p.startswith("\\") or "(" in p or "[" in p:
                try:
                    n += len(re.findall(p, low))
                except re.error:
                    pass
            else:
                n += low.count(p)
        if n:
            triggers[name] = n
    tokens = re.findall(r"[a-z'’]{4,}", low)
    stop = {"this", "that", "with", "have", "from", "they", "will",
            "your", "about", "what", "when", "were", "been", "here",
            "just", "like", "video", "would", "could", "their"}
    freq = {}
    for t in tokens:
        if t not in stop:
            freq[t] = freq.get(t, 0) + 1
    top = sorted(freq.items(), key=lambda kv: -kv[1])[:15]
    return {"triggers": triggers, "top_terms": top}


def summarize_gaps(comments, max_items=8):
    """Trous dans la raquette : questions non repondues / tres likées."""
    questions = []
    for c in comments:
        kind, _ = classify_comment(c.get("text", ""))
        if kind == "question":
            questions.append(c)
    questions.sort(key=lambda c: -(c.get("like_count") or 0))
    return [{"text": q["text"][:200], "likes": q.get("like_count", 0)}
            for q in questions[:max_items]]


# ------------------------------------------------------- orchestrateur

def analyze_hit_retention(video_id, title="", duration_s=0, max_comments=60,
                          buckets=20, interval=(7, 10)):
    """Point d'entree remake.py. Jamais d'exception : degrade en None."""
    result = {"video_id": video_id, "comments": [], "matrix": None,
              "interrupts": [], "hook_profile": None, "gaps": [],
              "source": None}
    if not video_id:
        return result
    try:
        comments = fetch_comments(video_id, max_comments)
    except Exception as exc:
        _warn("fetch_comments echec (%s): %s" % (video_id, exc))
        comments = []
    result["comments"] = comments
    if comments:
        result["source"] = comments[0].get("channel")
    result["matrix"] = build_retention_matrix(comments, duration_s,
                                              buckets=buckets)
    if result["matrix"].get("density"):
        result["interrupts"] = plan_pattern_interrupts(
            result["matrix"], interval=tuple(interval),
            duration_s=duration_s)
    result["hook_profile"] = build_hook_profile(title, comments)
    result["gaps"] = summarize_gaps(comments)
    return result
