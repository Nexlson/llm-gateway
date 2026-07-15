from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.core.errors import GatewayError


@dataclass
class ProviderResponse:
    status_code: int
    body: dict
    input_tokens: int
    output_tokens: int


class ProviderError(GatewayError):
    def __init__(
        self, message: str, status_code: int, provider: str, retryable: bool,
        error_type: str = "api_error", code: str | None = None,
    ) -> None:
        super().__init__(message, status_code, error_type, code=code)
        self.provider = provider
        self.retryable = retryable


@runtime_checkable
class ProviderAdapter(Protocol):
    name: str

    async def chat_completion(self, payload: dict, model: str) -> ProviderResponse:
        ...


RETRYABLE_STATUS = {408, 409, 429}


def is_retryable(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS or status_code >= 500
