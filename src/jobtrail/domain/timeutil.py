from datetime import datetime


def require_aware(value: datetime | None, name: str) -> datetime | None:
    """Every stored moment is UTC; a naive datetime is refused rather than guessed."""
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (use UTC)")
    return value
