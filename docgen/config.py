"""Управление конфигурацией docgen."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

from docgen.models import ProjectConfig, ProjectState

CONFIG_FILENAME = ".docgen.yaml"


def _default_state(git_repo: str) -> ProjectState:
    """Создать состояние проекта по умолчанию."""
    return ProjectState(
        config=ProjectConfig(
            git_repo=git_repo,
            llm_api_key=os.environ.get("OPENAI_API_KEY"),
            llm_base_url=os.environ.get("OPENAI_BASE_URL"),
        ),
    )


def find_project_root(path: Optional[str] = None) -> Optional[Path]:
    """Ищет .docgen.yaml начиная от path и поднимаясь вверх."""
    start = Path(path or os.getcwd()).resolve()
    for parent in [start] + list(start.parents):
        cfg = parent / CONFIG_FILENAME
        if cfg.exists():
            return parent
    return None


def load_state(path: Optional[str] = None) -> Optional[ProjectState]:
    """Загрузить состояние проекта из .docgen.yaml."""
    root = find_project_root(path)
    if root is None:
        return None
    cfg_path = root / CONFIG_FILENAME
    with open(cfg_path) as f:
        data = yaml.safe_load(f)
    if data is None:
        return None
    return ProjectState.model_validate(data)


def save_state(state: ProjectState, path: Optional[str] = None) -> Path:
    """Сохранить состояние проекта в .docgen.yaml.

    Если path указан — сохраняет туда.
    Иначе ищет существующий .docgen.yaml от CWD вверх.
    Если не находит — сохраняет в CWD.
    """
    if path is not None:
        cfg_path = Path(path).resolve()
        if cfg_path.is_dir():
            cfg_path = cfg_path / CONFIG_FILENAME
    else:
        found = find_project_root()
        if found is not None:
            cfg_path = found / CONFIG_FILENAME
        else:
            cfg_path = Path(os.getcwd()).resolve() / CONFIG_FILENAME
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg_path, "w") as f:
        yaml.dump(
            state.model_dump(mode="json"),
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )
    return cfg_path


def init_project(
    git_repo: str,
    llm_api_key: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_base_url: Optional[str] = None,
    access_token_env: Optional[str] = None,
    project_name: Optional[str] = None,
    max_turns: Optional[int] = None,
) -> ProjectState:
    """Инициализировать новый проект docgen.

    .docgen.yaml создаётся в текущей рабочей папке.
    """
    state = _default_state(git_repo)
    if llm_api_key:
        state.config.llm_api_key = llm_api_key
    if llm_model:
        state.config.llm_model = llm_model
    if llm_base_url:
        state.config.llm_base_url = llm_base_url
    if access_token_env:
        state.config.access_token_env = access_token_env
    if project_name:
        state.config.project_name = project_name
    if max_turns is not None:
        state.config.max_turns = max_turns
    save_state(state)
    return state
