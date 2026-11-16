"""Domain errors for document processing."""

from __future__ import annotations


class ProcessingError(Exception):
    """Base error with a message safe to show to an operator."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class UnsupportedDocumentError(ProcessingError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class EncryptedDocumentError(ProcessingError):
    def __init__(self, message: str = "PDF защищён паролем") -> None:
        super().__init__(message, retryable=False)


class DamagedDocumentError(ProcessingError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class DocumentTooLargeError(ProcessingError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)
