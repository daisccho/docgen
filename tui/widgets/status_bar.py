"""
Статус-бар — нижняя строка с информацией о проекте и хоткеями.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Label

from docgen.config import find_project_root, load_state


class DocgenStatusBar(Widget):
    """Нижняя строка состояния."""

    CSS = """
    DocgenStatusBar {
        height: 1;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
    }

    DocgenStatusBar Label {
        margin-right: 2;
    }

    .status-project {
        color: $accent;
    }

    .status-version {
        color: $text-muted;
    }

    .status-hint {
        color: $text-disabled;
        text-style: italic;
    }
    """

    def compose(self) -> ComposeResult:
        root = find_project_root()
        if root:
            state = load_state(str(root))
            project = state.config.project_name if state else "—"
            model = state.config.llm_model if state else "—"
        else:
            project = "не инициализирован"
            model = "—"

        yield Label(f"[bold]{project}[/]", classes="status-project")
        yield Label(f"модель: {model}", classes="status-version")
        yield Label("q:выход", classes="status-hint")
