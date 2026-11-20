from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE = 22050


def synth(seconds: float, base_hz: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    samples = round(seconds * SAMPLE_RATE)
    t = np.arange(samples) / SAMPLE_RATE
    signal = np.zeros(samples)
    block = round(5.0 * SAMPLE_RATE)
    for start in range(0, samples, block):
        piece = slice(start, start + block)
        for ratio in rng.uniform(1.0, 6.0, size=3):
            signal[piece] += 0.2 * np.sin(2 * np.pi * base_hz * ratio * t[piece])
    signal += 0.02 * rng.standard_normal(samples)
    return signal


def write_wav(path: Path, seconds: float, base_hz: float, seed: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, synth(seconds, base_hz, seed), SAMPLE_RATE)
    return path
