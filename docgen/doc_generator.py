"""Клиент LLM для docgen — обёртка над OpenAI API."""

from __future__ import annotations

from typing import Optional

from openai import OpenAI


class DocGenerator:
    """Клиент LLM для DocAgent."""

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self.model = model
        client_kwargs: dict = {}
        if api_key:
            client_kwargs["api_key"] = api_key
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client: OpenAI = OpenAI(**client_kwargs) if client_kwargs else OpenAI()
