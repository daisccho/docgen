"""
Экран статуса проекта — отображает текущее состояние docgen-проекта.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static, Label, Button

from docgen.config import find_project_root, load_release_map, load_state
from docgen.git_analyzer import get_snapshot_versions
from docgen.models import ProjectState


class InfoCard(Widget):
    """Карточка с заголовком и значением."""

    def __init__(self, title: str, value: str = "—", icon: str = "") -> None:
        super().__init__()
        self._title = title
        self._value = value
        self._icon = icon

    def compose(self) -> ComposeResult:
        icon = f"{self._icon} " if self._icon else ""
        yield Label(f"{icon}{self._title}", classes="card-title")
        yield Label(self._value, classes="card-value")

    def update_value(self, value: str) -> None:
        self.query_one(".card-value", Label).update(value)


class DashboardWidget(Widget):
    """Главный экран — статус проекта."""

    CSS = """
    DashboardScreen {
        padding: 1;
    }

    .cards-row {
        height: auto;
        margin-bottom: 1;
    }

    InfoCard {
        width: 1fr;
        height: auto;
        border: solid $primary 1;
        border-title-color: $primary;
        padding: 1;
        margin-right: 1;
    }

    InfoCard:last-child {
        margin-right: 0;
    }

    .card-title {
        text-style: bold;
        color: $text;
        margin-bottom: 0;
    }

    .card-value {
        color: $accent;
        text-style: bold;
        margin-top: 1;
    }

    #quick-actions {
        height: auto;
        margin-top: 1;
        margin-bottom: 1;
    }

    #quick-actions Button {
        margin-right: 1;
    }

    #details-section {
        height: 1fr;
        border: solid $surface-light 1;
        padding: 1;
        margin-top: 1;
    }

    Label.section-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        state = self._load_project_state()

        with Vertical():
            # Строка карточек
            with Horizontal(classes="cards-row"):
                yield InfoCard("Репозиторий", state.get("repo", "—"), "📦")
                yield InfoCard("Последний тег", state.get("last_tag", "—"), "🏷")
                yield InfoCard("Снэпшотов", state.get("snapshots", "0"), "📸")
                yield InfoCard("Модель", state.get("model", "—"), "🤖")

            # Быстрые действия
            with Horizontal(id="quick-actions"):
                yield Button("📸 Snapshot", id="btn-snapshot", variant="primary")
                yield Button("▶ Watch", id="btn-watch", variant="default")
                yield Button("📋 Версии", id="btn-versions", variant="default")

            # Детальная информация
            with Vertical(id="details-section"):
                yield Label("📄 Конфигурация", classes="section-title")
                yield Static(state.get("config_text", "Нет конфигурации"))

    def _load_project_state(self) -> dict:
        """Загрузить актуальное состояние проекта."""
        result: dict[str, str] = {}
        root = find_project_root()
        if not root:
            result["repo"] = "Не инициализирован"
            result["last_tag"] = "—"
            result["snapshots"] = "0"
            result["model"] = "—"
            result["config_text"] = (
                "Проект не инициализирован.\n"
                "Перейдите на вкладку Init или выполните:\n"
                "  docgen init --repo <URL>"
            )
            return result

        state = load_state(str(root))
        if state:
            cfg = state.config
            result["repo"] = cfg.git_repo.split("/")[-1] if cfg.git_repo else "—"
            result["model"] = cfg.llm_model
            result["config_text"] = (
                f"  Рабочая папка: {root}\n"
                f"  GIT: {cfg.git_repo}\n"
                f"  Модель: {cfg.llm_model}\n"
                f"  Max ходов: {cfg.max_turns}\n"
                f"  Токен (env): {cfg.github_token_env or '—'}"
            )

        release_map = load_release_map(str(root))
        if release_map and release_map.last_documented_release:
            result["last_tag"] = release_map.last_documented_release

        snaps = get_snapshot_versions(str(root))
        result["snapshots"] = str(len(snaps))

        return result

    def on_mount(self) -> None:
        """При монтировании обновить контекстную панель."""
        panel = self.app.query_one("ContextPanel")
        # Вызов через app, если panel существует
        pass
