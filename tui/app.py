"""
Textual TUI-приложение для docgen.
Главное приложение с таб-навигацией.
"""

from __future__ import annotations

from typing import Optional

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, TabbedContent, TabPane, Static, Label, Button, Input, Checkbox, RichLog

from docgen.config import find_project_root, init_project, load_release_map, load_state
from docgen.models import ProjectState
from docgen.git_analyzer import get_snapshot_versions


class DocgenTUI(App):
    """Главное TUI-приложение docgen."""

    TITLE = "docgen"
    SUB_TITLE = "автоматическая документация"
    CSS = """
    DocgenTUI {
        background: $surface;
    }

    #main-layout {
        height: 1fr;
    }

    #tab-content {
        height: 1fr;
    }

    #right-panel {
        width: 32;
        min-width: 24;
        max-width: 40;
        background: $panel;
        border-left: solid $primary;
        padding: 1;
        height: 1fr;
    }

    #dashboard-cards {
        height: auto;
        margin-bottom: 1;
    }

    .card {
        width: 1fr;
        height: auto;
        border: solid $primary;
        padding: 1;
        margin-right: 1;
    }

    .card:last-child {
        margin-right: 0;
    }

    .card-title {
        text-style: bold;
        color: $text;
    }

    .card-value {
        color: $accent;
        text-style: bold;
        margin-top: 1;
    }

    #quick-actions {
        height: auto;
        margin-bottom: 1;
    }

    #quick-actions Button {
        margin-right: 1;
    }

    #details-section {
        height: auto;
        border: solid $surface-lighten-1;
        padding: 1;
    }

    .section-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
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
    }

    RichLog {
        height: 1fr;
        border: solid $surface-lighten-1;
        padding: 1;
    }

    #status-bar {
        height: 3;
        border: solid $surface-lighten-1;
        padding: 1;
        margin-top: 1;
    }

    #watch-actions {
        height: auto;
        margin-bottom: 1;
    }

    #watch-actions Button {
        margin-right: 1;
    }

    #docgen-status-bar {
        dock: bottom;
        height: 1;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
    }

    #status-project {
        color: $accent;
        margin-right: 2;
    }

    #status-model {
        color: $text-muted;
        margin-right: 2;
    }

    #status-hint {
        color: $text-disabled;
        text-style: italic;
    }

    #init-form-actions, #snap-form-actions {
        height: auto;
        margin-top: 1;
        margin-bottom: 1;
    }

    #init-form-actions Button, #snap-form-actions Button {
        margin-right: 1;
    }

    #init-form-output, #snap-form-output {
        height: auto;
        border: solid $surface-lighten-1;
        padding: 1;
        margin-top: 1;
    }

    .help-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
        text-align: center;
    }

    .help-text {
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Выход", show=True),
        Binding("d", "switch_tab('dashboard')", "Статус", show=True),
        Binding("i", "switch_tab('init')", "Init", show=True),
        Binding("s", "switch_tab('snapshot')", "Snapshot", show=True),
        Binding("w", "switch_tab('watch')", "Watch", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.state: Optional[ProjectState] = None
        self._load_state()

    def _load_state(self) -> None:
        root = find_project_root()
        if root:
            self.state = load_state(str(root))
            if self.state:
                self.SUB_TITLE = self.state.config.project_name

    def _get_help(self, tab_id: str) -> tuple[str, str]:
        helps = {
            "dashboard": (
                "Статус проекта",
                "Текущее состояние проекта docgen.\n\n"
                "Хоткеи:\n"
                "  d — Статус\n  i — Init\n  s — Snapshot\n  w — Watch\n  q — Выход"
            ),
            "init": (
                "Инициализация",
                "Создание .docgen.yaml.\n\n"
                "Обязательно:\n  • Repo URL\n\n"
                "Опционально:\n  • GITHUB_TOKEN env\n  • API Key\n  • Base URL\n  • Модель"
            ),
            "snapshot": (
                "Снэпшот",
                "Создать снэпшот документации.\n\n"
                "Флаги:\n  • -c: полный аудит\n  • -l: лог в файл"
            ),
            "watch": (
                "Watch",
                "Автообновление документации.\n\n"
                "Проверяет новые теги каждые N минут."
            ),
        }
        return helps.get(tab_id, ("Помощь", ""))

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-layout"):
            with Vertical(id="tab-content"):
                with TabbedContent(initial="dashboard"):
                    with TabPane("Статус", id="dashboard"):
                        yield self._dashboard()
                    with TabPane("Init", id="init"):
                        yield self._init_form()
                    with TabPane("Snapshot", id="snapshot"):
                        yield self._snapshot_form()
                    with TabPane("Watch", id="watch"):
                        yield self._watch_monitor()
            with Vertical(id="right-panel"):
                yield Label("Помощь", id="help-title", classes="help-title")
                yield Static("", id="help-text", classes="help-text")
        yield self._status_bar()

    def _dashboard(self) -> Vertical:
        root = find_project_root()
        repo = "—"
        last_tag = "—"
        snapshots = "0"
        model = "—"
        config_text = "Проект не инициализирован."

        if root:
            state = load_state(str(root))
            if state:
                cfg = state.config
                repo = cfg.git_repo.split("/")[-1] if cfg.git_repo else "—"
                model = cfg.llm_model
                config_text = (
                    f"Рабочая папка: {root}\n"
                    f"GIT: {cfg.git_repo}\n"
                    f"Модель: {cfg.llm_model}\n"
                    f"Max ходов: {cfg.max_turns}\n"
                    f"Токен (env): {cfg.github_token_env or '—'}"
                )
            release_map = load_release_map(str(root))
            if release_map and release_map.last_documented_release:
                last_tag = release_map.last_documented_release
            snaps = get_snapshot_versions(str(root))
            snapshots = str(len(snaps))

        return Vertical(
            Horizontal(
                Vertical(Label("Репозиторий", classes="card-title"), Label(repo, classes="card-value"), classes="card"),
                Vertical(Label("Последний тег", classes="card-title"), Label(last_tag, classes="card-value"), classes="card"),
                Vertical(Label("Снэпшотов", classes="card-title"), Label(snapshots, classes="card-value"), classes="card"),
                Vertical(Label("Модель", classes="card-title"), Label(model, classes="card-value"), classes="card"),
                id="dashboard-cards",
            ),
            Horizontal(
                Button("Snapshot", id="dash-btn-snapshot", variant="primary"),
                Button("Watch", id="btn-watch", variant="default"),
                Button("Версии", id="btn-versions", variant="default"),
                id="quick-actions",
            ),
            Vertical(
                Label("Конфигурация", classes="section-title"),
                Static(config_text),
                id="details-section",
            ),
        )

    def _init_form(self) -> Vertical:
        return Vertical(
            Label("Инициализация проекта", classes="section-title"),
            *self._form_row("Repo URL*", Input(placeholder="https://github.com/user/repo.git", id="init-input-repo"), "URL git-репозитория"),
            *self._form_row("GITHUB_TOKEN env", Input(placeholder="GITHUB_TOKEN (имя переменной)", id="init-input-token-env"), "Имя переменной окружения"),
            *self._form_row("API Key", Input(placeholder="sk-...", id="init-input-api-key", password=True), "Или оставьте пустым для OPENAI_API_KEY"),
            *self._form_row("Base URL", Input(placeholder="https://api.openai.com/v1", id="init-input-base-url"), "Для OpenAI-совместимых провайдеров"),
            *self._form_row("Модель", Input(placeholder="gpt-4o", id="init-input-model", value="gpt-4o")),
            *self._form_row("Max ходов", Input(placeholder="10", id="init-input-iterations", value="10", type="integer"), "Максимум ходов агента"),
            Horizontal(Button("Выполнить init", id="init-btn-submit", variant="primary"), Button("Очистить", id="init-btn-clear", variant="default"), id="init-form-actions"),
            Label("Результат:", classes="section-title"),
            Static("", id="init-form-output"),
        )

    def _form_row(self, label: str, widget: Input, hint: str = "") -> list:
        hint_widget = Label(hint, classes="hint") if hint else Label("", classes="hint", id="hint-hidden")
        return [
            Horizontal(
                Label(label),
                Vertical(widget, hint_widget),
                classes="form-row",
            )
        ]

    def _snapshot_form(self) -> Vertical:
        return Vertical(
            Label("Создание снэпшота", classes="section-title"),
            *self._form_row("Тег релиза", Input(placeholder="v1.0 (пусто — последний релиз)", id="snap-input-release"), "Конкретный тег или пусто"),
            Vertical(
                Checkbox("Полный аудит (-c)", id="snap-check-audit"),
                Checkbox("Логировать (-l)", id="snap-check-log"),
            ),
            *self._form_row("Max ходов", Input(placeholder="оставьте пустым", id="snap-input-iterations", type="integer")),
            Horizontal(Button("Запустить snapshot", id="snap-btn-submit", variant="primary"), Button("Очистить", id="snap-btn-clear", variant="default"), id="snap-form-actions"),
            Label("Результат:", classes="section-title"),
            Static("", id="snap-form-output"),
        )

    def _watch_monitor(self) -> Vertical:
        return Vertical(
            Label("Watch-монитор", classes="section-title"),
            *self._form_row("Интервал (мин)", Input(placeholder="10", id="input-interval", value="10", type="integer"), "Как часто проверять новые теги"),
            Horizontal(
                Button("Запустить", id="btn-start", variant="primary"),
                Button("Остановить", id="btn-stop", variant="error"),
                Button("Очистить лог", id="btn-clear-log", variant="default"),
                id="watch-actions",
            ),
            RichLog(id="watch-log", highlight=True, markup=True, max_lines=500),
            Horizontal(
                Label("Статус: остановлен", id="label-status"),
                id="status-bar",
            ),
        )

    def _status_bar(self) -> Horizontal:
        root = find_project_root()
        project = "не инициализирован"
        model = "—"
        if root:
            state = load_state(str(root))
            if state:
                project = state.config.project_name
                model = state.config.llm_model

        return Horizontal(
            Label(f"[bold]{project}[/]", id="status-project"),
            Label(f"модель: {model}", id="status-model"),
            Label("q:выход", id="status-hint"),
            id="docgen-status-bar",
        )

    def action_switch_tab(self, tab: str) -> None:
        tc = self.query_one(TabbedContent)
        tc.active = tab
        self._update_help(tab)

    def _update_help(self, tab_id: str) -> None:
        title, text = self._get_help(tab_id)
        title_w = self.query_one("#help-title")
        text_w = self.query_one("#help-text")
        if hasattr(title_w, 'update'):
            title_w.update(title)
        if hasattr(text_w, 'update'):
            text_w.update(text)

    @on(TabbedContent.TabActivated)
    def _on_tab_changed(self, event: TabbedContent.TabActivated) -> None:
        self._update_help(event.tab.id or "dashboard")

    def on_mount(self) -> None:
        """При монтировании обновить контекстную панель."""
        self._update_help("dashboard")

    def _refresh_state(self) -> None:
        """Перезагрузить состояние проекта."""
        root = find_project_root()
        if root:
            self.state = load_state(str(root))
            self.SUB_TITLE = self.state.config.project_name if self.state else "автоматическая документация"
        else:
            self.state = None

    # ── Init form handlers ──────────────────────────────────────────

    @on(Button.Pressed, "#init-btn-submit")
    def _on_init_submit(self) -> None:
        """Обработчик кнопки «Выполнить init»."""
        repo = self.query_one("#init-input-repo", Input).value.strip()
        if not repo:
            self.query_one("#init-form-output", Static).update(
                "[red]Ошибка:[/] Repo URL обязателен"
            )
            return

        api_key = self.query_one("#init-input-api-key", Input).value.strip() or None
        base_url = self.query_one("#init-input-base-url", Input).value.strip() or None
        model = self.query_one("#init-input-model", Input).value.strip() or None
        token_env = self.query_one("#init-input-token-env", Input).value.strip() or None
        iterations_str = self.query_one("#init-input-iterations", Input).value.strip()

        max_turns: int | None = None
        if iterations_str:
            try:
                max_turns = int(iterations_str)
            except ValueError:
                self.query_one("#init-form-output", Static).update(
                    "[red]Ошибка:[/] Max ходов должно быть целым числом"
                )
                return

        # Выводим название проекта из URL
        project_name = repo.rstrip(".git").split("/")[-1] if "/" in repo else repo

        try:
            state = init_project(
                git_repo=repo,
                llm_api_key=api_key,
                llm_model=model,
                llm_base_url=base_url,
                github_token_env=token_env,
                project_name=project_name,
                max_turns=max_turns,
            )
            self._refresh_state()
            self.query_one("#init-form-output", Static).update(
                f"[green]✓ Проект инициализирован[/]\n\n"
                f"  • Репозиторий: {state.config.git_repo}\n"
                f"  • Модель: {state.config.llm_model or '—'}\n"
                f"  • Max ходов: {state.config.max_turns}\n"
                f"  • Токен (env): {state.config.github_token_env or '—'}\n"
                f"\n[dim].docgen.yaml создан в {find_project_root()}[/]"
            )
        except Exception as exc:
            self.query_one("#init-form-output", Static).update(
                f"[red]Ошибка:[/] {exc}"
            )

    @on(Button.Pressed, "#init-btn-clear")
    def _on_init_clear(self) -> None:
        """Обработчик кнопки «Очистить»."""
        for widget_id in ("init-input-repo", "init-input-token-env", "init-input-api-key",
                          "init-input-base-url", "init-input-model", "init-input-iterations"):
            try:
                self.query_one(f"#{widget_id}", Input).value = ""
            except Exception:
                pass
        self.query_one("#init-form-output", Static).update("")
