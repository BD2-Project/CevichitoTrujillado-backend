"""Errores de la aplicación que la API traduce a HTTP (módulo ligero)."""


class AppError(Exception):
    """Base de los errores esperados."""


class InvalidAudioError(AppError):
    """El archivo del usuario no sirve: vacío, no decodificable o sin segmentos (422)."""


class ServiceUnavailableError(AppError):
    """SoupDB no responde o el catálogo/índice no está listo (503)."""


class IntegrityError(AppError):
    """Una operación rompería una invariante del catálogo o del historial."""


class IncompatibleSchemaError(IntegrityError):
    """Una tabla existe en SoupDB con un schema distinto del esperado."""


class RepositoryError(AppError):
    """Datos inconsistentes leídos de la base."""
