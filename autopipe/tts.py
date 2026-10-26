"""edge-tts voice synthesis with word timing capture (WordBoundary)."""
import asyncio
import random
import time

from .config import get_config, get_paths
from .util import get_logger, save_json

log = get_logger("tts")

# Voix de secours US: le service Microsoft renvoie parfois NoAudioReceived
# pendant 1-2 minutes (pannes transientes observees le 2026-10-23 a 21:00 et
# 22:29). Rotater la voix repartit la charge et augmente les chances de passer.
DEFAULT_FALLBACK_VOICES = ["en-US-ChristopherNeural", "en-US-AriaNeural",
                           "en-US-EricNeural"]


async def _synthesize(text, out_mp3, voice, rate, volume, words,
                      boundary="WordBoundary"):
    import edge_tts
    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate or "+0%",
                                           volume=volume or "+0%",
                                           boundary=boundary)
    except TypeError:  # edge-tts versions without the boundary option
        communicate = edge_tts.Communicate(text, voice, rate=rate or "+0%",
                                           volume=volume or "+0%")
    with open(out_mp3, "wb") as fh:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                fh.write(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                # SentenceBoundary: timings plus grossiers (1 entree/phrase),
                # mais l'SRT reste fonctionnel en dernier recours.
                words.append({
                    "text": chunk["text"],
                    "start": chunk["offset"] / 1e7,
                    "end": (chunk["offset"] + chunk["duration"]) / 1e7,
                })


def synthesize(text, out_mp3, out_timings, voice=None, rate=None, volume=None):
    """Generates the audio file and a JSON of per-word timings.

    Returns the list of words [{text, start, end}] in seconds.
    Resilience (2026-10-23): 5 attempts with exponential backoff + jitter and
    voice rotation. edge-tts NoAudioReceived is transient Microsoft-side
    behaviour: 3 fast attempts (old code) all failed at 22:29 while the same
    combination succeeded 15 minutes later.
    """
    # Garde-fou (2026-10-26): un script non-str (ex: tableau JSON renvoye par
    # le LLM) est une erreur deterministe -> echec immediat et clair au lieu
    # de 5 tentatives (30 s) contre edge-tts "text must be str".
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("TTS: le texte doit etre un str non vide (recu %s) "
                           "- verifier le champ script de l'idee"
                           % type(text).__name__)
    cfg = get_config()
    words = []
    voice = voice or cfg["tts"].get("voice", "en-US-AndrewNeural")
    fallbacks = [v for v in (cfg["tts"].get("fallback_voices")
                             or DEFAULT_FALLBACK_VOICES) if v]
    candidates = [voice] + [v for v in fallbacks if v != voice]
    out_mp3 = str(out_mp3)
    out_timings = str(out_timings)
    log.info("TTS: %s characters, voice %s", len(text), voice)
    last_err = None
    attempts = 5
    for attempt in range(1, attempts + 1):
        used = candidates[(attempt - 1) % len(candidates)]
        if attempt > 1:
            log.info("TTS retry %s/%s with voice %s", attempt, attempts, used)
        try:
            asyncio.run(_synthesize(text, out_mp3, used, rate, volume, words))
            save_json(out_timings, {"voice": used, "words": words})
            if words:
                break
            raise RuntimeError("No word timings received (empty audio?)")
        except Exception as exc:
            last_err = exc
            words = []
            if attempt < attempts:
                wait_s = min(30.0, 2.0 * 2 ** (attempt - 1)) + random.uniform(0, 1)
                log.warning("TTS attempt %s/%s failed (%s), retry in %.0fs",
                            attempt, attempts, exc, wait_s)
                time.sleep(wait_s)
    if not words:
        raise RuntimeError("TTS failed after %s attempts: %s" % (attempts, last_err))
    log.info("TTS done: %s word timings -> %s", len(words), out_timings)
    return words


def test_voice(out_dir=None):
    """Quick test of the TTS chain."""
    cfg = get_config()
    paths = get_paths(cfg)
    out_dir = out_dir or paths.audio_cache
    out_mp3 = out_dir / "test_voice.mp3"
    out_timings = out_dir / "test_voice.json"
    text = "Hey! This is a test of the AutoPipe voice pipeline. If you can hear this sentence in a US English voice, everything works."
    words = synthesize(text, out_mp3, out_timings,
                       cfg["tts"].get("voice"), cfg["tts"].get("rate"), cfg["tts"].get("volume"))
    return out_mp3, words