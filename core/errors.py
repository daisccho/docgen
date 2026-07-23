"""Исключения docgen."""


class DocAgentError(Exception):
    """Базовое исключение docgen."""


class NotGitRepositoryError(DocAgentError):
    """Путь не является git-репозиторием."""


class RefNotFoundError(DocAgentError):
    """Запрашиваемый ref не найден."""


class RateLimitError(DocAgentError):
    """Исчерпан лимит запросов к LLM-провайдеру."""
