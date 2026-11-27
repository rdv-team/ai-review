/* global chrome, importScripts */
if (typeof importScripts === "function") {
  importScripts("issue.js", "status.js", "api.js", "commit-selection-store.js");
} else if (typeof require !== "undefined") {
  globalThis.RdvIssue = require("./issue.js");
  globalThis.RdvReviewStatus = require("./status.js");
  globalThis.RdvLocalReviewApi = require("./api.js");
  globalThis.RdvCommitSelection = require("./commit-selection-store.js");
}

const ACTIVE_ALARM = "rdv-local-review-poll";
const TRUSTED_STORAGE_ACCESS = "TRUSTED_CONTEXTS";
let selectionStore;
let selectionQueue = Promise.resolve();

async function selectionContext(context) {
  const settings = await getSettings();
  const api = new globalThis.RdvLocalReviewApi.LocalReviewApi(settings);
  const response = await api.getPrefixConfig(context.issueKey.split("-", 1)[0]);
  const config = response.config || {};
  const init = context.init ? { ...context.init } : null;
  if (init) delete init.mrTitle;
  return {
    issueKey: context.issueKey,
    mode: context.mode === "mr" ? "mr" : "task",
    init,
    config: { repoPath: config.repoPath, branch: config.branch, remote: config.remote },
    service: { port: settings.port }
  };
}

async function executeSelectionCommand(message) {
  if (!selectionStore) selectionStore = globalThis.RdvCommitSelection.createStore(chrome.storage.session);
  if (message.command === "issue" || message.command === "consume") return selectionStore(message.command, message);
  if (message.command === "activate") {
    return selectionStore("activate", { context: await selectionContext(message.context) });
  }
  const draft = await selectionStore(message.command === "current" ? "current" : "read", message);
  if (!draft) return null;
  const currentContext = await selectionContext(draft.context);
  if (globalThis.RdvCommitSelection.identity(currentContext) !== globalThis.RdvCommitSelection.identity(draft.context)) {
    await selectionStore("activate", { context: currentContext });
    if (message.command === "current") return selectionStore("current", message);
    throw new Error("stale_draft");
  }
  if (message.command === "current") return draft;
  return selectionStore(message.command, message);
}

function handleSelectionCommand(message) {
  const result = selectionQueue.then(() => executeSelectionCommand(message));
  selectionQueue = result.catch(() => undefined);
  return result;
}
const ACTION_ICON_PATHS = {
  default: {
    "16": "icons/review-default-16.png",
    "32": "icons/review-default-32.png",
    "48": "icons/review-default-48.png",
    "128": "icons/review-default-128.png"
  },
  error: {
    "16": "icons/review-error-16.png",
    "32": "icons/review-error-32.png",
    "48": "icons/review-error-48.png",
    "128": "icons/review-error-128.png"
  },
  done: {
    "16": "icons/review-done-16.png",
    "32": "icons/review-done-32.png",
    "48": "icons/review-done-48.png",
    "128": "icons/review-done-128.png"
  },
  running: {
    "16": "icons/review-running-16.png",
    "32": "icons/review-running-32.png",
    "48": "icons/review-running-48.png",
    "128": "icons/review-running-128.png"
  }
};

function shouldPollStatus(status) {
  return Boolean(status && globalThis.RdvReviewStatus.isActiveStatus(status));
}

function actionIconPath(status) {
  const iconState = globalThis.RdvReviewStatus.iconState(status);
  return ACTION_ICON_PATHS[iconState] || ACTION_ICON_PATHS.default;
}

async function setActionIcon(status) {
  if (!chrome.action || typeof chrome.action.setIcon !== "function") {
    return;
  }
  await chrome.action.setIcon({ path: actionIconPath(status) });
}

async function restrictLocalStorageAccess(storageArea) {
  if (!storageArea || typeof storageArea.setAccessLevel !== "function") {
    return false;
  }
  await storageArea.setAccessLevel({ accessLevel: TRUSTED_STORAGE_ACCESS });
  return true;
}

function reportStorageAccessFailure(error, logger = console) {
  logger.error("Не удалось ограничить доступ к chrome.storage.local.", error);
  return false;
}

async function getActiveIssueStatus() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const parsed = globalThis.RdvIssue.parseIssueFromUrl(tab && tab.url ? tab.url : "");
  if (parsed.ok) await handleSelectionCommand({ command: "issue", issueKey: parsed.issueKey });
  if (!parsed.ok) {
    return null;
  }
  const key = `issue:${parsed.issueKey}`;
  const stored = await chrome.storage.local.get(key);
  return stored[key] && stored[key].status ? stored[key].status : { detailStatus: "issue_detected" };
}

async function updateActiveActionIcon() {
  const status = await getActiveIssueStatus();
  await setActionIcon(status);
}

async function getSettings() {
  const stored = await chrome.storage.local.get({ servicePort: 8765, serviceToken: "" });
  return { port: stored.servicePort, token: stored.serviceToken };
}

async function saveIssueState(context, status) {
  if (!context || !context.issueKey) {
    return;
  }
  const key = `issue:${context.issueKey}`;
  const current = await chrome.storage.local.get(key);
  const previous = current[key] || {};
  const previousContext = previous.context || {};
  const nextContext = Object.assign({}, context);
  if (
    previousContext.issueKey === context.issueKey &&
    previousContext.mrDiscovery &&
    !context.mrDiscovery
  ) {
    nextContext.mrDiscovery = previousContext.mrDiscovery;
    nextContext.mergeRequests = Array.isArray(previousContext.mergeRequests) ? previousContext.mergeRequests : [];
  }
  const nextStatus = status === undefined ? previous.status || null : status;
  await chrome.storage.local.set({ [key]: { context: nextContext, status: nextStatus, updatedAt: Date.now() } });
  await updateActiveActionIcon().catch(() => null);
}

async function refreshIssueStatus(issueKey) {
  const settings = await getSettings();
  const api = new globalThis.RdvLocalReviewApi.LocalReviewApi(settings);
  const response = await api.getReviewByIssue(issueKey);
  const key = `issue:${issueKey}`;
  const current = await chrome.storage.local.get(key);
  const previous = current[key] || {};
  await saveIssueState(previous.context || { issueKey }, response.status);
  return response.status;
}

if (typeof chrome !== "undefined" && chrome.storage && chrome.storage.local) {
  void restrictLocalStorageAccess(chrome.storage.local).catch(reportStorageAccessFailure);
}

if (typeof chrome !== "undefined" && chrome.runtime && chrome.alarms) {
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message && message.type === "commitSelection") {
      if (!_sender || typeof _sender.url !== "string" || !_sender.url.startsWith(chrome.runtime.getURL(""))) {
        sendResponse({ ok: false, error: "untrusted_context" });
        return false;
      }
      handleSelectionCommand(message)
        .then((draft) => sendResponse({ ok: true, draft }))
        .catch((error) => sendResponse({ ok: false, error: error.code || error.message || "storage_unavailable" }));
      return true;
    }
    if (message && message.type === "jiraContext") {
      saveIssueState(message.context).then(() => sendResponse({ ok: true }));
      return true;
    }
    if (message && message.type === "issueStatusChanged") {
      saveIssueState(message.context, message.status).then(() => sendResponse({ ok: true }));
      return true;
    }
    if (message && message.type === "refreshIssueStatus") {
      refreshIssueStatus(message.issueKey)
        .then((status) => sendResponse({ ok: true, status }))
        .catch((error) => sendResponse({ ok: false, error: String(error.message || error) }));
      return true;
    }
    return false;
  });

  chrome.alarms.create(ACTIVE_ALARM, { periodInMinutes: 0.25 });
  chrome.alarms.onAlarm.addListener(async (alarm) => {
    if (alarm.name !== ACTIVE_ALARM) {
      return;
    }
    const stored = await chrome.storage.local.get(null);
    const activeIssues = Object.values(stored)
      .filter((item) => item && item.context && item.context.issueKey && shouldPollStatus(item.status))
      .map((item) => item.context.issueKey);
    await Promise.all(activeIssues.map((issueKey) => refreshIssueStatus(issueKey).catch(() => null)));
    await updateActiveActionIcon().catch(() => null);
  });
  if (chrome.tabs) {
    chrome.tabs.onActivated.addListener(() => {
      updateActiveActionIcon().catch(() => null);
    });
    chrome.tabs.onUpdated.addListener((_tabId, changeInfo) => {
      if (changeInfo.status === "complete" || changeInfo.url) {
        updateActiveActionIcon().catch(() => null);
      }
    });
  }
}

if (typeof module !== "undefined") {
  module.exports = {
    ACTION_ICON_PATHS,
    TRUSTED_STORAGE_ACCESS,
    actionIconPath,
    reportStorageAccessFailure,
    restrictLocalStorageAccess,
    shouldPollStatus
  };
}
