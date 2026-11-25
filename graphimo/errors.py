"""Typed errors shared across Graphimo application mixins."""


class ApplicationError(Exception):
    """A renderer-safe application failure with a stable error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
