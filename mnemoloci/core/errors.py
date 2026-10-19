from typing import Any


class MnemoError(Exception):
    """Base error carrying a stable machine-readable code and optional details."""

    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details


class ValidationError(MnemoError):
    pass


class ConflictError(MnemoError):
    pass
