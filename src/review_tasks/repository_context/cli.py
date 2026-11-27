"""Тонкие argparse-точки входа контекстных команд."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..infrastructure.git import ToolError
from .queries import code_search, file_find, file_read
from .sources import select_sources


def main(command: str, argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="\n")
    descriptions = {
        "code-search": "Поиск по всем сохранённым репозиториям: до 100 совпавших строк суммарно; общий Git timeout 10 секунд.",
        "file-find": "Поиск по всем сохранённым репозиториям: до 100 путей суммарно без признака усечения; общий Git timeout 10 секунд.",
        "file-read": "Чтение единственного совпадения пути или выбранного источника: до 500 строк; общий Git timeout 30 секунд.",
    }
    parser = argparse.ArgumentParser(
        prog=f"review-tasks {command}", description=descriptions[command], allow_abbrev=False,
        epilog=("Запуск из корня задачи. Все выбранные при init репозитории доступны: TARGET материалов "
                "или SHA настроенной ветки на момент подготовки для остальных. "
                "Нужны локальные репозитории с этими ревизиями. Multi-repo результат подписан Repository: ID (имя). "
                "Лимит не ограничивает байты/токены. Код 0: успех, включая пустой результат; 1: ошибка в stderr."),
    )
    parser.add_argument("--repository", help="сузить область до ID. Без флага поиск идёт по всем источникам; чтение выбирает единственное совпадение пути. При одинаковых путях укажите ID из диагностики или результата поиска.")
    if command == "code-search":
        parser.add_argument("--search-text", required=True, help="непустой буквальный текст")
        parser.add_argument("--file-pattern", action="append", help="Git pathspec с исключениями; повторяемый; по умолчанию всё дерево")
        parser.add_argument("--case-sensitive", action="store_true", help="учитывать регистр; по умолчанию не учитывается")
        parser.add_argument("--use-perl-regexp", action="store_true", help="PCRE (Perl Compatible Regular Expressions) вместо буквального текста")
    elif command == "file-find":
        parser.add_argument("--query-name", required=True, help="подстрока имени/пути; пустой запрос не даёт совпадений")
        parser.add_argument("--case-sensitive", action="store_true", help="учитывать регистр; по умолчанию не учитывается")
    else:
        parser.add_argument("--file-path", required=True, help="относительный путь файла в TARGET")
        parser.add_argument("--start-line", type=int, help="начало включительно; по умолчанию и при <=0: 1")
        parser.add_argument("--end-line", type=int, help="конец включительно; по умолчанию и при <=0: конец файла")
    try:
        args = parser.parse_args(argv)
        source = select_sources(Path.cwd(), args.repository)
        if command == "code-search":
            output = code_search(source, args.search_text, file_patterns=args.file_pattern,
                                 case_sensitive=args.case_sensitive, use_perl_regexp=args.use_perl_regexp)
        elif command == "file-find":
            output = file_find(source, args.query_name, case_sensitive=args.case_sensitive)
        else:
            output = file_read(source, args.file_path, start_line=args.start_line, end_line=args.end_line)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 1
    except (ToolError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0
