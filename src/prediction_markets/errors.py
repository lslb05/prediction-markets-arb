class SourceError(Exception):
    """Failed to obtain or interpret data from a venue's API."""


class SourceUnavailable(SourceError):
    """The venue didn't respond within the retry limit."""


class UnexpectedSchema(SourceError):
    """The payload's shape changed. Ingestion stops instead of guessing."""


class CredentialsMissing(Exception):
    """A credential the requested operation needs was not configured."""
