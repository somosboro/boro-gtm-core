"""The model boundary (CE-ADR-008).

A provider may suggest a reply label or draft text. Its output is advisory: it
is validated against a closed vocabulary, never parsed into a state, a date, a
cap or a suppression, and the engine works with no provider at all.
"""

from __future__ import annotations

import os
from typing import Protocol

import httpx

REPLY_LABELS = ("referral", "meeting_request", "pricing_request", "scope_request",
                "capability_request", "request_info", "positive_interest", "not_relevant",
                "rejection", "human_other")


class LlmProvider(Protocol):
    name: str

    def classify_reply(self, text: str) -> str | None: ...

    def draft(self, prompt: str) -> tuple[str, str] | None:
        """(model id, raw text) for a first-email draft, or None when unavailable."""


class NullProvider:
    """The default. Rules decide everything; nothing is guessed."""

    name = "none"

    def classify_reply(self, text: str) -> str | None:
        return None

    def draft(self, prompt: str) -> tuple[str, str] | None:
        return None


class AnthropicProvider:
    """Optional. Enabled only when GTM_ENGINE_LLM=anthropic and a key is present."""

    name = "anthropic"
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model: str | None = None,
                 client: httpx.Client | None = None) -> None:
        self._key = api_key
        self._model = model or os.environ.get("GTM_ENGINE_LLM_MODEL", "claude-haiku-4-5-20251001")
        self._client = client or httpx.Client(timeout=20)

    def classify_reply(self, text: str) -> str | None:
        prompt = ("Classify this reply to a B2B sales email. Answer with exactly one label from: "
                  + ", ".join(REPLY_LABELS) + ".\n\nReply:\n" + text[:4000])
        try:
            response = self._client.post(self.URL, headers={
                "x-api-key": self._key, "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }, json={"model": self._model, "max_tokens": 10,
                     "messages": [{"role": "user", "content": prompt}]})
            response.raise_for_status()
            answer = response.json()["content"][0]["text"].strip().lower()
        except Exception:  # noqa: BLE001 - advisory only; rules already decided the safe path
            return None
        return answer if answer in REPLY_LABELS else None


    #: Drafting writes from a research brief only (CE-ADR-022); the gate checks the result.
    DRAFT_MODEL_DEFAULT = "claude-opus-5-5"

    def draft(self, prompt: str) -> tuple[str, str] | None:
        model = os.environ.get("GTM_ENGINE_DRAFT_MODEL", self.DRAFT_MODEL_DEFAULT)
        try:
            response = self._client.post(self.URL, timeout=120, headers={
                "x-api-key": self._key, "anthropic-version": "2023-06-01",
                "anthropic-beta": "server-side-fallback-2026-07-01",
                "content-type": "application/json",
            }, json={"model": model, "max_tokens": 16000, "fallbacks": "default",
                     "messages": [{"role": "user", "content": prompt}]})
            response.raise_for_status()
            payload = response.json()
        except Exception:  # noqa: BLE001 - no draft is the safe outcome
            return None
        if payload.get("stop_reason") == "refusal":
            return None
        text = "".join(block.get("text", "") for block in payload.get("content", [])
                       if block.get("type") == "text").strip()
        return (payload.get("model") or model, text) if text else None


def provider_from_env() -> LlmProvider:
    if os.environ.get("GTM_ENGINE_LLM", "none").lower() == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            return AnthropicProvider(key)
    return NullProvider()
