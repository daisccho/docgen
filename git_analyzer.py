"""Git-операции для docgen: clone, fetch, diff, scan .md."""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import git
import requests
from git import Repo

from docgen.errors import DocAgentError, NotGitRepositoryError, RefNotFoundError
from docgen.models import ChangeType, CommitInfo, FileChange

# ── Вспомогательные функции ────────────────────────────────


def _get_repo(repo_path: str) -> Repo:
    """Открыть git-репозиторий."""
    try:
        return Repo(repo_path)
    except git.InvalidGitRepositoryError:
        raise NotGitRepositoryError(
            f"'{repo_path}' не является git-репозиторием."
        )


def _ensure_fetch_refspec(repo: Repo) -> None:
    """Убедиться, что remote.origin.fetch настроен (bare-клоны не всегда имеют его).

    Без fetch refspec GitPython кидает AssertionError:
        Remote 'origin' has no refspec set.
    """
    try:
        remote = repo.remotes.origin
        config = remote.config_reader
        current = config.get_value("fetch")
        if current and "+refs/heads/" in current:
            return  # уже есть
    except Exception:
        pass
    # Используем config_writer с правильным синтаксисом секции
    try:
        with repo.config_writer() as cw:
            cw.set_value(
                'remote "origin"', "fetch", "+refs/heads/*:refs/heads/*",
            )
    except Exception:
        pass


def _resolve_ref(repo: Repo, ref: str) -> git.Commit:
    """Преобразовать строковый ref (тег, ветка, SHA) в объект Commit."""
    # Сначала пробуем как тег
    try:
        tag = repo.tags[ref]
        return tag.commit
    except (IndexError, TypeError):
        pass
    # Потом как ветка
    try:
        branch = repo.branches[ref]
        return branch.commit
    except (IndexError, TypeError):
        pass
    # Наконец, как SHA
    try:
        return repo.commit(ref)
    except (ValueError, git.BadName):
        raise RefNotFoundError(
            f"Ref '{ref}' не найден."
        )


def _classify_change(diff) -> ChangeType:
    """Классифицировать тип изменения файла."""
    if diff.new_file:
        return ChangeType.ADDED
    if diff.deleted_file:
        return ChangeType.DELETED
    if diff.renamed_file:
        return ChangeType.RENAMED
    if diff.change_type == "M":
        return ChangeType.MODIFIED
    return ChangeType.UNKNOWN


# ── Аутентификация ─────────────────────────────────────────


def get_authenticated_url(repo_url: str, token: Optional[str]) -> str:
    """Встроить токен доступа в URL для git-операций.

    https://github.com/user/repo → https://<user>:TOKEN@...repo
    """
    if not token:
        return repo_url
    if "://" not in repo_url:
        return repo_url
    scheme, rest = repo_url.split("://", 1)
    # Определяем префикс пользователя по хосту
    user = "x-access-token"
    if "gitlab" in rest.lower():
        user = "oauth2"
    elif "bitbucket" in rest.lower():
        user = "x-token-auth"
    return f"{scheme}://{user}:{token}@{rest}"


# ── Clone / Fetch ──────────────────────────────────────────


def clone_repo(repo_url: str, clone_path: str, token: Optional[str] = None) -> Repo:
    """Клонировать репозиторий (bare) в clone_path.

    Если папка уже существует — открыть и сделать fetch.
    Возвращает открытый Repo.
    """
    url = get_authenticated_url(repo_url, token)
    if os.path.isdir(clone_path):
        repo = _get_repo(clone_path)
        _ensure_fetch_refspec(repo)
        repo.remotes.origin.fetch()
        return repo
    repo = Repo.clone_from(url, clone_path, bare=True)
    _ensure_fetch_refspec(repo)
    return repo


def ensure_clone(
    repo_url: str,
    clone_path: str,
    token: Optional[str] = None,
    verbose: bool = False,
) -> Repo:
    """Открыть существующий bare-клон или создать новый.

    Проверяет, что существующий .clone/ принадлежит тому же репозиторию.
    Если URL не совпадает — удаляет старый и клонирует заново.
    Если verbose=True — git clone пишет прогресс в терминал (как сам git).
    """
    # Если клон уже существует — проверяем remote URL
    if os.path.isdir(clone_path):
        try:
            repo = _get_repo(clone_path)
            actual_url = repo.remotes.origin.url

            # Сравниваем owner/repo из URL (без учёта протокола, токена, .git)
            def normalize_url(url: str) -> str:
                # Убираем протокол, токен, .git
                url = re.sub(r'^https?://[^@]+@', '', url)
                url = re.sub(r'\.git$', '', url)
                url = re.sub(r'^git@', '', url)
                url = re.sub(r'^ssh://', '', url)
                return url.rstrip('/')

            if normalize_url(actual_url) == normalize_url(repo_url):
                return repo
            # URL не совпадает — удаляем и клонируем заново
            import shutil
            shutil.rmtree(clone_path)
        except Exception:
            import shutil
            shutil.rmtree(clone_path)

    # Клонируем с прогрессом (stderr в терминал, как сам git)
    url = get_authenticated_url(repo_url, token)
    _clone_with_progress(url, clone_path, verbose)
    repo = _get_repo(clone_path)
    _ensure_fetch_refspec(repo)
    return repo


def _clone_with_progress(
    url: str,
    clone_path: str,
    verbose: bool = False,
) -> None:
    """Клонировать bare-репозиторий.

    При verbose=True git пишет прогресс напрямую в терминал (stderr
    наследуется от родителя) — та же однострочная анимация, что и в git.
    При verbose=False — тихо.
    """
    cmd = ["git", "clone", "--bare"]
    if verbose:
        cmd.append("--progress")
    cmd.extend([url, clone_path])

    if verbose:
        # stderr наследуется от терминала — git сам обновляет одну строку
        proc = subprocess.Popen(cmd)
        proc.wait()
    else:
        proc = subprocess.Popen(cmd, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        proc.wait()

    if proc.returncode != 0:
        raise DocAgentError(
            f"git clone завершился с ошибкой (код {proc.returncode})"
        )


def ref_exists_locally(clone_path: str, ref: str) -> bool:
    """Проверить, доступен ли ref (тег/ветка/SHA) в локальном клоне."""
    try:
        repo = _get_repo(clone_path)
        _resolve_ref(repo, ref)
        return True
    except Exception:
        return False


def ensure_ref_available(clone_path: str, ref: str) -> str:
    """Убедиться, что ref доступен локально. Если нет — сделать fetch.

    Возвращает SHA коммита.
    """
    repo = _get_repo(clone_path)
    try:
        commit = _resolve_ref(repo, ref)
        return commit.hexsha
    except RefNotFoundError:
        _ensure_fetch_refspec(repo)
        repo.remotes.origin.fetch()
        commit = _resolve_ref(repo, ref)
        return commit.hexsha


def fetch_repo(clone_path: str, token: Optional[str] = None) -> None:
    """Сделать fetch в уже существующий bare-клон."""
    repo = _get_repo(clone_path)
    _ensure_fetch_refspec(repo)
    repo.remotes.origin.fetch()


# ── Работа с тегами релизов ───────────────────────────────


def fetch_tags(clone_path: str, token: Optional[str] = None) -> None:
    """Сделать fetch --tags в существующий bare-клон."""
    repo = _get_repo(clone_path)
    _ensure_fetch_refspec(repo)
    repo.remotes.origin.fetch(tags=True)


def get_latest_tag(
    clone_path: str,
    github_token: Optional[str] = None,
    repo_url: Optional[str] = None,
) -> Optional[str]:
    """Получить последний релизный тег.

    Если есть github_token и repo_url — использует GitHub API /releases/latest.
    Иначе — git tag --sort=-version:refname | head -1.
    Возвращает None если тегов нет.
    """
    if github_token and repo_url:
        # Парсим owner/repo из URL
        m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", repo_url)
        if m:
            owner = m.group(1)
            repo_name = m.group(2).rstrip("/")
            api_url = f"https://api.github.com/repos/{owner}/{repo_name}/releases/latest"
            headers = {
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github.v3+json",
            }
            try:
                resp = requests.get(api_url, headers=headers, timeout=10)
                if resp.status_code == 200:
                    return resp.json()["tag_name"]
            except Exception:
                pass

    # Fallback: git tag
    try:
        result = subprocess.run(
            ["git", "tag", "--sort=-version:refname"],
            capture_output=True, text=True, cwd=clone_path,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return None


def get_tag_commit_hash(clone_path: str, tag_name: str) -> Optional[str]:
    """Получить хэш коммита, на который указывает тег."""
    repo = _get_repo(clone_path)
    try:
        tag = repo.tags[tag_name]
        return tag.commit.hexsha
    except (IndexError, TypeError):
        return None


def get_all_tags_with_hash(clone_path: str) -> dict[str, str]:
    """Получить все теги и их хэши: {тег: хэш}."""
    repo = _get_repo(clone_path)
    result: dict[str, str] = {}
    for tag in repo.tags:
        try:
            result[tag.name] = tag.commit.hexsha
        except Exception:
            pass
    return result


def sanitize_tag_name(tag: str) -> str:
    """Заменить / в имени тега на - для файловой системы."""
    return tag.replace("/", "-")


# ── Чтение информации из репозитория ───────────────────────


def get_head_hash(clone_path: str, branch: str = "HEAD") -> str:
    """Получить SHA коммита, на который указывает ветка/HEAD."""
    repo = _get_repo(clone_path)
    commit = _resolve_ref(repo, branch)
    return commit.hexsha


def get_commit_info(clone_path: str, treeish: str) -> CommitInfo:
    """Получить информацию о коммите."""
    repo = _get_repo(clone_path)
    commit = _resolve_ref(repo, treeish)
    return CommitInfo(
        hash=commit.hexsha,
        author=str(commit.author),
        message=commit.message.strip(),
        timestamp=datetime.fromtimestamp(commit.committed_date),
    )


# ── Сканирование и чтение .md-файлов ───────────────────────


def scan_md_files(clone_path: str, treeish: str = "HEAD") -> list[str]:
    """Найти все .md-файлы в репозитории на указанном коммите.

    Returns:
        Список относительных путей (например, ['README.md', 'docs/guide.md'])
    """
    repo = _get_repo(clone_path)
    commit = _resolve_ref(repo, treeish)
    files: list[str] = []
    for blob in commit.tree.traverse():
        if blob.type == "blob" and blob.path.endswith(".md"):
            files.append(blob.path)
    return sorted(files)


def read_file_from_repo(clone_path: str, treeish: str, file_path: str) -> str:
    """Прочитать содержимое файла на указанном коммите."""
    repo = _get_repo(clone_path)
    commit = _resolve_ref(repo, treeish)
    blob = commit.tree / file_path
    return blob.data_stream.read().decode("utf-8", errors="replace")


def extract_file_from_repo(
    clone_path: str, treeish: str, file_path: str, dest_path: str
) -> None:
    """Извлечь файл из репозитория на указанном коммите на диск."""
    full = Path(dest_path)
    full.parent.mkdir(parents=True, exist_ok=True)
    repo = _get_repo(clone_path)
    commit = _resolve_ref(repo, treeish)
    blob = commit.tree / file_path
    data = blob.data_stream.read()
    full.write_bytes(data)


# ── Diff ───────────────────────────────────────────────────


def get_diff_files(clone_path: str, from_ref: str, to_ref: str) -> list[FileChange]:
    """Получить список изменённых файлов между двумя ref."""
    repo = _get_repo(clone_path)
    from_commit = _resolve_ref(repo, from_ref)
    to_commit = _resolve_ref(repo, to_ref)

    diffs = from_commit.diff(to_commit, create_patch=True)

    changes: list[FileChange] = []
    for d in diffs:
        diff_text = d.diff.decode("utf-8", errors="replace") if d.diff else ""
        added = sum(1 for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++"))
        deleted = sum(1 for line in diff_text.splitlines() if line.startswith("-") and not line.startswith("---"))
        changes.append(FileChange(
            path=d.b_path or d.a_path or "",
            change_type=_classify_change(d),
            old_path=d.a_path if d.renamed_file else None,
            added_lines=added,
            deleted_lines=deleted,
        ))
    return changes


def get_raw_diffs(clone_path: str, from_ref: str, to_ref: str) -> dict[str, str]:
    """Получить сырой diff-текст: {путь_файла: текст_патча}."""
    repo = _get_repo(clone_path)
    from_commit = _resolve_ref(repo, from_ref)
    to_commit = _resolve_ref(repo, to_ref)
    diffs = from_commit.diff(to_commit, create_patch=True)

    result: dict[str, str] = {}
    for d in diffs:
        path = d.b_path or d.a_path or ""
        diff_text = d.diff.decode("utf-8", errors="replace") if d.diff else ""
        if diff_text:
            result[path] = diff_text
    return result


def get_new_md_files(clone_path: str, from_ref: str, to_ref: str) -> list[str]:
    """Получить список .md-файлов, добавленных между двумя ref."""
    repo = _get_repo(clone_path)
    from_commit = _resolve_ref(repo, from_ref)
    to_commit = _resolve_ref(repo, to_ref)
    diffs = from_commit.diff(to_commit, create_patch=False)

    added: list[str] = []
    for d in diffs:
        if d.new_file and d.b_path and d.b_path.endswith(".md"):
            added.append(d.b_path)
    return added


def get_deleted_md_files(clone_path: str, from_ref: str, to_ref: str) -> list[str]:
    """Получить список .md-файлов, удалённых между двумя ref."""
    repo = _get_repo(clone_path)
    from_commit = _resolve_ref(repo, from_ref)
    to_commit = _resolve_ref(repo, to_ref)
    diffs = from_commit.diff(to_commit, create_patch=False)

    deleted: list[str] = []
    for d in diffs:
        if d.deleted_file and d.a_path and d.a_path.endswith(".md"):
            deleted.append(d.a_path)
    return deleted


# ── Сканирование и чтение .md-файлов ───────────────────────


def get_snapshot_versions(work_dir: str) -> list[dict]:
    """Получить список версий документации (папок-снэпшотов).

    Returns:
        Список словарей: {name, dir}, отсортированных по имени (reverse).
    """
    root = Path(work_dir)
    versions: list[dict] = []
    for entry in root.iterdir():
        if entry.is_dir() and not entry.name.startswith("."):
            versions.append({"name": entry.name, "dir": str(entry)})
    versions.sort(key=lambda v: v["name"], reverse=True)
    return versions
