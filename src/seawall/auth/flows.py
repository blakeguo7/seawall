"""Authentication flows for provider credentials.

Each flow is a self-contained class with a single ``run()`` method that
performs the interactive authentication and returns the obtained credential.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class AuthFlow(ABC):
    """Abstract base for all auth flows."""

    @abstractmethod
    def run(self) -> str:
        """Execute the flow and return the obtained credential value."""


# ---------------------------------------------------------------------------
# ApiKeyFlow — directly prompt for and store an API key
# ---------------------------------------------------------------------------


class ApiKeyFlow(AuthFlow):
    """Prompt the user for an API key and persist it via :mod:`seawall.auth.storage`."""

    def __init__(self, provider: str, prompt_text: str | None = None) -> None:
        self.provider = provider
        self.prompt_text = prompt_text or f"Enter your {provider} API key"

    def run(self) -> str:
        import getpass

        key = getpass.getpass(f"{self.prompt_text}: ").strip()
        if not key:
            raise ValueError("API key cannot be empty.")
        return key
