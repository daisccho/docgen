"""
Экран инициализации проекта — интерактивная форма docgen init.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Static, Select


class InitFormWidget(Widget):
    """Форма для docgen init с валидацией полей."""

    CSS = """
    InitFormScreen {
        padding: 1;
    }

    .form-row {
        height: auto;
        margin-bottom: 1;
    }

    .form-row > Label {
        width: 20;
        text-style: bold;
        padding-top: 1;
    }

    .form-row > Vertical {
        width: 1fr;
    }

    .form-row .hint {
        width: 1fr;
        color: $text-muted;
        text-style: italic;
        margin-top: 0;
    }

    #form-actions {
        height: auto;
        margin-top: 2;
        margin-bottom: 1;
    }

    #form-actions Button {
        margin-right: 1;
    }

    #form-output {
        height: 3;
        border: solid $surface-light 1;
        padding: 1;
        margin-top: 1;
    }

    .section-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Инициализация проекта", classes="section-title")

            # Репозиторий (обязательный)
            with Horizontal(classes="form-row"):
                yield Label("Repo URL*")
                with Vertical():
                    yield Input(
                        placeholder="https://github.com/user/repo.git",
                        id="input-repo",
                    )
                    yield Label("URL git-репозитория", classes="hint")

            # GitHub token env
            with Horizontal(classes="form-row"):
                yield Label("GITHUB_TOKEN env")
                with Vertical():
                    yield Input(
                        placeholder="GITHUB_TOKEN (имя переменной)",
                        id="input-token-env",
                    )
                    yield Label(
                        "Имя переменной окружения с GitHub-токеном",
                        classes="hint",
                    )

            # LLM API Key
            with Horizontal(classes="form-row"):
                yield Label("API Key")
                with Vertical():
                    yield Input(
                        placeholder="sk-... или оставить для OPENAI_API_KEY",
                        id="input-api-key",
                        password=True,
                    )
                    yield Label(
                        "Или оставьте пустым для OPENAI_API_KEY из env",
                        classes="hint",
                    )

            # Base URL
            with Horizontal(classes="form-row"):
                yield Label("Base URL")
                with Vertical():
                    yield Input(
                        placeholder="https://api.openai.com/v1",
                        id="input-base-url",
                    )
                    yield Label(
                        "Для OpenAI-совместимых провайдеров",
                        classes="hint",
                    )

            # Model
            with Horizontal(classes="form-row"):
                yield Label("Модель")
                with Vertical():
                    yield Input(
                        placeholder="gpt-4o",
                        id="input-model",
                        value="gpt-4o",
                    )

            # Iterations
            with Horizontal(classes="form-row"):
                yield Label("Max ходов")
                with Vertical():
                    yield Input(
                        placeholder="10",
                        id="input-iterations",
                        value="10",
                        type="integer",
                    )
                    yield Label(
                        "Максимум ходов агента на один .md-файл",
                        classes="hint",
                    )

            # Кнопки
            with Horizontal(id="form-actions"):
                yield Button("Выполнить init", id="btn-init", variant="primary")
                yield Button("Очистить", id="btn-clear", variant="default")

            # Вывод
            yield Label("Результат:", classes="section-title")
            yield Static(id="form-output")
