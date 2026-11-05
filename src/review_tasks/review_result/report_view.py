"""Structured, lossless projection of validated review results."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from review_tasks.task_capsule.metadata import read_input_truncated
from .review_yaml import (
    ReviewYamlError, SEVERITY_TITLES, expand_review_result_inputs,
    read_review_yaml_documents, published_findings, finding_source_url,
    _load_source_links_for_documents,
)


@dataclass(frozen=True)
class ReportSnapshot:
    version: str
    view: dict[str, Any]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _title(text: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"[`*_#~]", "", text)
    text = " ".join(text.split())
    return text


def build_report_snapshot(task_dir: Path, task: str) -> ReportSnapshot:
    review_info = task_dir / "_review_info"
    paths = expand_review_result_inputs([review_info])
    if not paths:
        raise ReviewYamlError("Структурированный YAML-результат недоступен.")
    documents = read_review_yaml_documents(paths)
    links = _load_source_links_for_documents(documents)
    source_context = {}
    meta = review_info / "meta.txt"
    if meta.exists():
        for line in meta.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in {"task", "target", "ref", "source", "mr_id", "mr_ref"}:
                source_context[key.strip()] = value.strip()
    canonical = {
        "task": task, "source": source_context,
        "documents": [{"source": doc.source.relative_to(review_info).as_posix(),
                       "result": doc.parsed.model_dump(mode="json"),
                       "links": links[doc.source.parent]} for doc in documents],
    }

    def entry(doc, finding):
        record = {
            "id": _digest([doc.source.relative_to(review_info).as_posix(), finding.name]),
            "name": finding.name, "title": _title(finding.observation),
            "file": finding.file, "startLine": finding.start_line, "endLine": finding.end_line,
            "observation": finding.observation,
        }
        url = finding_source_url(doc, finding, links)
        if url:
            record["sourceUrl"] = url
        return record

    published = []
    for severity in SEVERITY_TITLES:
        for doc, finding in published_findings(documents, severity):
            record = entry(doc, finding)
            record.update(number=len(published) + 1, severity=severity, suggestion=finding.suggestion)
            published.append(record)
    rejected = []
    for doc in documents:
        for finding in doc.parsed.findings:
            if finding.self_check.decision == "drop":
                record = entry(doc, finding)
                record["dropReason"] = finding.self_check.drop_reason
                rejected.append(record)
    return ReportSnapshot(_digest(canonical), {
        "schemaVersion": 1, "published": published, "agentRejected": rejected,
        "summary": "\n\n".join(doc.parsed.summary for doc in documents),
        "questionsToAuthor": [item for doc in documents for item in doc.parsed.questions_to_author],
        "missingContext": [item for doc in documents for item in doc.parsed.missing_context],
        "inputTruncated": read_input_truncated(task_dir),
    })
