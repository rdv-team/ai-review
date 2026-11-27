/* global chrome */
(function instructionEditorMain(root) {
  "use strict";

  const INVALID_ISSUE_MESSAGE =
    "Не указан корректный ключ Jira-задачи. Откройте редактор из popup поддерживаемой страницы /browse/ABC-123.";
  const state = {
    issueKey: "",
    savedValue: null,
    pending: false,
    conflict: false,
    externalValue: null,
    loadRequestId: 0,
    refreshRequestId: 0
  };
  const $ = (id) => document.getElementById(id);
  const selection = { draft: null, repositories: null, boundaries: [], requestId: 0, pending: false, error: "", applied: true };

  function selectionMessage(error) {
    if (error && error.status === 404) return "Локальный сервис не поддерживает выбор границы. Обновите сервис; ограничение не заменяется полным ревью.";
    if (error && ["stale_draft", "revision_conflict"].includes(error.code || error.message)) {
      return "Выбор изменился или запуск уже принят. Откройте «Инструкция агенту» заново из popup.";
    }
    return `Не удалось обновить границу. ${error && error.message || "Проверьте сервис и хранилище."}`;
  }

  function renderSelectionStatus() {
    const result = selection.repositories
      ? root.RdvCommitSelection.selectionState(selection.repositories, selection.boundaries) : null;
    $("applyCommitSelection").disabled = selection.pending || Boolean(selection.error) || !result || !result.canApply;
    $("refreshCommitCandidates").disabled = selection.pending || !selection.draft;
    const status = $("commitSelectionStatus");
    status.className = selection.error || (result && result.missing.length) ? "error" : "";
    status.textContent = selection.error || (selection.pending ? "Загрузка списка…"
      : result && result.missing.length ? "Выбранная граница исчезла. Явно выберите другой коммит или «Все коммиты»."
        : result ? `Новых коммитов: ${result.count}.${result.count ? selection.applied ? " Граница сохранена до принятого запуска." : " Нажмите «Сохранить», чтобы сохранить выбор." : " Новых коммитов нет; сохранение недоступно."}` : "");
  }

  function renderCommitRepositories() {
    const container = $("commitRepositories");
    container.replaceChildren();
    const repositories = [...selection.repositories];
    for (const boundary of selection.boundaries) {
      if (!repositories.some((repo) => repo.repoPath === boundary.repoPath)) {
        repositories.push({ repoPath: boundary.repoPath, commits: [] });
      }
    }
    repositories.forEach((repo, index) => {
      const wrapper = document.createElement("div");
      wrapper.className = "commit-repository";
      const boundary = selection.boundaries.find((item) => item.repoPath === repo.repoPath);
      const heading = document.createElement("div");
      heading.className = "commit-repository-heading";
      const label = document.createElement("label");
      label.textContent = repo.repoPath.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || repo.repoPath;
      label.title = repo.repoPath;
      const pathDetails = document.createElement("details");
      pathDetails.className = "commit-repository-path";
      const pathSummary = document.createElement("summary");
      pathSummary.textContent = "Путь";
      const path = document.createElement("code");
      path.textContent = repo.repoPath;
      pathDetails.append(pathSummary, path);
      heading.append(label, pathDetails);
      wrapper.appendChild(heading);
      if (!repo.commits.length && !boundary) {
        wrapper.className += " is-empty";
        const empty = document.createElement("span");
        empty.className = "commit-repository-empty";
        empty.textContent = "Коммитов задачи нет";
        heading.appendChild(empty);
        container.appendChild(wrapper);
        return;
      }
      const select = document.createElement("select");
      select.id = `commitBoundary${index}`;
      label.htmlFor = select.id;
      select.setAttribute("aria-label", `Граница ревью: ${label.textContent}`);
      const detail = document.createElement("div");
      detail.id = `${select.id}Detail`;
      detail.className = "commit-detail";
      select.setAttribute("aria-describedby", detail.id);
      const all = document.createElement("option");
      all.value = "";
      all.textContent = "Все коммиты";
      select.appendChild(all);
      for (const commit of repo.commits) {
        const option = document.createElement("option");
        option.value = commit.sha.toLowerCase();
        // Git %cs is a calendar date. Display it without a timezone conversion.
        option.textContent = `${commit.date} · ${commit.sha.slice(0, 8)} · ${commit.subject}`;
        option.title = `${commit.sha}\n${commit.subject}\n${commit.date}`;
        select.appendChild(option);
      }
      if (boundary && !repo.commits.some((commit) => commit.sha.toLowerCase() === boundary.sha)) {
        const missing = document.createElement("option");
        missing.value = boundary.sha;
        missing.textContent = `${boundary.sha} · граница недоступна`;
        select.appendChild(missing);
      }
      select.value = boundary ? boundary.sha : "";
      const updateDetail = () => {
        const commit = repo.commits.find((item) => item.sha.toLowerCase() === select.value);
        detail.replaceChildren();
        detail.hidden = !select.value;
        if (!select.value) return;
        detail.className = commit ? "commit-detail" : "commit-detail is-missing";
        const meta = document.createElement("div");
        meta.className = "commit-detail-meta";
        const subject = document.createElement("div");
        subject.className = "commit-detail-subject";
        subject.textContent = commit ? commit.subject : "Граница недоступна";
        const badge = document.createElement("span");
        badge.className = "commit-count";
        badge.textContent = commit ? `Новых: ${commit.afterCount}` : "Требуется перевыбор";
        meta.append(subject, badge);
        detail.appendChild(meta);
        if (!commit) {
          const hint = document.createElement("div");
          hint.className = "commit-detail-hint";
          hint.textContent = "Выберите доступный коммит или «Все коммиты».";
          detail.appendChild(hint);
        }
        const more = document.createElement("details");
        more.className = "commit-more";
        const summary = document.createElement("summary");
        summary.textContent = "Подробнее";
        const sha = document.createElement("code");
        sha.className = "commit-detail-sha";
        sha.textContent = commit ? commit.sha : select.value;
        more.append(summary, sha);
        if (commit) {
          const date = document.createElement("div");
          date.className = "commit-detail-date";
          date.textContent = `Дата коммита: ${commit.date}`;
          more.appendChild(date);
        }
        detail.appendChild(more);
      };
      updateDetail();
      select.addEventListener("change", () => {
        selection.applied = false;
        selection.boundaries = selection.boundaries.filter((item) => item.repoPath !== repo.repoPath);
        if (select.value) selection.boundaries.push({ repoPath: repo.repoPath, sha: select.value });
        updateDetail();
        renderSelectionStatus();
      });
      wrapper.append(select, detail);
      container.appendChild(wrapper);
    });
    renderSelectionStatus();
  }

  async function refreshCommitCandidates(refresh = true) {
    if (!selection.draft || selection.pending) return;
    const requestId = ++selection.requestId;
    const draftId = selection.draft.draftId;
    selection.pending = true;
    selection.error = "";
    renderSelectionStatus();
    try {
      await root.RdvCommitSelection.request(chrome.runtime, "read", { draftId });
      const context = selection.draft.context;
      if (context.mode === "mr" && !context.init) {
        selection.error = "Сначала выберите MR в popup. Инструкцию можно редактировать.";
        return;
      }
      const settings = await chrome.storage.local.get({ servicePort: 8765, serviceToken: "" });
      const api = new root.RdvLocalReviewApi.LocalReviewApi({ port: settings.servicePort, token: settings.serviceToken });
      const response = await api.getCommitCandidates(state.issueKey, context.init, refresh);
      await root.RdvCommitSelection.request(chrome.runtime, "read", { draftId });
      if (requestId !== selection.requestId || draftId !== selection.draft.draftId) return;
      if (!response || !response.ok || !Array.isArray(response.repositories)) throw new Error("invalid_candidates");
      selection.repositories = response.repositories;
      renderCommitRepositories();
    } catch (error) {
      if (requestId === selection.requestId) {
        selection.error = selectionMessage(error);
        if (!refresh && error.status !== 404 && !["stale_draft", "revision_conflict"].includes(error.code || error.message)) {
          selection.error += " Нажмите «Обновить список», чтобы получить актуальные коммиты из remote.";
        }
      }
    } finally {
      if (requestId === selection.requestId) {
        selection.pending = false;
        renderSelectionStatus();
      }
    }
  }

  async function applyCommitSelection() {
    if (selection.pending || selection.error || !selection.repositories || !root.RdvCommitSelection.selectionState(selection.repositories, selection.boundaries).canApply) return;
    selection.pending = true;
    renderSelectionStatus();
    try {
      selection.draft = await root.RdvCommitSelection.request(chrome.runtime, "apply", {
        draftId: selection.draft.draftId, revision: selection.draft.revision, boundaries: selection.boundaries
      });
      selection.boundaries = selection.draft.boundaries;
      selection.applied = true;
    } catch (error) {
      selection.error = selectionMessage(error);
    } finally {
      selection.pending = false;
      renderSelectionStatus();
    }
  }

  async function initCommitSelection() {
    $("refreshCommitCandidates").addEventListener("click", () => refreshCommitCandidates(true));
    $("applyCommitSelection").addEventListener("click", applyCommitSelection);
    try {
      const params = new URL(window.location.href).searchParams;
      const draftId = params.get("draftId");
      if (!draftId) {
        selection.error = params.get("selectionUnavailable") === "config_missing"
          ? "Чтобы выбрать коммиты, сначала настройте проект в расширении. Инструкцию можно сохранить сейчас."
          : "Выбор коммитов недоступен. Проверьте настройки проекта и работу локального сервиса, затем откройте страницу заново из расширения. Инструкцию можно сохранить сейчас.";
        renderSelectionStatus();
        return;
      }
      selection.draft = await root.RdvCommitSelection.request(chrome.runtime, "read", { draftId });
      if (selection.draft.context.issueKey !== state.issueKey) throw new Error("stale_draft");
      selection.boundaries = selection.draft.boundaries;
      await refreshCommitCandidates(false);
    } catch (error) {
      selection.error = selectionMessage(error);
      renderSelectionStatus();
    }
  }

  function characterCount(value) {
    return `${root.RdvUserInstructionStore.codePointCount(String(value || ""))} / ${root.RdvUserInstructionStore.MAX_LENGTH}`;
  }

  function draftValidationState(value) {
    const count = root.RdvUserInstructionStore.codePointCount(String(value || ""));
    return { count, overLimit: count > root.RdvUserInstructionStore.MAX_LENGTH };
  }

  function isDirty(draft, savedValue) {
    const canonicalDraft = root.RdvUserInstructionStore.normalizeInstruction(String(draft || ""));
    const canonicalSaved = savedValue === null
      ? ""
      : root.RdvUserInstructionStore.normalizeInstruction(savedValue);
    return canonicalDraft !== canonicalSaved;
  }

  function editorActionState({ draft, savedValue, pending, conflict }) {
    const validation = draftValidationState(draft);
    return {
      saveDisabled: Boolean(pending || conflict || validation.overLimit || !isDirty(draft, savedValue)),
      deleteDisabled: Boolean(pending || conflict || savedValue === null),
      conflictActionsHidden: !conflict,
      overwriteDisabled: Boolean(pending || validation.overLimit)
    };
  }

  function showMessage(kind, text) {
    const message = $("pageMessage");
    message.className = `message ${kind}`;
    message.textContent = text;
  }

  function showSaveStatus(kind, text) {
    const status = $("saveStatus");
    status.className = `save-status ${kind || ""}`.trim();
    status.textContent = text || "";
  }

  function renderActionState() {
    const actions = editorActionState({
      draft: $("instructionText").value,
      savedValue: state.savedValue,
      pending: state.pending,
      conflict: state.conflict
    });
    $("saveInstruction").disabled = actions.saveDisabled;
    $("deleteInstruction").disabled = actions.deleteDisabled;
    $("conflictPanel").hidden = actions.conflictActionsHidden;
    $("reloadCurrentInstruction").disabled = state.pending;
    $("overwriteInstruction").disabled = actions.overwriteDisabled;
  }

  function setPending(pending) {
    state.pending = pending;
    renderActionState();
  }

  function renderDraftState() {
    const draft = $("instructionText").value;
    const dirty = isDirty(draft, state.savedValue);
    const validation = draftValidationState(draft);
    $("characterCount").textContent = characterCount(draft);
    $("characterCount").classList.toggle("is-over-limit", validation.overLimit);
    $("draftValidation").hidden = !validation.overLimit;
    $("instructionText").setAttribute("aria-invalid", validation.overLimit ? "true" : "false");
    $("unsavedMarker").hidden = !dirty;
    document.title = `${state.issueKey || "Jira"}${dirty ? " *" : ""} · Инструкция агенту · RDV AIR`;
    renderActionState();
    return dirty;
  }

  function clearConflict() {
    state.conflict = false;
    state.externalValue = null;
    renderActionState();
  }

  function enterConflict(externalValue) {
    state.conflict = true;
    state.externalValue = externalValue;
    showMessage(
      "validation",
      "Сохранённая инструкция изменилась после открытия страницы. Ваш текст оставлен без изменений."
    );
    showSaveStatus("validation", "Выберите, какую версию оставить.");
    renderDraftState();
  }

  async function persistDraft(storageArea, issueKey, draft) {
    return root.RdvUserInstructionStore.save(storageArea, issueKey, draft);
  }

  async function loadCurrentSnapshot(storageArea, issueKey) {
    const savedValue = await root.RdvUserInstructionStore.load(storageArea, issueKey);
    return { savedValue, draft: savedValue === null ? "" : savedValue };
  }

  async function persistDraftIfCurrent(storageArea, issueKey, baseline, draft) {
    const current = await root.RdvUserInstructionStore.load(storageArea, issueKey);
    if (current !== baseline) {
      return { saved: false, conflict: true, current };
    }
    const savedValue = await persistDraft(storageArea, issueKey, draft);
    return { saved: true, conflict: false, current: savedValue, savedValue };
  }

  async function deleteIfCurrent(storageArea, issueKey, baseline) {
    const current = await root.RdvUserInstructionStore.load(storageArea, issueKey);
    if (current !== baseline) {
      return { deleted: false, conflict: true, current };
    }
    await root.RdvUserInstructionStore.remove(storageArea, issueKey);
    return { deleted: true, conflict: false, current: null };
  }

  function reconcileExternalSnapshot(savedValue, draft, current) {
    if (current === savedValue) {
      return { changed: false, conflict: false, savedValue, draft };
    }
    if (isDirty(draft, savedValue)) {
      return { changed: true, conflict: true, savedValue, draft, current };
    }
    return {
      changed: true,
      conflict: false,
      savedValue: current,
      draft: current === null ? "" : current,
      current
    };
  }

  function applyExternalSnapshot(current) {
    if (current === state.savedValue) {
      if (state.conflict) {
        clearConflict();
        showMessage("success", "Сохранённая инструкция снова совпадает с открытой версией.");
        showSaveStatus("", "");
        renderDraftState();
      }
      return false;
    }
    const result = reconcileExternalSnapshot(state.savedValue, $("instructionText").value, current);
    if (result.conflict) {
      enterConflict(current);
      return true;
    }
    state.savedValue = result.savedValue;
    $("instructionText").value = result.draft;
    clearConflict();
    showMessage("success", "Актуальная сохранённая инструкция загружена.");
    showSaveStatus("success", "Обновлено из локального хранилища.");
    renderDraftState();
    return true;
  }

  async function loadInstruction() {
    const requestId = ++state.loadRequestId;
    setPending(true);
    showMessage("loading", `Загрузка инструкции для ${state.issueKey}…`);
    try {
      await root.RdvUserInstructionStore.ensureTrustedStorageAccess(chrome.storage.local);
      const snapshot = await loadCurrentSnapshot(chrome.storage.local, state.issueKey);
      if (requestId !== state.loadRequestId) {
        return false;
      }
      state.savedValue = snapshot.savedValue;
      $("instructionText").value = snapshot.draft;
      clearConflict();
      $("instructionForm").hidden = false;
      showMessage("success", snapshot.savedValue === null ? "Инструкция для этой задачи не задана." : "Сохранённая инструкция загружена.");
      showSaveStatus("", "");
      renderDraftState();
      $("instructionText").focus();
      return true;
    } catch (error) {
      if (requestId !== state.loadRequestId) {
        return false;
      }
      $("instructionForm").hidden = false;
      showMessage("error", `Не удалось прочитать локально сохранённую инструкцию. ${error.message || ""}`.trim());
      showSaveStatus("error", "Проверьте доступ к хранилищу и перезагрузите страницу.");
      $("instructionText").focus();
      return false;
    } finally {
      if (requestId === state.loadRequestId) {
        setPending(false);
      }
    }
  }

  function validationMessage(error) {
    if (!error || error.name !== "UserInstructionError") {
      return "Не удалось проверить инструкцию.";
    }
    if (error.code === "nul") {
      return "Удалите недопустимый NUL-символ.";
    }
    if (error.code === "too_long") {
      return "Сократите инструкцию до 2000 символов.";
    }
    return "Введите текст инструкции или используйте кнопку «Удалить».";
  }

  function confirmInstructionDeletion(confirmFn) {
    return Boolean(
      typeof confirmFn === "function" &&
      confirmFn("Удалить сохранённую инструкцию и текущий черновик? Это действие нельзя отменить.")
    );
  }

  async function saveInstruction(event) {
    if (event && typeof event.preventDefault === "function") {
      event.preventDefault();
    }
    const draft = $("instructionText").value;
    if (state.pending || state.conflict || !isDirty(draft, state.savedValue)) {
      return false;
    }
    const baseline = state.savedValue;
    let saved = false;
    let conflict = false;
    setPending(true);
    showSaveStatus("loading", "Проверка и сохранение…");
    try {
      const result = await persistDraftIfCurrent(chrome.storage.local, state.issueKey, baseline, draft);
      if (result.conflict) {
        conflict = true;
        enterConflict(result.current);
      } else {
        state.savedValue = result.savedValue;
        $("instructionText").value = result.savedValue === null ? "" : result.savedValue;
        clearConflict();
        showMessage("success", result.savedValue === null ? "Инструкция удалена." : "Инструкция сохранена и будет использоваться в следующих ревью этой задачи.");
        showSaveStatus("success", "Сохранено.");
        renderDraftState();
        saved = true;
      }
    } catch (error) {
      const validation = error && error.name === "UserInstructionError";
      showMessage(validation ? "validation" : "error", validation ? validationMessage(error) : `Не удалось сохранить инструкцию. ${error.message || ""}`.trim());
      showSaveStatus(validation ? "validation" : "error", "Изменения не сохранены. Текст оставлен для исправления.");
      renderDraftState();
    } finally {
      setPending(false);
    }
    if (conflict) {
      $("reloadCurrentInstruction").focus();
    }
    return saved;
  }

  async function deleteInstruction() {
    if (state.pending || state.conflict || state.savedValue === null) {
      return false;
    }
    if (!confirmInstructionDeletion(root.confirm)) {
      return false;
    }
    const draft = $("instructionText").value;
    const baseline = state.savedValue;
    let deleted = false;
    let conflict = false;
    setPending(true);
    showSaveStatus("loading", "Проверка и удаление…");
    try {
      const result = await deleteIfCurrent(chrome.storage.local, state.issueKey, baseline);
      if (result.conflict) {
        conflict = true;
        enterConflict(result.current);
      } else {
        state.savedValue = null;
        clearConflict();
        if ($("instructionText").value === draft) {
          $("instructionText").value = "";
        }
        showMessage("success", "Инструкция удалена. Следующие ревью будут запущены без неё.");
        showSaveStatus("success", "Удалено.");
        renderDraftState();
        $("instructionText").focus();
        deleted = true;
      }
    } catch (error) {
      showMessage("error", `Не удалось удалить инструкцию. ${error.message || ""}`.trim());
      showSaveStatus("error", "Удаление не выполнено. Текст оставлен без изменений.");
      renderDraftState();
    } finally {
      setPending(false);
    }
    if (conflict) {
      $("reloadCurrentInstruction").focus();
    }
    return deleted;
  }

  async function reloadCurrentInstruction() {
    if (state.pending) {
      return false;
    }
    setPending(true);
    showSaveStatus("loading", "Загрузка актуального значения…");
    try {
      const snapshot = await loadCurrentSnapshot(chrome.storage.local, state.issueKey);
      state.savedValue = snapshot.savedValue;
      $("instructionText").value = snapshot.draft;
      clearConflict();
      showMessage("success", "Актуальная сохранённая инструкция загружена. Локальный черновик заменён.");
      showSaveStatus("success", "Актуальная версия загружена.");
      renderDraftState();
      $("instructionText").focus();
      return true;
    } catch (error) {
      showMessage("error", `Не удалось загрузить актуальную инструкцию. ${error.message || ""}`.trim());
      showSaveStatus("error", "Локальный текст оставлен без изменений.");
      renderDraftState();
      return false;
    } finally {
      setPending(false);
    }
  }

  async function overwriteInstruction() {
    if (state.pending || !state.conflict) {
      return false;
    }
    const draft = $("instructionText").value;
    setPending(true);
    showSaveStatus("loading", "Сохранение поверх актуального значения…");
    try {
      const savedValue = await persistDraft(chrome.storage.local, state.issueKey, draft);
      state.savedValue = savedValue;
      $("instructionText").value = savedValue === null ? "" : savedValue;
      clearConflict();
      showMessage("success", savedValue === null ? "Актуальная инструкция удалена." : "Ваш текст сохранён поверх актуальной инструкции.");
      showSaveStatus("success", "Сохранено поверх.");
      renderDraftState();
      return true;
    } catch (error) {
      const validation = error && error.name === "UserInstructionError";
      showMessage(validation ? "validation" : "error", validation ? validationMessage(error) : `Не удалось сохранить инструкцию. ${error.message || ""}`.trim());
      showSaveStatus(validation ? "validation" : "error", "Локальный текст оставлен без изменений.");
      renderDraftState();
      return false;
    } finally {
      setPending(false);
    }
  }

  async function refreshExternalState() {
    if (state.pending || !state.issueKey) {
      return false;
    }
    const requestId = ++state.refreshRequestId;
    try {
      const current = await root.RdvUserInstructionStore.load(chrome.storage.local, state.issueKey);
      if (requestId !== state.refreshRequestId || state.pending) {
        return false;
      }
      return applyExternalSnapshot(current);
    } catch (error) {
      if (requestId !== state.refreshRequestId) {
        return false;
      }
      showMessage("error", `Не удалось проверить актуальность инструкции. ${error.message || ""}`.trim());
      showSaveStatus("error", "Локальный текст оставлен без изменений.");
      renderDraftState();
      return false;
    }
  }

  function handleStorageChanged(changes, areaName) {
    if (areaName !== "local" || !state.issueKey || state.pending) {
      return;
    }
    const key = root.RdvUserInstructionStore.storageKey(state.issueKey);
    if (changes && Object.prototype.hasOwnProperty.call(changes, key)) {
      void refreshExternalState();
    }
  }

  function handleBeforeUnload(event) {
    if (!isDirty($("instructionText").value, state.savedValue)) {
      return undefined;
    }
    event.preventDefault();
    event.returnValue = "";
    return "";
  }

  async function init() {
    state.issueKey = root.RdvUserInstructionStore.issueKeyFromLocation(window.location);
    if (!state.issueKey) {
      showMessage("error", INVALID_ISSUE_MESSAGE);
      return;
    }
    $("issueKey").textContent = state.issueKey;
    $("instructionForm").addEventListener("submit", saveInstruction);
    $("instructionText").addEventListener("input", () => {
      renderDraftState();
      showSaveStatus("", "");
    });
    $("deleteInstruction").addEventListener("click", deleteInstruction);
    $("reloadCurrentInstruction").addEventListener("click", reloadCurrentInstruction);
    $("overwriteInstruction").addEventListener("click", overwriteInstruction);
    window.addEventListener("beforeunload", handleBeforeUnload);
    window.addEventListener("focus", () => {
      void refreshExternalState();
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        void refreshExternalState();
      }
    });
    if (chrome.storage.onChanged && typeof chrome.storage.onChanged.addListener === "function") {
      chrome.storage.onChanged.addListener(handleStorageChanged);
    }
    renderDraftState();
    await loadInstruction();
    void initCommitSelection();
  }

  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", init);
  }

  if (typeof module !== "undefined") {
    module.exports = {
      INVALID_ISSUE_MESSAGE,
      characterCount,
      confirmInstructionDeletion,
      deleteIfCurrent,
      draftValidationState,
      editorActionState,
      handleBeforeUnload,
      isDirty,
      loadCurrentSnapshot,
      persistDraft,
      persistDraftIfCurrent,
      reconcileExternalSnapshot,
      validationMessage
    };
  }
})(typeof globalThis !== "undefined" ? globalThis : window);
