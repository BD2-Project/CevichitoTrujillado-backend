"""La decodificación debe coincidir con la del perfil de audio de SoupDB."""

from pathlib import Path

from copyright_app.config import MIN_SEGMENT_RATIO, SAMPLE_RATE, SEGMENT_SECONDS
from copyright_app.domain.models import SegmentInterval
from copyright_app.errors import InvalidAudioError


def intervals_for_length(
    samples: int, *, sample_rate: int = SAMPLE_RATE, seconds: float = SEGMENT_SECONDS
) -> list[SegmentInterval]:
    """Intervalos de una señal de ``samples`` muestras (función pura)."""
    if samples < 0:
        raise ValueError("samples debe ser >= 0")
    size = round(seconds * sample_rate)
    lengths = [min(size, samples - start) for start in range(0, samples, size)]
    if lengths and lengths[-1] < MIN_SEGMENT_RATIO * size:
        lengths.pop()
    return [
        SegmentInterval(
            index=index,
            start_seconds=index * seconds,
            end_seconds=index * seconds + length / sample_rate,
        )
        for index, length in enumerate(lengths)
    ]


class LibrosaSegmenter:
    """Decodifica con ``librosa`` (mismos parámetros que el perfil) y segmenta."""

    def intervals(self, path: Path) -> list[SegmentInterval]:
        import librosa  # import diferido: pesado y solo necesario al analizar

        try:
            signal, _ = librosa.load(path, sr=SAMPLE_RATE, mono=True)
        except Exception as exc:  # noqa: BLE001 - cualquier fallo de decodificación es 422
            raise InvalidAudioError("el audio no se puede decodificar") from exc
        return intervals_for_length(len(signal))
