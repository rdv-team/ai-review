"""Console entry points for the fixed Stage 2 writer surface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any

from review_tasks.sgr_schema import ReviewYamlError
from review_tasks.review_result.stage2_storage import (
    set_result,
    upsert_finding,
    upsert_trace,
    upsert_verification_channel,
)


class _RussianParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"Ошибка аргументов: {message}. Пример вызова приведён в --help.\n")


def _run(
    parser: argparse.ArgumentParser,
    argv: list[str] | None,
    operation: str,
    action: Callable[[argparse.Namespace], tuple[bool, str | None]],
) -> int:
    try:
        args = parser.parse_args(argv)
        changed, candidate = action(args)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 1
    except (ReviewYamlError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    result: dict[str, Any] = {"ok": True, "operation": operation, "changed": changed}
    if candidate is not None:
        result["candidate"] = candidate
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


def finding_main(argv: list[str] | None = None) -> int:
    operation = "stage2-upsert-finding"
    parser = _RussianParser(prog=f"review-tasks {operation}", description="Добавить или обновить finding Stage 2.", allow_abbrev=False)
    parser.add_argument(
        "--candidate",
        required=True,
        help="идентификатор текущего кандидата из `review-tasks stage1-list-candidates`",
    )
    parser.add_argument(
        "--start-line",
        required=True,
        type=int,
        help="минимальный достаточный TARGET-диапазон доказательства",
    )
    parser.add_argument(
        "--end-line",
        required=True,
        type=int,
        help="минимальный достаточный TARGET-диапазон доказательства",
    )
    parser.add_argument(
        "--observation",
        required=True,
        help=(
            "фактическое поведение видимого кода: механизм, который подтверждает или "
            "опровергает подозрение, и его последствие. Не пиши здесь название категории "
            "или рекомендацию"
        ),
    )
    parser.add_argument(
        "--question-to-author",
        help=("вопрос автору по этой находке; при исправлении повтори актуальный текст. "
              "Отсутствие аргумента удаляет вопрос, сохраняя channels и traces"),
    )
    parser.add_argument(
        "--source",
        required=True,
        help=(
            "основной источник вывода: `ai_knowledge` — самостоятельный анализ видимого "
            "кода; `std`/`rdv`/`ssl`/`platform_docs`/`v8std` — соответствующий "
            "подтверждающий источник; `mixed` — несколько типов источников"
        ),
    )
    parser.add_argument(
        "--confidence",
        required=True,
        help=(
            "уверенность в решении после контр-проверки и `verification`: `high` — вывод "
            "прямо подтверждён видимым кодом; `medium` — остаётся ограниченная "
            "неопределённость; `low` — не хватает существенного контекста или подтверждения"
        ),
    )
    parser.add_argument(
        "--false-positive-risk",
        required=True,
        help=(
            "риск ошибочно признать подозрение дефектом после контр-проверки: `low` — "
            "безопасное объяснение опровергнуто; `medium` — остаётся ограниченная "
            "альтернатива; `high` — существенная альтернатива не исключена"
        ),
    )
    parser.add_argument(
        "--alternative-interpretation",
        help=(
            "сильнейшее правдоподобное объяснение, при котором дефекта нет; не передавай "
            "аргумент только если такого объяснения действительно нет"
        ),
    )
    parser.add_argument(
        "--decision",
        required=True,
        help="результат контр-проверки: `keep` — дефект подтверждён; `drop` — подозрение опровергнуто",
    )
    parser.add_argument(
        "--drop-reason",
        help="обязательное для `drop` объяснение выполненной проверки и причины отклонения",
    )
    parser.add_argument(
        "--severity",
        help=(
            "влияние подтверждённого дефекта: `critical` — тяжёлые последствия и срочное "
            "исправление; `important` — существенное нарушение поведения; `desirable` — "
            "ограниченное неблокирующее влияние; для `drop` не передаётся"
        ),
    )
    parser.add_argument(
        "--suggestion-text",
        help=(
            "обязательное для `keep` исправление причины finding, а не пересказ "
            "`--observation`; для `drop` не передаётся"
        ),
    )
    parser.add_argument(
        "--suggestion-code-file",
        help=(
            "необязательный файл `_review_info/tmp/<candidate>-suggestion.bsl` с примером "
            "BSL-исправления для `keep`; имя должно соответствовать `--candidate`. CLI "
            "добавляет файл к `--suggestion-text`. Для `drop` не передаётся"
        ),
    )
    parser.add_argument(
        "--reset-details",
        action="store_true",
        help=(
            "очищает `verification.channels` и `traces`; используй только для удаления "
            "ошибочного канала/trace или полной пересборки"
        ),
    )
    parser.add_argument("--area", help=argparse.SUPPRESS)
    parser.add_argument("--file", help=argparse.SUPPRESS)

    def action(args: argparse.Namespace) -> tuple[bool, str | None]:
        values = vars(args)
        if values.pop("area") is not None or values.pop("file") is not None:
            raise ReviewYamlError("`--area` и `--file` запрещены: identity копируется из Stage 1.")
        changed = upsert_finding(**values)
        return changed, args.candidate

    return _run(parser, argv, operation, action)


def verification_channel_main(argv: list[str] | None = None) -> int:
    operation = "stage2-upsert-verification-channel"
    parser = _RussianParser(
        prog=f"review-tasks {operation}",
        description=(
            "Добавить или обновить один `verification`-канал по ключу `capability`; "
            "CLI пересчитывает `needed` и `verification_summary`."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--candidate",
        required=True,
        help="идентификатор `finding`, уже записанного через `review-tasks stage2-upsert-finding`",
    )
    parser.add_argument(
        "--capability",
        required=True,
        help="стабильный ключ способа проверки; повтор обновляет `channel` с тем же точным значением",
    )
    parser.add_argument(
        "--hypothesis",
        required=True,
        help="конкретное утверждение, которое проверялось; не действие «проверить», а ожидаемый факт",
    )
    parser.add_argument(
        "--status",
        required=True,
        help=(
            "результат проверки: `applied` — получен пригодный результат; `unavailable` — "
            "проверка или пригодный результат недоступны"
        ),
    )
    parser.add_argument(
        "--result-note",
        required=True,
        help="что установлено проверкой либо почему результат недоступен и что осталось неизвестным",
    )

    def action(args: argparse.Namespace) -> tuple[bool, str | None]:
        return upsert_verification_channel(**vars(args)), args.candidate

    return _run(parser, argv, operation, action)


def trace_main(argv: list[str] | None = None) -> int:
    operation = "stage2-upsert-trace"
    parser = _RussianParser(
        prog=f"review-tasks {operation}",
        description=(
            "Добавить или обновить одну межстрочную `trace` по ключу "
            "`established_at + obligation`."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--candidate",
        required=True,
        help="идентификатор `finding`, уже записанного через `review-tasks stage2-upsert-finding`",
    )
    parser.add_argument(
        "--established-at",
        required=True,
        type=int,
        help="TARGET-строка, на которой возникает межстрочное обязательство",
    )
    parser.add_argument(
        "--obligation",
        required=True,
        help="конкретное действие или результат, требуемые после `established_at`",
    )
    parser.add_argument(
        "--status",
        required=True,
        help=(
            "исход: `satisfied` — обязательство выполнено; `violated` — видимые строки "
            "доказывают нарушение; `not_visible` — материала недостаточно для обоих выводов"
        ),
    )
    parser.add_argument(
        "--satisfied-at",
        type=int,
        help=(
            "TARGET-строка выполнения или нарушения: обязательна для `satisfied`, опциональна "
            "для `violated`, не передаётся для `not_visible`"
        ),
    )

    def action(args: argparse.Namespace) -> tuple[bool, str | None]:
        return upsert_trace(**vars(args)), args.candidate

    return _run(parser, argv, operation, action)


def set_result_main(argv: list[str] | None = None) -> int:
    operation = "stage2-set-result"
    parser = _RussianParser(
        prog=f"review-tasks {operation}",
        description=(
            "Атомарно заменить missing_context Stage 2; отсутствие повторяемого аргумента "
            "записывает пустой список."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--question",
        action="append",
        default=[],
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--missing-context",
        action="append",
        default=[],
        help="повторяемый конкретный отсутствующий материал, необходимый для проверки",
    )

    def action(args: argparse.Namespace) -> tuple[bool, str | None]:
        if args.question:
            raise ReviewYamlError(
                "`--question` больше не поддерживается. Запишите вопрос через "
                "`stage2-upsert-finding --question-to-author`."
            )
        del args.question
        return set_result(**vars(args)), None

    return _run(parser, argv, operation, action)


__all__ = ["finding_main", "set_result_main", "trace_main", "verification_channel_main"]
