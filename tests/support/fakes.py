"""Dobles en memoria de los puertos del dominio (sin SoupDB)."""

from pathlib import Path

from copyright_app.domain.models import (
    AudioSegment,
    NeighborRow,
    SegmentInterval,
    Song,
    SongEntry,
    segment_audio_ref,
)
from copyright_app.schemas import AnalysisResult, AnalysisSummary, StoredAnalysis


def make_entry(song_id: int, title: str, path: str, segments: int = 3, first: int = 1) -> SongEntry:
    song = Song(song_id, title, "Artista", path, segments * 5.0)
    return SongEntry(
        song,
        tuple(
            AudioSegment(first + i, song_id, i, i * 5.0, i * 5.0 + 5.0, segment_audio_ref(path, i))
            for i in range(segments)
        ),
    )


class InMemoryCatalog:
    def __init__(self, entries: list[SongEntry]) -> None:
        self.entries = {e.song.song_id: e for e in entries}

    def list_songs(self) -> list[SongEntry]:
        return [self.entries[k] for k in sorted(self.entries)]

    def get_song(self, song_id: int) -> SongEntry | None:
        return self.entries.get(song_id)

    def songs_by_id(self, song_ids) -> dict[int, Song]:
        return {i: self.entries[i].song for i in song_ids if i in self.entries}


class FakeSearch:
    """Devuelve vecinos predefinidos por índice de segmento; registra las referencias."""

    def __init__(self, neighbors: dict[int, list[NeighborRow]], ready: bool = True) -> None:
        self.neighbors = neighbors
        self.ready = ready
        self.calls: list[tuple[str, int, bool]] = []
        self.root: Path | None = None

    def similar_segments(self, reference: str, k: int) -> list[NeighborRow]:
        path, _, segment = reference.partition("#segment=")
        exists = self.root is not None and (self.root / path).is_file()
        self.calls.append((reference, k, exists))
        return self.neighbors.get(int(segment), [])

    def index_ready(self) -> bool:
        return self.ready


class FakeSegmenter:
    def __init__(self, count: int, tail: float = 5.0) -> None:
        self.count = count
        self.tail = tail

    def intervals(self, path: Path) -> list[SegmentInterval]:
        assert path.is_file()
        return [
            SegmentInterval(i, i * 5.0, i * 5.0 + (self.tail if i == self.count - 1 else 5.0))
            for i in range(self.count)
        ]


class InMemoryHistory:
    def __init__(self) -> None:
        self.saved: dict[str, StoredAnalysis] = {}

    def save(self, result: AnalysisResult) -> StoredAnalysis:
        stored = StoredAnalysis(**result.model_dump(), created_at="2026-11-18T00:00:00.000000Z")
        self.saved[result.analysis_id] = stored
        return stored

    def list_summaries(self) -> list[AnalysisSummary]:
        return [
            AnalysisSummary(
                analysis_id=s.analysis_id,
                filename=s.filename,
                created_at=s.created_at,
                segment_seconds=s.segment_seconds,
                match_count=len(s.matches),
                song_count=len({m.song_id for m in s.matches}),
                best_score=max((m.score for m in s.matches), default=None),
            )
            for s in self.saved.values()
        ]

    def get(self, analysis_id: str) -> StoredAnalysis | None:
        return self.saved.get(analysis_id)


def neighbor(entry: SongEntry, index: int, score: float) -> NeighborRow:
    s = entry.segments[index]
    return NeighborRow(
        s.segment_id, s.song_id, s.segment_index, s.start_seconds, s.end_seconds, s.audio_ref, score
    )
