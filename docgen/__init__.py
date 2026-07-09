"""DocAgent — AI-агент для автоматического поддержания документации.

Работает как мини-агент: LLM с доступом к терминалу в .clone/,
сама решает, где лежит код, проверяет актуальность .md и обновляет.
"""

from __future__ import annotations

from docgen.models import ReleaseMap

__version__ = "0.0.1"
