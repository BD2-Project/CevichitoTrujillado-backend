"""Genera un catálogo WAV reproducible para probar la demo local."""

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

from copyright_app.config import SEGMENT_SECONDS

SAMPLE_RATE = 22050
DURATION = 20
TRACKS = (("Alpha", 220), ("Beta", 520), ("Gamma", 1200))


def generate(root: Path) -> None:
    audio_root = root / "fma"
    audio_root.mkdir(parents=True, exist_ok=True)
    segment_count = int(DURATION / SEGMENT_SECONDS)
    segments = [
        [index, index * SEGMENT_SECONDS, (index + 1) * SEGMENT_SECONDS]
        for index in range(segment_count)
    ]
    t = np.arange(round(SEGMENT_SECONDS * SAMPLE_RATE)) / SAMPLE_RATE
    songs = []
    for song_id, (title, frequency) in enumerate(TRACKS, start=1):
        rng = np.random.default_rng(song_id)
        tones = rng.uniform(1, 6, size=(segment_count, 3))
        wave = 0.2 * np.sin(2 * np.pi * frequency * tones[:, :, None] * t).sum(axis=1)
        wave += rng.normal(0, 0.02, size=wave.shape)
        filename = title.lower() + ".wav"
        sf.write(audio_root / filename, wave.ravel(), SAMPLE_RATE, subtype="PCM_16")
        songs.append(
            {
                "song_id": song_id,
                "title": title,
                "artist": "Audio sintético",
                "audio_path": f"fma/{filename}",
                "duration_seconds": DURATION,
                "first_segment_id": (song_id - 1) * segment_count + 1,
                "segments": segments,
            }
        )
    catalog = {
        "version": 1,
        "segment_seconds": SEGMENT_SECONDS,
        "song_count": len(songs),
        "segment_count": segment_count * len(songs),
        "songs": songs,
    }
    (root / "catalog.json").write_text(json.dumps(catalog, indent=2), encoding="utf-8")


if __name__ == "__main__":
    generate(Path(sys.argv[1]))
