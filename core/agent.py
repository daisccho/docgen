"""DocAgent — ядро: snapshot, watch, инкрементальное обновление."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import yaml
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from core.config import (
    find_project_root,
    load_release_map,
    save_release_map,
    save_state,
)
from core.doc_generator import DocGenerator
from core.errors import DocAgentError, RateLimitError, RefNotFoundError
from core.git_analyzer import (
    ensure_clone,
    ensure_ref_available,
    extract_file_from_repo,
    fetch_repo,
    fetch_tags,
    get_deleted_md_files,
    get_diff_files,
    get_all_tags_with_hash,
    get_head_hash,
    get_latest_tag,
    get_new_md_files,
    get_raw_diffs,
    get_snapshot_versions,
    read_file_from_repo,
    sanitize_tag_name,
    scan_md_files,
)
from core.models import GenerationResult, ProjectState

CHECKPOINT_FILENAME = ".docgen-resume.yaml"
CLONE_DIR = ".clone"

# Определение инструментов LLM
TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": "Выполнить shell-команду в директории репозитория. "
                           "Используй для изучения кода, grep, cat файлов.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell-команда для выполнения",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_summary",
            "description": "Прочитать SUMMARY.md — файл с общим описанием проекта: "
                           "архитектура, основные модули, назначение, принятые "
                           "соглашения. Вызови этот инструмент в самом начале, "
                           "чтобы быстро понять, что из себя представляет проект, "
                           "не изучая его с нуля через терминал. "
                           "Если SUMMARY.md ещё не создан — будет сообщение об этом.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_summary",
            "description": "Записать или обновить SUMMARY.md — файл с общим описанием "
                           "проекта. Вызывай после того, как изучил код и архитектуру "
                           "через терминал, чтобы сохранить понимание проекта для "
                           "последующих запусков. Передавай содержимое напрямую "
                           "в параметре content — не пиши во временные файлы. "
                           "Пиши на русском языке.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Полный текст SUMMARY.md. Опиши: назначение "
                                       "проекта, архитектуру, основные модули и их "
                                       "взаимодействие, ключевые технологии, принятые "
                                       "соглашения по документации.",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_changelog",
            "description": "Прочитать CHANGELOG.md — историю изменений документации. "
                           "Если файла нет — будет сообщено. Вызови в начале, чтобы "
                           "узнать, какие записи уже есть, и не создавать дубликаты.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_changelog_entry",
            "description": "Добавить новую запись в CHANGELOG.md. "
                           "Заголовок записи сформируется автоматически. "
                           "Не нужно читать старый CHANGELOG — просто вызови "
                           "этот инструмент с текстом записи (без заголовка). "
                           "Если запись для этой пары версий уже существует — "
                           "вернёт ошибку.",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_ref": {
                        "type": "string",
                        "description": "Старый тег (от которого, напр. v0.80.2)",
                    },
                    "to_ref": {
                        "type": "string",
                        "description": "Новый тег (до которого, напр. v0.80.10)",
                    },
                    "content": {
                        "type": "string",
                        "description": "Текст новой записи для CHANGELOG.md. "
                                       "Только тело записи, без заголовка ## [...].",
                    },
                },
                "required": ["from_ref", "to_ref", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "collect_changelog",
            "description": "Собрать список изменений между двумя релизами. "
                           "Читает CHANGELOG.md файлы, git commit messages и "
                           "собирает консолидированный changelog. "
                           "Вызывай в начале фазы update или audit, чтобы понять, "
                           "какие изменения произошли в коде.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]


@dataclass
class PlannerResult:
    """Результат работы агента-планировщика с контекстом для последующих фаз."""
    files_to_update: set[str] = field(default_factory=set)
    summary_content: str = ""
    changelog_content: str = ""
    project_overview: str = ""


class DocAgent:
    """Агент управления документацией."""

    def __init__(self, state: ProjectState, verbose: bool = False,
                 log: bool = False, log_file: Optional[str] = None) -> None:
        self.state = state
        self.verbose = verbose
        self._log_enabled = log
        self._log_file_path = log_file


        # Рабочая директория — там, где лежит .docgen.yaml
        root = find_project_root()
        self._work_dir = str(root) if root else os.getcwd()
        self._clone_dir = os.path.join(self._work_dir, CLONE_DIR)

        # LLM-клиент
        cfg = state.config
        self._generator: Optional[DocGenerator] = None
        api_key = cfg.llm_api_key or os.environ.get("OPENAI_API_KEY")
        if api_key:
            self._generator = DocGenerator(
                api_key=api_key,
                model=cfg.llm_model,
                base_url=cfg.llm_base_url,
            )

        # Токен доступа к git (из переменной окружения)
        self._token: Optional[str] = None
        if cfg.github_token_env:
            self._token = os.environ.get(cfg.github_token_env)

        # Максимум ходов инструментального цикла
        self._max_turns = cfg.max_turns

        self._log_enabled = log
        self._log_file: Optional[Any] = None
        self._summary_path = os.path.join(self._work_dir, "SUMMARY.md")
        self._changelog_path = os.path.join(self._work_dir, "CHANGELOG.md")
        self._resuming = False

# ── Чекпойнт и возобновление ─────────────────────────

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        """Определить, является ли ошибка исчерпанием лимитов LLM."""
        err_msg = str(exc).lower()
        keywords = [
            "rate limit", "quota", "limit exceeded", "credits",
            "daily limit", "insufficient", "upstream request failed",
            "429", "too many requests",
        ]
        return any(kw in err_msg for kw in keywords)

    def _checkpoint_path(self) -> str:
        """Путь к файлу чекпойнта."""
        return os.path.join(self._work_dir, CHECKPOINT_FILENAME)

    def _save_checkpoint(
        self,
        *,
        phase: str,
        command: str = "watch",
        from_ref: Optional[str] = None,
        to_ref: Optional[str] = None,
        completed_files: Optional[list[str]] = None,
        release_tag: Optional[str] = None,
        is_check: bool = False,
    ) -> None:
        """Сохранить чекпойнт для возобновления."""
        data: dict[str, Any] = {
            "command": command,
            "phase": phase,
            "completed_files": completed_files or [],
            "log_file": self._log_file_path or "",
            "timestamp": datetime.now().isoformat(),
        }
        if command == "watch":
            data["from_ref"] = from_ref
            data["to_ref"] = to_ref
        elif command == "snapshot":
            data["release_tag"] = release_tag
            data["is_check"] = is_check
        path = self._checkpoint_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
            if self.verbose:
                self._log(f"  [agent]   💾 Чекпойнт сохранён: {path}")
        except Exception as exc:
            if self.verbose:
                self._log(f"  [agent]   ⚠ Не удалось сохранить чекпойнт: {exc}")

    def _load_checkpoint(self) -> Optional[dict[str, Any]]:
        """Загрузить чекпойнт, если существует."""
        path = self._checkpoint_path()
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                return None
            return data
        except Exception:
            return None

    def _clear_checkpoint(self) -> None:
        """Удалить чекпойнт."""
        path = self._checkpoint_path()
        try:
            if os.path.isfile(path):
                os.remove(path)
                if self.verbose:
                    self._log(f"  [agent]   ✅ Чекпойнт удалён")
        except Exception as exc:
            if self.verbose:
                self._log(f"  [agent]   ⚠ Не удалось удалить чекпойнт: {exc}")

    def _resume_watch(self, checkpoint: dict[str, Any]) -> None:
        """Продолжить прерванный watch из чекпойнта."""
        from_ref = checkpoint.get("from_ref", "")
        to_ref = checkpoint.get("to_ref", "")
        phase = checkpoint.get("phase", "update_docs")
        completed = checkpoint.get("completed_files", [])

        self._log(f"  [agent]   ▶ Продолжение watch: {from_ref} → {to_ref}, фаза: {phase}")

        try:
            if phase == "update_docs" or phase == "changelog":
                old_snapshot_dir = os.path.join(
                    self._work_dir, sanitize_tag_name(from_ref)
                )
                new_snapshot_dir = os.path.join(
                    self._work_dir, sanitize_tag_name(to_ref)
                )
                if not os.path.isdir(new_snapshot_dir):
                    os.makedirs(new_snapshot_dir, exist_ok=True)

                if phase == "update_docs":
                    self._update_docs(
                        old_snapshot_dir=old_snapshot_dir,
                        new_snapshot_dir=new_snapshot_dir,
                        from_ref=from_ref,
                        to_ref=to_ref,
                        copy_new_from_repo=True,
                        skip_files=completed,
                    )

                self._ensure_changelog(
                    old_tag=from_ref,
                    latest_tag=to_ref,
                    updated_files=None,
                    added_files=None,
                    removed_files=None,
                )

            if phase in ("update_docs", "changelog", "summary"):
                old_snapshot_dir = os.path.join(
                    self._work_dir, sanitize_tag_name(from_ref)
                )
                new_snapshot_dir = os.path.join(
                    self._work_dir, sanitize_tag_name(to_ref)
                )
                self._ensure_summary(
                    snapshot_dir=new_snapshot_dir,
                    ref=to_ref,
                )

            # Сохраняем release_map
            release_map = load_release_map()
            release_map.last_documented_release = to_ref
            save_release_map(release_map)
            self._clear_checkpoint()
            self._log("✅ Прерванный watch успешно завершён")

        except RateLimitError:
            # Повторный рейт-лимит — обновляем чекпойнт
            new_completed = completed
            if phase == "update_docs":
                new_completed = completed
            self._save_checkpoint(
                command="watch",
                phase=phase,
                from_ref=from_ref,
                to_ref=to_ref,
                completed_files=new_completed,
            )
            self._log("⚠ Повторный рейт-лимит — запустите watch ещё раз позже")

    def _resume_snapshot(self, checkpoint: dict[str, Any]) -> GenerationResult:
        """Продолжить прерванный snapshot из чекпойнта."""
        release_tag = checkpoint.get("release_tag", "")
        phase = checkpoint.get("phase", "summary")
        is_check = checkpoint.get("is_check", False)
        completed = checkpoint.get("completed_files", [])

        self._log(f"  [agent]   ▶ Продолжение snapshot: {release_tag}, фаза: {phase}")

        # Определяем директорию снэпшота
        snapshot_dir = os.path.join(self._work_dir, sanitize_tag_name(release_tag))

        # Если снэпшот-директории нет — создаём (безопасно повторять)
        if not os.path.isdir(snapshot_dir):
            os.makedirs(snapshot_dir, exist_ok=True)

        result = GenerationResult(
            commit_hash=release_tag,
            output_dir=snapshot_dir,
        )

        try:
            if phase == "summary":
                self._ensure_summary(
                    snapshot_dir=snapshot_dir,
                    ref=release_tag,
                )

            if phase in ("summary", "full_audit") and is_check:
                audit = self._full_audit(
                    snapshot_dir=snapshot_dir,
                    ref=release_tag,
                    skip_files=completed,
                )
                result.docs_updated = audit.docs_updated
                result.docs_copied = audit.docs_copied

            if phase == "full_audit" and not is_check:
                pass

            self._clear_checkpoint()
            self._log("✅ Прерванный snapshot успешно завершён")
            return result

        except RateLimitError:
            new_completed = completed
            if phase == "full_audit":
                new_completed = completed
            self._save_checkpoint(
                command="snapshot",
                phase=phase,
                release_tag=release_tag,
                is_check=is_check,
                completed_files=new_completed,
            )
            self._log("⚠ Повторный рейт-лимит — запустите snapshot ещё раз позже")
            return result

    # ── Открытые методы ─────────────────────────────────

    def snapshot(
        self,
        release_tag: Optional[str] = None,
        check: bool = False,
        max_turns: Optional[int] = None,
    ) -> GenerationResult:
        """Создать снэпшот документации.

        Без release_tag — на последнем релизном теге. С release_tag — строго на
        указанном теге (например v1.0.0). Ветки и SHA не поддерживаются.

        Копирует все .md-файлы из репозитория в <work_dir>/<hash>/.
        Если check=True, дополнительно запускает аудит.
        """
        self._log_open("snapshot")

        # Проверка чекпойнта для возобновления
        checkpoint = self._load_checkpoint()
        if checkpoint and checkpoint.get("command") == "snapshot":
            cp_tag = checkpoint.get("release_tag")
            target_tag = release_tag or None
            if cp_tag == target_tag:
                self._log(
                    f"Обнаружен прерванный snapshot для {cp_tag} — продолжаю..."
                )
                self._resuming = True
                return self._resume_snapshot(checkpoint)

        ref = release_tag

        # ── Клон: создать или открыть ──
        if self.verbose:
            self._log(f"  [agent]   ⏳ Клонирование {self.state.config.git_repo}...")
            t0 = time.monotonic()

        if ref:
            # Явно указанное значение должно быть именно Git-тегом.
            ensure_clone(
                self.state.config.git_repo, self._clone_dir, self._token,
                verbose=self.verbose,
            )
            fetch_tags(self._clone_dir, self._token)
            tags = get_all_tags_with_hash(self._clone_dir)
            if ref not in tags:
                raise RefNotFoundError(
                    f"Релизный тег не найден: {ref}. "
                    "Ветки и SHA-коммиты не поддерживаются."
                )
            if self.verbose:
                self._log(f"  [agent]   ✅ Клон готов ({time.monotonic() - t0:.1f}с)")
            if self.verbose:
                self._log(f"  [agent]   🔍 Поиск тега {ref}...")
                t0 = time.monotonic()
            commit_hash = tags[ref]
            if self.verbose:
                self._log(f"  [agent]   ✅ Тег найден ({time.monotonic() - t0:.1f}с)")
        else:
            # Без ref — ищем последний релиз через GitHub API или Git-теги.
            ensure_clone(
                self.state.config.git_repo, self._clone_dir, self._token,
                verbose=self.verbose,
            )
            fetch_tags(self._clone_dir, self._token)
            latest = get_latest_tag(
                self._clone_dir,
                github_token=self._token,
                repo_url=self.state.config.git_repo,
            )
            if not latest:
                raise DocAgentError(
                    "В репозитории отсутствуют релизные теги. "
                    "Создание snapshot остановлено."
                )
            ref = latest
            if self.verbose:
                self._log(f"  [agent]   🔍 Последний релиз: {ref}")
                t0 = time.monotonic()
            commit_hash = ensure_ref_available(self._clone_dir, ref)
            if self.verbose:
                self._log(f"  [agent]   ✅ Ref найден ({time.monotonic() - t0:.1f}с)")

        if self.verbose:
            label = ref or "HEAD"
            self._log(f"\n  [agent] ▶ Создание снэпшота на {label} → {commit_hash[:8]}")

        md_files = scan_md_files(self._clone_dir, treeish=commit_hash)
        dir_name = sanitize_tag_name(ref) if ref else commit_hash
        snapshot_dir = os.path.join(self._work_dir, dir_name)

        # Копируем все .md
        copied = 0
        total = len(md_files)
        for rel_path in md_files:
            extract_file_from_repo(self._clone_dir, commit_hash, rel_path,
                                   os.path.join(snapshot_dir, rel_path))
            copied += 1
            if self.verbose:
                if total <= 20:
                    self._log(f"  [agent]   📄 {rel_path}")
                elif copied % 10 == 0 or copied == total:
                    self._log(f"  [agent]   📄 Прогресс: {copied}/{total}")

        if self.verbose:
            self._log(f"  [agent]   Скопировано {copied} .md-файлов в {snapshot_dir}")

        result = GenerationResult(
            commit_hash=commit_hash,
            output_dir=snapshot_dir,
            release_tag=ref,
            docs_copied=copied,
        )

        # ── Генерация/обновление SUMMARY при каждом snapshot ──
        if self._generator:
            n_summary = max_turns if max_turns is not None else self._max_turns
            n_summary = max(5, n_summary)
            if self.verbose:
                label = "Создание" if not os.path.isfile(self._summary_path) else "Обновление"
                self._log(f"\n  [agent] ▶ {label} SUMMARY.md (до {n_summary} ходов)")
            try:
                self._ensure_summary(snapshot_dir, ref=commit_hash, max_turns=n_summary)
            except RateLimitError:
                self._save_checkpoint(
                    phase="summary",
                    command="snapshot",
                    release_tag=ref,
                    is_check=check,
                )
                raise

        if check and self._generator:
            n_audit = max_turns if max_turns is not None else self._max_turns
            if self.verbose:
                self._log(f"\n  [agent] ▶ Полный аудит документации (до {n_audit} ходов)")
            try:
                audit = self._full_audit(snapshot_dir, ref=commit_hash, max_turns=n_audit)
            except RateLimitError:
                self._save_checkpoint(
                    phase="full_audit",
                    command="snapshot",
                    release_tag=ref,
                    is_check=check,
                )
                raise
            audit.release_tag = ref
            result = audit
        elif check and not self._generator:
            if self.verbose:
                self._log(f"  [agent]   ⚠ LLM не настроен — аудит пропущен")
            result.warnings.append("LLM не настроен — аудит не выполнялся")

        # Сохраняем в release-map, если указан тег релиза
        if ref:
            release_map = load_release_map()
            release_map.last_documented_release = ref
            release_map.releases[ref] = commit_hash
            save_release_map(release_map)

        self._log_close()
        save_state(self.state)

        return result

    def watch(self, interval: int) -> None:
        """Запустить демон: fetch → обновление → сон."""
        self._log_open("watch")

        # Проверка чекпойнта для возобновления
        checkpoint = self._load_checkpoint()
        if checkpoint and checkpoint.get("command") == "watch":
            self._log("Обнаружен прерванный процесс watch — продолжаю...")
            self._resuming = True
            self._resume_watch(checkpoint)
            return

        if self.verbose:
            self._log(f"  [agent]   ⏳ Открытие {self.state.config.git_repo}...")
            t0 = time.monotonic()
        ensure_clone(
            self.state.config.git_repo, self._clone_dir, self._token,
            verbose=self.verbose,
        )
        if self.verbose:
            self._log(f"  [agent]   ✅ Клон готов ({time.monotonic() - t0:.1f}с)")
        if self.verbose:
            self._log(f"\n  [agent] ▶ Watch запущен, интервал {interval} мин")
            self._log(f"  [agent]   Рабочая папка: {self._work_dir}")

        while True:
            try:
                self._watch_tick()
            except RateLimitError:
                self._log("⚠ Достигнут лимит LLM — прогресс сохранён")
                self._log("Для продолжения запустите watch повторно")
                break
            except Exception as exc:
                self._log(f"  [agent]   ⚠ Ошибка: {exc}")

            self._write_heartbeat()

            if self.verbose:
                now = datetime.now().strftime("%H:%M:%S")
                self._log(f"\n  [agent] 💤 Сон {interval} мин")
            time.sleep(interval * 60)

    # ── Heartbeat ────────────────────────────────────────

    def _write_heartbeat(self) -> None:
        """Обновить heartbeat-метку для проверки живучести watch-процесса."""
        try:
            hb_path = self._work_dir / ".watch_heartbeat"
            hb_path.write_text(datetime.now().isoformat())
        except Exception:
            pass

    # ── Внутренние методы ────────────────────────────────

    def _watch_tick(self) -> None:
        """Один такт watch: fetch → проверить теги → обновить."""
        if self.verbose:
            self._log(f"  [agent]   ⏳ Fetch тегов {self.state.config.git_repo}...")
            t0 = time.monotonic()
        fetch_repo(self._clone_dir, self._token)
        fetch_tags(self._clone_dir, self._token)
        if self.verbose:
            self._log(f"  [agent]   ✅ Fetch готов ({time.monotonic() - t0:.1f}с)")

        # ── Проверка наличия новых тегов ──
        if self.verbose:
            self._log(f"  [agent]   🔍 Проверка наличия тегов...")

        latest_tag = get_latest_tag(
            self._clone_dir,
            github_token=self._token,
            repo_url=self.state.config.git_repo,
        )

        if not latest_tag:
            if self.verbose:
                self._log(f"  [agent]   Тегов нет — пропускаем")
            return

        old_tag = None
        release_map = load_release_map()
        if release_map:
            old_tag = release_map.last_documented_release

        if old_tag == latest_tag:
            if self.verbose:
                self._log(f"  [agent]   Релизы не изменились ({latest_tag}), пропускаем")
            return

        if old_tag is None:
            # Первый запуск — делаем полный snapshot
            if self.verbose:
                self._log(f"  [agent]   Первый запуск — делаем snapshot")
            self.snapshot(release_tag=latest_tag, check=True)
            return

        if self.verbose:
            self._log(f"  [agent]   Новый релиз: {old_tag} → {latest_tag}")

        old_snapshot_dir = os.path.join(self._work_dir, sanitize_tag_name(old_tag))
        new_snapshot_dir = os.path.join(self._work_dir, sanitize_tag_name(latest_tag))

        if not os.path.isdir(old_snapshot_dir):
            if self.verbose:
                self._log(f"  [agent]   ⚠ Снэпшот {old_tag} не найден — делаем snapshot")
            self.snapshot(release_tag=latest_tag, check=True)
            return

        result, planner_result = self._update_docs(
            old_snapshot_dir=old_snapshot_dir,
            new_snapshot_dir=new_snapshot_dir,
            from_ref=old_tag,
            to_ref=latest_tag,
            copy_new_from_repo=True,
            max_turns=self._max_turns,
        )

        # ── CHANGELOG.md документации ──
        if self.verbose:
            self._log(f"\n  [agent] ▶ Создание CHANGELOG.md: {old_tag} → {latest_tag}")

        # Собираем .md-файлы, изменённые разработчиками (из git diff между версиями)
        dev_md_proc = subprocess.run(
            ["git", "diff", f"{old_tag}..{latest_tag}", "--name-only", "--", "*.md"],
            capture_output=True, text=True, cwd=self._clone_dir,
        )
        developer_md_files = [
            f for f in dev_md_proc.stdout.strip().split("\n") if f
        ] if dev_md_proc.returncode == 0 else []
        # Исключаем служебные файлы из списка изменённых разработчиками
        _skip_md_files = {"SUMMARY.md", "CHANGELOG.md", "SAMPLE.md", "SAMPLES.md"}
        developer_md_files = [f for f in developer_md_files if f not in _skip_md_files]

        try:
            self._ensure_changelog(
                old_tag, latest_tag,
                max_turns=self._max_turns,
                updated_files=result.updated_files,
                added_files=result.added_files,
                removed_files=result.removed_files,
                developer_md_files=developer_md_files,
                planner_result=planner_result,
            )
        except RateLimitError:
            self._save_checkpoint(
                phase="changelog",
                command="watch",
                from_ref=old_tag,
                to_ref=latest_tag,
            )
            raise

        # Обновляем SUMMARY в соответствии с изменениями
        if self._generator and self.verbose:
            self._log(f"\n  [agent] ▶ Обновление SUMMARY.md для {latest_tag}")
        try:
            if self._generator:
                self._ensure_summary(
                    new_snapshot_dir, ref=latest_tag,
                    max_turns=max(5, self._max_turns),
                    planner_result=planner_result,
                )
        except RateLimitError:
            self._save_checkpoint(
                phase="summary",
                command="watch",
                from_ref=old_tag,
                to_ref=latest_tag,
            )
            raise

        # Сохраняем состояние в release-map
        release_map = load_release_map()
        if release_map is not None:
            release_map.last_documented_release = latest_tag
            # Определяем хэш коммита для тега
            try:
                commit_hash = subprocess.run(
                    ["git", "rev-list", "-n1", latest_tag],
                    capture_output=True, text=True, timeout=15,
                    cwd=self._clone_dir,
                ).stdout.strip()
                if commit_hash:
                    release_map.releases[latest_tag] = commit_hash
            except Exception:
                pass
            save_release_map(release_map)

        if self.verbose:
            self._log(f"\n  [agent] ✔ Обновлено: {result.docs_updated}, "
                  f"скопировано: {result.docs_copied}, "
                  f"добавлено: {result.docs_added}")

    def _update_docs(
        self,
        old_snapshot_dir: str,
        new_snapshot_dir: str,
        from_ref: str,
        to_ref: str,
        copy_new_from_repo: bool = False,
        max_turns: int = 10,
        skip_files: Optional[list[str]] = None,
    ) -> tuple[GenerationResult, PlannerResult]:
        """Инкрементальное обновление документации.

        Args:
            old_snapshot_dir: Папка с предыдущей версией документации.
            new_snapshot_dir: Куда писать новую версию.
            from_ref: Git-реф начала.
            to_ref: Git-реф конца.
            copy_new_from_repo: Если True — новые .md копируются из
                репозитория, а не из старого снэпшота.

        Returns:
            Кортеж (GenerationResult, PlannerResult).
            PlannerResult содержит кэшированный контекст для
            последующих фаз (changelog, summary).
        """
        commit_hash = get_head_hash(self._clone_dir, to_ref)
        result = GenerationResult(commit_hash=commit_hash, output_dir=new_snapshot_dir)

        # ── Шаг 1: diff ──
        diff_files = get_diff_files(self._clone_dir, from_ref, to_ref)
        changed_code = [f for f in diff_files if not f.path.endswith(".md")]

        if self.verbose:
            self._log(f"  [agent]   Diff {from_ref}..{to_ref}:"
                  f"{len(diff_files)} файлов")

        # ── Шаг 2: сканируем .md-файлы из старого снэпшота и из репозитория ──
        old_md_files: list[str] = self._scan_snapshot_md(old_snapshot_dir)
        new_repo_md = set(scan_md_files(self._clone_dir, to_ref))

        # ── Предзагрузка SUMMARY.md и CHANGELOG.md ──
        summary_content = ""
        if os.path.isfile(self._summary_path):
            with open(self._summary_path, encoding="utf-8") as f:
                summary_content = f.read()
        changelog_content = ""
        if os.path.isfile(self._changelog_path):
            with open(self._changelog_path, encoding="utf-8") as f:
                changelog_content = f.read()

        # ── Шаг 3: LLM-маппинг (какие .md обновлять) ──
        planner_result = PlannerResult()
        if self._generator and changed_code:
            planner_result = self._map_changes_to_docs_agentic(
                changed_code, old_md_files, new_repo_md,
                from_ref, to_ref, max_turns=max_turns,
                summary_content=summary_content,
                changelog_content=changelog_content,
            )
        elif self._generator and not changed_code:
            if self.verbose:
                self._log(f"  [agent]   Нет изменений кода — ничего не обновляем")
            # Всё равно передаём кэш для changelog/summary
            planner_result = PlannerResult(
                summary_content=summary_content,
                changelog_content=changelog_content,
            )
        elif not self._generator:
            if self.verbose:
                self._log(f"  [agent]   ⚠ LLM не настроен — копируем без изменений")

        docs_to_update = planner_result.files_to_update

        # Пропускаем уже обработанные файлы (при возобновлении)
        skip_set = set(skip_files or [])
        if skip_set:
            docs_to_update = [d for d in docs_to_update if d not in skip_set]
            if self.verbose:
                self._log(f"  [agent]   Пропущено {len(skip_set)} уже обработанных файлов")

        # ── Шаг 4: копируем старый снэпшот ──
        # Сначала копируем всё из старого снэпшота
        self._copy_snapshot(old_snapshot_dir, new_snapshot_dir)
        result.docs_copied = len(old_md_files)

        # ── Шаг 5: обновляем файлы, которые затронул LLM ──
        raw_diffs = get_raw_diffs(self._clone_dir, from_ref, to_ref)
        updated = 0
        if self.verbose and docs_to_update:
            self._log(f"  [agent]   ── Обновление файлов ──")
        for rel_path in docs_to_update:
            if self.verbose:
                self._log(f"  [agent]   📄 {rel_path}")
            old_content = self._read_doc_file(new_snapshot_dir, rel_path)
            if old_content is None:
                # Файл не в снэпшоте — может, из репозитория?
                if copy_new_from_repo and rel_path in new_repo_md:
                    try:
                        old_content = read_file_from_repo(self._clone_dir, to_ref, rel_path)
                    except Exception:
                        continue
                else:
                    continue

            # Собираем релевантные diff'ы
            relevant = {
                p: d for p, d in raw_diffs.items()
                if self._is_relevant_diff(p, rel_path)
            }
            if not relevant:
                relevant = raw_diffs

            try:
                new_content = self._run_agentic_update(
                    old_doc=old_content,
                    code_diffs=relevant,
                    file_path=rel_path,
                    max_turns=max_turns,
                    from_ref=from_ref,
                    to_ref=to_ref,
                    planner_result=planner_result,
                )
            except RateLimitError:
                completed_so_far = docs_to_update[:docs_to_update.index(rel_path)]
                self._save_checkpoint(
                    phase="update_docs",
                    command="watch",
                    from_ref=from_ref,
                    to_ref=to_ref,
                    completed_files=completed_so_far,
                )
                raise
            if new_content:
                dest = Path(new_snapshot_dir) / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(new_content, encoding="utf-8")
                updated += 1
                if self.verbose:
                    self._log(f"  [agent]   ✏️ {rel_path} (обновлён агентом)")
            else:
                if self.verbose:
                    self._log(f"  [agent]   ✓ {rel_path} (актуален, без изменений)")

        result.docs_updated = updated

        # ── Шаг 6: новые .md из репозитория ──
        added = 0
        if copy_new_from_repo:
            if self.verbose and new_repo_md:
                self._log(f"  [agent]   ── Новые файлы из репозитория ──")
            for md_path in sorted(new_repo_md):
                dest = Path(new_snapshot_dir) / md_path
                if not dest.exists():
                    try:
                        content = read_file_from_repo(self._clone_dir, to_ref, md_path)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_text(content, encoding="utf-8")
                        added += 1
                        if self.verbose:
                            self._log(f"  [agent]   ➕ {md_path} (новый .md)")
                    except Exception as exc:
                        if self.verbose:
                            self._log(f"  [agent]   ⚠ Не удалось скопировать {md_path}: {exc}")
        result.docs_added = added

        # ── Шаг 7: удалённые .md (в репозитории файл удалён) ──
        deleted_md = get_deleted_md_files(self._clone_dir, from_ref, to_ref)
        removed = 0
        if self.verbose and deleted_md:
            self._log(f"  [agent]   ── Удалённые файлы ──")
        for md_path in deleted_md:
            dest = Path(new_snapshot_dir) / md_path
            if dest.exists():
                dest.unlink()
                removed += 1
                if self.verbose:
                    self._log(f"  [agent]   🗑 {md_path} (удалён из репозитория)")
        result.docs_removed = removed

        return result, planner_result

    # ── LLM-маппинг ──────────────────────────────────────

    def _map_changes_to_docs(
        self,
        changed_files: list,
        doc_files: list[str],
    ) -> set[str]:
        """LLM определяет, какие .md нужно обновить на основе изменённых файлов."""
        if not self._generator:
            return set()

        llm_client = self._generator._client
        if not llm_client:
            return set()

        # Сокращаем список для контекста
        changed_sample = "\n".join(
            f"  {'+' if f.change_type.value == 'added' else '~'} {f.path} "
            f"(+{f.added_lines}/-{f.deleted_lines})"
            for f in changed_files[:50]
        )
        total_changed = len(changed_files)

        doc_sample = "\n".join(f"  {d}" for d in doc_files[:100])
        total_docs = len(doc_files)

        prompt = (
            f"Ты — аналитик документации. Твоя задача — определить, "
            f"какие .md-файлы из документации нужно обновить "
            f"на основе изменений в коде.\n\n"
            f"Изменённые файлы кода ({total_changed} всего, показаны первые 50):\n"
            f"{changed_sample}\n\n"
            f"Файлы документации ({total_docs} всего, показаны первые 100):\n"
            f"{doc_sample}\n\n"
            f"Верни JSON-массив путей .md-файлов, которые нужно обновить. "
            f"Отвечай ТОЛЬКО JSON, без пояснений.\n"
            f"Пример: [\"docs/guide.md\", \"README.md\"]\n"
            f"Если ничего обновлять не нужно: []"
        )

        try:
            if self.verbose:
                self._log(f"  [agent]   🤖 LLM-маппинг: какие .md обновить...")
                t0 = time.monotonic()
            resp = llm_client.chat.completions.create(
                model=self._generator.model,
                messages=[
                    {"role": "system",
                     "content": "Ты — аналитик документации. Отвечай ТОЛЬКО JSON."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=2000,
                temperature=0.1,
            )
            if self.verbose:
                self._log(f"  [agent]   ✅ LLM-маппинг готов"
        f"({time.monotonic() - t0:.1f}с)")
            text = resp.choices[0].message.content.strip()

            # Извлекаем JSON
            json_match = re.search(r"\[.*?\]", text, re.DOTALL)
            if json_match:
                import json
                paths = json.loads(json_match.group())
                if isinstance(paths, list):
                    # Фильтруем только существующие
                    valid = {p for p in paths if p in doc_files}
                    if self.verbose:
                        self._log(f"  [agent]   📋 LLM выбрал: {paths}")
                        filtered = set(paths) - valid
                        if filtered:
                            self._log(f"  [agent]   ⚠ Не найдено в снэпшоте: {sorted(filtered)}")
                        if valid:
                            self._log(f"  [agent]   ✏️ Будет обновлено: {sorted(valid)}")
                        else:
                            self._log(f"  [agent]   ✓ Ничего не требует обновления")
                    return valid
                elif self.verbose:
                    self._log(f"  [agent]   ⚠ LLM вернул не массив: {paths!r}")
            elif self.verbose:
                preview = text[:500].replace("\n", " ")
                self._log(f"  [agent]   ⚠ Не удалось извлечь JSON из ответа LLM:"
        f"\"{preview}…\"")
        except Exception as exc:
            if self.verbose:
                self._log(f"  [agent]   ⚠ LLM-маппинг: {exc}")

        if self.verbose:
            self._log(f"  [agent]   ✓ Ничего не требует обновления (пустой результат)")
        return set()

    def _is_relevant_diff(self, changed_path: str, doc_path: str) -> bool:
        """Проверить, релевантен ли изменённый файл кода для данного .md."""
        # Простая эвристика: если путь кода — подпуть .md или наоборот
        # Например, docs/installation.md и src/install/...
        # LLM всё равно решила маппинг — здесь просто фильтруем шум
        code_base = os.path.splitext(changed_path)[0].replace("/", ".")
        doc_base = os.path.splitext(doc_path)[0].replace("/", ".")
        # Если есть общая подстрока достаточной длины
        common = os.path.commonprefix([code_base, doc_base])
        return len(common) > 3

    # ── Агентный LLM-маппинг ────────────────────────────

    def _map_changes_to_docs_agentic(
        self,
        changed_files: list,
        old_md_files: list[str],
        new_md_files: set[str],
        from_ref: str,
        to_ref: str,
        max_turns: int = 10,
        summary_content: str = "",
        changelog_content: str = "",
    ) -> PlannerResult:
        """Агентный цикл: LLM с доступом к терминалу анализирует diff
        и решает, какие .md-файлы требуют обновления.

        Возвращает PlannerResult с путями файлов и контекстом для
        последующих фаз (воркеры, changelog, summary).
        """
        if not self._generator:
            return PlannerResult()
        client = self._generator._client
        if not client:
            return PlannerResult()

        max_turns = max(3, max_turns)

        # ── Формируем сводку diff ──
        changed_sample = "\n".join(
            f"  {'+' if f.change_type.value == 'added' else '~'} {f.path} "
            f"(+{f.added_lines}/-{f.deleted_lines})"
            for f in changed_files[:80]
        )
        total_changed = len(changed_files)

        md_sample = "\n".join(f"  {d}" for d in old_md_files[:80])
        total_md = len(old_md_files)

        new_md_list = sorted(new_md_files - set(old_md_files))
        deleted_md_list = sorted(set(old_md_files) - new_md_files)

        new_md_text = "\n".join(f"  + {p}" for p in new_md_list[:20]) if new_md_list else "  (нет)"
        deleted_md_text = "\n".join(f"  - {p}" for p in deleted_md_list[:20]) if deleted_md_list else "  (нет)"

        system_prompt = (
            "Ты — аналитик документации. Твоя задача — определить, "
            "какие .md-файлы нужно обновить на основе изменений в коде.\n\n"
            "Контекст проекта уже предоставлен — тебе не нужно вызывать "
            "read_summary или collect_changelog для получения информации.\n\n"
            "У тебя есть доступ к терминалу:\n"
            "  • terminal — текущая папка уже является bare git-"
            f"репозиторием {self._clone_dir}. Не выполняй cd и не ищи "
            "рабочую копию; используй git diff, git show и git ls-tree.\n\n"
            "Правила:\n"
            "1. Если изменения кода не затрагивают API, интерфейсы или "
            "описанную функциональность — не обновляй .md.\n"
            "2. Если изменения минимальны (исправление опечатки, "
            "рефакторинг без изменения сигнатур) — не обновляй.\n"
            "3. Обрати внимание на новые файлы в репозитории — "
            "возможно, для них нужна документация.\n"
            "4. Если файл .md удалён из репозитория — не помечай его "
            "как требующий обновления.\n"
            "5. В конце верни ТОЛЬКО JSON без пояснений в формате:\n"
            '    {"project_overview": "2-3 предложения о проекте: '
            'архитектура, ключевые пакеты, назначение",\n'
            '     "files": ["path/to/file1.md", "path/to/file2.md"]}\n'
            '6. Пример: {"project_overview": "Проект представляет собой...", '
            '"files": ["docs/guide.md", "README.md"]}\n'
            "7. Если ничего не нужно обновлять: "
            '{"project_overview": "...", "files": []}\n'
            "8. ВАЖНО: у тебя ограниченный бюджет ходов. Не трать всё на "
            "изучение — обязательно оставь 1-2 хода, чтобы выдать "
            "финальный JSON. Если исследуешь diff и .md — делай это "
            "быстро и целенаправленно.\n"
        )

        # Добавляем контекст из SUMMARY и CHANGELOG
        context_section = ""
        if summary_content:
            context_section += (
                "\n### Содержимое SUMMARY.md\n\n"
                f"{summary_content[:5000]}\n"
            )
        if changelog_content:
            context_section += (
                "\n### Содержимое CHANGELOG.md\n\n"
                f"{changelog_content[:5000]}\n"
            )

        user_prompt = (
            f"### Git diff {from_ref[:8]}..{to_ref[:8]}\n\n"
            f"Всего изменённых файлов: {total_changed}\n"
            f"(показаны первые 80):\n{changed_sample}\n\n"
            f"### Файлы документации\n\n"
            f"В старом снэпшоте ({total_md} всего, первые 80):\n{md_sample}\n\n"
            f"Новые .md в репозитории:\n{new_md_text}\n\n"
            f"Удалённые .md из репозитория:\n{deleted_md_text}\n\n"
            f"{context_section}\n"
            f"Определи, какие .md требуют обновления. "
            f"Используй терминал для изучения кода и diff'ов."
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        if self.verbose:
            self._log(f"  [agent]   🤖 Агентный маппинг: {total_changed} файлов кода →"
        f"{total_md} .md")

        content = self._run_tool_loop(client, messages, "LLM-маппинг", max_turns,
                                      from_ref=from_ref, to_ref=to_ref)
        if not content:
            if self.verbose:
                self._log(f"  [agent]   ⚠ Агентный маппинг вернул пустой ответ")
            return PlannerResult(
                summary_content=summary_content,
                changelog_content=changelog_content,
            )

        # Извлекаем JSON из ответа — поддерживаем новый формат (объект с overview)
        # и старый (просто массив)
        files: set[str] = set()
        overview = ""

        # Пробуем распарсить как JSON-объект (новый формат)
        obj_match = re.search(r"\{.*\}", content, re.DOTALL)
        if obj_match:
            try:
                data = json.loads(obj_match.group())
                if isinstance(data, dict):
                    overview = data.get("project_overview", "") or ""
                    file_list = data.get("files", [])
                    if isinstance(file_list, list):
                        files = {p for p in file_list
                                 if p in old_md_files or p in new_md_files}
                elif isinstance(data, list):
                    # Старый формат — просто массив
                    files = {p for p in data
                             if p in old_md_files or p in new_md_files}
            except Exception:
                pass

        # Fallback: ищем массив (старый формат "[]")
        if not files:
            arr_match = re.search(r"\[.*?\]", content, re.DOTALL)
            if arr_match:
                try:
                    data = json.loads(arr_match.group())
                    if isinstance(data, list):
                        files = {p for p in data
                                 if p in old_md_files or p in new_md_files}
                except Exception:
                    pass

        if self.verbose:
            if files:
                self._log(f"  [agent]   📋 Агент выбрал: {sorted(files)}")
            else:
                self._log(f"  [agent]   ✓ Ничего не требует обновления")

        return PlannerResult(
            files_to_update=files,
            summary_content=summary_content,
            changelog_content=changelog_content,
            project_overview=overview,
        )

    # ── Инструментальный цикл LLM ─────────────────────────

    def _run_agentic_update(
        self,
        old_doc: str,
        code_diffs: dict[str, str],
        file_path: str,
        max_turns: int = 10,
        from_ref: Optional[str] = None,
        to_ref: Optional[str] = None,
        planner_result: Optional[PlannerResult] = None,
    ) -> Optional[str]:
        """Обновить .md-файл через LLM с доступом к терминалу.

        LLM может выполнять команды в репозитории (.clone/), чтобы
        изучить код, проверить структуру, найти зависимости и только
        потом сгенерировать обновлённую документацию.

        Если передан planner_result, воркер получает предварительно
        загруженный контекст (SUMMARY, CHANGELOG, сводку проекта)
        и не вызывает read_summary/collect_changelog.

        max_turns — сколько ходов (вызовов terminal) даётся агенту.

        Returns:
            Текст обновлённого .md или None при ошибке.
        """
        if not self._generator:
            return None
        client = self._generator._client
        if not client:
            return None

        max_turns = max(10, max_turns)

        # Формируем диффы для контекста
        diffs_text = self._format_diffs_for_prompt(code_diffs)

        # Контекст от планировщика (если есть)
        planner_context = ""
        if planner_result:
            if planner_result.project_overview:
                planner_context += (
                    f"Сводка проекта:\n{planner_result.project_overview}\n\n"
                )
            if planner_result.summary_content:
                planner_context += (
                    f"Содержимое SUMMARY.md:\n"
                    f"{planner_result.summary_content[:3000]}\n\n"
                )
            if planner_result.changelog_content:
                planner_context += (
                    f"Содержимое CHANGELOG.md:\n"
                    f"{planner_result.changelog_content[:3000]}\n\n"
                )

        system_prompt = (
            "Ты — технический писатель с доступом к терминалу.\n"
            "Обновляй .md-файл документации на основе изменений в коде.\n\n"
            "Контекст проекта (от планировщика) — не вызывай "
            "read_summary или collect_changelog:\n"
            f"{planner_context}"
            "Доступные инструменты:\n"
            "  • terminal — текущая папка уже является bare git-"
            f"репозиторием {self._clone_dir}. Используй git show/diff/"
            "ls-tree без cd, чтобы:\n"
            "  • Изучить, как устроен изменившийся код\n"
            "  • Проверить экспортируемые функции/классы\n"
            "  • Найти соответствие между diff и документом\n"
            "  • Прочитать README или другие .md для контекста\n\n"
            "Когда будешь готов — верни обновлённый Markdown-документ "
            "строго между отдельными строками DOCGEN_DOCUMENT_START и "
            "DOCGEN_DOCUMENT_END. Не вызывай инструмент, если он не нужен.\n\n"
            "Правила:\n"
            "1. Сохраняй стиль, структуру и терминологию оригинала.\n"
            "2. Обновляй только разделы, которые затрагивает diff.\n"
            "3. Если изменений нет — верни документ как есть.\n"
            "4. Пиши на русском языке, если не указано иное.\n"
            "5. Верни ПОЛНЫЙ обновлённый Markdown-документ.\n"
            "6. Рассуждения выполняй во внутренних шагах. В финальном "
            "ответе не добавляй пояснений или вводного текста.\n"
            "7. ВАЖНО: у тебя ограниченный бюджет ходов. Не трать всё на "
            "изучение кода — обязательно оставь 1-2 хода, чтобы выдать "
            "обновлённый документ. Если diff незначительный — можно "
            "сразу вернуть ответ без изучения кода.\n"
        )

        user_prompt = (
            f"Файл: {file_path}\n\n"
            f"### Текущая версия документации\n\n"
            f"{old_doc[:12000]}\n\n"
            f"### Изменения кода (git diff)\n\n"
            f"{diffs_text}\n\n"
            f"Обнови документацию. Можешь использовать terminal."
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Воркеру даём только terminal — read_summary/collect_changelog не нужны
        worker_tools = [t for t in TOOL_DEFS
                        if t["function"]["name"] == "terminal"]
        response = self._run_tool_loop(
            client, messages, file_path, max_turns,
            from_ref=from_ref, to_ref=to_ref,
            tools=worker_tools,
        )
        content = self._extract_document_response(response)
        if not content or len(content) < 50:
            return old_doc
        return content

    # ── Инструментальный цикл LLM ─────────────────────────

    def _run_terminal(self, command: str) -> str:
        """Выполнить shell-команду в директории клона репозитория.

        Возвращает stdout (до 8000 символов) или описание ошибки.
        """
        write_violation = self._terminal_write_violation(command)
        if write_violation:
            return f"[ERROR: изменяющая команда заблокирована: {write_violation}]"

        # Дополнительный чёрный список разрушительных команд
        dangerous = [
            r"rm\s.*-rf\s+(/|\*|\.|~)",   # rm -rf /, rm -rf *, rm -rf ., rm -rf ~
            r"rm\s.*\s/(dev|proc|sys|etc|bin)",  # rm ... /dev /proc ...
            r">\s*/dev/(sd|hd|nvme|mmc)",   # > /dev/sda, > /dev/nvme0
            r"mkfs\.",                      # mkfs.ext4, mkfs.ntfs ...
            r"dd\s+if=",                    # dd if=/dev/...
            r":\(\)\s*\{\s*:\|:&\s*\}\s*;:",  # fork bomb
            r"sudo\s+(rm|mkfs|dd|shutdown|reboot|halt)",
            r"chmod\s.*777\s/",
            r">\s*/etc/",
        ]
        cmd_lower = command.lower()
        for pattern in dangerous:
            if re.search(pattern, cmd_lower):
                return f"[ERROR: опасная команда заблокирована]"

        try:
            start = time.monotonic()
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                cwd=self._clone_dir,
            )
            elapsed = time.monotonic() - start
            output = result.stdout
            if result.returncode != 0:
                if result.stderr:
                    output += f"\n--- STDERR ---\n{result.stderr[-2000:]}"
                output += f"\n[exit code: {result.returncode}]"
            else:
                if result.stderr:
                    output += f"\n--- STDERR ---\n{result.stderr[-2000:]}"
            if self.verbose:
                self._log(f"  [agent]     ⏱ {elapsed:.1f}с")
            return output[:8000]
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            if self.verbose:
                self._log(f"  [agent]     ⏱ {elapsed:.1f}с (timeout)")
            return "[ERROR: команда выполнялась дольше 30 секунд]"
        except Exception as exc:
            if self.verbose:
                self._log(f"  [agent]     ⚠ Ошибка: {exc}")
            return f"[ERROR: {exc}]"

    @staticmethod
    def _terminal_write_violation(command: str) -> Optional[str]:
        """Вернуть причину блокировки команды, способной менять состояние."""
        lowered = command.lower()
        without_safe_stderr = re.sub(
            r"2\s*>\s*(?:nul|/dev/null|&1)", "", lowered
        )
        if re.search(r"(?:>>?|<)", without_safe_stderr):
            return "перенаправление ввода/вывода"

        mutating_patterns = [
            r"(?:^|[;&|]\s*)(?:rm|del|erase|rmdir|rd|mv|move|cp|copy|"
            r"ren|rename|mkdir|md|touch|tee)\b",
            r"\b(?:set-content|add-content|out-file|new-item|remove-item|"
            r"move-item|copy-item|clear-content)\b",
            r"(?:^|[;&|]\s*)(?:python|python3|py|powershell|pwsh|node|"
            r"ruby|perl)\b",
            r"\bgit\s+(?:add|am|apply|checkout|cherry-pick|clean|commit|"
            r"config(?!\s+--get)|merge|mv|push|rebase|reset|restore|rm|"
            r"switch|tag)\b",
            r"\bsed\b[^\r\n]*\s-i(?:\s|$)",
            r"\b(?:curl|wget|invoke-webrequest|start-bitstransfer)\b",
        ]
        for pattern in mutating_patterns:
            if re.search(pattern, lowered):
                return "команда записи или запуска интерпретатора"
        return None

    def _format_cmd_line(self, cmd: str) -> list[str]:
        """Отформатировать команду с переносом по ширине терминала."""
        import shutil, textwrap
        term_width = shutil.get_terminal_size((80, 20)).columns
        prefix = "  [agent]     🔧 $ "
        cont_prefix = " " * len(prefix)
        wrap_width = term_width - len(prefix)
        if wrap_width < 40:
            wrap_width = 40
        lines = textwrap.wrap(cmd, width=wrap_width)
        if not lines:
            return [f"{prefix}"]
        result = [f"{prefix}{lines[0]}"]
        for line in lines[1:]:
            result.append(f"{cont_prefix}{line}")
        return result

    @staticmethod
    def _format_diffs_for_prompt(code_diffs: dict[str, str]) -> str:
        """Собрать code diffs в текст для промпта (с лимитом ~6000 символов)."""
        parts: list[str] = []
        total = 0
        for path, diff_text in code_diffs.items():
            chunk = f"=== {path} ===\n{diff_text[:3000]}"
            if total + len(chunk) > 6000:
                chunk = chunk[:6000 - total]
            parts.append(chunk)
            total += len(chunk)
            if total >= 6000:
                break
        return "\n\n".join(parts) if parts else "- Изменений кода не обнаружено."

    # ── Общий инструментальный цикл ────────────────────────

    def _run_tool_loop(
        self,
        client: Any,
        messages: list[dict[str, Any]],
        file_path: str,
        max_turns: int = 10,
        from_ref: Optional[str] = None,
        to_ref: Optional[str] = None,
        tools: Optional[list[dict]] = None,
    ) -> Optional[str]:
        """Выполнить цикл LLM с инструментами.

        Возвращает финальный текст ответа или None.
        """
        if tools is None:
            tools = TOOL_DEFS
        for turn in range(max_turns):
            if self.verbose:
                self._log(f"  [agent]     🤖 Ход {turn+1}/{max_turns}: запрос к LLM...")
            try:
                resp = client.chat.completions.create(
                    model=self._generator.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    max_tokens=8192,
                    timeout=120,
                )
            except Exception as exc:
                if self.verbose:
                    self._log(f"  [agent]     ⚠ LLM ошибка: {exc}")
                if self._is_rate_limit_error(exc):
                    raise RateLimitError(str(exc)) from exc
                return None

            msg = resp.choices[0].message

            if self.verbose:
                usage = resp.usage
                if usage:
                    self._log(f"  [agent]     ✅ LLM ответил ("

                          f"prompt: {usage.prompt_tokens}, "
                          f"completion: {usage.completion_tokens})")
                else:
                    self._log(f"  [agent]     ✅ LLM ответил")

            # Показываем рассуждения, если модель их вернула
            self._show_reasoning(msg)

            # Если нет tool_calls — это финальный ответ
            if not msg.tool_calls:
                if self.verbose:
                    content_preview = (msg.content or "")[:80].replace("\n", " ")
                    self._log(f"  [agent]     📝 Финальный ответ:"
        f"\"{content_preview}…\"")
                return (msg.content or "").strip()

            assistant_msg = msg.to_dict() if hasattr(msg, 'to_dict') else {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
            messages.append(assistant_msg)

            for tc in msg.tool_calls:
                if tc.function.name == "terminal":
                    try:
                        args = json.loads(tc.function.arguments)
                        cmd = args.get("command", "")
                    except (json.JSONDecodeError, KeyError):
                        result = "[ERROR: невалидные аргументы]"
                    else:
                        if self.verbose:
                            for line in self._format_cmd_line(cmd):
                                self._log(line)
                        result = self._run_terminal(cmd)
                        if self.verbose:
                            self._log(f"  [agent]     ✅ Команда выполнена"
                                  f"({len(result)} символов)")
                elif tc.function.name == "read_summary":
                    try:
                        if os.path.isfile(self._summary_path):
                            with open(self._summary_path, encoding="utf-8") as f:
                                content = f.read()
                            result = f"Содержимое SUMMARY.md ({len(content)} символов):\n\n{content}"
                        else:
                            result = "SUMMARY.md ещё не создан. Изучи проект через терминал, затем вызови write_summary, чтобы сохранить описание."
                    except Exception as exc:
                        result = f"[ERROR: {exc}]"
                    if self.verbose:
                        self._log("  [agent]     📖 SUMMARY.md прочитан")

                elif tc.function.name == "write_summary":
                    try:
                        args = json.loads(tc.function.arguments)
                        content = args.get("content", "")
                        with open(self._summary_path, "w", encoding="utf-8") as f:
                            f.write(content)
                        result = f"SUMMARY.md записан ({len(content)} символов)."
                    except (json.JSONDecodeError, KeyError) as exc:
                        result = f"[ERROR: невалидные аргументы: {exc}]"
                    except Exception as exc:
                        result = f"[ERROR: {exc}]"
                    if self.verbose:
                        self._log(f"  [agent]     📝 SUMMARY.md записан")

                elif tc.function.name == "read_changelog":
                    try:
                        if os.path.isfile(self._changelog_path):
                            with open(self._changelog_path, encoding="utf-8") as f:
                                content = f.read()
                            result = f"Содержимое CHANGELOG.md ({len(content)} символов):\n\n{content}"
                        else:
                            result = "CHANGELOG.md ещё не создан. Создай новую запись."
                    except Exception as exc:
                        result = f"[ERROR: {exc}]"

                elif tc.function.name == "submit_changelog_entry":
                    try:
                        args = json.loads(tc.function.arguments)
                        from_ref = args.get("from_ref", "")
                        to_ref = args.get("to_ref", "")
                        content = args.get("content", "")
                        if not from_ref or not to_ref:
                            result = "[ERROR: from_ref и to_ref обязательны]"
                            break

                        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        header_line = f"## [{now_str}] {from_ref} -> {to_ref}"

                        # Проверка на дубликаты
                        if os.path.isfile(self._changelog_path):
                            with open(self._changelog_path, encoding="utf-8") as f:
                                existing = f.read()
                            marker = f"{from_ref} -> {to_ref}"
                            if marker in existing:
                                result = (
                                    f"[ERROR: запись для {marker} уже существует в "
                                    f"CHANGELOG.md. Новая запись не добавлена.]"
                                )
                                break

                        # Формируем полную запись
                        entry = f"\n{header_line}\n\n{content.strip()}\n"

                        header = "# CHANGELOG документации\n\n"
                        if not os.path.isfile(self._changelog_path):
                            with open(self._changelog_path, "w", encoding="utf-8") as f:
                                f.write(header)
                        with open(self._changelog_path, "a", encoding="utf-8") as f:
                            f.write(entry)
                        result = f"Запись добавлена в CHANGELOG.md ({len(content)} символов)."
                    except (json.JSONDecodeError, KeyError) as exc:
                        result = f"[ERROR: невалидные аргументы: {exc}]"
                    except Exception as exc:
                        result = f"[ERROR: {exc}]"

                elif tc.function.name == "collect_changelog":
                    if from_ref and to_ref:
                        result = self._collect_changelog(from_ref, to_ref)
                    else:
                        result = "[ERROR: ref'ы не заданы — collect_changelog требует контекст]"
                    if self.verbose:
                        self._log(f"  [agent]     📋 Changelog собран")

                else:
                    result = f"[ERROR: неизвестный инструмент {tc.function.name}]"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result[:8000],
                })

        # Превышен лимит ходов
        if self.verbose:
            self._log(f"  [agent]     ⚠ Лимит ходов ({max_turns}) превышен для {file_path}"
                  f" — нет финального ответа")
        return None

    def _show_reasoning(self, msg: Any) -> None:
        """Показать рассуждения LLM, если модель их вернула.

        Поддерживает:
        - reasoning_content (DeepSeek R1, OpenAI o-series)
        - model_extra.reasoning_content

        Формат: 🤔 первая мысль, │ разделители, └ последняя.
        """
        reasoning = None
        if hasattr(msg, "reasoning_content") and msg.reasoning_content:
            reasoning = msg.reasoning_content
        elif hasattr(msg, "model_extra"):
            extra = msg.model_extra or {}
            reasoning = extra.get("reasoning_content") or extra.get("reasoning")

        if reasoning:
            import shutil, textwrap
            term_width = shutil.get_terminal_size((80, 20)).columns
            # Минус префикс "  [agent]     " (13) + "🤔/│/└ " (2) = 15
            width = min(term_width - 15, 80)
            if width < 40:
                width = 40
            lines = reasoning.strip().split("\n")
            # Разбиваем на параграфы
            paragraphs = []
            buf = []
            for line in lines:
                if line.strip():
                    buf.append(line)
                elif buf:
                    paragraphs.append(" ".join(buf))
                    buf = []
            if buf:
                paragraphs.append(" ".join(buf))

            if not paragraphs:
                return

            # Склеиваем параграфы в плоский список строк с pipe-форматом
            out_lines: list[str] = []
            for i, para in enumerate(paragraphs):
                wrapped = textwrap.fill(para, width=width).split("\n")
                if i == 0:
                    out_lines.append(f"  [agent]     \x1b[33m🤔 {wrapped[0]}\x1b[0m")
                    for j, sub in enumerate(wrapped[1:]):
                        if i == len(paragraphs) - 1 and j == len(wrapped) - 2:
                            out_lines.append(f"  [agent]     \x1b[33m└ {sub}\x1b[0m")
                        else:
                            out_lines.append(f"  [agent]     \x1b[33m│ {sub}\x1b[0m")
                elif i == len(paragraphs) - 1:
                    out_lines.append(f"  [agent]     \x1b[33m│\x1b[0m")
                    out_lines.append(f"  [agent]     \x1b[33m└ {wrapped[0]}\x1b[0m")
                    for sub in wrapped[1:]:
                        out_lines.append(f"  [agent]     \x1b[33m  {sub}\x1b[0m")
                else:
                    out_lines.append(f"  [agent]     \x1b[33m│\x1b[0m")
                    out_lines.append(f"  [agent]     \x1b[33m│ {wrapped[0]}\x1b[0m")
                    for sub in wrapped[1:]:
                        out_lines.append(f"  [agent]     \x1b[33m│ {sub}\x1b[0m")

            # Лимит 30 строк, остальное — "… (ещё N строк)"
            MAX_LINES = 30
            if len(out_lines) > MAX_LINES:
                for line in out_lines[:MAX_LINES]:
                    self._log(line)
                self._log(f"  [agent]     \x1b[90m… (ещё {len(out_lines) - MAX_LINES} строк)\x1b[0m")
            else:
                for line in out_lines:
                    self._log(line)

    def _collect_changelog(self, from_ref: str, to_ref: str) -> str:
        """Собрать изменения между двумя ref'ами.

        1. git log --oneline между ref'ами
        2. Ищет changelog-подобные файлы (*CHANGELOG*, *CHANGES*, *RELEASE_NOTES*,
           *HISTORY*, *NEWS*, *History*) и сравнивает их между ref'ами
        3. Если файлы не менялись — показывает их содержимое в to_ref
        """
        parts: list[str] = []
        clone = self._clone_dir

        if self.verbose:
            self._log("  [agent]     ⏳ Сбор коммитов и CHANGELOG...")
        t0 = time.monotonic()

        # 1. Коммиты между ref'ами (с телом сообщения)
        try:
            log_output = subprocess.run(
                ["git", "log", "--format=%h %s%n%b", "--no-decorate",
                 f"{from_ref}..{to_ref}"],
                capture_output=True, text=True, timeout=30, cwd=clone,
            )
            commits = log_output.stdout.strip()
            if commits:
                n = len(commits.split("\n"))
                parts.append(f"### Коммиты ({n} шт.)\n\n```\n{commits[:4000]}\n```")
        except Exception as exc:
            parts.append(f"### Коммиты\n\n[Ошибка: {exc}]")

        # 2. Ищем changelog-подобные файлы
        changelog_patterns = [
            "*CHANGELOG*", "*CHANGES*", "*RELEASE_NOTES*",
            "*HISTORY*", "*NEWS*", "*History*",
            "*changelog*", "*changes*", "*release*notes*",
        ]
        found_changelogs: list[str] = []
        # Максимум символов на весь changelog
        MAX_CHARS = 8000

        try:
            for pattern in changelog_patterns:
                ls = subprocess.run(
                    ["git", "ls-tree", "--name-only", "-r", to_ref, "--", pattern],
                    capture_output=True, text=True, timeout=15, cwd=clone,
                )
                for p in ls.stdout.strip().split("\n"):
                    p = p.strip()
                    if p and p not in found_changelogs:
                        found_changelogs.append(p)

            # Сортируем для стабильности вывода
            found_changelogs.sort()

            if found_changelogs:
                parts.append(f"### Найденные changelog-файлы\n\n{chr(10).join(f'- {p}' for p in found_changelogs)}")

                for i, path in enumerate(found_changelogs):
                    # Проверяем общий размер перед добавлением
                    current_size = sum(len(p) for p in parts)
                    if current_size > MAX_CHARS:
                        remaining = len(found_changelogs) - i
                        parts.append(f"… (ещё {remaining} changelog-файлов пропущено)")
                        break
                    # Сначала проверяем, изменился ли файл
                    diff = subprocess.run(
                        ["git", "diff", f"{from_ref}..{to_ref}", "--", path],
                        capture_output=True, text=True, timeout=15, cwd=clone,
                    )
                    if diff.stdout.strip():
                        text = diff.stdout[:2000]
                        parts.append(f"### {path} (изменён)\n\n```diff\n{text}\n```")
                    else:
                        # Не изменился — показываем текущее содержимое
                        content = subprocess.run(
                            ["git", "show", f"{to_ref}:{path}"],
                            capture_output=True, text=True, timeout=15, cwd=clone,
                        )
                        if content.stdout.strip():
                            text = content.stdout[:1500]
                            parts.append(f"### {path} (без изменений)\n\n```\n{text[:1000]}\n```")
                            if len(text) > 1000:
                                parts.append(f"  … (ещё {len(text) - 1000} символов)")
        except Exception as exc:
            parts.append(f"[Ошибка при чтении changelog: {exc}]")

        elapsed = time.monotonic() - t0
        if self.verbose:
            self._log(f"  [agent]     ✅ Changelog собран ({elapsed:.1f}с)")
        return "\n\n".join(parts) if parts else "[Изменений между ref'ами не найдено]"

    def _ensure_summary(
        self, snapshot_dir: str, ref: str, max_turns: int = 10,
        planner_result: Optional[PlannerResult] = None,
    ) -> None:
        """Создать или обновить SUMMARY.md, запустив LLM-агента для изучения проекта.

        Если передан planner_result, агент получает кэшированный
        CHANGELOG и сводку проекта.

        Args:
            snapshot_dir: Директория снэпшота (.md-файлы).
            ref: Git-реф (хэш коммита) для которого создаётся снэпшот.
            max_turns: Максимум ходов агента.
        """
        if not self._generator:
            return
        client = self._generator._client
        if not client:
            return

        summary_exists = os.path.isfile(self._summary_path)
        if summary_exists:
            with open(self._summary_path, encoding="utf-8") as f:
                old_summary = f.read()
        else:
            old_summary = None

        system_prompt = (
            "Ты — аналитик проекта. Твоя задача — изучить код в репозитории "
            "и написать SUMMARY.md — подробное описание проекта.\n\n"
            "У тебя есть инструменты:\n"
            "  • terminal — выполняй команды в папке "
            f"{self._clone_dir} (bare-репозиторий).\n"
            "    ВАЖНО: это bare-репозиторий (только git-объекты,\n"
            "    без рабочей директории). Команды ls, cat там покажут только\n"
            "    метаданные git, НЕ исходный код.\n"
            f"    Ты документируешь версию {ref}. Чтобы прочитать файл\n"
            "    из этой версии кода, всегда используй:\n"
            f"      git show {ref}:<путь>\n\n"
            "  • read_summary — прочитать текущий SUMMARY.md (если есть).\n"
            "  • write_summary — сохрани описание проекта. Вызови этот "
            "инструмент после изучения.\n\n"
            "Директория снэпшота (../<хэш>/) содержит ТОЛЬКО .md-файлы "
            "документации. Для изучения кода используй git show.\n\n"
            "SUMMARY.md должен иметь структуру:\n"
            "1. ## Назначение проекта — 2-3 абзаца: чем занимается, "
            "ключевая философия, основная ценность.\n"
            "2. ## Архитектура и пакеты — для каждого публикуемого "
            "пакета/модуля: название, назначение, структура, основные "
            "компоненты/классы, ключевые зависимости. Если проект — "
            "монорепозиторий, группируй по назначению.\n"
            "3. ## Ключевые технологии и зависимости — язык, рантайм, "
            "сборка, тестирование, основные SDK/библиотеки. Без деталей "
            "реализации, только стек.\n"
            "4. ## Структура репозитория — ascii-дерево с краткими "
            "пояснениями.\n"
            "5. ## Принятые соглашения — подразделы: Код и стиль, "
            "Архитектурные решения, Документация.\n"
            "6. ## О проекте — мета-информация по репозиторию. "
            "Держатель (holder) — название организации или пользователя, "
            "которому принадлежит репозиторий на GitHub, "
            "без дополнительных персоналий. Лицензия (из LICENSE), "
            "сайт проекта (из README.md, package.json homepage), "
            "npm-пакеты, сообщество (Discord и т.п.), "
            "актуальная версия: \"<тег> (<короткий хэш 8 символов>) "
            "от <дата коммита>\". Все элементы — единый список "
            "без дубликатов.\n"
            "    Держателя бери из git remote URL: "
            "`git config --get remote.origin.url` — "
            "извлеки owner между github.com/ и /repo-name.\n"
            "    Лицензию читай из LICENSE "
            "(строка 'MIT License', 'Apache', 'GPL' и т.п.) — "
            "не путай с Copyright (юридической атрибуцией автора).\n"
            "    README.md, package.json читай через "
            "git show {ref}:<путь>.\n"
            "    Актуальную версию (тег, короткий хэш, дату) получи через:\n"
            "      git log -1 --format='%h %as' {ref}\n"
            "    НЕ включай сюда changelog / изменения между версиями "
            "— для этого есть отдельный CHANGELOG.md.\n"
            "    НЕ перечисляй мейнтейнеров, коммиттеров, участников "
            "организации, copyright holder'ов — только держатель "
            "(одна строка из git remote).\n\n"
            "Правила:\n"
            "- Пиши на русском языке.\n"
            "- Размер: 200-800 строк. Достаточно подробно, чтобы "
            "другой агент мог понять проект без чтения кода.\n"
            "- Факты проверяй через терминал (git show) — не выдумывай.\n"
            "- Если есть CHANGELOG.md — прочитай его для контекста "
            "изменений, но не копируй содержимое в SUMMARY.md.\n"
            "- После изучения вызови write_summary с полным текстом "
            "SUMMARY.md.\n"
        )

        # Добавляем кэшированный контекст от планировщика
        planner_context = ""
        if planner_result:
            if planner_result.project_overview:
                planner_context += (
                    f"Сводка проекта:\n{planner_result.project_overview}\n\n"
                )
            if planner_result.changelog_content:
                planner_context += (
                    f"Текущий CHANGELOG.md:\n{planner_result.changelog_content[:3000]}\n\n"
                )

        if old_summary:
            user_prompt = (
                f"{planner_context}"
                f"Ранее созданный SUMMARY.md:\n\n{old_summary[:12000]}\n\n"
                f"Проверь, актуален ли он для версии {ref}. "
                "Изучи текущую структуру кода "
                f"через 'git show {ref}:<путь>' (ты в {self._clone_dir}), "
                "сравни с описанием в SUMMARY и обнови при необходимости. "
                "Вызови write_summary с обновлённой версией."
            )
        else:
            user_prompt = (
                f"{planner_context}"
                f"Изучи код в репозитории для версии {ref} и создай SUMMARY.md.\n"
                "Используй terminal для просмотра структуры.\n"
                f"ВАЖНО: ты в {self._clone_dir} — bare-репозиторий. "
                "Читай файлы кода через:\n"
                f"  git show {ref}:packages/ai/src/index.ts\n"
                f"  git ls-tree --name-only {ref} packages/\n\n"
                "Затем вызови write_summary, чтобы сохранить описание проекта."
            )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        self._run_tool_loop(client, messages, "SUMMARY.md", max_turns)

    def _ensure_changelog(
        self,
        old_tag: str,
        latest_tag: str,
        max_turns: int = 8,
        updated_files: Optional[list[str]] = None,
        added_files: Optional[list[str]] = None,
        removed_files: Optional[list[str]] = None,
        developer_md_files: Optional[list[str]] = None,
        planner_result: Optional[PlannerResult] = None,
    ) -> None:
        """Создать или дополнить CHANGELOG.md, запустив LLM-агента.

        Если передан planner_result, агент получает предварительно
        загруженный контекст SUMMARY и CHANGELOG.

        Args:
            old_tag: Старый тег (от которого).
            latest_tag: Новый тег (до которого).
            max_turns: Максимум ходов агента.
        """
        if not self._generator:
            return
        client = self._generator._client
        if not client:
            return

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        system_prompt = (
            "Ты — редактор CHANGELOG.md. Этот файл содержит историю "
            "изменений только документации анализируемого проекта. "
            "Служебные файлы самого инструмента документирования "
            "(SUMMARY.md, CHANGELOG.md, SAMPLE.md, SAMPLES.md и т.п.) "
            "в него не попадают. ЗАПРЕЩЕНО упоминать эти файлы в записи.\n\n"
            "Твоя задача — изучить изменения "
            f"между {old_tag} и {latest_tag} и написать новую запись "
            "для CHANGELOG.md.\n\n"
            "У тебя есть инструменты:\n"
            "  • terminal — выполняй команды в папке "
            f"{self._clone_dir} (bare-репозиторий).\n"
            "    ВАЖНО: это bare-репозиторий (только git-объекты).\n"
            "    Читай файлы и дифы через:\n"
            f"      git show {latest_tag}:<путь>\n"
            f"      git diff {old_tag}..{latest_tag} -- <путь>\n\n"
            "  • collect_changelog — собрать коммиты и changelog-файлы "
            f"проекта между {old_tag} и {latest_tag}.\n"
            "  • submit_changelog_entry — добавить новую запись "
            "в CHANGELOG.md. ЗАГОЛОВОК ФОРМИРУЕТСЯ АВТОМАТИЧЕСКИ. "
            "Передай from_ref, to_ref и content (тело записи без заголовка). "
            "Предыдущие записи не пострадают.\n\n"
            "Каждая запись содержит две секции:\n"
            "1. Изменения в проекте — на основе коммитов между "
            f"{old_tag} и {latest_tag} (git log, collect_changelog).\n"
            "2. Изменения в документации — какие .md-файлы были "
            "добавлены, удалены или изменены.\n"
            "   Внутри этой секции две подсекции:\n"
            "   - Изменено docgen — файлы, которые обработал docgen.\n"
            "   - Изменено разработчиками — файлы, которые "
            "изменили разработчики в git.\n\n"
            "Подсекции включай только если есть соответствующие "
            "изменения.\n\n"
            "СТРОГИЕ ПРАВИЛА (нарушение недопустимо):\n"
            "1. ЗАПРЕЩЕНО включать в запись SUMMARY.md, CHANGELOG.md, "
            "SAMPLE.md, SAMPLES.md или любые другие служебные файлы "
            "инструмента документирования.\n"
            "2. ЗАПРЕЩЕНО писать про работу docgen, обновление SUMMARY.md "
            "или создание/изменение самого CHANGELOG.md.\n"
            "3. Только одна запись на переход между версиями.\n"
            "4. Пиши на русском языке.\n"
            "5. Факты проверяй через collect_changelog и terminal.\n"
            "6. Сначала изучи изменения, затем вызови "
            "submit_changelog_entry с from_ref, to_ref и content "
            "(без заголовка ##).\n"
        )

        # Формируем информацию о файлах, обработанных docgen и разработчиками
        docgen_info_parts = []
        if added_files:
            docgen_info_parts.append(f"  - Добавлено docgen: {', '.join(added_files)}")
        if removed_files:
            docgen_info_parts.append(f"  - Удалено docgen: {', '.join(removed_files)}")
        if updated_files:
            docgen_info_parts.append(f"  - Обновлено docgen: {', '.join(updated_files)}")
        docgen_info = "\n".join(docgen_info_parts)

        dev_info = ""
        if developer_md_files:
            dev_info = (
                ".md-файлы, изменённые разработчиками "
                f"(из git diff {old_tag}..{latest_tag}):\n"
                + "\n".join(f"  - {f}" for f in developer_md_files)
            )

        changelog_exists = os.path.isfile(self._changelog_path)

        # Собираем контекст для агента
        context_parts = []
        if docgen_info:
            context_parts.append(
                "Файлы, обработанные docgen "
                f"({old_tag} -> {latest_tag}):\n{docgen_info}"
            )
        if dev_info:
            context_parts.append(dev_info)
        # Добавляем кэшированный контекст от планировщика
        if planner_result:
            if planner_result.project_overview:
                context_parts.append(
                    f"Сводка проекта:\n{planner_result.project_overview}"
                )
            if planner_result.changelog_content:
                context_parts.append(
                    f"Текущий CHANGELOG.md:\n{planner_result.changelog_content[:3000]}"
                )
        context_str = "\n\n".join(context_parts)

        instructions = ""
        if docgen_info and dev_info:
            instructions = (
                "Раздели изменения на две подсекции:\n"
                "  - Изменено docgen — файлы, которые docgen "
                "обновил/добавил/удал самостоятельно.\n"
                "  - Изменено разработчиками — .md-файлы, которые "
                "изменились между версиями в git (разработчики "
                "сами обновили документацию).\n\n"
                "Файлы могут пересекаться — в таком случае укажи "
                "в обеих подсекциях."
            )
        elif docgen_info and not dev_info:
            instructions = (
                "Все изменения в .md-файлах сделаны docgen — "
                "укажи это в секции 'Изменено docgen'."
            )
        elif not docgen_info and dev_info:
            instructions = (
                "Разработчики сами обновили .md-файлы — "
                "укажи это в секции 'Изменено разработчиками'. "
                "Docgen ничего не менял."
            )

        user_prompt = (
            f"Напиши новую запись для CHANGELOG.md о переходе "
            f"{old_tag} -> {latest_tag} ({now_str}).\n\n"
            f"{context_str}\n\n"
            f"{instructions}\n\n"
            "Изучи изменения через collect_changelog и terminal, "
            "затем вызови submit_changelog_entry."
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        self._run_tool_loop(
            client, messages, "CHANGELOG.md", max_turns,
            from_ref=old_tag, to_ref=latest_tag,
        )

    # ── Логирование ──────────────────────────────────────

    def _log(self, msg: str) -> None:
        """Вывести в консоль (+ в лог-файл, если включено)."""
        if self.verbose:
            try:
                print(msg, flush=True)
            except UnicodeEncodeError:
                stream = sys.stdout
                reconfigure = getattr(stream, "reconfigure", None)
                if reconfigure:
                    reconfigure(encoding="utf-8", errors="replace")
                    print(msg, flush=True)
                else:
                    encoding = getattr(stream, "encoding", None) or "ascii"
                    safe = msg.encode(encoding, errors="replace").decode(encoding)
                    print(safe, flush=True)
        if self._log_enabled and self._log_file:
            clean = re.sub(r'\x1b\[[0-9;]*m', '', msg)
            now = datetime.now().strftime("%H:%M:%S")
            # Убираем leading \n — оно нужно для консольного форматирования,
            # но в файл создаёт лишний перенос после [время]
            self._log_file.write(f"[{now}] {clean.lstrip(chr(10))}\n")
            self._log_file.flush()

    def _log_open(self, name: str) -> None:
        """Открыть лог-файл для команды."""
        if not self._log_enabled:
            return
        if self._log_file_path:
            log_path = Path(self._log_file_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            log_dir = Path(self._work_dir) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = log_dir / f"{name}_{stamp}.log"
        mode = "a" if self._resuming else "w"
        self._log_file = open(log_path, mode, encoding="utf-8")
        self._log(f"  [agent] ▶ Лог открыт: {log_path}")

    def _log_close(self) -> None:
        """Закрыть лог-файл."""
        if self._log_file:
            self._log_file.close()
            self._log_file = None

    # ── Полный аудит (snapshot -c) ──────────────────────


    def _full_audit(
        self, snapshot_dir: str, ref: str, max_turns: int = 10,
        skip_files: Optional[list[str]] = None,
    ) -> GenerationResult:
        """Полный аудит всех .md на соответствие коду указанной версии.

        Args:
            snapshot_dir: Директория снэпшота (.md-файлы).
            ref: Git-реф (хэш коммита), для которого проверяется документация.
            max_turns: Максимум ходов агента на один .md файл.
            skip_files: Список файлов, которые уже обработаны (возобновление).

        Returns:
            GenerationResult
        """
        md_files = self._scan_snapshot_md(snapshot_dir)
        # Пропускаем уже обработанные файлы
        skip_set = set(skip_files or [])
        if skip_set:
            md_files = [f for f in md_files if f not in skip_set]

        commit_hash = ref
        result = GenerationResult(
            commit_hash=commit_hash,
            output_dir=snapshot_dir,
        )

        if self.verbose:
            if skip_set:
                self._log(f"  [agent]   Пропущено {len(skip_set)} уже проверенных файлов")
            self._log(f"\n  [agent]   Всего .md для аудита: {len(md_files)}")

        updated = 0
        total_files = len(md_files)
        for idx, rel_path in enumerate(md_files, 1):
            if self.verbose:
                self._log(f"\n  [agent]   🔍 Аудит: [{idx}/{total_files}] {rel_path}")

            old_content = self._read_doc_file(snapshot_dir, rel_path)
            if not old_content:
                continue

            try:
                new_content = self._run_agentic_audit(
                    old_doc=old_content,
                    file_path=rel_path,
                    ref=commit_hash,
                    max_turns=max_turns,
                )
            except RateLimitError:
                completed_so_far = md_files[:md_files.index(rel_path)]
                # Добавляем ранее пропущенные файлы к completed
                total_completed = list(skip_set) + completed_so_far
                self._save_checkpoint(
                    phase="full_audit",
                    command="snapshot",
                    release_tag=ref,
                    is_check=True,
                    completed_files=total_completed,
                )
                raise

            if new_content and new_content != old_content:
                dest = Path(snapshot_dir) / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(new_content, encoding="utf-8")
                updated += 1
                if self.verbose:
                    self._log(f"  [agent]   ✏️ {rel_path} — обновлён")
            elif new_content and self.verbose:
                self._log(f"  [agent]   ✓ {rel_path} — актуален")

        result.docs_updated = updated
        result.docs_copied = len(md_files) - updated
        return result

    def _run_agentic_audit(
        self,
        old_doc: str,
        file_path: str,
        ref: str,
        max_turns: int = 10,
    ) -> Optional[str]:
        """Проверить один .md-файл на соответствие коду через LLM + терминал.

        LLM сама решает:
        - Какой код описывает этот .md
        - Где он находится (grep, find, ls)
        - Актуален ли он
        - Обновить или оставить как есть

        max_turns — сколько ходов (вызовов terminal) даётся агенту.

        Если документация актуальна — возвращает original content.
        Если устарела — возвращает обновлённую версию.
        """
        if not self._generator:
            return None
        client = self._generator._client
        if not client:
            return None

        max_turns = max(10, max_turns)

        system_prompt = (
            "Ты — аудитор документации. Проверь, соответствует ли этот "
            f".md-файл коду версии {ref} в репозитории.\n\n"
            "У тебя есть доступ к терминалу и специальным инструментам:\n"
            "  • read_summary — прочитать описание проекта (архитектура, "
            "модули, соглашения).\n"
            "  • collect_changelog — понять, какие изменения произошли "
            "в коде (CHANGELOG'и + коммиты).\n"
            f"  • terminal — выполняй команды в папке {self._clone_dir} "
            "(bare-репозиторий).\n"
            "    ВАЖНО: это bare-репозиторий. Чтобы прочитать "
            f"файл из кода версии {ref}, используй:\n"
            f"      git show {ref}:<путь>\n"
            "    Например:\n"
            "      git show {ref}:packages/ai/src/index.ts\n"
            "      git ls-tree --name-only {ref} packages/\n\n"
            "Правила:\n"
            "1. Если документация ПОЛНОСТЬЮ актуальна — верни её "
            "дословно, без изменений.\n"
            "2. Если устарела — обнови с сохранением стиля, структуры "
            "и терминологии.\n"
            "3. Если документации нет в коде (файл удалён) — напиши "
            "'DELETED' и ничего больше.\n"
            "4. Пиши на русском языке.\n"
            "5. Не добавляй разделы, которых нет.\n"
            "6. Верни ПОЛНЫЙ документ, не только изменения.\n"
            "7. Рассуждения выполняй во внутренних шагах. В финальном "
            "ответе не добавляй пояснений, оценок и вводного текста. "
            "Верни документ строго между отдельными строками "
            "DOCGEN_DOCUMENT_START и DOCGEN_DOCUMENT_END.\n"
            "8. ВАЖНО: у тебя ограниченный бюджет ходов. Если файл "
            "заведомо актуален или изменения тривиальны — сразу верни "
            "его как есть, не тратя ходы на изучение.\n"
        )

        user_prompt = (
            f"Файл: {file_path}\n\n"
            f"### Текущая документация\n\n"
            f"{old_doc[:12000]}\n\n"
            f"Проверь, соответствует ли этот .md текущему коду. "
            f"Используй терминал для изучения репозитория."
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        response = self._run_tool_loop(client, messages, file_path, max_turns)
        content = self._extract_document_response(response, allow_deleted=True)

        # Если LLM сказала DELETED или ответ пустой — не трогаем
        if not content or content == "DELETED":
            if content == "DELETED" and self.verbose:
                self._log(f"  [agent]     🗑 Отмечен как удалённый")
            return old_doc
        if len(content) < 20:
            return old_doc

        return content

    def _extract_document_response(
        self,
        response: Optional[str],
        *,
        allow_deleted: bool = False,
    ) -> Optional[str]:
        """Извлечь только Markdown-документ из финального ответа LLM."""
        if not response:
            return None

        stripped = response.strip()
        if allow_deleted and stripped == "DELETED":
            return stripped

        start_marker = "DOCGEN_DOCUMENT_START"
        end_marker = "DOCGEN_DOCUMENT_END"
        start = stripped.find(start_marker)
        end = stripped.find(end_marker, start + len(start_marker))
        if start < 0 or end < 0:
            if self.verbose:
                self._log(
                    "  [agent]     ⚠ Ответ аудита отклонён: "
                    "нет маркеров документа"
                )
            return None

        document = stripped[start + len(start_marker):end].strip()
        return document or None

    # ── Хелперы ──────────────────────────────────────────

    @staticmethod
    def _scan_snapshot_md(snapshot_dir: str) -> list[str]:
        """Найти все .md в папке снэпшота, вернуть относительные пути."""
        root = Path(snapshot_dir)
        if not root.is_dir():
            return []
        files: list[str] = []
        for f in sorted(root.rglob("*.md")):
            rel = f.relative_to(root).as_posix()
            files.append(rel)
        return files

    @staticmethod
    def _copy_snapshot(src: str, dst: str) -> None:
        """Скопировать содержимое снэпшота из src в dst."""
        src_path = Path(src)
        dst_path = Path(dst)
        if not src_path.is_dir():
            dst_path.mkdir(parents=True, exist_ok=True)
            return
        # Копируем все .md с сохранением структуры
        for f in src_path.rglob("*.md"):
            rel = f.relative_to(src_path)
            target = dst_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f.read_bytes())

    @staticmethod
    def _read_doc_file(snapshot_dir: str, rel_path: str) -> Optional[str]:
        """Прочитать .md-файл из снэпшота."""
        p = Path(snapshot_dir) / rel_path
        if p.exists():
            return p.read_text(encoding="utf-8")
        return None
