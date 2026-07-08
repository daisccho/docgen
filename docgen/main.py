"""CLI docgen — init, snapshot, watch, versions."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import click

from docgen.agent import DocAgent
from docgen.config import find_project_root, init_project, load_state, save_state
from docgen.errors import DocAgentError
from docgen.git_analyzer import (
    get_snapshot_versions,
)
from docgen.models import ProjectState


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


# ── init ──────────────────────────────────────────────


@cli.command()
@click.option("--repo", required=True, help="URL git-репозитория (https://...)")
@click.option("--token-env", default=None,
              help="Имя переменной окружения с токеном доступа к git")
@click.option("--api-key", default=None, help="API-ключ LLM (или OPENAI_API_KEY в env)")
@click.option("--base-url", default=None, help="Base URL для OpenAI-совместимого API")
@click.option("--model", default="gpt-4o", help="Модель LLM")
@click.option("--project", default="default", help="Имя проекта")
@click.option("--iterations", "-i", default=None, type=int,
              help="Максимум ходов (по умолч. 10)")
def init(repo: str, token_env: Optional[str], api_key: Optional[str],
         base_url: Optional[str], model: str, project: str,
         iterations: Optional[int]) -> None:
    """Инициализировать проект docgen в текущей папке.

    Создаёт .docgen.yaml, клонирует репозиторий в .clone/.
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
        access_token_env=token_env,
        project_name=project,
        max_turns=iterations,
    )

    # Клонируем репозиторий
    _echo_info("Клонирование репозитория...")
    try:
        agent = DocAgent(state, verbose=True)
    except Exception as exc:
        _echo_warn(f"Ошибка клонирования: {exc}")
        _echo_info("Вы можете клонировать вручную: git clone --bare <URL> .clone")

    saved = find_project_root()
    _echo_ok(f"Проект инициализирован: {saved / '.docgen.yaml'}")
    click.echo()
    click.echo("Что дальше:")
    click.echo("  docgen snapshot           — создать первый снэпшот документации")
    click.echo("  docgen snapshot -c        — снэпшот + проверка актуальности")
    click.echo("  docgen watch -b main -t 5 — запустить автообновление каждые 5 мин")
    click.echo("  docgen versions           — список версий")


# ── snapshot ──────────────────────────────────────


@cli.command()
@click.option("--ref", "-r", default=None,
              help="Тег, хэш или ветка (v1.0, abc1234). По умолчанию HEAD")
@click.option("--check", "-c", is_flag=True,
              help="Проверить каждый .md на соответствие коду (полный аудит)")
@click.option("--iterations", "-i", default=None, type=int,
              help="Максимум ходов агента (вызовов terminal) на один .md")
@click.option("--log", "-l", is_flag=True, help="Логировать в logs/")
@click.option("--verbose", "-v", is_flag=True, help="Подробный вывод")
@click.pass_context
def snapshot(ctx: click.Context, ref: Optional[str],
             check: bool, iterations: Optional[int],
             log: bool, verbose: bool) -> None:
    """Создать снэпшот документации.

    Копирует все .md-файлы из репозитория в папку <commit_hash>/.
    По умолчанию — на HEAD. Через --ref можно указать тег, хэш или ветку.
    Флаг -c запускает LLM-аудит, -l включает лог в logs/.
    """
    _echo_title()
    state = _require_state()

    verbose = verbose or ctx.obj.get("verbose", False)
    agent = DocAgent(state, verbose=verbose, log=log)

    try:
        result = agent.snapshot(ref=ref, check=check, max_turns=iterations)
    except DocAgentError as exc:
        click.secho(f"❌ {exc}", fg="red", err=True)
        sys.exit(1)

    _echo_ok(f"Снэпшот создан: {result.commit_hash[:8]}")
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
@click.option("--branch", "-b", default="main", help="Ветка для отслеживания")
@click.option("--interval", "-t", default=10, type=int,
              help="Интервал проверки в минутах")
@click.option("--iterations", "-i", default=None, type=int,
              help="Максимум ходов (вызовов terminal) на один .md")
@click.option("--log", "-l", is_flag=True, help="Логировать в logs/")
@click.option("--verbose", "-v", is_flag=True, help="Подробный вывод")
@click.pass_context
def watch(ctx: click.Context, branch: str, interval: int,
          iterations: Optional[int], log: bool,
          verbose: bool) -> None:
    """Запустить демон автообновления документации.

    Каждые N минут проверяет ветку BRANCH, при новых коммитах
    генерирует обновлённую документацию в новой папке <commit_hash>/.
    -l включает лог в logs/.
    """
    _echo_title()
    state = _require_state()

    verbose = verbose or ctx.obj.get("verbose", False)

    click.echo(f"  Ветка: {branch}")
    click.echo(f"  Интервал: {interval} мин")
    click.echo(f"  Рабочая папка: {find_project_root()}")
    click.echo()

    try:
        agent = DocAgent(state, verbose=verbose, log=log)
        if iterations is not None:
            agent._max_turns = iterations
        agent.watch(branch=branch, interval=interval)
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
    state = _require_state()

    root = find_project_root()
    if not root:
        click.echo("Нет созданных версий.")
        return

    snaps = get_snapshot_versions(str(root))
    if not snaps:
        click.echo("Нет созданных версий.")
        _echo_info("Создайте первую: docgen snapshot")
        return

    click.echo(f"Версии ({len(snaps)}):")
    for s in snaps:
        click.echo(f"  • {s['hash']}")
    click.echo()
