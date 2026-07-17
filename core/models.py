"""Pydantic-модели для docgen — новая архитектура."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ChangeType(str, Enum):
    """Тип изменения в git-diff."""
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    UNKNOWN = "unknown"


class FileChange(BaseModel):
    """Одно изменение в файле."""
    path: str = Field(description="Путь к файлу")
    change_type: ChangeType = Field(description="Тип изменения")
    old_path: Optional[str] = Field(None, description="Старый путь (при переименовании)")
    added_lines: int = Field(0, description="Добавлено строк")
    deleted_lines: int = Field(0, description="Удалено строк")


class CommitInfo(BaseModel):
    """Информация о коммите."""
    hash: str = Field(description="SHA коммита")
    author: str = Field(description="Автор")
    message: str = Field(description="Сообщение коммита")
    timestamp: datetime = Field(description="Дата коммита")


class GitDiffAnalysis(BaseModel):
    """Результат анализа diff между двумя коммитами."""
    from_commit: CommitInfo = Field(description="Исходный коммит")
    to_commit: CommitInfo = Field(description="Целевой коммит")
    files_changed: list[FileChange] = Field(description="Список изменённых файлов")


class GenerationResult(BaseModel):
    """Результат генерации документации для одной версии."""
    commit_hash: str = Field(description="Хэш коммита")
    output_dir: str = Field(description="Путь к папке с результатом")
    release_tag: Optional[str] = Field(None, description="Тег релиза, если задан")
    docs_updated: int = Field(0, description="Сколько .md обновлено через LLM")
    docs_copied: int = Field(0, description="Сколько .md скопировано без изменений")
    docs_added: int = Field(0, description="Сколько .md добавлено (новые файлы)")
    docs_removed: int = Field(0, description="Сколько .md удалено")
    tokens_used: int = Field(0, description="Затрачено токенов")
    warnings: list[str] = Field(default_factory=list, description="Предупреждения")
    updated_files: list[str] = Field(default_factory=list, description="Пути .md, обновлённых через LLM")
    added_files: list[str] = Field(default_factory=list, description="Пути новых .md из репозитория")
    removed_files: list[str] = Field(default_factory=list, description="Пути .md, удалённых из снэпшота")


class ProjectConfig(BaseModel):
    """Конфигурация проекта docgen."""
    git_repo: str = Field(description="URL git-репозитория")
    github_token_env: Optional[str] = Field(
        None,
        description="Имя переменной окружения с GitHub-токеном (не сам токен)",
    )
    llm_provider: str = Field("openai", description="Провайдер LLM")
    llm_model: str = Field("gpt-4o", description="Модель LLM")
    llm_api_key: Optional[str] = Field(None, description="API-ключ LLM")
    llm_base_url: Optional[str] = Field(
        None, description="Базовый URL для OpenAI-совместимых провайдеров",
    )
    project_name: str = Field("default", description="Имя проекта")
    max_turns: int = Field(
        10,
        description="Максимум ходов агента (вызовов terminal) на один .md",
        ge=5,
        le=500,
    )


class ReleaseMap(BaseModel):
    """Маппинг тегов релизов на хэши коммитов."""
    last_documented_release: Optional[str] = Field(
        None, description="Имя последнего задокументированного тега",
    )
    releases: dict[str, str] = Field(
        default_factory=dict,
        description="Карта: тег → хэш коммита",
    )


class ProjectState(BaseModel):
    """Состояние проекта — сериализуется в .docgen.yaml."""
    config: ProjectConfig = Field(description="Конфигурация")
