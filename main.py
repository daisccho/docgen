"""CLI docgen — init, snapshot, watch, versions, tui."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import click

from docgen.agent import DocAgent
from docgen.config import (
    find_project_root,
    init_project,
    load_release_map,
    load_state,
    save_release_map,
    save_state,
)
from docgen.errors import DocAgentError
from docgen.git_analyzer import (
    fetch_tags,
    get_all_tags_with_hash,
    get_latest_tag,
    get_snapshot_versions,
    get_tag_commit_hash,
    sanitize_tag_name,
)
from docgen.models import ProjectState, ReleaseMap
from docgen.tui.commands.tui import tui as tui_cmd


def _require_state() -> ProjectState:
    """Загрузить .docgen.yaml или выйти с ошибкой."""
    state = load_state()
    if state is None:
        click.secho(
            "❌ Проект не инициализирован. Сначала: docgen init --repo <URL>",
            fg="red", err=True,
        )
        sys.exit(1)
    return state


def _echo_ok(msg: str) -> None:
    click.secho(f"  ✔ {msg}", fg="green")


def _echo_warn(msg: str) -> None:
    click.secho(f"  ⚠ {msg}", fg="yellow")


def _echo_info(msg: str) -> None:
    click.secho(f"  ℹ {msg}", fg="blue")


def _echo_title() -> None:
    click.echo()
    click.secho("╔══ docgen ══╗", bold=True)
    click.echo()


# ── CLI ──────────────────────────────────────────────


@click.group(invoke_without_command=False)
@click.option("--verbose", "-v", is_flag=True, help="Подробный вывод")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """docgen — автономная поддержка документации через ИИ-агент."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose


# ── tui ──────────────────────────────────────────────
cli.add_command(tui_cmd)


# ── init ──────────────────────────────────────────────


@cli.command()
@click.option("--repo", required=True, help="URL git-репозитория (https://...)")
@click.option("--github-token-env", default=None,
              help="Имя переменной окружения с GitHub-токеном")
@click.option("--api-key", default=None, help="API-ключ LLM (или OPENAI_API_KEY в env)")
@click.option("--base-url", default=None, help="Base URL для OpenAI-совместимого API")
@click.option("--model", default="gpt-4o", help="Модель LLM")
@click.option("--project", default="default", help="Имя проекта")
@click.option("--iterations", "-i", default=None, type=int,
              help="Максимум ходов (по умолч. 10)")
def init(repo: str, github_token_env: Optional[str], api_key: Optional[str],
         base_url: Optional[str], model: str, project: str,
         iterations: Optional[int]) -> None:
    """Инициализировать проект docgen в текущей папке.

    Создаёт .docgen.yaml, .release-map.yaml и клонирует репозиторий в .clone/.
    """
    _echo_title()

    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    base_url = base_url or os.environ.get("OPENAI_BASE_URL")

    if not api_key:
        _echo_warn("API-ключ не указан. LLM-генерация будет недоступна.")

    state = init_project(
        git_repo=repo,
        llm_api_key=api_key,
        llm_model=model,
        llm_base_url=base_url,
        github_token_env=github_token_env,
        project_name=project,
        max_turns=iterations,
    )

    # Создаём пустой release map
    save_release_map(ReleaseMap())

    # Клонируем репозиторий
    _echo_info("Клонирование репозитория...")
    try:
        agent = DocAgent(state, verbose=True)
    except Exception as exc:
        _echo_warn(f"Ошибка клонирования: {exc}")
        _echo_info("Вы можете клонировать вручную: git clone --bare <URL> .clone")

    saved = find_project_root()
    _echo_ok(f"Проект инициализирован: {saved / '.docgen.yaml'}")
    _echo_ok(f"Release map создан: {saved / '.release-map.yaml'}")
    click.echo()
    click.echo("Что дальше:")
    click.echo("  docgen snapshot           — создать первый снэпшот по последнему релизу")
    click.echo("  docgen snapshot -r v1.0   — снэпшот по конкретному тегу")
    click.echo("  docgen snapshot -c        — снэпшот + проверка актуальности")
    click.echo("  docgen watch -t 5         — следить за новыми релизами каждые 5 мин")
    click.echo("  docgen versions           — список версий")
    click.echo("  docgen tui                — TUI-интерфейс")


# ── snapshot ──────────────────────────────────────


@cli.command()
@click.option("--release", "-r", default=None,
              help="Тег релиза (v1.0, release-2024-01). По умолчанию — последний релиз")
@click.option("--check", "-c", is_flag=True,
              help="Проверить каждый .md на соответствие коду (полный аудит)")
@click.option("--iterations", "-i", default=None, type=int,
              help="Максимум ходов агента (вызовов terminal) на один .md")
@click.option("--log", "-l", is_flag=True, help="Логировать в logs/")
@click.option("--verbose", "-v", is_flag=True, help="Подробный вывод")
@click.pass_context
def snapshot(ctx: click.Context, release: Optional[str],
             check: bool, iterations: Optional[int],
             log: bool, verbose: bool) -> None:
    """Создать снэпшот документации по тегу релиза.

    Без -r — по последнему релизу (HEAD релизной ветки).
    С -r — по указанному тегу (например v1.0, release-2024-01).
    Копирует .md-файлы в папку <sanitized_tag_name>/.
    -c запускает LLM-аудит, -l включает лог в logs/.
    """
    _echo_title()
    state = _require_state()

    verbose = verbose or ctx.obj.get("verbose", False)
    agent = DocAgent(state, verbose=verbose, log=log)

    try:
        result = agent.snapshot(release_tag=release, check=check, max_turns=iterations)
    except DocAgentError as exc:
        click.secho(f"❌ {exc}", fg="red", err=True)
        sys.exit(1)

    label = result.release_tag or result.commit_hash[:8]
    _echo_ok(f"Снэпшот создан: {label}")
    _echo_info(f"Файлов скопировано: {result.docs_copied}")
    if result.docs_updated:
        _echo_info(f"Обновлено через LLM: {result.docs_updated}")
    if result.docs_added:
        _echo_info(f"Добавлено: {result.docs_added}")
    click.echo(f"\n📂 {result.output_dir}")

    if result.warnings:
        for w in result.warnings:
            _echo_warn(w)


# ── watch ──────────────────────────────────────────────


@cli.command()
@click.option("--interval", "-t", default=10, type=int,
              help="Интервал проверки новых релизов в минутах")
@click.option("--iterations", "-i", default=None, type=int,
              help="Максимум ходов (вызовов terminal) на один .md")
@click.option("--log", "-l", is_flag=True, help="Логировать в logs/")
@click.option("--verbose", "-v", is_flag=True, help="Подробный вывод")
@click.pass_context
def watch(ctx: click.Context, interval: int,
          iterations: Optional[int], log: bool,
          verbose: bool) -> None:
    """Запустить демон автообновления документации по релизам.

    Каждые N минут проверяет наличие новых тегов в репозитории,
    и при появлении нового релиза генерирует обновлённую документацию
    в новой папке <sanitized_tag_name>/.
    -l включает лог в logs/.
    """
    _echo_title()
    state = _require_state()

    verbose = verbose or ctx.obj.get("verbose", False)

    click.echo(f"  Интервал: {interval} мин")
    click.echo(f"  Рабочая папка: {find_project_root()}")
    click.echo()

    try:
        agent = DocAgent(state, verbose=verbose, log=log)
        if iterations is not None:
            agent._max_turns = iterations
        agent.watch(interval=interval)
    except KeyboardInterrupt:
        click.echo("\n  ✋ Остановлено пользователем.")
        sys.exit(0)
    except DocAgentError as exc:
        click.secho(f"❌ {exc}", fg="red", err=True)
        sys.exit(1)


# ── versions ──────────────────────────────────────────


@cli.command()
def versions() -> None:
    """Список созданных версий документации (папок-снэпшотов)."""
    _echo_title()
    _require_state()

    root = find_project_root()
    if not root:
        click.echo("Нет созданных версий.")
        return

    snaps = get_snapshot_versions(str(root))
    if not snaps:
        click.echo("Нет созданных версий.")
        _echo_info("Создайте первую: docgen snapshot")
        return

    release_map = load_release_map()
    current = release_map.last_documented_release
    click.echo(f"Версии ({len(snaps)}):")
    for s in snaps:
        marker = " ★" if s["name"] == current else ""
        click.echo(f"  {s['name']}{marker}")
    click.echo()
