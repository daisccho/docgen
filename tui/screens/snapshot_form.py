"""
Экран Snapshot — интерактивная форма для docgen snapshot.
"""

from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Static, Select, Checkbox


class SnapshotFormWidget(Widget):
    """Форма запуска snapshot."""

    CSS = """
    SnapshotFormScreen {
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
            yield Label("Создание снэпшота", classes="section-title")

            # Тег релиза
            with Horizontal(classes="form-row"):
                yield Label("Тег релиза")
                with Vertical():
                    yield Input(
                        placeholder="v1.0 (оставьте пустым — последний релиз)",
                        id="input-release",
                    )
                    yield Label(
                        "Конкретный тег или пусто для последнего релиза",
                        classes="hint",
                    )

            # Флаги
            with Horizontal(classes="form-row"):
                with Vertical():
                    yield Checkbox("Полный аудит (-c)", id="check-audit")
                    yield Checkbox("Логировать (-l)", id="check-log")

            # Iterations
            with Horizontal(classes="form-row"):
                yield Label("Max ходов")
                with Vertical():
                    yield Input(
                        placeholder="оставьте пустым — значение из конфига",
                        id="input-iterations",
                        type="integer",
                    )

            # Кнопки
            with Horizontal(id="form-actions"):
                yield Button("Запустить snapshot", id="btn-snapshot", variant="primary")
                yield Button("Очистить", id="btn-clear", variant="default")

            # Вывод
            yield Label("Результат:", classes="section-title")
            yield Static(id="form-output")
