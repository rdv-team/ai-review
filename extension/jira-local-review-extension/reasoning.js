/* global chrome, hljs */
(function reasoningMain(root) {
  "use strict";

  const $ = (id) => document.getElementById(id);

  function escapeHtml(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function renderYaml(yaml, hljsImpl) {
    const highlighter = hljsImpl || (typeof hljs !== "undefined" ? hljs : null);
    if (!highlighter || typeof highlighter.highlight !== "function") {
      return escapeHtml(yaml);
    }
    try {
      return highlighter.highlight(String(yaml || ""), { language: "yaml" }).value;
    } catch {
      return escapeHtml(yaml);
    }
  }

  async function copyToClipboard(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      return false;
    }
  }

  async function handleCopyClick(button, text) {
    const success = await copyToClipboard(text);
    root.RdvUiFeedback.flashButtonFeedback(button, {
      success,
      successLabel: "Скопировано",
      failLabel: "Не удалось",
    });
  }

  async function getSettings() {
    const stored = await chrome.storage.local.get({ servicePort: 8765, serviceToken: "" });
    return { port: stored.servicePort, token: stored.serviceToken };
  }

  const pageUtils = root.RdvArtifactPageUtils;

  function jobIdFromLocation(locationRef) {
    return pageUtils.jobIdFromLocation(locationRef);
  }

  function issueKeyFromLocation(locationRef) {
    return pageUtils.issueKeyFromLocation(locationRef);
  }

  function buildReasoningPageUrl(jobId, runtime, issueKey) {
    return pageUtils.buildReasoningPageUrl(jobId, runtime, issueKey);
  }

  function renderError(message) {
    $("error").hidden = false;
    $("error").textContent = message;
  }

  async function init() {
    const jobId = jobIdFromLocation(window.location);
    if (!jobId) {
      renderError("jobId не указан.");
      return;
    }
    const issueKeyFromUrl = issueKeyFromLocation(window.location);
    pageUtils.applyArtifactPageTitle(
      $("title"),
      "reasoning",
      pageUtils.resolveArtifactLabel({ issueKeyFromUrl, issueKeyFromApi: "", jobId }),
    );
    const api = new root.RdvLocalReviewApi.LocalReviewApi(await getSettings());
    try {
      const reasoning = await api.getReasoning(jobId);
      const yamlSource = reasoning.content || "";
      pageUtils.applyArtifactPageTitle(
        $("title"),
        "reasoning",
        pageUtils.resolveArtifactLabel({
          issueKeyFromUrl,
          issueKeyFromApi: reasoning.issueKey,
          jobId,
        }),
      );
      $("reasoningPath").textContent = reasoning.reasoningPath || "";
      const codeEl = $("contentCode");
      codeEl.innerHTML = renderYaml(yamlSource);
      codeEl.classList.add("hljs");
      $("copyYaml").addEventListener("click", () => handleCopyClick($("copyYaml"), yamlSource));
    } catch (error) {
      renderError(error.message || String(error));
    }
  }

  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", init);
  }

  if (typeof module !== "undefined") {
    module.exports = {
      escapeHtml,
      renderYaml,
      jobIdFromLocation,
      issueKeyFromLocation,
      buildReasoningPageUrl,
      copyToClipboard,
      handleCopyClick,
    };
  }
})(typeof globalThis !== "undefined" ? globalThis : window);
