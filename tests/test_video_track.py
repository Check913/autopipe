"""Tests regression du montage video (build_video_track)."""
import shutil
import subprocess

import pytest

from autopipe.util import media_duration
from autopipe.video import build_video_track


def _make_clip(path, seconds=2.0):
    """Petit clip de test via ffmpeg lavfi (disque, pas de mock)."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg absent")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=duration=%.1f:size=320x240:rate=30" % seconds,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(path)],
        check=True)
    return path


def test_track_covers_voice_duration(tmp_path):
    """Bug 2026-10-30 : 1 seul clip Pixabay de ~10 s pour une voix quiz de
    ~110 s -> la piste s'arretait a la duree des segments et le mux final
    tronquait la video ET l'audio a ~12 s. Le remplissage doit boucler les
    segments jusqu'a couvrir audio_dur."""
    clip = _make_clip(tmp_path / "clip.mp4", 2.0)
    track = build_video_track([{"path": str(clip)}], audio_dur=5.0,
                              work_dir=tmp_path, width=320, height=240,
                              fps=30, loop_tail=0.0)
    dur = float(media_duration(track))
    assert dur >= 4.5, "piste trop courte: %.2fs pour une voix de 5.0s" % dur
    # Pas de debordement absurde non plus (le dernier segment peut depasser
    # le besoin d'au plus sa propre duree).
    assert dur <= 9.0, "piste absurding longue: %.2fs" % dur
