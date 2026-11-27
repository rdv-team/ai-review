/* global chrome, marked */
(function reportMain(root) {
  "use strict";

  const FRONT_MATTER_RE = /^---\r?\n[\s\S]*?\r?\n---\r?\n?/;
  const FINDING_TITLE_LIMIT = 120;
  const SEVERITIES = { critical: "Критично", important: "Важно", desirable: "Желательно" };
  const DECISIONS = { accepted: "Принято", rejected: "Отклонено" };
  const $ = (id) => document.getElementById(id);
  const changesAction = root.RdvChangesAction || (typeof require !== "undefined" ? require("./changes-action.js") : null);
  const emptyFinding = () => ({ decision: null, comment: "", revision: 0, updatedAt: null });
  const ICON_PATHS = {
    check: 'm5 12 4 4L19 6', cross: 'm6 6 12 12M18 6 6 18',
    comment: 'M21 15a3 3 0 0 1-3 3H8l-5 3V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3Z',
    observation: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Zm0 0v6h6M8 13h8M8 17h6',
    proposal: 'm9 12 2 2 4-4M5 4h14v16H5z',
    pencil: 'm16 3 5 5M3 21l5-1L21 7l-5-5L3 16v5Z',
    save: 'M19 21H5a2 2 0 0 1-2-2V3h14l4 4v12a2 2 0 0 1-2 2ZM7 3v6h10V3M7 21v-8h10v8'
  };
  const icon = (name) => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${ICON_PATHS[name]}"/></svg>`;
  function decisionBadge(decision) {
    const mark = decision === "accepted" ? icon("check") : decision === "rejected" ? icon("cross") : "";
    return `<span class="status-badge ${escapeHtml(decision || "pending")}">${mark}${DECISIONS[decision] || "Без решения"}</span>`;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  }

  function safeUrl(value) {
    const url = String(value || "").trim();
    if (url.startsWith("#")) return url;
    try { return ["https:", "http:", "mailto:"].includes(new URL(url).protocol) ? url : null; }
    catch { return null; }
  }

  function findingTitle(finding) {
    const text = String(finding.title || finding.name || "Наблюдение").replace(/\s+/g, " ").trim();
    const characters = Array.from(text);
    if (characters.length <= FINDING_TITLE_LIMIT) return text;
    const prefix = characters.slice(0, FINDING_TITLE_LIMIT - 1).join("");
    const boundary = prefix.lastIndexOf(" ");
    return `${(boundary > prefix.length / 2 ? prefix.slice(0, boundary) : prefix).trimEnd()}…`;
  }

  function stripFrontMatter(markdown) { return String(markdown || "").replace(FRONT_MATTER_RE, ""); }

  function renderMarkdown(markdown, markedImpl) {
    const parser = markedImpl || (typeof marked !== "undefined" ? marked : null);
    if (!parser || typeof parser.parse !== "function") return escapeHtml(stripFrontMatter(markdown));
    const renderer = new parser.Renderer();
    renderer.html = ({ text }) => escapeHtml(text);
    renderer.link = function renderLink({ href, title, tokens }) {
      const text = this.parser.parseInline(tokens);
      const url = safeUrl(href);
      return url ? `<a href="${escapeHtml(url)}"${title ? ` title="${escapeHtml(title)}"` : ""} target="_blank" rel="noopener noreferrer">${text}</a>` : text;
    };
    renderer.image = ({ text }) => escapeHtml(text);
    return parser.parse(stripFrontMatter(markdown), { gfm: true, breaks: false, renderer });
  }

  function locationText(finding) {
    return `${finding.file || ""}${finding.startLine != null ? `:${finding.startLine}${finding.endLine != null && finding.endLine !== finding.startLine ? `–${finding.endLine}` : ""}` : ""}`;
  }

  function sourceHtml(finding) {
    const label = escapeHtml(locationText(finding));
    const url = safeUrl(finding.sourceUrl);
    return url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
  }

  function sourceBlockHtml(finding) {
    const url = safeUrl(finding.sourceUrl);
    const lines = finding.startLine != null ? (finding.endLine != null && finding.endLine !== finding.startLine ? `Строки ${finding.startLine}–${finding.endLine}` : `Строка ${finding.startLine}`) : "";
    const subtitle = [lines, url ? "" : "Ссылка недоступна"].filter(Boolean).join(" · ");
    const content = `<span class="source-icon">${icon("observation")}</span><span class="source-info"><span class="source-path">${escapeHtml(finding.file || "Файл не указан")}</span>${subtitle ? `<span class="source-subtitle">${escapeHtml(subtitle)}</span>` : ""}</span>`;
    return url ? `<a class="detail-source" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${content}</a>` : `<div class="detail-source unavailable">${content}</div>`;
  }

  function authorQuestionsHtml(questions) {
    if (!questions || !questions.length) return '<p class="muted">Нет данных.</p>';
    const section = (heading, content, level = 4) => content && String(content).trim() ? `<section><h${level}>${heading}</h${level}>${renderMarkdown(content)}</section>` : "";
    return `<span id="question-new-tab" class="visually-hidden">Откроется в новой вкладке</span><div class="author-questions">${questions.map((question, index) => {
      if (typeof question === "string") return `<div class="author-question"><div class="question-text">${renderMarkdown(question)}</div></div>`;
      const id = `author-question-${index}`;
      const channels = (question.verificationChannels || []).map((channel) => {
        const fields = `${section("Проверяли", channel.hypothesis, 5)}${section("Результат", channel.resultNote, 5)}`;
        return fields ? `<div class="question-channel">${fields}</div>` : "";
      }).join("");
      const reference = question.findingLabel ? `<div class="question-reference">${escapeHtml(question.findingLabel)}</div>` : "";
      const research = `${reference}${section("Суть наблюдения", question.observation)}${section("Альтернативное объяснение", question.alternativeInterpretation)}${channels ? `<section><h4>Результаты проверки</h4>${channels}</section>` : ""}${section("Предложение", question.suggestion)}${section("Причина отклонения", question.dropReason)}`;
      const url = safeUrl(question.sourceUrl);
      return `<div class="author-question"><div id="${id}-text" class="question-text">${renderMarkdown(question.text)}</div><div class="question-actions"><details class="question-details"><summary aria-labelledby="${id}-toggle ${id}-text"><span id="${id}-toggle">Подробности проверки</span></summary><div class="question-research">${research}</div></details>${url ? `<a class="question-source" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" aria-labelledby="${id}-source ${id}-text" aria-describedby="question-new-tab"><span id="${id}-source">Открыть код в GitLab</span></a>` : ""}</div></div>`;
    }).join("")}</div>`;
  }

  function markdownText(markdown, markedImpl) {
    const parser = markedImpl || (typeof marked !== "undefined" ? marked : null);
    if (!parser || !parser.lexer) return stripFrontMatter(markdown);
    function tokensText(tokens) {
      return (tokens || []).map((token) => {
        const content = token.tokens ? tokensText(token.tokens) : token.text || "";
        switch (token.type) {
          case "space": return "\n";
          case "code": return `${token.text}\n\n`;
          case "heading": case "paragraph": return `${content}\n\n`;
          case "blockquote": return `${content}\n`;
          case "br": return "\n";
          case "hr": return "\n";
          case "link": return `${content}${safeUrl(token.href) && content !== token.href ? ` (${token.href})` : ""}`;
          case "list": return token.items.map((item, index) => `${token.ordered ? `${token.start + index}.` : "•"} ${tokensText(item.tokens).trimEnd()}`).join("\n") + "\n\n";
          case "table": return [token.header, ...token.rows].map((row) => row.map((cell) => tokensText(cell.tokens)).join("\t")).join("\n") + "\n\n";
          default: return content;
        }
      }).join("");
    }
    return tokensText(parser.lexer(stripFrontMatter(markdown), { gfm: true })).trimEnd();
  }

  function statistics(view, findings) {
    const published = view.published || [];
    const result = { total: published.length, critical: published.filter((item) => item.severity === "critical").length, accepted: 0, rejected: 0, undecided: 0, agentRejected: (view.agentRejected || []).length };
    for (const item of published) {
      const decision = (findings[item.id] || {}).decision;
      if (decision === "accepted") result.accepted += 1;
      else if (decision === "rejected") result.rejected += 1;
      else result.undecided += 1;
    }
    return result;
  }

  function buildJiraExport(view, findings, markedImpl) {
    const accepted = (view.published || []).filter((item) => (findings[item.id] || {}).decision === "accepted");
    const html = accepted.map((item) => `<section><h2>${escapeHtml(SEVERITIES[item.severity] || item.severity)} · ${escapeHtml(item.number)}. ${sourceHtml(item)}</h2><h3>Суть и последствия</h3>${renderMarkdown(item.observation, markedImpl)}${item.suggestion ? `<h3>Предложение</h3>${renderMarkdown(item.suggestion, markedImpl)}` : ""}</section>`).join("\n");
    const text = accepted.map((item) => `${SEVERITIES[item.severity] || item.severity} · ${item.number}. ${locationText(item)}${safeUrl(item.sourceUrl) ? `\n${item.sourceUrl}` : ""}\n\nСуть и последствия\n${markdownText(item.observation, markedImpl)}${item.suggestion ? `\n\nПредложение\n${markdownText(item.suggestion, markedImpl)}` : ""}`).join("\n\n");
    return { html, text, count: accepted.length };
  }

  async function writeJiraClipboard(exported, clipboard = navigator.clipboard, ClipboardItemImpl = root.ClipboardItem) {
    if (!exported.count) throw new Error("Нет принятых замечаний для копирования.");
    if (!clipboard || !clipboard.write || !ClipboardItemImpl) throw new Error("Форматированное копирование недоступно.");
    await clipboard.write([new ClipboardItemImpl({
      "text/html": new Blob([exported.html], { type: "text/html" }),
      "text/plain": new Blob([exported.text], { type: "text/plain" })
    })]);
  }

  class ReportStateQueue {
    constructor({ api, jobId, reportId, findings = {}, onChange = () => {} }) {
      Object.assign(this, { api, jobId, reportId, onChange });
      this.entries = new Map();
      for (const [id, finding] of Object.entries(findings)) this.entry(id, finding);
    }

    entry(id, initial = emptyFinding()) {
      if (!this.entries.has(id)) this.entries.set(id, {
        confirmed: { ...emptyFinding(), ...initial }, draft: { ...emptyFinding(), ...initial },
        generation: 0, decisionGeneration: 0, resetGeneration: 0, commentOverride: null,
        dirty: false, status: "saved", error: null, pending: null, failedSubmission: false
      });
      return this.entries.get(id);
    }

    snapshot(confirmed = false) {
      return Object.fromEntries([...this.entries].map(([id, entry]) => [id, { ...(confirmed ? entry.confirmed : entry.draft) }]));
    }

    hasUnsentComment(id) {
      const entry = this.entry(id);
      return entry.draft.comment !== entry.confirmed.comment;
    }

    edit(id, patch, immediate = false) {
      const entry = this.entry(id);
      Object.assign(entry.draft, patch);
      entry.generation += 1;
      const decisionChanged = Object.prototype.hasOwnProperty.call(patch, "decision");
      const reset = decisionChanged && patch.decision === null && patch.comment === "";
      if (reset) { entry.commentOverride = ""; entry.resetGeneration += 1; }
      if (decisionChanged) {
        entry.decisionGeneration += 1;
        entry.dirty = true;
        if (!["conflict", "report-changed"].includes(entry.status)) {
          entry.status = "pending";
          entry.error = null;
          if (immediate) this.flush(id);
        }
      }
      // Comment edits are local drafts. No timer, blur, navigation or decision
      // save may send this text before the explicit submission action.
      this.onChange(id);
    }

    reset(id) { this.edit(id, { decision: null, comment: "" }, true); }

    flush(id, force = false, submission = null) {
      const entry = this.entry(id);
      if (entry.pending) return entry.pending;
      if (["conflict", "report-changed"].includes(entry.status)) return Promise.resolve();
      if (!entry.dirty && !force) return Promise.resolve();
      entry.pending = this.saveLoop(id, submission).finally(() => { entry.pending = null; });
      return entry.pending;
    }

    async saveLoop(id, submission) {
      const entry = this.entry(id);
      let result = null;
      do {
        const decisionGeneration = entry.decisionGeneration;
        const resetGeneration = entry.resetGeneration;
        const payload = {
          reportId: this.reportId, findingId: id, decision: entry.draft.decision,
          comment: submission ? submission.comment : entry.commentOverride !== null ? entry.commentOverride : entry.confirmed.comment,
          expectedRevision: entry.confirmed.revision, submitToMemory: Boolean(submission)
        };
        entry.status = "saving";
        entry.failedSubmission = Boolean(submission);
        this.onChange(id);
        let body;
        try { body = await this.api.saveReportState(this.jobId, payload); }
        catch (error) {
          body = error.body;
          if (!body || !body.stateSaved || !body.finding) {
            entry.error = error;
            const code = body && body.error && body.error.code;
            entry.status = code === "report_state_conflict" ? "conflict" : ["report_changed", "report_state_unavailable"].includes(code) ? "report-changed" : "error";
            this.onChange(id);
            return result;
          }
        }
        entry.confirmed = { ...body.finding };
        entry.draft.revision = body.finding.revision;
        entry.draft.updatedAt = body.finding.updatedAt;
        if (resetGeneration === entry.resetGeneration) entry.commentOverride = null;
        entry.dirty = decisionGeneration !== entry.decisionGeneration;
        entry.error = body.memorySaved === false ? new Error("Состояние сохранено, аналитическая память не обновлена.") : null;
        if (submission) result = { generation: submission.generation, memorySaved: body.memorySaved === true };
        entry.failedSubmission = Boolean(submission) && body.memorySaved === false;
        entry.status = entry.error ? "memory-error" : entry.dirty ? "pending" : "saved";
        this.onChange(id);
        // Only later decision/reset changes are flushed. Later comment edits
        // stay local even when an explicit submission just completed.
        submission = null;
      } while (entry.dirty);
      return result;
    }

    async retry(id) {
      const entry = this.entry(id);
      if (entry.status === "report-changed") return;
      if (entry.status === "conflict") {
        try {
          const current = await this.api.getReportState(this.jobId);
          if (current.reportId !== this.reportId) { entry.status = "report-changed"; this.onChange(id); return; }
          entry.confirmed = { ...emptyFinding(), ...current.findings[id] };
        } catch (error) { entry.error = error; this.onChange(id); return; }
      }
      entry.status = "pending";
      // The save retry is for decisions only. Comments are sent exclusively
      // through submit(), including retrying a failed explicit submission.
      return this.flush(id, true);
    }

    async submit(id) {
      const entry = this.entry(id);
      const submission = { comment: entry.draft.comment, generation: entry.generation };
      const resetGeneration = entry.resetGeneration;
      if (!submission.comment.trim() && !this.hasUnsentComment(id) && !entry.failedSubmission) throw new Error("Добавьте комментарий.");
      await this.flush(id);
      if (entry.dirty || ["conflict", "report-changed"].includes(entry.status) || (entry.status === "error" && !entry.failedSubmission)) throw new Error("Сначала сохраните решение карточки.");
      if (resetGeneration !== entry.resetGeneration) throw new Error("Отправка отменена после сброса карточки.");
      const result = await this.flush(id, true, submission);
      if (["error", "conflict", "report-changed"].includes(entry.status)) throw new Error("Не удалось отправить комментарий. Проверьте ошибку и повторите попытку.");
      if (!result || !result.memorySaved) throw new Error("Отправка в память не подтверждена. Повторите отправку.");
      return result;
    }

    async drain() {
      await Promise.all([...this.entries].map(([id, entry]) => ["error", "conflict", "report-changed"].includes(entry.status) ? entry.pending : this.flush(id)));
      if ([...this.entries.values()].some((entry) => entry.dirty || (["error", "conflict", "report-changed"].includes(entry.status) && !entry.failedSubmission))) throw new Error("Сначала сохраните решения: проверьте ошибки в карточках и повторите сохранение.");
    }
  }

  async function copyToClipboard(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch { return false; }
  }
  async function handleCopyClick(button, text) {
    const success = await copyToClipboard(text);
    root.RdvUiFeedback.flashButtonFeedback(button, { success, successLabel: "Скопировано", failLabel: "Не удалось" });
  }
  const pageUtils = root.RdvArtifactPageUtils;
  const jobIdFromLocation = (locationRef) => pageUtils.jobIdFromLocation(locationRef);
  const issueKeyFromLocation = (locationRef) => pageUtils.issueKeyFromLocation(locationRef);
  function renderError(message) { $("error").hidden = false; $("error").textContent = message; }

  function applyReportHeading(titleElement, issueKeyElement, label) {
    pageUtils.applyArtifactPageTitle(null, "report", label);
    titleElement.textContent = "Отчёт ревью";
    issueKeyElement.textContent = label;
    issueKeyElement.hidden = !label;
  }

  function attachPanelResizer(workspace, separator, windowRef = window) {
    if (!separator) return;
    let width = 400;
    let drag = null;
    function setWidth(next) {
      const maximum = Math.max(280, Math.min(720, Math.floor(workspace.getBoundingClientRect().width - 18 - 480)));
      width = Math.round(Math.max(280, Math.min(maximum, next)));
      workspace.style.setProperty("--finding-list-width", `${width}px`);
      separator.setAttribute("aria-valuemin", "280");
      separator.setAttribute("aria-valuemax", String(Math.floor(maximum)));
      separator.setAttribute("aria-valuenow", String(width));
      separator.setAttribute("aria-valuetext", `${width} px`);
    }
    separator.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.isPrimary === false) return;
      event.preventDefault();
      drag = { pointerId: event.pointerId, x: event.clientX, width };
      separator.setPointerCapture(event.pointerId);
      workspace.classList.add("resizing");
    });
    separator.addEventListener("pointermove", (event) => {
      if (drag && event.pointerId === drag.pointerId) setWidth(drag.width + event.clientX - drag.x);
    });
    function stopDrag(event) {
      if (!drag || event.pointerId !== drag.pointerId) return;
      drag = null;
      workspace.classList.remove("resizing");
      if (separator.hasPointerCapture(event.pointerId)) separator.releasePointerCapture(event.pointerId);
    }
    for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) separator.addEventListener(type, stopDrag);
    separator.addEventListener("dblclick", () => setWidth(400));
    separator.addEventListener("keydown", (event) => {
      const next = { ArrowLeft: width - 20, ArrowRight: width + 20, Home: 280, End: 720 }[event.key];
      if (next == null) return;
      event.preventDefault();
      setWidth(next);
    });
    windowRef.addEventListener("resize", () => setWidth(width));
    setWidth(width);
  }

  function attachInteractive(view, reportState, api, jobId, discussion = null, readOnly = false, report = {}) {
    let tab = "published";
    let suggestionEditor = null;
    let suggestionRequest = null;
    let listTab = null;
    const commentExpanded = new Map();
    const agentSends = new Map();
    const selected = { published: (view.published[0] || {}).id, agentRejected: (view.agentRejected[0] || {}).id };
    const queue = readOnly ? null : new ReportStateQueue({ api, jobId, reportId: view.reportId, findings: reportState.findings, onChange: update });
    if (queue) for (const finding of view.published) queue.entry(finding.id);
    const findingState = (id) => readOnly ? (reportState.findings || {})[id] || emptyFinding() : queue.entry(id).draft;
    const hasUnsentComment = (id) => !readOnly && queue.hasUnsentComment(id);
    function currentFinding() { return (view[tab] || []).find((item) => item.id === selected[tab]); }
    function update() {
      const counts = statistics(view, readOnly ? reportState.findings || {} : queue.snapshot());
      $("statistics").innerHTML = [["Всего", counts.total, "total"], ["Критично", counts.critical, "critical"], ["Принято", counts.accepted, "accepted"], ["Отклонено", counts.rejected, "rejected"], ["Без решения", counts.undecided, "undecided"]].map(([label, count, tone]) => `<span class="metric"><span class="metric-dot ${tone}" aria-hidden="true"></span>${label}<strong>${count}</strong></span>`).join("");
      const reviewed = counts.accepted + counts.rejected;
      const complete = counts.total > 0 && reviewed === counts.total;
      $("statistics").innerHTML += `<span class="metric review-progress${complete ? " complete" : ""}" title="${complete ? "Все замечания рассмотрены" : "Принятые и отклонённые замечания"}">${complete ? icon("check") : '<span class="metric-dot" aria-hidden="true"></span>'}Рассмотрено<strong>${reviewed} / ${counts.total}</strong><progress max="${counts.total || 1}" value="${reviewed}" aria-label="Рассмотрено замечаний"></progress></span>`;
      $("previewJira").textContent = `Скопировать принятые · ${counts.accepted}`;
      $("previewJira").disabled = counts.accepted === 0;
      renderList();
      if (readOnly || tab !== "published" || !currentFinding()) return;
      const entry = queue.entry(selected.published);
      $("accept").setAttribute("aria-pressed", String(entry.draft.decision === "accepted"));
      $("reject").setAttribute("aria-pressed", String(entry.draft.decision === "rejected"));
      $("saveStatus").dataset.state = entry.status;
      $("saveStatus").textContent = ({ saved: entry.confirmed.decision == null ? "Без решения" : "Решение сохранено", pending: "Решение ожидает сохранения…", saving: "Сохранение решения…", "memory-error": "Состояние сохранено · память не обновлена", error: "Ошибка сохранения · повторите попытку", conflict: "Изменено в другой вкладке · повтор сохранит ваше решение", "report-changed": "Отчёт изменён · обновите страницу" })[entry.status];
      $("retrySave").hidden = !["error", "conflict", "memory-error"].includes(entry.status);
      $("reloadReport").hidden = entry.status !== "report-changed";
      if ($("detailDecision")) $("detailDecision").innerHTML = decisionBadge(entry.draft.decision);
      updateCommentEditor();
    }
    function renderList() {
      const list = $("findingList");
      // Keep button nodes stable: a save started by textarea blur must not remove
      // the card that is about to receive the user's click.
      if (listTab === tab) {
        for (const card of list.querySelectorAll(".finding-card")) {
          const item = (view[tab] || []).find((finding) => finding.id === card.dataset.findingId);
          updateCard(card, item);
        }
        return;
      }
      listTab = tab;
      list.replaceChildren();
      if (tab === "summary") return;
      const items = view[tab] || [];
      if (!items.length) { list.innerHTML = `<p class="empty">${tab === "published" ? "Опубликованных замечаний нет." : "Исключённых наблюдений нет."}</p>`; return; }
      const groups = tab === "published" ? Object.keys(SEVERITIES).map((severity) => [severity, items.filter((item) => item.severity === severity)]) : [["agent", items]];
      for (const [severity, group] of groups) {
        if (!group.length) continue;
        const heading = document.createElement("h2");
        heading.className = `severity-heading ${severity}`;
        heading.textContent = `${SEVERITIES[severity] || "Исключено агентом"} · ${group.length}`;
        list.appendChild(heading);
        for (const item of group) {
          const card = document.createElement("button");
          card.dataset.findingId = item.id;
          updateCard(card, item);
          card.addEventListener("click", () => { flushSelected(); if (selected[tab] !== item.id) suggestionEditor = null; selected[tab] = item.id; showDetails(); update(); });
          list.appendChild(card);
        }
      }
    }
    function updateCard(card, item) {
      const state = tab === "published" ? findingState(item.id) : null;
      card.className = `finding-card${selected[tab] === item.id ? " selected" : ""}`;
      card.setAttribute("aria-pressed", String(selected[tab] === item.id));
      card.innerHTML = `<span class="card-title">${escapeHtml(item.number != null ? `${item.number}. ` : "")}${escapeHtml(findingTitle(item))}</span><span class="card-location">${escapeHtml(locationText(item))}</span>${state ? `<span class="card-status">${decisionBadge(state.decision)}${state.comment.trim() || hasUnsentComment(item.id) ? `<span class="comment-indicator">${icon("comment")}${hasUnsentComment(item.id) ? "Черновик комментария" : "Есть комментарий"}</span>` : ""}</span>` : ""}`;
    }
    function flushSelected() { if (!readOnly && tab === "published" && selected.published) queue.flush(selected.published); }
    function proposalHtml(finding) {
      const editable = !readOnly && report.canEditSuggestions === true;
      const editing = suggestionEditor && suggestionEditor.findingId === finding.id;
      const button = (id, symbol, label) => `<button type="button" id="${id}" class="suggestion-action" title="${label}" aria-label="${label}"${suggestionRequest ? " disabled" : ""}>${icon(symbol)}</button>`;
      const actions = !editable ? "" : editing
        ? button("saveSuggestion", "save", "Сохранить предложение") + button("cancelSuggestion", "cross", "Отменить")
        : button("editSuggestion", "pencil", "Редактировать предложение");
      return `<section class="finding-section proposal-section"><h3>${icon("proposal")}Предложение<span class="suggestion-actions">${actions}</span></h3>${editing
        ? `<textarea id="suggestionDraft" class="suggestion-draft" aria-label="Предложение в Markdown"${suggestionRequest ? " disabled" : ""}>${escapeHtml(suggestionEditor.draft)}</textarea><p class="suggestion-error" role="status">${escapeHtml(suggestionEditor.error || "")}</p>`
        : renderMarkdown(finding.suggestion)}</section>`;
    }
    function bindSuggestion(finding) {
      if (readOnly || report.canEditSuggestions !== true) return;
      if (!suggestionEditor) {
        $("editSuggestion").addEventListener("click", () => {
          if (suggestionRequest) return;
          suggestionEditor = { findingId: finding.id, draft: finding.suggestion, expectedSuggestion: finding.suggestion };
          showDetails(); $("suggestionDraft").focus();
        });
        return;
      }
      $("suggestionDraft").addEventListener("input", () => { suggestionEditor.draft = $("suggestionDraft").value; });
      $("cancelSuggestion").addEventListener("click", () => { if (!suggestionRequest) { suggestionEditor = null; showDetails(); } });
      $("saveSuggestion").addEventListener("click", () => {
        if (suggestionRequest) return suggestionRequest;
        const editor = suggestionEditor;
        const payload = { reportId: view.reportId, findingId: editor.findingId, suggestion: editor.draft, expectedSuggestion: editor.expectedSuggestion };
        suggestionRequest = (async () => {
          try {
            const result = await Promise.resolve().then(() => api.saveReportSuggestion(jobId, payload));
            const target = view.published.find((item) => item.id === payload.findingId);
            target.suggestion = result.suggestion;
            report.content = result.content;
            if (suggestionEditor === editor) suggestionEditor = null;
          } catch (error) {
            const message = error.message || "Не удалось сохранить предложение. Повторите попытку.";
            if (suggestionEditor === editor) editor.error = message;
            else renderError(message);
          } finally {
            suggestionRequest = null;
            showDetails();
          }
        })();
        showDetails();
        return suggestionRequest;
      });
    }
    function showDetails() {
      $("decisionPanel").hidden = true;
      if (discussion) discussion.setFinding(tab === "published" ? currentFinding() : null, view.reportId);
      $("workspace").classList.toggle("summary-mode", tab === "summary");
      if (tab === "summary") {
        const section = (title, value) => `<h3>${title}</h3>${Array.isArray(value) ? value.length ? `<ul>${value.map((item) => `<li>${renderMarkdown(item)}</li>`).join("")}</ul>` : '<p class="muted">Нет данных.</p>' : value ? renderMarkdown(value) : '<p class="muted">Нет данных.</p>'}`;
        $("details").innerHTML = `<div class="text-column"><h2>Итоги исследования</h2><h3>Вопросы автору</h3>${authorQuestionsHtml(view.questionsToAuthor)}${section("Недостающий контекст", view.missingContext)}</div>`;
        return;
      }
      const finding = currentFinding();
      $("details").scrollTop = 0;
      if (!finding) { $("details").innerHTML = '<p class="empty">В этом разделе нет замечаний.</p>'; return; }
      const badges = tab === "published" ? `<div class="detail-badges"><span class="severity-badge ${escapeHtml(finding.severity)}">${escapeHtml(SEVERITIES[finding.severity])}</span><span id="detailDecision">${decisionBadge(findingState(finding.id).decision)}</span><span class="detail-index">Замечание №${escapeHtml(finding.number)}</span></div>` : `<div class="detail-badges"><span class="status-badge">Исключено агентом</span>${finding.number != null ? `<span class="detail-index">Отклонено агентом №${escapeHtml(finding.number)}</span>` : ""}</div>`;
      const section = (className, heading, content, symbol) => `<section class="finding-section ${className}"><h3>${icon(symbol)}${heading}</h3>${renderMarkdown(content)}</section>`;
      const savedComment = readOnly && tab === "published" ? findingState(finding.id).comment : "";
      $("details").innerHTML = `<div class="text-column">${badges}${sourceBlockHtml(finding)}${section("observation-section", "Суть наблюдения", finding.observation, "observation")}${tab === "published" ? finding.suggestion ? proposalHtml(finding) : "" : section("drop-section", "Причина отклонения", finding.dropReason, "cross")}${savedComment ? `<section class="finding-section"><h3>${icon("comment")}Сохранённый комментарий</h3><p class="saved-comment">${escapeHtml(savedComment)}</p></section>` : ""}</div>`;
      if (tab === "published" && finding.suggestion) bindSuggestion(finding);
      if (!readOnly && tab === "published") { $("decisionPanel").hidden = false; $("comment").value = findingState(finding.id).comment; }
    }
    function updateCommentEditor() {
      const id = selected.published;
      const entry = queue.entry(id);
      const expanded = commentExpanded.get(id) !== false;
      $("commentEditor").hidden = !expanded;
      $("toggleComment").setAttribute("aria-expanded", String(expanded));
      $("commentToggleLabel").textContent = queue.hasUnsentComment(id) ? "Комментарий · черновик" : entry.draft.comment.trim() ? "Комментарий · есть текст" : "Комментарий";
      const sending = agentSends.get(id) || {};
      $("sendAgent").disabled = (!entry.draft.comment.trim() && !queue.hasUnsentComment(id) && !entry.failedSubmission) || sending.pending === true;
      $("sendAgent").title = "Сохранить обратную связь в локальный файл памяти. Агент не запускается.";
      $("agentSendStatus").textContent = sending.pending ? "Отправка…" : sending.error ? sending.error : sending.sent ? "Отправлено в память" : queue.hasUnsentComment(id) ? "Комментарий не отправлен" : "";
      $("agentSendStatus").dataset.state = sending.error ? "error" : "";
    }
    function setCommentExpanded(expanded) {
      commentExpanded.set(selected.published, expanded);
      if (!expanded) flushSelected();
      updateCommentEditor();
      if (expanded) $("comment").focus();
    }
    function clearAgentStatus(id) {
      if ((agentSends.get(id) || {}).pending) agentSends.set(id, { pending: true });
      else agentSends.delete(id);
    }
    for (const button of $("tabs").querySelectorAll("button")) {
      if (button.dataset.tab === "published") button.textContent = `Замечания · ${view.published.length}`;
      if (button.dataset.tab === "agentRejected") button.textContent = `Отклонено агентом · ${view.agentRejected.length}`;
      if (button.dataset.tab === "summary") button.textContent = (view.questionsToAuthor || []).length ? `Итоги · вопросов: ${view.questionsToAuthor.length}` : "Итоги";
      button.addEventListener("click", () => {
        flushSelected(); if (tab !== button.dataset.tab) suggestionEditor = null; tab = button.dataset.tab;
        for (const other of $("tabs").querySelectorAll("button")) other.setAttribute("aria-pressed", String(other === button));
        showDetails(); update();
      });
    }
    if (!readOnly) {
    for (const [id, decision] of [["accept", "accepted"], ["reject", "rejected"]]) $(id).addEventListener("click", () => { clearAgentStatus(selected.published); queue.edit(selected.published, { decision }, true); });
    $("reset").addEventListener("click", () => {
      clearAgentStatus(selected.published);
      $("comment").value = "";
      queue.reset(selected.published);
    });
    $("comment").addEventListener("input", () => { clearAgentStatus(selected.published); queue.edit(selected.published, { comment: $("comment").value }); });
    $("comment").addEventListener("blur", flushSelected);
    $("toggleComment").addEventListener("click", () => setCommentExpanded(commentExpanded.get(selected.published) === false));
    $("sendAgent").addEventListener("click", async () => {
      const id = selected.published;
      const entry = queue.entry(id);
      const generation = entry.generation;
      if ((!entry.draft.comment.trim() && !queue.hasUnsentComment(id) && !entry.failedSubmission) || (agentSends.get(id) || {}).pending) return;
      agentSends.set(id, { pending: true }); update();
      try {
        const result = await queue.submit(id);
        if (result.generation === entry.generation) agentSends.set(id, { sent: true });
        else agentSends.delete(id);
      } catch (error) {
        if (generation === entry.generation) agentSends.set(id, { error: error.message || "Не удалось отправить. Повторите попытку." });
        else agentSends.delete(id);
      }
      update();
    });
    $("retrySave").addEventListener("click", () => queue.retry(selected.published));
    $("reloadReport").addEventListener("click", () => window.location.reload());
    window.addEventListener("beforeunload", (event) => {
      if (suggestionRequest || (suggestionEditor && suggestionEditor.draft !== suggestionEditor.expectedSuggestion) || [...queue.entries].some(([id, entry]) => entry.dirty || entry.pending || queue.hasUnsentComment(id))) { event.preventDefault(); event.returnValue = ""; }
    });
    async function refreshPreview() {
      await suggestionRequest;
      await queue.drain();
      const exported = buildJiraExport(view, queue.snapshot(true));
      $("jiraPreview").innerHTML = exported.html;
      $("copyJira").disabled = exported.count === 0;
      return exported;
    }
    $("previewJira").addEventListener("click", async () => {
      try { await refreshPreview(); $("clipboardStatus").textContent = "Проверьте материал перед копированием."; $("jiraDialog").showModal(); }
      catch (error) { renderError(error.message); }
    });
    $("closePreview").addEventListener("click", () => $("jiraDialog").close());
    $("copyJira").addEventListener("click", async () => {
      try {
        const exported = await refreshPreview();
        await writeJiraClipboard(exported);
        $("clipboardStatus").textContent = "Скопировано с форматированием. Вставьте в Jira.";
        root.RdvUiFeedback.flashButtonFeedback($("copyJira"), { success: true, successLabel: "Скопировано" });
      } catch (error) {
        $("clipboardStatus").textContent = `${error.message} Выделите материал в предпросмотре и скопируйте вручную.`;
        root.RdvUiFeedback.flashButtonFeedback($("copyJira"), { success: false, failLabel: "Не удалось" });
      }
    });
    }
    for (const id of ["workspace", "statistics", "tabs"]) $(id).hidden = false;
    $("previewJira").hidden = readOnly;
    $("truncatedWarning").hidden = view.inputTruncated !== true;
    showDetails(); update();
    attachPanelResizer($("workspace"), $("panelResizer"));
    return queue;
  }

  function attachDiscussionAction(api, jobId, capability) {
    const reportButton = $("discussReport");
    const findingButton = $("discussFinding");
    const status = $("discussionStatus");
    const message = $("discussionMessage");
    const dismiss = $("dismissDiscussionStatus");
    const available = capability && capability.available === true;
    const reason = !capability ? "Обновите локальный сервис, чтобы обсуждать отчёт с агентом."
      : !available ? (capability.reason && capability.reason.message) || "Обсуждение недоступно для этого отчёта." : "";
    let selectedContext = null;
    let pending = false;
    let statusTimer;
    function updateButtons() {
      reportButton.disabled = pending;
      findingButton.disabled = pending || !selectedContext;
      reportButton.setAttribute("aria-disabled", String(!available || pending));
      findingButton.setAttribute("aria-disabled", String(!available || pending || !selectedContext));
      findingButton.hidden = !selectedContext;
      reportButton.setAttribute("aria-busy", String(pending));
      findingButton.setAttribute("aria-busy", String(pending));
    }
    function showStatus(text, state = "", duration = 0) {
      clearTimeout(statusTimer);
      message.textContent = text;
      status.dataset.state = state;
      status.hidden = !text;
      dismiss.hidden = state !== "error";
      if (duration) statusTimer = setTimeout(() => showStatus(""), duration);
    }
    async function open(context) {
      if (pending || context === null) return;
      if (!available) { showStatus(reason, "error"); return; }
      // Capture the selected IDs before awaiting; later selection changes only
      // affect the next opening, never the request already being sent.
      const payload = { ...context };
      pending = true;
      updateButtons();
      showStatus("Открытие OpenCode…");
      try {
        await api.openDiscussion(jobId, payload);
        showStatus("Процесс OpenCode запущен. Дождитесь готовности в открывшемся окне.", "", 3000);
      } catch (error) {
        showStatus(error.message || "Не удалось открыть обсуждение. Повторите попытку.", "error");
      } finally {
        pending = false;
        updateButtons();
      }
    }
    reportButton.addEventListener("click", () => open({}));
    findingButton.addEventListener("click", () => open(selectedContext));
    dismiss.addEventListener("click", () => showStatus(""));
    showStatus("");
    updateButtons();
    return {
      setFinding(finding, reportId) {
        selectedContext = finding ? { reportId, findingId: finding.id } : null;
        updateButtons();
      }
    };
  }

  async function attachChangesAction(api, jobId) {
    const button = $("openChanges");
    button.hidden = true;
    if (!jobId) return;
    try {
      const response = await api.refreshStatus(jobId);
      if (!changesAction.canOpenChanges(response.status)) return;
    } catch {
      return;
    }
    button.hidden = false;
    let changesError = null;
    button.addEventListener("click", () => changesAction.openChanges(api, jobId, button, null, {
      successLabel: "Открыто",
      failLabel: "Не удалось",
      onError: (error) => {
        changesError = error.message || "Не удалось открыть изменения. Повторите попытку.";
        renderError(changesError);
      },
      onSuccess: () => {
        if (changesError && $("error").textContent === changesError) {
          $("error").textContent = "";
          $("error").hidden = true;
        }
      }
    }));
  }

  async function attachVersions(api, jobId, versionId) {
    const control = $("versionControl");
    const list = $("reportVersion");
    function render(versions, currentAvailable = true) {
      list.replaceChildren();
      const add = (value, label, disabled = false) => {
        const item = document.createElement(disabled ? "span" : "a");
        item.textContent = label;
        if (disabled) item.setAttribute("aria-disabled", "true");
        else {
          const target = new URL(window.location.href);
          if (value) target.searchParams.set("version", value);
          else target.searchParams.delete("version");
          item.setAttribute("href", target.href);
        }
        if (value === versionId) item.setAttribute("aria-current", "page");
        list.appendChild(item);
      };
      add("", currentAvailable ? "Текущая" : "Текущая · недоступна", !currentAvailable);
      for (const version of versions) add(version.versionId, `Архивировано ${new Date(version.archivedAt).toLocaleString()}`);
      if (versionId && !versions.some((version) => version.versionId === versionId)) add(versionId, "Выбранная архивная версия · недоступна", true);
      const selected = versions.find((version) => version.versionId === versionId);
      $("versionLabel").textContent = !versionId ? "Текущая" : selected ? new Date(selected.archivedAt).toLocaleString(undefined, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "Архив";
      control.title = selected ? `Архивировано ${new Date(selected.archivedAt).toLocaleString()}` : "Версии отчёта";
      control.hidden = !versionId && versions.length === 0;
    }
    render([]);
    control.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { control.open = false; control.querySelector("summary").focus(); }
    });
    window.addEventListener("click", (event) => {
      if (control.open && !control.contains(event.target)) control.open = false;
    });
    try {
      const history = await api.getReportVersions(jobId);
      render(history.versions, history.currentAvailable);
    } catch {
      // Older services keep their current-report behavior without history.
    }
  }

  async function init() {
    const jobId = jobIdFromLocation(window.location);
    if (!jobId) { renderError("jobId не указан."); return; }
    const issueKeyFromUrl = issueKeyFromLocation(window.location);
    const versionId = new URL(window.location.href).searchParams.get("version") || "";
    const readOnly = Boolean(versionId);
    $("readOnlyNotice").hidden = !readOnly;
    if (readOnly) for (const id of ["discussReport", "discussFinding", "openChanges", "previewJira", "decisionPanel"]) $(id).hidden = true;
    const title = (issueKeyFromApi) => applyReportHeading($("title"), $("reportIssueKey"), pageUtils.resolveArtifactLabel({ issueKeyFromUrl, issueKeyFromApi, jobId }));
    title("");
    try {
      const stored = await chrome.storage.local.get({ servicePort: 8765, serviceToken: "" });
      const api = new root.RdvLocalReviewApi.LocalReviewApi({ port: stored.servicePort, token: stored.serviceToken });
      void attachVersions(api, jobId, versionId);
      if (!readOnly) void attachChangesAction(api, jobId);
      const report = await (readOnly ? api.getReportVersion(jobId, versionId) : api.getReport(jobId));
      const discussion = readOnly ? null : attachDiscussionAction(api, jobId, report.discussion);
      title(report.issueKey);
      $("reportPath").textContent = report.reportPath || "";
      $("copyMarkdown").addEventListener("click", () => handleCopyClick($("copyMarkdown"), report.content || ""));
      if (report.view) {
        try {
          const reportState = readOnly ? report.reportState : await api.getReportState(jobId);
          if (!readOnly && reportState.reportId !== report.view.reportId) throw new Error("Отчёт изменился. Обновите страницу.");
          attachInteractive(report.view, reportState, api, jobId, discussion, readOnly, report);
          return;
        } catch (error) { renderError(`Интерактивные действия недоступны: ${error.message}`); }
      } else renderError(readOnly ? `Структурированный просмотр недоступен. Показан исходный Markdown.${report.viewError && report.viewError.message ? ` ${report.viewError.message}` : ""}` : "Интерактивные действия недоступны для этого отчёта. Показан исходный Markdown.");
      $("content").hidden = false;
      $("content").innerHTML = renderMarkdown(report.content || "");
    } catch (error) { renderError(error.message || String(error)); }
  }

  if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", init);
  if (typeof module !== "undefined") module.exports = { jobIdFromLocation, issueKeyFromLocation, stripFrontMatter, renderMarkdown, markdownText, copyToClipboard, handleCopyClick, escapeHtml, safeUrl, findingTitle, sourceBlockHtml, statistics, buildJiraExport, writeJiraClipboard, ReportStateQueue, applyReportHeading, attachPanelResizer, attachInteractive, attachDiscussionAction, attachChangesAction, init };
})(typeof globalThis !== "undefined" ? globalThis : window);
