"""
Экран Watch — мониторинг в реальном времени.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Static, RichLog


class WatchMonitorWidget(Widget):
    """Мониторинг watch-режима с логом в реальном времени."""

    CSS = """
    WatchMonitorScreen {
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

    #watch-actions {
        height: auto;
        margin-bottom: 1;
    }

    #watch-actions Button {
        margin-right: 1;
    }

    RichLog {
        height: 1fr;
        border: solid $surface-light 1;
        padding: 1;
    }

    #status-bar {
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

    running = reactive(False)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Watch-монитор", classes="section-title")

            # Интервал
            with Horizontal(classes="form-row"):
                yield Label("Интервал (мин)")
                with Vertical():
                    yield Input(
                        placeholder="10",
                        id="input-interval",
                        value="10",
                        type="integer",
                    )
                    yield Label(
                        "Как часто проверять новые теги",
                        classes="hint",
                    )

            # Кнопки управления
            with Horizontal(id="watch-actions"):
                yield Button("Запустить", id="btn-start", variant="primary")
                yield Button("Остановить", id="btn-stop", variant="error")
                yield Button("Очистить лог", id="btn-clear-log", variant="default")

            # Лог
            yield RichLog(id="watch-log", highlight=True, markup=True, max_lines=500)

            # Статус
            with Horizontal(id="status-bar"):
                yield Label("Статус: остановлен", id="label-status")
