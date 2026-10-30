/* global chrome */
(function popupMain(root) {
  "use strict";

  const state = {
    context: null,
    config: null,
    status: null,
    api: null,
    settings: null,
    submitPending: false,
    preparePending: false,
    reviewRequestPending: false,
    instructionIssueKey: null,
    instructionSet: false,
    instructionLoading: false,
    instructionError: null,
    instructionRequestId: 0,
    reviewSourceMode: "task",
    sourceModeTouched: false,
    selectedMrKey: null,
    sourceIssueKey: null,
    mrDiscoveryLoadedForIssue: null,
    mrDiscoveryPending: false,
    mrDiscoveryRequestId: 0
  };

  const FORCE_REVIEW_DETAIL_STATUSES = new Set(["done", "done_with_warnings", "already_done", "summary_unavailable"]);
  const REQUIRES_JIRA_ISSUE_MESSAGE =
    "Откройте страницу задачи Jira вида https://jira-my-company.ru/browse/ABC-123, чтобы начать ревью ИИ-агентом.";
  const MR_DISCOVERY_MESSAGES = {
    "mr-discovery-loading": "Ищем открытые MR в GitLab…",
    "mr-discovery-loading-with-issue": "Ищем открытые MR для {issueKey} в GitLab…",
    "mr-discovery-empty": "Открытых MR с ключом {issueKey} в заголовке не найдено.",
    "mr-discovery-empty-no-issue": "Открытых MR по этой задаче не найдено.",
    "mr-discovery-token-missing-single": "Не задан токен GitLab ({instanceId}). Установите переменную окружения {tokenEnv} и перезапустите локальный сервис.",
    "mr-discovery-token-missing-multiple": "Не заданы токены GitLab для: {instances}. Задайте переменные окружения и перезапустите локальный сервис.",
    "mr-discovery-service-unavailable": "Локальный сервис недоступен. Проверьте, что он запущен, и нажмите «Обновить».",
    "mr-discovery-partial-header": "Список может быть неполным: не все проекты удалось опросить в GitLab.",
    "mr-discovery-all-failed": "Не удалось получить список MR из GitLab. Проверьте токены, доступ к проектам и настройки в service.json.",
    "mr-discovery-no-gitlab-remotes": "В prefix config нет репозиториев с remote на настроенные GitLab-инстансы.",
    "mr-discovery-config-missing": "Заполните prefix config, чтобы искать MR в GitLab.",
    "mr-discovery-instances-not-configured": "В service.json не настроены GitLab-инстансы.",
    "mr-discovery-remote-diagnostics-header": "Не найдены репозитории с выбранным remote «{remote}» на настроенных GitLab-инстансах.",
    "mr-discovery-remote-diagnostics-instances": "Настроенные GitLab-инстансы: {instances}",
    "mr-discovery-remote-diagnostics-repository": "Репозиторий: {repoPath}",
    "mr-discovery-remote-diagnostics-selected": "Remote «{remote}»: {outcome}",
    "mr-discovery-remote-diagnostics-alternative": "Используйте remote «{remote}» ({instanceId}/{projectPath}): {remoteUrl}",
    "mr-discovery-remote-diagnostics-inspection-error": "Не удалось проверить репозиторий: {message}"
  };
  const MR_REMOTE_REJECTION_MESSAGES = {
    missing_remote: "не найден",
    empty_remote: "URL remote пуст",
    not_gitlab_remote: "не является URL GitLab",
    unconfigured_instance: "GitLab-хост {host} не настроен",
    ambiguous_instance: "GitLab-хост совпадает с несколькими настройками",
    invalid_port: "некорректный порт в URL remote"
  };
  const MR_DISCOVERY_WARNING_MESSAGES = {
    project_access_denied: "{instanceId}/{projectPath}: нет доступа (проверьте токен и права read_api)",
    project_not_found: "{instanceId}/{projectPath}: проект не найден в GitLab",
    project_timeout: "{instanceId}/{projectPath}: таймаут запроса",
    project_rate_limited: "{instanceId}/{projectPath}: превышен лимит запросов GitLab",
    instance_unreachable: "{instanceId}: GitLab недоступен ({baseUrl})",
    project_generic: "{instanceId}/{projectPath}: {message}"
  };

  function isSupportedJiraIssueContext(context) {
    return Boolean(context && context.ok && context.issueKey && context.projectKey);
  }

  function notJiraIssueStatus() {
    return {
      detailStatus: "not_jira_issue",
      availableActions: [],
      error: {
        code: "not_jira_issue",
        message: REQUIRES_JIRA_ISSUE_MESSAGE
      }
    };
  }

  const $ = (id) => document.getElementById(id);

  function hasText(value) {
    return typeof value === "string" && value.trim().length > 0;
  }

  function formatTemplate(template, params) {
    const values = params || {};
    return String(template || "").replace(/\{([A-Za-z0-9_]+)\}/g, (_match, key) => {
      const value = values[key];
      return value === undefined || value === null ? "" : String(value);
    });
  }

  function warningText(warning) {
    if (typeof warning === "string") {
      return warning.trim();
    }
    if (!warning || typeof warning !== "object") {
      return "";
    }
    return [warning.code, warning.message].filter(hasText).join(": ").trim();
  }

  function renderableWarnings(status) {
    const warnings = Array.isArray(status.warnings) ? status.warnings : [];
    return warnings.map(warningText).filter(hasText);
  }

  function setConfirmActionsState(element, expanded) {
    element.inert = !expanded;
    element.setAttribute("aria-hidden", expanded ? "false" : "true");
  }

  async function getSettings() {
    const stored = await chrome.storage.local.get({ servicePort: 8765, serviceToken: "" });
    return { port: stored.servicePort, token: stored.serviceToken };
  }

  function resetInstructionState(issueKey) {
    state.instructionRequestId += 1;
    state.instructionIssueKey = issueKey || null;
    state.instructionSet = false;
    state.instructionLoading = Boolean(issueKey);
    state.instructionError = null;
  }

  function setInstructionReadError(issueKey, error) {
    if (!state.context || state.context.issueKey !== issueKey) {
      return;
    }
    state.instructionIssueKey = issueKey;
    state.instructionSet = false;
    state.instructionLoading = false;
    state.instructionError = error || new Error("instruction_storage_unavailable");
  }

  function renderInstructionControl(context) {
    const control = $("userInstructionControl");
    const supported = isSupportedJiraIssueContext(context);
    control.hidden = !supported;
    if (!supported) {
      return;
    }
    const current = state.instructionIssueKey === context.issueKey;
    const status = $("userInstructionState");
    const error = $("userInstructionError");
    status.classList.toggle("is-set", current && state.instructionSet && !state.instructionError);
    status.classList.toggle("is-empty", current && !state.instructionSet && !state.instructionError && !state.instructionLoading);
    if (!current || state.instructionLoading) {
      status.textContent = "Проверка сохранённой инструкции…";
    } else if (state.instructionError) {
      status.textContent = "Состояние не проверено";
    } else {
      status.textContent = state.instructionSet
        ? "Задана · будет использована при запуске"
        : "Не задана";
    }
    error.hidden = !state.instructionError;
    error.textContent = state.instructionError
      ? "Не удалось прочитать локальную инструкцию. Повторите попытку перед запуском ревью."
      : "";
    $("openInstructionEditor").disabled = !current || state.instructionLoading;
  }

  async function refreshInstructionState(issueKey, storageArea = chrome.storage.local) {
    if (!root.RdvUserInstructionStore.isValidIssueKey(issueKey)) {
      resetInstructionState(null);
      renderInstructionControl(state.context || {});
      return false;
    }
    const requestId = ++state.instructionRequestId;
    state.instructionIssueKey = issueKey;
    state.instructionLoading = true;
    state.instructionError = null;
    renderInstructionControl(state.context || {});
    try {
      await root.RdvUserInstructionStore.ensureTrustedStorageAccess(storageArea);
      const value = await root.RdvUserInstructionStore.load(storageArea, issueKey);
      if (
        requestId !== state.instructionRequestId ||
        !state.context ||
        state.context.issueKey !== issueKey
      ) {
        return false;
      }
      state.instructionSet = value !== null;
      state.instructionLoading = false;
      state.instructionError = null;
      renderInstructionControl(state.context);
      return true;
    } catch (error) {
      if (requestId !== state.instructionRequestId) {
        return false;
      }
      setInstructionReadError(issueKey, error);
      renderInstructionControl(state.context || {});
      return false;
    }
  }

  async function getActiveIssueKey(chromeRef = chrome) {
    const [tab] = await chromeRef.tabs.query({ active: true, currentWindow: true });
    const parsed = root.RdvIssue.parseIssueFromUrl(tab && tab.url ? tab.url : "");
    return parsed.ok ? parsed.issueKey : "";
  }

  async function readFreshInstructionForSubmit(issueKey, storageArea, currentIssueKeyFn) {
    if (await currentIssueKeyFn() !== issueKey) {
      const error = new Error("stale_issue_context");
      error.code = "stale_issue_context";
      throw error;
    }
    await root.RdvUserInstructionStore.ensureTrustedStorageAccess(storageArea);
    const value = await root.RdvUserInstructionStore.load(storageArea, issueKey);
    if (await currentIssueKeyFn() !== issueKey) {
      const error = new Error("stale_issue_context");
      error.code = "stale_issue_context";
      throw error;
    }
    return value;
  }

  function fillServiceSettings(settings) {
    $("servicePort").value = String(settings.port || 8765);
    $("serviceToken").value = settings.token || "";
  }

  async function saveServiceSettings() {
    const button = $("saveServiceSettings");
    try {
      const port = Number($("servicePort").value || 8765);
      const token = $("serviceToken").value.trim();
      await chrome.storage.local.set({ servicePort: port, serviceToken: token });
      state.settings = { port, token };
      state.api = new root.RdvLocalReviewApi.LocalReviewApi(state.settings);
      await refreshAll();
      root.RdvUiFeedback.flashButtonFeedback(button, { success: true, successLabel: "Сохранено" });
    } catch {
      root.RdvUiFeedback.flashButtonFeedback(button, { success: false, failLabel: "Не удалось" });
    }
  }

  async function getActiveIssueBundle() {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const parsed = root.RdvIssue.parseIssueFromUrl(tab && tab.url ? tab.url : "");
    if (!parsed.ok) {
      return {
        context: { ok: false, detailStatus: "not_jira_issue", url: tab ? tab.url : "" },
        cachedStatus: null
      };
    }
    const key = `issue:${parsed.issueKey}`;
    const stored = await chrome.storage.local.get(key);
    const bundle = stored[key] || {};
    return {
      context: Object.assign({}, parsed, bundle.context || {}),
      cachedStatus: bundle.status || null
    };
  }

  async function getActiveContext() {
    const bundle = await getActiveIssueBundle();
    return bundle.context;
  }

  function renderCachedIssueState(bundle) {
    const previousProjectKey = state.context && state.context.projectKey;
    const previousIssueKey = state.context && state.context.issueKey;
    state.context = bundle.context;
    if (previousIssueKey !== bundle.context.issueKey) {
      resetInstructionState(bundle.context.issueKey);
    }
    if (previousProjectKey !== bundle.context.projectKey) {
      state.config = null;
    }
    syncSourceSelection();
    if (!bundle.context.ok) {
      state.status = notJiraIssueStatus();
      state.config = null;
      render();
      return false;
    }
    if (bundle.cachedStatus) {
      state.status = bundle.cachedStatus;
      render();
      return true;
    }
    state.status = {
      detailStatus: "issue_detected",
      detailText: root.RdvReviewStatus.DETAIL_TEXT.issue_detected,
      availableActions: ["refresh"]
    };
    render();
    return true;
  }

  async function saveCurrentIssueState() {
    if (!state.context || !state.context.issueKey) {
      return;
    }
    const key = `issue:${state.context.issueKey}`;
    await chrome.storage.local.set({
      [key]: {
        context: state.context,
        status: state.status,
        updatedAt: Date.now()
      }
    });
  }

  async function notifyIssueStatusChanged() {
    if (!state.context || !state.context.issueKey || !chrome.runtime || typeof chrome.runtime.sendMessage !== "function") {
      return;
    }
    await sendRuntimeMessage({ type: "issueStatusChanged", context: state.context, status: state.status });
  }

  async function sendRuntimeMessage(message) {
    try {
      const result = chrome.runtime.sendMessage(message);
      if (result && typeof result.then === "function") {
        return await result.catch(() => null);
      }
    } catch {
      return null;
    }
    return null;
  }

  function isCompletedReviewStatus(status) {
    return FORCE_REVIEW_DETAIL_STATUSES.has(status && status.detailStatus);
  }

  function shouldForceOnSubmitConfirm(status) {
    const actions = (status && status.availableActions) || [];
    return actions.includes("retry") || isCompletedReviewStatus(status);
  }

  function mergeRequestKey(mr) {
    if (!mr || !mr.gitlabProjectPath || !mr.mrId) {
      return "";
    }
    return mr.gitlabInstanceId
      ? `${mr.gitlabInstanceId}:${mr.gitlabProjectPath}!${mr.mrId}`
      : `${mr.gitlabProjectPath}!${mr.mrId}`;
  }

  function normalizeMergeRequests(context) {
    if (!context || !context.mrDiscovery || !Array.isArray(context.mergeRequests)) {
      return [];
    }
    return context.mergeRequests
      .filter((mr) => {
        return (
          mr &&
          Number.isInteger(mr.mrId) &&
          mr.mrId > 0 &&
          hasText(mr.mrUrl) &&
          hasText(mr.gitlabProjectPath) &&
          hasText(mr.gitlabInstanceId)
        );
      })
      .map((mr) => ({
        gitlabInstanceId: mr.gitlabInstanceId,
        mrId: mr.mrId,
        mrUrl: mr.mrUrl,
        gitlabProjectPath: mr.gitlabProjectPath,
        label: mr.label || `${mr.gitlabProjectPath.split("/").pop()} !${mr.mrId}`,
        title: mr.title || ""
      }));
  }

  function normalizeMrSource(source) {
    if (!source || source.mode !== "mr") {
      return null;
    }
    if (!Number.isInteger(source.mrId) || source.mrId <= 0 || !hasText(source.mrUrl) || !hasText(source.gitlabProjectPath)) {
      return null;
    }
    return {
      gitlabInstanceId: source.gitlabInstanceId || "",
      mrId: source.mrId,
      mrUrl: source.mrUrl,
      gitlabProjectPath: source.gitlabProjectPath,
      label: `${source.gitlabProjectPath.split("/").pop()} !${source.mrId}`,
      title: source.title || ""
    };
  }

  function mergeRequestCandidates(context, status) {
    const mergeRequests = normalizeMergeRequests(context);
    const fromStatus = normalizeMrSource(status && status.source);
    if (!fromStatus) {
      return mergeRequests;
    }
    const statusKey = mergeRequestKey(fromStatus);
    if (mergeRequests.some((mr) => mergeRequestKey(mr) === statusKey)) {
      return mergeRequests;
    }
    return [...mergeRequests, fromStatus];
  }

  function sourceKeyFromStatus(status) {
    const source = status && status.source;
    if (!source || source.mode !== "mr") {
      return "";
    }
    return mergeRequestKey(source);
  }

  function syncReviewSourceState(previous, context, status) {
    const mergeRequests = mergeRequestCandidates(context, status);
    const keys = new Set(mergeRequests.map(mergeRequestKey));
    const statusKey = sourceKeyFromStatus(status);
    const issueKey = context && context.issueKey ? context.issueKey : "";
    const issueChanged = previous.sourceIssueKey !== issueKey;
    const next = {
      reviewSourceMode: issueChanged ? "task" : previous.reviewSourceMode || "task",
      sourceModeTouched: issueChanged ? false : Boolean(previous.sourceModeTouched),
      selectedMrKey: issueChanged ? null : previous.selectedMrKey || null,
      sourceIssueKey: issueKey
    };
    if (!next.sourceModeTouched && statusKey) {
      next.reviewSourceMode = "mr";
    }
    if (next.selectedMrKey && !keys.has(next.selectedMrKey)) {
      next.selectedMrKey = null;
    }
    if (!next.selectedMrKey && statusKey) {
      next.selectedMrKey = statusKey;
    }
    if (next.reviewSourceMode === "mr" && mergeRequests.length === 0 && !statusKey) {
      next.selectedMrKey = null;
    }
    return next;
  }

  function applyReviewSourceState(next) {
    state.reviewSourceMode = next.reviewSourceMode;
    state.sourceModeTouched = next.sourceModeTouched;
    state.selectedMrKey = next.selectedMrKey;
    state.sourceIssueKey = next.sourceIssueKey;
  }

  function syncSourceSelection() {
    const next = syncReviewSourceState(state, state.context, state.status);
    if (state.sourceIssueKey !== next.sourceIssueKey) {
      state.mrDiscoveryLoadedForIssue = null;
      state.mrDiscoveryPending = false;
    }
    applyReviewSourceState(next);
  }

  function selectedMergeRequest() {
    const key = state.selectedMrKey;
    return mergeRequestCandidates(state.context, state.status).find((mr) => mergeRequestKey(mr) === key) || null;
  }

  function buildSourceInit(mode, selectedMrKey, mergeRequests) {
    if (mode !== "mr") {
      return null;
    }
    const selected = (mergeRequests || []).find((mr) => mergeRequestKey(mr) === selectedMrKey);
    if (!selected) {
      return null;
    }
    const init = {
      mrId: selected.mrId,
      mrUrl: selected.mrUrl,
      gitlabProjectPath: selected.gitlabProjectPath
    };
    if (hasText(selected.gitlabInstanceId)) {
      init.gitlabInstanceId = selected.gitlabInstanceId;
    }
    if (hasText(selected.title)) {
      init.mrTitle = selected.title;
    }
    return init;
  }

  function buildCurrentSourceInit() {
    return buildSourceInit(
      state.reviewSourceMode,
      state.selectedMrKey,
      mergeRequestCandidates(state.context, state.status)
    );
  }

  function normalizeDiscoveryResponse(response, issueKey) {
    const candidates = Array.isArray(response && response.candidates)
      ? response.candidates
          .filter((mr) => {
            return (
              mr &&
              hasText(mr.gitlabInstanceId) &&
              Number.isInteger(mr.mrId) &&
              mr.mrId > 0 &&
              hasText(mr.mrUrl) &&
              hasText(mr.gitlabProjectPath)
            );
          })
          .map((mr) => ({
            gitlabInstanceId: mr.gitlabInstanceId,
            mrId: mr.mrId,
            mrUrl: mr.mrUrl,
            gitlabProjectPath: mr.gitlabProjectPath,
            title: mr.title || "",
            label: mr.label || `${mr.gitlabProjectPath.split("/").pop()} !${mr.mrId}`
          }))
      : [];
    return {
      ok: Boolean(response && response.ok),
      issueKey,
      state: ["ok", "empty", "error"].includes(response && response.state) ? response.state : "error",
      candidates,
      warnings: Array.isArray(response && response.warnings) ? response.warnings : [],
      error: response && response.error && typeof response.error === "object"
        ? Object.assign({}, response.error, {
            diagnostics: response.error.diagnostics && typeof response.error.diagnostics === "object" ? response.error.diagnostics : null
          })
        : null
    };
  }

  function setDiscoveryOnContext(discovery) {
    state.context = Object.assign({}, state.context || {}, {
      mrDiscovery: discovery,
      mergeRequests: discovery.candidates
    });
    const keys = new Set(discovery.candidates.map(mergeRequestKey));
    if (state.selectedMrKey && !keys.has(state.selectedMrKey)) {
      state.selectedMrKey = null;
    }
    if (state.reviewSourceMode === "mr" && discovery.state === "ok" && discovery.candidates.length === 1) {
      state.selectedMrKey = mergeRequestKey(discovery.candidates[0]);
    }
  }

  function beginMrDiscoveryRequest(activeState) {
    activeState.mrDiscoveryRequestId += 1;
    return {
      requestId: activeState.mrDiscoveryRequestId,
      issueKey: activeState.context.issueKey
    };
  }

  function isActiveMrDiscoveryRequest(activeState, requestId, issueKey) {
    return (
      activeState.mrDiscoveryRequestId === requestId &&
      Boolean(activeState.context && activeState.context.issueKey === issueKey)
    );
  }

  async function loadMrDiscoveryForCurrentIssue() {
    if (!state.context || !state.context.issueKey || !state.api) {
      return;
    }
    const { requestId, issueKey } = beginMrDiscoveryRequest(state);
    state.mrDiscoveryPending = true;
    render();
    try {
      const response = await state.api.discoverMergeRequestsByIssue(issueKey);
      if (!isActiveMrDiscoveryRequest(state, requestId, issueKey)) {
        return;
      }
      const discovery = normalizeDiscoveryResponse(response, issueKey);
      setDiscoveryOnContext(discovery);
      state.mrDiscoveryLoadedForIssue = issueKey;
    } catch (error) {
      if (!isActiveMrDiscoveryRequest(state, requestId, issueKey)) {
        return;
      }
      setDiscoveryOnContext({
        ok: false,
        issueKey,
        state: "error",
        candidates: [],
        warnings: [],
        error: { code: "discovery_service_unavailable", params: { message: error.message || String(error) } }
      });
      state.mrDiscoveryLoadedForIssue = issueKey;
    } finally {
      if (!isActiveMrDiscoveryRequest(state, requestId, issueKey)) {
        return;
      }
      state.mrDiscoveryPending = false;
      await saveCurrentIssueState();
      render();
    }
  }

  function computeUiState(status, options) {
    status = root.RdvReviewStatus.normalizeStatus(status);
    const actions = status.availableActions || [];
    const hasIssue = Boolean(options && options.hasIssue);
    const configReady = Boolean(options && options.configReady);
    const sourceReady = !options || options.sourceReady !== false;
    const completedReview = isCompletedReviewStatus(status);
    const canStartReview = actions.includes("submit") || actions.includes("retry") || status.detailStatus === "config_ready";
    const canSubmit = canStartReview || completedReview;
    const canPrepare = canStartReview || status.detailStatus === "handoff_required";
    const canRerun = actions.includes("retry") || completedReview;
    const warnings = renderableWarnings(status);
    const hasHandoffContent = hasText(status.taskDir) || hasText(status.manualCommand);
    return {
      submitHidden: !canSubmit,
      submitText: canRerun ? "Повторить ревью" : "Отправить на ревью",
      submitDisabled: !hasIssue || !configReady || !sourceReady,
      prepareHidden: !canPrepare,
      prepareDisabled: !hasIssue || !configReady || !sourceReady,
      cancelHidden: !actions.includes("cancel"),
      openAgentLogHidden:
        !["review_running", "done"].includes(status.detailStatus) || !hasText(status.jobId) || !actions.includes("open_agent_log"),
      openChangesHidden:
        !hasText(status.jobId) || !actions.includes("open_changes"),
      openReportHidden: !actions.includes("open_report") || !status.reportPath,
      openReasoningHidden: !actions.includes("open_reasoning") || !status.reasoningPath,
      summaryHidden: !status.summary,
      handoffHidden: status.detailStatus !== "handoff_required" || !hasHandoffContent,
      warningsHidden: warnings.length === 0,
      diagnosticsHidden: !status.feedback
    };
  }

  function render() {
    const context = state.context || {};
    const status = root.RdvReviewStatus.normalizeStatus(state.status);
    const ui = computeUiState(status, {
      hasIssue: Boolean(context.issueKey),
      configReady: root.RdvLocalReviewApi.isConfigReady(state.config),
      sourceReady: state.reviewSourceMode !== "mr" || (!state.mrDiscoveryPending && Boolean(selectedMergeRequest()))
    });
    $("issueKey").textContent = context.issueKey || "JIRA";
    $("issueTitle").textContent = context.title || (context.descriptionDetected === false ? "Описание не найдено" : "");
    $("displayStatus").textContent = status.displayStatus;
    $("detailStatus").textContent = status.detailText;
    $("duration").textContent = Number.isFinite(status.durationSec) ? `${status.durationSec} sec` : "";
    if (ui.submitHidden) {
      state.submitPending = false;
    }
    $("submitSlot").hidden = ui.submitHidden;
    $("submitSlot").classList.toggle("confirm-pending", state.submitPending);
    setConfirmActionsState($("submitConfirmActions"), state.submitPending);
    $("submitReview").textContent = ui.submitText;
    $("submitReview").disabled = ui.submitDisabled || state.submitPending || state.reviewRequestPending;
    $("submitConfirmSend").disabled = ui.submitDisabled || state.reviewRequestPending;
    $("submitConfirmCancel").disabled = ui.submitDisabled || state.reviewRequestPending;
    if (ui.prepareHidden) {
      state.preparePending = false;
    }
    $("prepareSlot").hidden = ui.prepareHidden;
    $("prepareSlot").classList.toggle("confirm-pending", state.preparePending);
    setConfirmActionsState($("prepareConfirmActions"), state.preparePending);
    $("prepareReview").disabled = ui.prepareDisabled || state.preparePending || state.reviewRequestPending;
    $("prepareConfirmSend").disabled = ui.prepareDisabled || state.reviewRequestPending;
    $("prepareConfirmCancel").disabled = ui.prepareDisabled || state.reviewRequestPending;
    $("cancelReview").hidden = ui.cancelHidden;
    $("openAgentLog").hidden = ui.openAgentLogHidden;
    $("openChanges").hidden = ui.openChangesHidden;
    $("openReport").hidden = ui.openReportHidden;
    $("openReasoning").hidden = ui.openReasoningHidden;
    renderSourceSelector(context);
    renderSummary(status, ui);
    renderHandoff(status, ui);
    renderWarnings(status, ui);
    renderDiagnostics(status, ui);
    renderProjectConfig(context);
    renderInstructionControl(context);
  }

  function renderProjectConfig(context) {
    const supported = isSupportedJiraIssueContext(context);
    const configTab = $("settingsTabConfig");
    const configPanel = $("configPanel");
    configTab.hidden = !supported;
    if (!supported) {
      configPanel.hidden = true;
      setSettingsTab("service");
      return;
    }
    const missing = root.RdvLocalReviewApi.requiredConfigFields(state.config);
    const ready = missing.length === 0;
    $("configReadiness").textContent = ready ? "Конфигурация готова" : "Конфигурация не готова";
    $("selectedIde").textContent = `IDE: ${state.config && state.config.ide ? state.config.ide : "—"}`;
    $("missingConfigFields").textContent = ready ? "" : `Не заполнено: ${missing.join(", ")}`;
    $("openProjectConfig").textContent = `Настроить проект ${context.projectKey}…`;
  }

  function buildProjectConfigTabOptions(projectKey, runtime, openerTabId) {
    const url = root.RdvPrefixConfig.buildEditorUrl(projectKey, runtime);
    if (!url) {
      return null;
    }
    const options = { url, active: true };
    if (typeof openerTabId === "number") {
      options.openerTabId = openerTabId;
    }
    return options;
  }

  async function openProjectConfig() {
    if (!isSupportedJiraIssueContext(state.context)) {
      return false;
    }
    return createProjectConfigTab(state.context.projectKey, chrome);
  }

  async function createProjectConfigTab(projectKey, chromeRef) {
    try {
      const [tab] = await chromeRef.tabs.query({ active: true, currentWindow: true });
      const options = buildProjectConfigTabOptions(projectKey, chromeRef.runtime, tab && tab.id);
      if (!options) {
        return false;
      }
      await chromeRef.tabs.create(options);
      return true;
    } catch {
      return false;
    }
  }

  function buildInstructionEditorTabOptions(issueKey, runtime, openerTabId) {
    const url = root.RdvUserInstructionStore.buildEditorUrl(issueKey, runtime);
    if (!url) {
      return null;
    }
    const options = { url, active: true };
    if (typeof openerTabId === "number") {
      options.openerTabId = openerTabId;
    }
    return options;
  }

  async function createInstructionEditorTab(issueKey, chromeRef) {
    try {
      const [tab] = await chromeRef.tabs.query({ active: true, currentWindow: true });
      const options = buildInstructionEditorTabOptions(issueKey, chromeRef.runtime, tab && tab.id);
      if (!options) {
        return false;
      }
      await chromeRef.tabs.create(options);
      return true;
    } catch (_error) {
      return false;
    }
  }

  async function openInstructionEditor() {
    if (!isSupportedJiraIssueContext(state.context)) {
      return false;
    }
    return createInstructionEditorTab(state.context.issueKey, chrome);
  }

  function renderSourceSelector(context) {
    const mergeRequests = mergeRequestCandidates(context, state.status);
    const taskButton = $("sourceTask");
    const mrButton = $("sourceMr");
    const list = $("mrList");
    $("sourceSelector").hidden = !context.issueKey;
    taskButton.classList.toggle("active", state.reviewSourceMode === "task");
    mrButton.classList.toggle("active", state.reviewSourceMode === "mr");
    mrButton.disabled = false;
    list.hidden = state.reviewSourceMode !== "mr";
    list.textContent = "";
    if (state.reviewSourceMode !== "mr") {
      return;
    }
    if (state.mrDiscoveryPending) {
      appendMrDiscoveryMessage(list, "mr-discovery-loading-with-issue", { issueKey: context.issueKey });
      return;
    }
    const discovery = context && context.mrDiscovery;
    if (discovery && discovery.state === "error") {
      if (discovery.error && discovery.error.code === "no_gitlab_remotes" && hasRemoteDiscoveryDiagnostics(discovery.error.diagnostics)) {
        renderRemoteDiscoveryDiagnostics(list, discovery.error.diagnostics);
        return;
      }
      renderMrDiscoveryError(list, discovery);
      return;
    }
    if (mergeRequests.length === 0) {
      appendMrDiscoveryMessage(list, context.issueKey ? "mr-discovery-empty" : "mr-discovery-empty-no-issue", { issueKey: context.issueKey });
      return;
    }
    renderMrDiscoveryWarnings(list, discovery);
    for (const mr of mergeRequests) {
      const key = mergeRequestKey(mr);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "mr-option";
      button.classList.toggle("active", key === state.selectedMrKey);
      button.dataset.mrKey = key;
      button.title = mr.mrUrl;
      button.textContent = mr.title && !/^#\d+$/.test(mr.title) ? `${mr.label} · ${mr.title}` : mr.label;
      list.appendChild(button);
    }
  }

  function appendMrDiscoveryMessage(list, key, params) {
    const message = document.createElement("div");
    message.className = "mr-empty";
    message.dataset.uiKey = key;
    message.textContent = formatTemplate(MR_DISCOVERY_MESSAGES[key] || key, params);
    list.appendChild(message);
  }

  function renderMrDiscoveryError(list, discovery) {
    appendMrDiscoveryMessage(list, mrDiscoveryErrorKey(discovery.error), mrDiscoveryErrorParams(discovery.error));
    if (discovery.error && discovery.error.code === "all_projects_failed") {
      renderMrDiscoveryWarnings(list, discovery, false);
    }
  }

  function hasRemoteDiscoveryDiagnostics(diagnostics) {
    return Boolean(
      diagnostics &&
        hasText(diagnostics.selectedRemote) &&
        Array.isArray(diagnostics.configuredInstances) &&
        Array.isArray(diagnostics.repositories)
    );
  }

  function remoteDiscoveryLines(diagnostics) {
    if (!hasRemoteDiscoveryDiagnostics(diagnostics)) {
      return [];
    }
    const instances = diagnostics.configuredInstances
      .filter((instance) => instance && hasText(instance.id) && hasText(instance.host))
      .map((instance) => `${instance.id} (${instance.host}${instance.port ? `:${instance.port}` : ""})`)
      .join(", ") || "не указаны";
    const lines = [
      formatTemplate(MR_DISCOVERY_MESSAGES["mr-discovery-remote-diagnostics-header"], { remote: diagnostics.selectedRemote }),
      formatTemplate(MR_DISCOVERY_MESSAGES["mr-discovery-remote-diagnostics-instances"], { instances })
    ];
    for (const repository of diagnostics.repositories) {
      if (!repository || typeof repository !== "object") {
        continue;
      }
      if (hasText(repository.repoPath)) {
        lines.push(formatTemplate(MR_DISCOVERY_MESSAGES["mr-discovery-remote-diagnostics-repository"], { repoPath: repository.repoPath }));
      }
      if (repository.inspectionError && hasText(repository.inspectionError.message)) {
        lines.push(formatTemplate(MR_DISCOVERY_MESSAGES["mr-discovery-remote-diagnostics-inspection-error"], repository.inspectionError));
        continue;
      }
      const selected = repository.selectedRemote;
      if (selected && hasText(selected.name)) {
        const reasonTemplate = MR_REMOTE_REJECTION_MESSAGES[selected.reason] || "не подходит для настроенных GitLab-инстансов";
        const reason = formatTemplate(reasonTemplate, selected);
        const url = hasText(selected.remoteUrl) ? ` (${selected.remoteUrl})` : "";
        lines.push(
          formatTemplate(MR_DISCOVERY_MESSAGES["mr-discovery-remote-diagnostics-selected"], {
            remote: selected.name,
            outcome: `${reason}${url}`
          })
        );
      }
      const alternatives = Array.isArray(repository.alternativeRemotes) ? repository.alternativeRemotes : [];
      for (const alternative of alternatives) {
        if (!alternative || !hasText(alternative.name) || !hasText(alternative.gitlabInstanceId) || !hasText(alternative.gitlabProjectPath)) {
          continue;
        }
        lines.push(
          formatTemplate(MR_DISCOVERY_MESSAGES["mr-discovery-remote-diagnostics-alternative"], {
            remote: alternative.name,
            instanceId: alternative.gitlabInstanceId,
            projectPath: alternative.gitlabProjectPath,
            remoteUrl: alternative.remoteUrl || "URL не указан"
          })
        );
      }
    }
    return lines;
  }

  function renderRemoteDiscoveryDiagnostics(list, diagnostics) {
    const message = document.createElement("div");
    message.className = "mr-empty diagnostics-scroll";
    message.dataset.uiKey = "mr-discovery-no-gitlab-remotes";
    message.textContent = remoteDiscoveryLines(diagnostics).join("\n");
    list.appendChild(message);
  }

  function mrDiscoveryErrorKey(error) {
    const code = error && error.code;
    if (code === "gitlab_instances_not_configured") {
      return "mr-discovery-instances-not-configured";
    }
    if (code === "prefix_config_missing") {
      return "mr-discovery-config-missing";
    }
    if (code === "discovery_service_unavailable") {
      return "mr-discovery-service-unavailable";
    }
    if (code === "gitlab_token_missing") {
      const instances = error && error.params && Array.isArray(error.params.instances) ? error.params.instances : [];
      return instances.length > 1 ? "mr-discovery-token-missing-multiple" : "mr-discovery-token-missing-single";
    }
    if (code === "no_gitlab_remotes") {
      return "mr-discovery-no-gitlab-remotes";
    }
    if (code === "all_projects_failed") {
      return "mr-discovery-all-failed";
    }
    return "mr-discovery-all-failed";
  }

  function mrDiscoveryErrorParams(error) {
    const params = Object.assign({}, (error && error.params) || {});
    const instances = Array.isArray(params.instances) ? params.instances : [];
    if (instances.length === 1) {
      params.instanceId = instances[0].id;
      params.tokenEnv = instances[0].tokenEnv;
    }
    if (instances.length > 1) {
      params.instances = instances.map((item) => `${item.id} (${item.tokenEnv})`).join(", ");
    }
    return params;
  }

  function renderMrDiscoveryWarnings(list, discovery, showPartialHeader = true) {
    const warnings = discovery && Array.isArray(discovery.warnings) ? discovery.warnings : [];
    if (warnings.length === 0) {
      return;
    }
    if (showPartialHeader) {
      appendMrDiscoveryMessage(list, "mr-discovery-partial-header", {});
    }
    for (const warning of warnings) {
      const code = warning && warning.code;
      const template = MR_DISCOVERY_WARNING_MESSAGES[code] || code || "";
      const line = document.createElement("div");
      line.className = "mr-warning";
      line.dataset.warningCode = code || "";
      line.textContent = formatTemplate(template, (warning && warning.params) || {});
      list.appendChild(line);
    }
  }

  function renderSummary(status, ui) {
    const summary = status.summary;
    $("summarySection").hidden = ui.summaryHidden;
    if (!summary) {
      return;
    }
    $("criticalCount").textContent = String(summary.criticalCount || 0);
    $("majorCount").textContent = String(summary.majorCount || 0);
    $("minorCount").textContent = String(summary.minorCount || 0);
    $("summaryText").textContent = summary.summaryText || "";
    $("summaryText").hidden = !summary.summaryText;
    $("tokenUsage").textContent = status.tokenUsage || summary.tokenUsage ? String(status.tokenUsage || summary.tokenUsage) : "—";
  }

  function renderDiagnostics(status, ui) {
    const container = $("diagnostics");
    container.hidden = ui.diagnosticsHidden;
    container.textContent = "";
    if (ui.diagnosticsHidden) {
      return;
    }
    renderFeedback(container, status.feedback, status.diagnostics, status.jobId, state.api);
  }

  function formatDiagnosticValue(value) {
    if (typeof value === "string") {
      return value;
    }
    if (value !== null && typeof value === "object") {
      return JSON.stringify(value, null, 2);
    }
    return String(value);
  }

  function normalizeDiagnosticEntries(entries) {
    if (!Array.isArray(entries)) {
      return [];
    }
    const seen = new Set();
    return entries.filter((entry) => {
      if (!entry || typeof entry !== "object" || !hasText(entry.id) || entry.id.startsWith("comparison.") || !hasText(entry.label) || seen.has(entry.id)) {
        return false;
      }
      const hasValue = entry.value !== null && entry.value !== undefined && (typeof entry.value !== "string" || hasText(entry.value));
      if (!hasValue && !hasText(entry.logRef)) {
        return false;
      }
      seen.add(entry.id);
      return true;
    });
  }

  function renderFeedback(container, feedback, diagnostics, jobId, apiClient) {
    container.className = `diagnostics feedback feedback-${feedback.tone || "error"}`;
    const title = document.createElement("div");
    title.className = "feedback-title";
    title.textContent = feedback.title || "Не удалось выполнить операцию";
    container.appendChild(title);

    const message = document.createElement("div");
    message.className = "feedback-message";
    message.textContent = feedback.message || "";
    container.appendChild(message);

    const entries = feedback.tone === "error" ? normalizeDiagnosticEntries(diagnostics) : [];
    if (entries.length === 0) {
      return;
    }
    const details = document.createElement("details");
    details.className = "technical-details";
    const summary = document.createElement("summary");
    summary.textContent = "Технические детали";
    details.appendChild(summary);
    const body = document.createElement("div");
    body.className = "technical-details-body";
    for (const entry of entries) {
      const block = document.createElement("div");
      block.className = "diagnostic-entry";
      block.dataset.diagnosticId = entry.id;
      const label = document.createElement("div");
      label.className = "diagnostic-label";
      label.textContent = entry.label;
      block.appendChild(label);
      if (entry.value !== null && entry.value !== undefined) {
        const value = document.createElement("pre");
        value.className = "diagnostic-value";
        value.textContent = formatDiagnosticValue(entry.value);
        block.appendChild(value);
      }
      if (hasText(entry.logRef) && hasText(jobId) && apiClient && typeof apiClient.openJobLog === "function") {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "secondary diagnostic-open-log";
        button.textContent = "Открыть лог";
        button.addEventListener("click", async () => {
          try {
            await apiClient.openJobLog(jobId, entry.logRef);
            root.RdvUiFeedback.flashButtonFeedback(button, { success: true, successLabel: "Открыт" });
          } catch {
            root.RdvUiFeedback.flashButtonFeedback(button, { success: false, failLabel: "Не удалось" });
          }
        });
        block.appendChild(button);
      }
      body.appendChild(block);
    }
    details.appendChild(body);
    container.appendChild(details);
  }

  function renderHandoff(status, ui) {
    $("handoffSection").hidden = ui.handoffHidden;
    if (ui.handoffHidden) {
      $("handoffTaskDir").textContent = "";
      $("handoffCommand").textContent = "";
      return;
    }
    $("handoffTaskDir").textContent = status.taskDir || "";
    $("handoffCommand").textContent = status.manualCommand || 'agent -p --trust --approve-mcps --force --workspace "<taskDir>" --output-format text "Выполни /review-start-task"';
  }

  function renderWarnings(status, ui) {
    $("warningsSection").hidden = ui.warningsHidden;
    $("warningsList").textContent = "";
    if (ui.warningsHidden) {
      return;
    }
    for (const text of renderableWarnings(status)) {
      const item = document.createElement("li");
      item.textContent = text;
      $("warningsList").appendChild(item);
    }
  }

  async function refreshAll(options = {}) {
    const bundle = await getActiveIssueBundle();
    renderCachedIssueState(bundle);
    await refreshInstructionState(bundle.context.issueKey);
    if (!bundle.context.ok) {
      await notifyIssueStatusChanged();
      return;
    }
    state.context = bundle.context;
    try {
      await state.api.health();
      const configResponse = await state.api.getPrefixConfig(state.context.projectKey);
      state.config = configResponse.config;
    } catch (error) {
      if (error.status === 404) {
        state.config = null;
        state.status = { detailStatus: "config_missing", availableActions: ["save_config"] };
      } else {
        state.status = { detailStatus: "service_unavailable", availableActions: ["refresh"], error: { code: "service_unavailable", message: error.message } };
        syncSourceSelection();
        await notifyIssueStatusChanged();
        render();
        return;
      }
    }
    try {
      const statusResponse = await state.api.getReviewByIssue(state.context.issueKey);
      state.status = statusResponse.status;
      if (
        !root.RdvLocalReviewApi.isConfigReady(state.config) &&
        ["issue_detected", "config_ready"].includes(state.status.detailStatus)
      ) {
        state.status = { detailStatus: "config_missing", availableActions: ["save_config"] };
      }
      if (state.status.detailStatus === "issue_detected" && root.RdvLocalReviewApi.isConfigReady(state.config)) {
        state.status = Object.assign({}, state.status, { detailStatus: "config_ready", detailText: "Готово к ревью", availableActions: ["submit", "refresh"] });
      }
      syncSourceSelection();
      await saveCurrentIssueState();
    } catch (error) {
      state.status = { detailStatus: "service_unavailable", availableActions: ["refresh"], error: { code: "service_unavailable", message: error.message } };
      syncSourceSelection();
    }
    if (
      options.allowMrDiscovery &&
      state.context &&
      state.context.issueKey &&
      state.status &&
      state.status.detailStatus !== "service_unavailable" &&
      (state.reviewSourceMode === "mr" || state.mrDiscoveryLoadedForIssue === state.context.issueKey)
    ) {
      await loadMrDiscoveryForCurrentIssue();
    }
    await notifyIssueStatusChanged();
    render();
  }

  async function submitReview(options) {
    resetConfirmStates();
    const forceReview = Boolean(options && options.force);
    const handoff = Boolean(options && options.handoff);
    const issueKey = state.context && state.context.issueKey;
    if (!root.RdvUserInstructionStore.isValidIssueKey(issueKey)) {
      return false;
    }
    if (!beginReviewRequest()) {
      return false;
    }
    render();
    try {
      let userInstruction;
      try {
        userInstruction = await readFreshInstructionForSubmit(
          issueKey,
          chrome.storage.local,
          () => getActiveIssueKey(chrome)
        );
      } catch (error) {
        setInstructionReadError(issueKey, error);
        return false;
      }
      if (!state.context || state.context.issueKey !== issueKey) {
        setInstructionReadError(issueKey, new Error("stale_issue_context"));
        return false;
      }
      state.instructionIssueKey = issueKey;
      state.instructionSet = userInstruction !== null;
      state.instructionLoading = false;
      state.instructionError = null;
      const payload = root.RdvLocalReviewApi.buildReviewRequest({
        context: state.context,
        ide: state.config && state.config.ide ? state.config.ide : "codex",
        force: forceReview,
        timeoutSec: Number(
          state.config && state.config.timeoutSec
            ? state.config.timeoutSec
            : root.RdvLocalReviewApi.DEFAULT_TIMEOUT_SEC
        ),
        init: buildCurrentSourceInit(),
        runner: handoff ? { mode: "handoff" } : null,
        userInstruction
      });
      const response = await state.api.createReview(payload);
      state.status = response.status;
      await sendRuntimeMessage({ type: "refreshIssueStatus", issueKey });
      await notifyIssueStatusChanged();
      return true;
    } catch (error) {
      state.status = Object.assign({}, state.status || {}, {
        feedback: {
          code: "review_create_failed",
          tone: "error",
          title: "Не удалось запустить ревью",
          message: error && error.message ? error.message : "Повторите попытку."
        }
      });
      return false;
    } finally {
      endReviewRequest();
      render();
    }
  }

  async function cancelThenRefreshProjection(api, issueKey, jobId) {
    const cancelResponse = await api.cancel(jobId);
    try {
      const statusResponse = await api.getReviewByIssue(issueKey);
      return statusResponse.status;
    } catch (error) {
      if (cancelResponse && cancelResponse.status) {
        return cancelResponse.status;
      }
      throw error;
    }
  }

  function resetConfirmStates() {
    state.submitPending = false;
    state.preparePending = false;
  }

  function beginReviewRequest(requestState = state) {
    if (requestState.reviewRequestPending) {
      return false;
    }
    requestState.reviewRequestPending = true;
    return true;
  }

  function endReviewRequest(requestState = state) {
    requestState.reviewRequestPending = false;
  }

  function beginSubmitConfirm() {
    resetConfirmStates();
    state.submitPending = true;
  }

  function beginPrepareConfirm() {
    resetConfirmStates();
    state.preparePending = true;
  }

  function handleSubmitClick() {
    beginSubmitConfirm();
    render();
  }

  async function handleSubmitConfirmSend() {
    resetConfirmStates();
    await submitReview({ force: shouldForceOnSubmitConfirm(state.status) });
  }

  function handleSubmitConfirmCancel() {
    resetConfirmStates();
    render();
  }

  function handlePrepareClick() {
    beginPrepareConfirm();
    render();
  }

  async function handlePrepareConfirmSend() {
    resetConfirmStates();
    await submitReview({ force: false, handoff: true });
  }

  function handlePrepareConfirmCancel() {
    resetConfirmStates();
    render();
  }

  function resolveSettingsTab(tab) {
    return tab === "config" ? "config" : "service";
  }

  function setSettingsTab(tab) {
    const active = resolveSettingsTab(tab);
    const isService = active === "service";
    const serviceTab = $("settingsTabService");
    const configTab = $("settingsTabConfig");
    const servicePanel = $("servicePanel");
    const configPanel = $("configPanel");
    if (!serviceTab || !configTab || !servicePanel || !configPanel) {
      return;
    }
    serviceTab.classList.toggle("active", isService);
    configTab.classList.toggle("active", !isService);
    serviceTab.setAttribute("aria-selected", isService ? "true" : "false");
    configTab.setAttribute("aria-selected", isService ? "false" : "true");
    servicePanel.hidden = !isService;
    configPanel.hidden = isService;
  }

  async function setSourceMode(mode) {
    state.reviewSourceMode = mode;
    state.sourceModeTouched = true;
    resetConfirmStates();
    render();
    if (
      mode === "mr" &&
      state.context &&
      state.context.issueKey &&
      state.mrDiscoveryLoadedForIssue !== state.context.issueKey
    ) {
      await loadMrDiscoveryForCurrentIssue();
    }
  }

  function handleMrListClick(event) {
    const button = event.target && event.target.closest ? event.target.closest("[data-mr-key]") : null;
    if (!button) {
      return;
    }
    state.selectedMrKey = button.dataset.mrKey;
    state.reviewSourceMode = "mr";
    state.sourceModeTouched = true;
    resetConfirmStates();
    render();
  }

  async function cancelReview() {
    if (!state.status || !state.status.jobId) {
      return;
    }
    try {
      state.status = await cancelThenRefreshProjection(state.api, state.context.issueKey, state.status.jobId);
      syncSourceSelection();
      await notifyIssueStatusChanged();
      render();
    } catch (error) {
      state.status = { detailStatus: "service_unavailable", availableActions: ["refresh"], error: { code: "service_unavailable", message: error.message } };
      syncSourceSelection();
      await notifyIssueStatusChanged();
      render();
    }
  }

  async function openAgentLog(apiClient, jobId, button, feedbackFn) {
    if (!apiClient || !jobId || !button || button.disabled) {
      return false;
    }
    const showFeedback = feedbackFn || root.RdvUiFeedback.flashButtonFeedback;
    button.disabled = true;
    try {
      await apiClient.openAgentLog(jobId);
      showFeedback(button, { success: true, successLabel: "✓" });
      return true;
    } catch {
      showFeedback(button, { success: false, failLabel: "×" });
      return false;
    }
  }

  function handleOpenAgentLog() {
    const jobId = state.status && state.status.jobId;
    return openAgentLog(state.api, jobId, $("openAgentLog"));
  }

  async function openChanges(apiClient, jobId, button, feedbackFn) {
    if (!apiClient || !jobId || !button || button.disabled) {
      return false;
    }
    const showFeedback = feedbackFn || root.RdvUiFeedback.flashButtonFeedback;
    button.disabled = true;
    try {
      await apiClient.openChanges(jobId);
      showFeedback(button, { success: true, successLabel: "✓" });
      return true;
    } catch {
      showFeedback(button, { success: false, failLabel: "×" });
      return false;
    }
  }

  function handleOpenChanges() {
    const jobId = state.status && state.status.jobId;
    return openChanges(state.api, jobId, $("openChanges"));
  }

  function buildReportPageUrl(jobId, runtime, issueKey) {
    return root.RdvArtifactPageUtils.buildReportPageUrl(jobId, runtime, issueKey);
  }

  function buildReasoningPageUrl(jobId, runtime, issueKey) {
    return root.RdvArtifactPageUtils.buildReasoningPageUrl(jobId, runtime, issueKey);
  }

  function buildArtifactTabCreateOptions({ url, openerTabId, background }) {
    const options = { url, active: !background };
    if (typeof openerTabId === "number") {
      options.openerTabId = openerTabId;
    }
    return options;
  }

  async function openArtifactTab({ url, button, background }) {
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      await chrome.tabs.create(
        buildArtifactTabCreateOptions({
          url,
          openerTabId: tab && tab.id,
          background
        })
      );
      root.RdvUiFeedback.flashButtonFeedback(button, { success: true, successLabel: "Открыто" });
    } catch {
      root.RdvUiFeedback.flashButtonFeedback(button, { success: false, failLabel: "Не удалось" });
    }
  }

  async function openReport(options) {
    const button = $("openReport");
    if (!state.status || !state.status.jobId || !state.status.reportPath) {
      root.RdvUiFeedback.flashButtonFeedback(button, { success: false, failLabel: "Не удалось" });
      return;
    }
    const url = buildReportPageUrl(state.status.jobId, chrome.runtime, state.status.issueKey);
    await openArtifactTab({ url, button, background: Boolean(options && options.background) });
  }

  async function openReasoning(options) {
    const button = $("openReasoning");
    if (!state.status || !state.status.jobId || !state.status.reasoningPath) {
      root.RdvUiFeedback.flashButtonFeedback(button, { success: false, failLabel: "Не удалось" });
      return;
    }
    const url = buildReasoningPageUrl(state.status.jobId, chrome.runtime, state.status.issueKey);
    await openArtifactTab({ url, button, background: Boolean(options && options.background) });
  }

  function handleArtifactAuxClick(event, openFn) {
    if (!event || event.button !== 1) {
      return null;
    }
    event.preventDefault();
    return openFn({ background: true });
  }

  function preventArtifactMiddleMouseDefault(event) {
    if (event && event.button === 1) {
      event.preventDefault();
    }
  }

  function refreshInstructionAfterResume() {
    const issueKey = state.context && state.context.issueKey;
    if (root.RdvUserInstructionStore.isValidIssueKey(issueKey)) {
      return refreshInstructionState(issueKey);
    }
    return Promise.resolve(false);
  }

  async function init() {
    state.settings = await getSettings();
    state.api = new root.RdvLocalReviewApi.LocalReviewApi(state.settings);
    fillServiceSettings(state.settings);
    $("refreshButton").addEventListener("click", () => refreshAll({ allowMrDiscovery: true }));
    $("saveServiceSettings").addEventListener("click", saveServiceSettings);
    $("openProjectConfig").addEventListener("click", openProjectConfig);
    $("openInstructionEditor").addEventListener("click", openInstructionEditor);
    $("submitReview").addEventListener("click", handleSubmitClick);
    $("submitConfirmSend").addEventListener("click", handleSubmitConfirmSend);
    $("submitConfirmCancel").addEventListener("click", handleSubmitConfirmCancel);
    $("prepareReview").addEventListener("click", handlePrepareClick);
    $("prepareConfirmSend").addEventListener("click", handlePrepareConfirmSend);
    $("prepareConfirmCancel").addEventListener("click", handlePrepareConfirmCancel);
    $("cancelReview").addEventListener("click", cancelReview);
    $("openAgentLog").addEventListener("click", handleOpenAgentLog);
    $("openChanges").addEventListener("click", handleOpenChanges);
    $("openReport").addEventListener("click", () => openReport({ background: false }));
    $("openReasoning").addEventListener("click", () => openReasoning({ background: false }));
    $("openReport").addEventListener("mousedown", preventArtifactMiddleMouseDefault);
    $("openReasoning").addEventListener("mousedown", preventArtifactMiddleMouseDefault);
    $("openReport").addEventListener("auxclick", (event) => handleArtifactAuxClick(event, openReport));
    $("openReasoning").addEventListener("auxclick", (event) => handleArtifactAuxClick(event, openReasoning));
    $("sourceTask").addEventListener("click", () => setSourceMode("task"));
    $("sourceMr").addEventListener("click", () => setSourceMode("mr"));
    $("settingsTabService").addEventListener("click", () => setSettingsTab("service"));
    $("settingsTabConfig").addEventListener("click", () => setSettingsTab("config"));
    $("mrList").addEventListener("click", handleMrListClick);
    setSettingsTab("service");
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") {
        resetConfirmStates();
      } else {
        void refreshInstructionAfterResume();
      }
    });
    window.addEventListener("focus", () => {
      void refreshInstructionAfterResume();
    });
    await refreshAll({ allowMrDiscovery: false });
  }

  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", init);
  }

  if (typeof module !== "undefined") {
    module.exports = {
      beginPrepareConfirm,
      beginReviewRequest,
      beginSubmitConfirm,
      openArtifactTab,
      buildArtifactTabCreateOptions,
      buildReportPageUrl,
      buildReasoningPageUrl,
      buildSourceInit,
      cancelThenRefreshProjection,
      computeUiState,
      buildProjectConfigTabOptions,
      buildInstructionEditorTabOptions,
      createProjectConfigTab,
      createInstructionEditorTab,
      normalizeDiagnosticEntries,
      renderFeedback,
      handleArtifactAuxClick,
      openAgentLog,
      openChanges,
      openProjectConfig,
      openInstructionEditor,
      isSupportedJiraIssueContext,
      mergeRequestCandidates,
      mergeRequestKey,
      normalizeMergeRequests,
      normalizeDiscoveryResponse,
      renderMrDiscoveryError,
      beginMrDiscoveryRequest,
      isActiveMrDiscoveryRequest,
      mrDiscoveryErrorKey,
      hasRemoteDiscoveryDiagnostics,
      normalizeMrSource,
      notJiraIssueStatus,
      render,
      readFreshInstructionForSubmit,
      refreshInstructionState,
      remoteDiscoveryLines,
      resetConfirmStates,
      endReviewRequest,
      resolveSettingsTab,
      REQUIRES_JIRA_ISSUE_MESSAGE,
      setSettingsTab,
      setConfirmActionsState,
      shouldForceOnSubmitConfirm,
      syncReviewSourceState
    };
  }
})(typeof globalThis !== "undefined" ? globalThis : window);
