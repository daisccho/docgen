"""DocAgent — ядро: snapshot, watch, инкрементальное обновление."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from docgen.config import (
    find_project_root,
    load_release_map,
    save_release_map,
    save_state,
)
from docgen.doc_generator import DocGenerator
from docgen.git_analyzer import (
    clone_repo,
    ensure_clone,
    ensure_ref_available,
    extract_file_from_repo,
    fetch_repo,
    get_deleted_md_files,
    get_diff_files,
    get_head_hash,
    get_latest_tag,
    get_new_md_files,
    get_raw_diffs,
    get_snapshot_versions,
    read_file_from_repo,
    scan_md_files,
)
from docgen.models import GenerationResult, ProjectState

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
                           "последующих запусков. Пиши на русском языке.",
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
            "name": "collect_changelog",
            "description": "Собрать список изменений между двумя ref'ами (тегами, "
                           "хэшами коммитов или ветками). "
                           "Читает CHANGELOG.md файлы, git commit messages и "
                           "собирает консолидированный changelog. "
                           "Вызывай в начале фазы update или audit, чтобы понять, "
                           "какие изменения произошли в коде.",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_ref": {
                        "type": "string",
                        "description": "Начальный ref (тег, хэш или ветка)",
                    },
                    "to_ref": {
                        "type": "string",
                        "description": "Конечный ref (тег, хэш или ветка)",
                    },
                },
                "required": ["from_ref", "to_ref"],
            },
        },
    },
]


class DocAgent:
    """Агент управления документацией."""

    def __init__(self, state: ProjectState, verbose: bool = False,
                 log: bool = False) -> None:
        self.state = state
        self.verbose = verbose


        # Рабочая директория — там, где лежит .docgen.yaml
        root = find_project_root()
        self._work_dir = str(root) if root else os.getcwd()
        self._clone_dir = os.path.join(self._work_dir, CLONE_DIR)

        # LLM-клиент
        cfg = state.config
        self._generator: Optional[DocGenerator] = None
        if cfg.llm_api_key:
            self._generator = DocGenerator(
                api_key=cfg.llm_api_key,
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

    # ── Открытые методы ─────────────────────────────────

    def snapshot(
        self,
        release_tag: Optional[str] = None,
        check: bool = False,
        max_turns: Optional[int] = None,
    ) -> GenerationResult:
        """Создать снэпшот документации.

        Без release_tag — на текущем HEAD. С release_tag — на указанном теге, хэше
        или ветке (например v1.0, abc1234, main).

        Копирует все .md-файлы из репозитория в <work_dir>/<hash>/.
        Если check=True, дополнительно запускает аудит.
        """
        self._log_open("snapshot")
        ref = release_tag

        # ── Клон: создать или открыть ──
        if self.verbose:
            self._log(f"  [agent]   ⏳ Клонирование {self.state.config.git_repo}...")
            t0 = time.monotonic()

        if ref:
            # Фиксированный ref: не фетчим, если уже есть локально
            ensure_clone(
                self.state.config.git_repo, self._clone_dir, self._token,
                verbose=self.verbose,
            )
            if self.verbose:
                self._log(f"  [agent]   ✅ Клон готов ({time.monotonic() - t0:.1f}с)")
            if self.verbose:
                self._log(f"  [agent]   🔍 Поиск тега {ref}...")
                t0 = time.monotonic()
            commit_hash = ensure_ref_available(self._clone_dir, ref)
            if self.verbose:
                self._log(f"  [agent]   ✅ Тег найден ({time.monotonic() - t0:.1f}с)")
        else:
            # Без ref — ищем последний релиз через GitHub API, иначе HEAD
            ensure_clone(
                self.state.config.git_repo, self._clone_dir, self._token,
                verbose=self.verbose,
            )
            latest = get_latest_tag(
                self._clone_dir,
                github_token=self._token,
                repo_url=self.state.config.git_repo,
            )
            if latest:
                ref = latest
                if self.verbose:
                    self._log(f"  [agent]   🔍 Последний релиз: {ref}")
                    t0 = time.monotonic()
                commit_hash = ensure_ref_available(self._clone_dir, ref)
                if self.verbose:
                    self._log(f"  [agent]   ✅ Ref найден ({time.monotonic() - t0:.1f}с)")
            else:
                if self.verbose:
                    self._log(f"  [agent]   ⏳ Релизов нет — фетчим HEAD...")
                    t0 = time.monotonic()
                clone_repo(self.state.config.git_repo, self._clone_dir, self._token)
                if self.verbose:
                    self._log(f"  [agent]   ✅ Клон готов ({time.monotonic() - t0:.1f}с)")
                commit_hash = get_head_hash(self._clone_dir)

        if self.verbose:
            label = ref or "HEAD"
            self._log(f"\n  [agent] ▶ Создание снэпшота на {label} → {commit_hash[:8]}")

        md_files = scan_md_files(self._clone_dir, treeish=commit_hash)
        dir_name = ref if ref else commit_hash
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
            self._ensure_summary(snapshot_dir, ref=commit_hash, max_turns=n_summary)

        if check and self._generator:
            n_audit = max_turns if max_turns is not None else self._max_turns
            if self.verbose:
                self._log(f"\n  [agent] ▶ Полный аудит документации (до {n_audit} ходов)")
            audit = self._full_audit(snapshot_dir, ref=commit_hash, max_turns=n_audit)
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

    def watch(self, interval: int, branch: str = "main") -> None:
        """Запустить демон: fetch → обновление → сон."""
        self._log_open("watch")
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
            self._log(f"\n  [agent] ▶ Watch запущен: ветка {branch}, интервал {interval} мин")
            self._log(f"  [agent]   Рабочая папка: {self._work_dir}")

        while True:
            try:
                self._watch_tick(branch)
            except Exception as exc:
                self._log(f"  [agent]   ⚠ Ошибка: {exc}")

            if self.verbose:
                now = datetime.now().strftime("%H:%M:%S")
                self._log(f"\n  [agent] 💤 Сон {interval} мин ({now})")
            time.sleep(interval * 60)

    # ── Внутренние методы ────────────────────────────────

    def _watch_tick(self, branch: str) -> None:
        """Один такт watch: fetch → проверить теги → обновить."""
        if self.verbose:
            self._log(f"  [agent]   ⏳ Fetch тегов {self.state.config.git_repo}...")
            t0 = time.monotonic()
        fetch_repo(self._clone_dir, self._token)
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

        old_snapshot_dir = os.path.join(self._work_dir, old_tag)
        new_snapshot_dir = os.path.join(self._work_dir, latest_tag)

        if not os.path.isdir(old_snapshot_dir):
            if self.verbose:
                self._log(f"  [agent]   ⚠ Снэпшот {old_tag} не найден — делаем snapshot")
            self.snapshot(release_tag=latest_tag, check=True)
            return

        result = self._update_docs(
            old_snapshot_dir=old_snapshot_dir,
            new_snapshot_dir=new_snapshot_dir,
            from_ref=old_tag,
            to_ref=latest_tag,
            copy_new_from_repo=True,
            max_turns=self._max_turns,
        )

        # ── CHANGELOG.md документации ──
        changelog_path = os.path.join(self._work_dir, "CHANGELOG.md")
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        entry_parts = [f"## {now_str} | {old_tag} → {latest_tag}"]
        if result.docs_updated:
            entry_parts.append(f"- Обновлено: {result.docs_updated} файлов")
        if result.docs_added:
            entry_parts.append(f"- Добавлено: {result.docs_added} файлов")
            if result.added_files:
                for f in result.added_files:
                    entry_parts.append(f"  - {f}")
        if result.docs_removed:
            entry_parts.append(f"- Удалено: {result.docs_removed} файлов")
            if result.removed_files:
                for f in result.removed_files:
                    entry_parts.append(f"  - {f}")
        if not (result.docs_updated or result.docs_added or result.docs_removed):
            entry_parts.append("- Изменений нет")

        entry = "\n".join(entry_parts) + "\n"

        if os.path.isfile(changelog_path):
            with open(changelog_path, "a", encoding="utf-8") as f:
                f.write("\n" + entry)
        else:
            header = "# CHANGELOG документации\n\n"
            with open(changelog_path, "w", encoding="utf-8") as f:
                f.write(header + entry)

        if self.verbose:
            self._log(f"  [agent]   📋 CHANGELOG: {old_tag} → {latest_tag}")

        # Обновляем SUMMARY в соответствии с изменениями
        if self._generator and self.verbose:
            self._log(f"\n  [agent] ▶ Обновление SUMMARY.md для {latest_tag}")
        if self._generator:
            self._ensure_summary(
                new_snapshot_dir, ref=latest_tag,
                max_turns=max(5, self._max_turns),
            )

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
    ) -> GenerationResult:
        """Инкрементальное обновление документации.

        Args:
            old_snapshot_dir: Папка с предыдущей версией документации.
            new_snapshot_dir: Куда писать новую версию.
            from_ref: Git-реф начала.
            to_ref: Git-реф конца.
            copy_new_from_repo: Если True — новые .md копируются из
                репозитория, а не из старого снэпшота.
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

        # ── Шаг 3: LLM-маппинг (какие .md обновлять) ──
        docs_to_update: set[str] = set()
        if self._generator and changed_code:
            docs_to_update = self._map_changes_to_docs_agentic(
                changed_code, old_md_files, new_repo_md,
                from_ref, to_ref, max_turns=max_turns,
            )
        elif self._generator and not changed_code:
            if self.verbose:
                self._log(f"  [agent]   Нет изменений кода — ничего не обновляем")
        elif not self._generator:
            if self.verbose:
                self._log(f"  [agent]   ⚠ LLM не настроен — копируем без изменений")

        # ── Шаг 4: копируем старый снэпшот ──
        # Сначала копируем всё из старого снэпшота
        self._copy_snapshot(old_snapshot_dir, new_snapshot_dir)
        result.docs_copied = len(old_md_files)

        # ── Шаг 5: обновляем файлы, которые затронул LLM ──
        raw_diffs = get_raw_diffs(self._clone_dir, from_ref, to_ref)
        updated = 0
        for rel_path in docs_to_update:
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

            new_content = self._run_agentic_update(
                old_doc=old_content,
                code_diffs=relevant,
                file_path=rel_path,
                max_turns=max_turns,
            )
            if new_content:
                dest = Path(new_snapshot_dir) / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(new_content, encoding="utf-8")
                updated += 1
                if self.verbose:
                    self._log(f"  [agent]   ↔ {rel_path} (синхронизирован из репозитория)")

        result.docs_updated = updated

        # ── Шаг 6: новые .md из репозитория ──
        added = 0
        if copy_new_from_repo:
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
        for md_path in deleted_md:
            dest = Path(new_snapshot_dir) / md_path
            if dest.exists():
                dest.unlink()
                removed += 1
                if self.verbose:
                    self._log(f"  [agent]   🗑 {md_path} (удалён из репозитория)")
        result.docs_removed = removed

        return result

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
    ) -> set[str]:
        """Агентный цикл: LLM с доступом к терминалу анализирует diff
        и решает, какие .md-файлы требуют обновления.

        Returns:
            Множество путей .md, которые нужно обновить.
        """
        if not self._generator:
            return set()
        client = self._generator._client
        if not client:
            return set()

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
            "У тебя есть доступ к терминалу и специальным инструментам:\n"
            "  • read_summary — прочитать описание проекта (если есть).\n"
            "    Вызови в самом начале, чтобы быстро понять архитектуру.\n"
            "  • write_summary — после изучения кода сохрани описание "
            "проекта для будущих запусков.\n"
            "  • collect_changelog(from_ref, to_ref) — собрать список "
            "изменений между версиями: CHANGELOG'и, подобные им файлы "
            "и комментарии коммитов.\n"
            "  • terminal — изучай код, diff'ы, читай файлы.\n\n"
            "📋 Рекомендуемый порядок:\n"
            "1. Сначала read_summary — пойми, что за проект.\n"
            "2. Затем collect_changelog(from_ref, to_ref) — узнай, "
            "что изменилось (агент сам найдёт CHANGELOG-подобные файлы).\n"
            "3. Используй terminal для точечной проверки кода.\n\n"
            "Правила:\n"
            "1. Если изменения кода не затрагивают API, интерфейсы или "
            "описанную функциональность — не обновляй .md.\n"
            "2. Если изменения минимальны (исправление опечатки, "
            "рефакторинг без изменения сигнатур) — не обновляй.\n"
            "3. Обрати внимание на новые файлы в репозитории — "
            "возможно, для них нужна документация.\n"
            "4. Если файл .md удалён из репозитория — не помечай его "
            "как требующий обновления.\n"
            "5. В конце верни ТОЛЬКО JSON-массив с путями .md-файлов, "
            "которые нужно обновить. Без пояснений.\n"
            "6. Пример: [\"docs/guide.md\", \"README.md\"]\n"
            "7. Если ничего не нужно обновлять: []\n"
            "8. ВАЖНО: у тебя ограниченный бюджет ходов. Не трать всё на "
            "изучение — обязательно оставь 1-2 хода, чтобы выдать "
            "финальный JSON. Если исследуешь diff и .md — делай это "
            "быстро и целенаправленно.\n"
        )

        user_prompt = (
            f"### Git diff {from_ref[:8]}..{to_ref[:8]}\n\n"
            f"Всего изменённых файлов: {total_changed}\n"
            f"(показаны первые 80):\n{changed_sample}\n\n"
            f"### Файлы документации\n\n"
            f"В старом снэпшоте ({total_md} всего, первые 80):\n{md_sample}\n\n"
            f"Новые .md в репозитории:\n{new_md_text}\n\n"
            f"Удалённые .md из репозитория:\n{deleted_md_text}\n\n"
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

        content = self._run_tool_loop(client, messages, "LLM-маппинг", max_turns)
        if not content:
            if self.verbose:
                self._log(f"  [agent]   ⚠ Агентный маппинг вернул пустой ответ")
        return set()

        # Извлекаем JSON из ответа
        json_match = re.search(r"\[.*?\]", content, re.DOTALL)
        if json_match:
            try:
                paths = json.loads(json_match.group())
                if isinstance(paths, list):
                    valid = {p for p in paths if p in old_md_files or p in new_md_files}
                    if self.verbose:
                        self._log(f"  [agent]   📋 Агент выбрал: {paths}")
                        filtered = set(paths) - valid
                        if filtered:
                            self._log(f"  [agent]   ⚠ Не найдено в снэпшотах: {sorted(filtered)}")
                        if valid:
                            self._log(f"  [agent]   ✏️ Будет обновлено: {sorted(valid)}")
                        else:
                            self._log(f"  [agent]   ✓ Ничего не требует обновления")
                    return valid
                elif self.verbose:
                    self._log(f"  [agent]   ⚠ Агент вернул не массив: {paths!r}")
            except Exception as exc:
                if self.verbose:
                    self._log(f"  [agent]   ⚠ Ошибка парсинга ответа: {exc}")
        elif self.verbose:
            preview = content[:300].replace("\n", " ")
            self._log(f"  [agent]   ⚠ Не удалось извлечь JSON из ответа:"
        f"\"{preview}…\"")

        if self.verbose:
            self._log(f"  [agent]   ✓ Ничего не требует обновления (пустой результат)")
        return set()

    # ── Инструментальный цикл LLM ─────────────────────────

    def _run_agentic_update(
        self,
        old_doc: str,
        code_diffs: dict[str, str],
        file_path: str,
        max_turns: int = 10,
    ) -> Optional[str]:
        """Обновить .md-файл через LLM с доступом к терминалу.

        LLM может выполнять команды в репозитории (.clone/), чтобы
        изучить код, проверить структуру, найти зависимости и только
        потом сгенерировать обновлённую документацию.

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

        system_prompt = (
            "Ты — технический писатель с доступом к терминалу и "
            "специальным инструментам.\n"
            "Обновляй .md-файл документации на основе изменений в коде.\n\n"
            "Инструменты:\n"
            "  • read_summary — прочитать описание проекта (архитектура, "
            "модули, соглашения). Вызови в начале, если не знаком с проектом.\n"
            "  • collect_changelog — понять, какие изменения произошли "
            "в коде (CHANGELOG'и + коммиты).\n"
            "  • terminal — выполняй команды в репозитории, чтобы:\n"
            "  • Изучить, как устроен изменившийся код\n"
            "  • Проверить экспортируемые функции/классы\n"
            "  • Найти соответствие между diff и документом\n"
            "  • Прочитать README или другие .md для контекста\n\n"
            "Когда будешь готов — просто напиши обновлённый Markdown-документ "
            "в ответе. Не вызывай инструмент, если он не нужен — "
            "вывод сразу считается финальной версией.\n\n"
            "Правила:\n"
            "1. Сохраняй стиль, структуру и терминологию оригинала.\n"
            "2. Обновляй только разделы, которые затрагивает diff.\n"
            "3. Если изменений нет — верни документ как есть.\n"
            "4. Пиши на русском языке, если не указано иное.\n"
            "5. Верни ПОЛНЫЙ обновлённый Markdown-документ.\n"
            "6. Напиши рассуждения, а затем — финальную версию.\n"
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

        content = self._run_tool_loop(client, messages, file_path, max_turns)
        if not content or len(content) < 50:
            return old_doc
        return content

    # ── Инструментальный цикл LLM ─────────────────────────

    def _run_terminal(self, command: str) -> str:
        """Выполнить shell-команду в директории клона репозитория.

        Возвращает stdout (до 8000 символов) или описание ошибки.
        """
        # Чёрный список опасных команд
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
    ) -> Optional[str]:
        """Выполнить цикл LLM с инструментами.

        Возвращает финальный текст ответа или None.
        """
        for turn in range(max_turns):
            if self.verbose:
                self._log(f"  [agent]     🤖 Ход {turn+1}/{max_turns}: запрос к LLM...")
            try:
                resp = client.chat.completions.create(
                    model=self._generator.model,
                    messages=messages,
                    tools=TOOL_DEFS,
                    tool_choice="auto",
                    max_tokens=8192,
                    timeout=120,
                )
            except Exception as exc:
                if self.verbose:
                    self._log(f"  [agent]     ⚠ LLM ошибка: {exc}")
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

                elif tc.function.name == "collect_changelog":
                    try:
                        args = json.loads(tc.function.arguments)
                        from_ref = args.get("from_ref", "")
                        to_ref = args.get("to_ref", "")
                        result = self._collect_changelog(from_ref, to_ref)
                    except (json.JSONDecodeError, KeyError) as exc:
                        result = f"[ERROR: невалидные аргументы: {exc}]"
                    except Exception as exc:
                        result = f"[ERROR: {exc}]"
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

        # 1. Коммиты между ref'ами
        try:
            log_output = subprocess.run(
                ["git", "log", "--oneline", "--no-decorate",
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

        try:
            for pattern in changelog_patterns:
                ls = subprocess.run(
                    ["git", "ls-files", "--", pattern],
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

                for path in found_changelogs[:15]:
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

    def _ensure_summary(self, snapshot_dir: str, ref: str, max_turns: int = 10) -> None:
        """Создать или обновить SUMMARY.md, запустив LLM-агента для изучения проекта.

        Вызывается при каждом snapshot. Если SUMMARY.md уже есть — агент
        читает его, изучает изменения в проекте и обновляет.

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
            f"  • terminal — выполняй команды в папке {self._clone_dir} "
            "(bare-репозиторий).\n"
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
            "SUMMARY.md должен включать:\n"
            "1. Назначение проекта (кратко, 2-3 предложения)\n"
            "2. Архитектура: основные модули/пакеты, их ответственность\n"
            "3. Ключевые технологии и зависимости\n"
            "4. Структура репозитория (основные директории)\n"
            "5. Принятые соглашения (стиль кода, naming, архитектурные решения)\n"
            "6. Любая другая информация, полезная для понимания проекта\n\n"
            "Пиши на русском языке.\n"
            "После изучения вызови write_summary с полным текстом SUMMARY.md.\n"
            "Не возвращай SUMMARY как финальный ответ — это сделает write_summary."
        )

        if old_summary:
            user_prompt = (
                f"Ранее созданный SUMMARY.md:\n\n{old_summary[:12000]}\n\n"
                f"Проверь, актуален ли он для версии {ref}. "
                "Изучи текущую структуру кода "
                f"через 'git show {ref}:<путь>' (ты в {self._clone_dir}), "
                "сравни с описанием в SUMMARY и обнови при необходимости. "
                "Вызови write_summary с обновлённой версией."
            )
        else:
            user_prompt = (
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

    # ── Логирование ──────────────────────────────────────

    def _log(self, msg: str) -> None:
        """Вывести в консоль (+ в лог-файл, если включено)."""
        if self.verbose:
            print(msg)
        if self._log_enabled and self._log_file:
            clean = re.sub(r'\x1b\[[0-9;]*m', '', msg)
            now = datetime.now().strftime("%H:%M:%S")
            self._log_file.write(f"[{now}] {clean}\n")
            self._log_file.flush()

    def _log_open(self, name: str) -> None:
        """Открыть лог-файл для команды."""
        if not self._log_enabled:
            return
        log_dir = Path(self._work_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = log_dir / f"{name}_{stamp}.log"
        self._log_file = open(log_path, "w", encoding="utf-8")
        self._log(f"  [agent] ▶ Лог открыт: {log_path}")

    def _log_close(self) -> None:
        """Закрыть лог-файл."""
        if self._log_file:
            self._log_file.close()
            self._log_file = None

    # ── Полный аудит (snapshot -c) ──────────────────────


    def _full_audit(self, snapshot_dir: str, ref: str, max_turns: int = 10) -> GenerationResult:
        """Полный аудит всех .md на соответствие коду указанной версии.

        Args:
            snapshot_dir: Директория снэпшота (.md-файлы).
            ref: Git-реф (хэш коммита), для которого проверяется документация.
            max_turns: Максимум ходов агента на один .md файл.

        Returns:
            GenerationResult
        """
        md_files = self._scan_snapshot_md(snapshot_dir)
        commit_hash = ref
        result = GenerationResult(
            commit_hash=commit_hash,
            output_dir=snapshot_dir,
        )

        if self.verbose:
            self._log(f"\n  [agent]   Всего .md для аудита: {len(md_files)}")

        updated = 0
        total_files = len(md_files)
        for idx, rel_path in enumerate(md_files, 1):
            if self.verbose:
                self._log(f"\n  [agent]   🔍 Аудит: [{idx}/{total_files}] {rel_path}")

            old_content = self._read_doc_file(snapshot_dir, rel_path)
            if not old_content:
                continue

            new_content = self._run_agentic_audit(
                old_doc=old_content,
                file_path=rel_path,
                ref=commit_hash,
                max_turns=max_turns,
            )

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
            "7. После анализа напиши свои рассуждения, а затем — "
            "финальную версию документа.\n"
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

        content = self._run_tool_loop(client, messages, file_path, max_turns)

        # Если LLM сказала DELETED или ответ пустой — не трогаем
        if not content or content == "DELETED":
            if content == "DELETED" and self.verbose:
                self._log(f"  [agent]     🗑 Отмечен как удалённый")
            return old_doc
        if len(content) < 20:
            return old_doc

        return content

    # ── Хелперы ──────────────────────────────────────────

    @staticmethod
    def _scan_snapshot_md(snapshot_dir: str) -> list[str]:
        """Найти все .md в папке снэпшота, вернуть относительные пути."""
        root = Path(snapshot_dir)
        if not root.is_dir():
            return []
        files: list[str] = []
        for f in sorted(root.rglob("*.md")):
            rel = str(f.relative_to(root))
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
