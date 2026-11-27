"""Контекст локального репозитория, закреплённый за подготовленным ревью."""

from .sources import prepare_sources, write_sources

__all__ = ["prepare_sources", "write_sources"]
