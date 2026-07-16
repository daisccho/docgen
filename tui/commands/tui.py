"""
CLI-команда docgen tui — запуск TUI-интерфейса.
"""

from __future__ import annotations

import click


@click.command()
@click.option("--verbose", "-v", is_flag=True, help="Подробный вывод")
def tui(verbose: bool) -> None:
    """Запустить TUI-интерфейс docgen."""
    from docgen.tui.app import DocgenTUI
    app = DocgenTUI()
    app.run()
