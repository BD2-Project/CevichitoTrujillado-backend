"""Constantes de la aplicación (sin I/O ni dependencias).

Los valores de audio deben coincidir con el perfil ``audio_segment`` que SoupDB
usa al vectorizar (plugin ``soupdb-vector``): 22 050 Hz mono, bloques de 5 s sin
solapamiento y el último bloque solo si dura al menos la mitad.
"""

from typing import Final

SERVICE_NAME: Final = "soupdb-copyright-api"
API_PREFIX: Final = "/api/v1"

SAMPLE_RATE: Final = 22050
SEGMENT_SECONDS: Final = 5.0
MIN_SEGMENT_RATIO: Final = 0.5

#: Tablas de la aplicación en SoupDB.
SONGS: Final = "songs"
AUDIO_SEGMENTS: Final = "audio_segments"
ANALYSES: Final = "analyses"
ANALYSIS_MATCHES: Final = "analysis_matches"

#: Índice vectorial (lo crea el bootstrap por SQL y lo gestiona el plugin de SoupDB).
INDEX_NAME: Final = "copyright_audio_hnsw"
INDEX_KIND: Final = "HNSW"
INDEXED_COLUMN: Final = "audio_ref"
METRIC: Final = "euclidean"
PROFILE_ID: Final = "copyright-audio-v1"
CODEBOOK_K: Final = 128
VECTOR_DIMENSION: Final = CODEBOOK_K

#: Subcarpeta del volumen ``/audio`` donde el backend escribe los candidatos.
QUERIES_DIR: Final = "queries"
#: Extensiones de audio admitidas (las que decodifica el perfil).
AUDIO_EXTENSIONS: Final = (".wav", ".mp3", ".flac", ".ogg", ".au")
AUDIO_MEDIA_TYPES: Final = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".au": "audio/basic",
}
