/* global chrome */
(function configEditorMain(root) {
  "use strict";

  const INVALID_PREFIX_MESSAGE =
    "Не указан корректный префикс проекта. Откройте редактор со страницы поддерживаемой задачи Jira вида /browse/ABC-123.";
  const SERVICE_ERROR_MESSAGE =
    "Локальный сервис недоступен. Проверьте настройки port/token в popup и повторите попытку.";

  const state = { projectKey: "", api: null };
  const $ = (id) => document.getElementById(id);

  function fields() {
    return {
      repoPath: $("repoPath"),
      reviewsRoot: $("reviewsRoot"),
      branch: $("branch"),
      remote: $("remote"),
      ide: $("ide"),
      opencodeModel: $("opencodeModel"),
      useMcp: $("useMcp"),
      timeoutSec: $("timeoutSec")
    };
  }

  function fillForm(config) {
    const normalized = root.RdvPrefixConfig.normalizeConfig(config);
    const formFields = fields();
    for (const key of Object.keys(formFields)) {
      if (key === "useMcp") {
        formFields[key].checked = normalized[key];
      } else {
        formFields[key].value = String(normalized[key]);
      }
    }
    updateModelVisibility();
  }

  function updateModelVisibility() {
    $("opencodeModelField").hidden = $("ide").value !== "opencode";
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

  function setDirty(dirty) {
    $("unsavedMarker").hidden = !dirty;
    if (state.projectKey) {
      document.title = `${state.projectKey}${dirty ? " *" : ""} · Конфигурация проекта · RDV AIR`;
    }
  }

  function setPending(pending) {
    $("saveConfig").disabled = pending;
    $("retryLoad").disabled = pending;
  }

  async function getSettings() {
    const stored = await chrome.storage.local.get({ servicePort: 8765, serviceToken: "" });
    return { port: stored.servicePort, token: stored.serviceToken };
  }

  async function loadPrefixConfig(apiClient, projectKey) {
    try {
      const response = await apiClient.getPrefixConfig(projectKey);
      return { config: response.config, isNew: false };
    } catch (error) {
      if (error.status === 404) {
        return { config: root.RdvPrefixConfig.DEFAULT_CONFIG, isNew: true };
      }
      throw error;
    }
  }

  async function persistPrefixConfig(apiClient, projectKey, config, requiredConfigFieldsFn) {
    const validation = validateConfig(config, requiredConfigFieldsFn);
    if (!validation.valid) {
      return { saved: false, missing: validation.missing };
    }
    const response = await apiClient.savePrefixConfig(projectKey, config);
    return { saved: true, config: response.config || config };
  }

  async function loadConfig() {
    setPending(true);
    $("retryLoad").hidden = true;
    showMessage("loading", `Загрузка конфигурации проекта ${state.projectKey}…`);
    try {
      const result = await loadPrefixConfig(state.api, state.projectKey);
      fillForm(result.config);
      $("configForm").hidden = false;
      showMessage(
        "success",
        result.isNew
          ? "Конфигурация ещё не создана. Заполните поля и сохраните её."
          : "Сохранённая конфигурация загружена."
      );
    } catch (error) {
      $("configForm").hidden = false;
      $("retryLoad").hidden = false;
      showMessage("error", `${SERVICE_ERROR_MESSAGE} ${error.message || ""}`.trim());
    } finally {
      setPending(false);
    }
  }

  function validateConfig(config, requiredConfigFieldsFn) {
    const missing = requiredConfigFieldsFn(config);
    return { valid: missing.length === 0, missing };
  }

  async function saveConfig(event) {
    if (event && typeof event.preventDefault === "function") {
      event.preventDefault();
    }
    const config = root.RdvPrefixConfig.configFromFields(fields());
    const validation = validateConfig(config, root.RdvLocalReviewApi.requiredConfigFields);
    if (!validation.valid) {
      showMessage("validation", `Заполните обязательные поля: ${validation.missing.join(", ")}.`);
      showSaveStatus("validation", "Конфигурация не сохранена.");
      return false;
    }
    setPending(true);
    showMessage("loading", `Сохранение конфигурации проекта ${state.projectKey}…`);
    showSaveStatus("loading", "Сохранение…");
    try {
      const result = await persistPrefixConfig(
        state.api,
        state.projectKey,
        config,
        root.RdvLocalReviewApi.requiredConfigFields
      );
      fillForm(result.config);
      showMessage("success", `Конфигурация проекта ${state.projectKey} сохранена.`);
      showSaveStatus("success", "Сохранено.");
      setDirty(false);
      return true;
    } catch (error) {
      showMessage("error", `${SERVICE_ERROR_MESSAGE} ${error.message || ""}`.trim());
      showSaveStatus("error", "Не удалось сохранить. Повторите попытку.");
      return false;
    } finally {
      setPending(false);
    }
  }

  async function init() {
    state.projectKey = root.RdvPrefixConfig.projectKeyFromLocation(window.location);
    if (!state.projectKey) {
      showMessage("error", INVALID_PREFIX_MESSAGE);
      return;
    }
    $("projectKey").textContent = state.projectKey;
    setDirty(false);
    state.api = new root.RdvLocalReviewApi.LocalReviewApi(await getSettings());
    $("configForm").addEventListener("submit", saveConfig);
    const markDirty = () => {
      setDirty(true);
      showSaveStatus("", "");
    };
    $("configForm").addEventListener("input", markDirty);
    $("configForm").addEventListener("change", markDirty);
    $("ide").addEventListener("change", updateModelVisibility);
    $("retryLoad").addEventListener("click", loadConfig);
    await loadConfig();
  }

  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", init);
  }

  if (typeof module !== "undefined") {
    module.exports = {
      INVALID_PREFIX_MESSAGE,
      SERVICE_ERROR_MESSAGE,
      fillForm,
      init,
      loadConfig,
      loadPrefixConfig,
      persistPrefixConfig,
      saveConfig,
      setDirty,
      showSaveStatus,
      updateModelVisibility,
      validateConfig
    };
  }
})(typeof globalThis !== "undefined" ? globalThis : window);
