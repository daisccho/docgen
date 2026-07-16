"""
Контекстная панель — подсказки и справка по текущему экрану.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static, Label
from textual.widget import Widget


class ContextPanel(Widget):
    """Панель контекстной помощи — обновляется при смене вкладки."""

    CSS = """
    ContextPanel {
        padding: 0 1;
    }

    Label.panel-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
        text-align: center;
    }

    Static.help-text {
        color: $text-muted;
        margin-bottom: 1;
    }

    .shortcut {
        color: $accent;
    }

    .key {
        text-style: bold;
        color: $text;
    }
    """

    HELP_MAP = {
        "dashboard": {
            "title": "Статус проекта",
            "text": (
                "Текущее состояние проекта docgen.\n\n"
                "Хоткеи:\n"
                "  [bold]d[/] — Статус\n"
                "  [bold]i[/] — Init\n"
                "  [bold]s[/] — Snapshot\n"
                "  [bold]w[/] — Watch\n"
                "  [bold]q[/] — Выход"
            ),
        },
        "init": {
            "title": "Инициализация",
            "text": (
                "Создание .docgen.yaml.\n\n"
                "Обязательно:\n"
                "  • Repo URL\n\n"
                "Опционально:\n"
                "  • GITHUB_TOKEN env\n"
                "  • API Key\n"
                "  • Base URL\n"
                "  • Модель\n"
                "  • Max ходов\n\n"
                "Альтернатива:\n"
                "  docgen init --repo <URL>"
            ),
        },
        "snapshot": {
            "title": "Снэпшот",
            "text": (
                "Создать снэпшот документации.\n\n"
                "Тег релиза:\n"
                "  • Указать конкретный тег\n"
                "  • Или оставить пустым\n"
                "    (последний релиз)\n\n"
                "Флаги:\n"
                "  • -c: полный аудит\n"
                "  • -l: лог в файл\n\n"
                "Альтернатива:\n"
                "  docgen snapshot [-r v1.0]"
            ),
        },
        "watch": {
            "title": "Watch",
            "text": (
                "Автоматическое обновление\n"
                "документации.\n\n"
                "Проверяет новые теги\n"
                "каждые N минут.\n\n"
                "При обнаружении нового\n"
                "релиза:\n"
                "  • Обновляет .md-файлы\n"
                "  • Создаёт снэпшот\n"
                "  • Пишет CHANGELOG\n\n"
                "Альтернатива:\n"
                "  docgen watch -t 5"
            ),
        },
    }

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Помощь", id="panel-title", classes="panel-title")
            yield Static("", id="panel-text", classes="help-text")

    def on_tab_changed(self, tab_id: str) -> None:
        """Обновить контекст при смене вкладки."""
        help_data = self.HELP_MAP.get(tab_id)
        if help_data:
            self.query_one("#panel-title", Label).update(help_data["title"])
            self.query_one("#panel-text", Static).update(help_data["text"])
