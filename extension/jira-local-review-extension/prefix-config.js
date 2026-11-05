(function attachPrefixConfig(root) {
  "use strict";

  const DEFAULT_CONFIG = Object.freeze({
    repoPath: "",
    reviewsRoot: "",
    branch: "origin/master",
    remote: "origin",
    ide: "codex",
    opencodeModel: "",
    useMcp: true,
    timeoutSec: 1800
  });
  const PROJECT_KEY_PATTERN = /^[A-Z][A-Z0-9]+$/;
  const ALLOWED_IDES = new Set(["codex", "cursor", "opencode"]);

  function isValidProjectKey(value) {
    return typeof value === "string" && PROJECT_KEY_PATTERN.test(value);
  }

  function projectKeyFromLocation(locationRef) {
    try {
      const value = new URL(locationRef.href).searchParams.get("projectKey") || "";
      return isValidProjectKey(value) ? value : "";
    } catch {
      return "";
    }
  }

  function buildEditorUrl(projectKey, runtime) {
    if (!isValidProjectKey(projectKey) || !runtime || typeof runtime.getURL !== "function") {
      return "";
    }
    const url = new URL(runtime.getURL("config.html"));
    url.searchParams.set("projectKey", projectKey);
    return url.toString();
  }

  function normalizeRepoPath(value) {
    return String(value || "")
      .replace(/\r\n?/g, "\n")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .join("\n");
  }

  function normalizeConfig(config) {
    const value = config || {};
    const timeoutSec = Number(value.timeoutSec);
    return {
      repoPath: normalizeRepoPath(value.repoPath),
      reviewsRoot: typeof value.reviewsRoot === "string" ? value.reviewsRoot.trim() : "",
      branch: typeof value.branch === "string" ? value.branch.trim() : DEFAULT_CONFIG.branch,
      remote: typeof value.remote === "string" ? value.remote.trim() : DEFAULT_CONFIG.remote,
      ide: ALLOWED_IDES.has(value.ide) ? value.ide : DEFAULT_CONFIG.ide,
      opencodeModel: typeof value.opencodeModel === "string" ? value.opencodeModel.trim() : "",
      useMcp: typeof value.useMcp === "boolean" ? value.useMcp : DEFAULT_CONFIG.useMcp,
      timeoutSec: Number.isFinite(timeoutSec) && timeoutSec > 0 ? timeoutSec : DEFAULT_CONFIG.timeoutSec
    };
  }

  function configFromFields(fields) {
    return normalizeConfig({
      repoPath: fields.repoPath.value,
      reviewsRoot: fields.reviewsRoot.value,
      branch: fields.branch.value,
      remote: fields.remote.value,
      ide: fields.ide.value,
      opencodeModel: fields.opencodeModel.value,
      useMcp: fields.useMcp.checked,
      timeoutSec: fields.timeoutSec.value
    });
  }

  const prefixConfig = {
    DEFAULT_CONFIG,
    buildEditorUrl,
    configFromFields,
    isValidProjectKey,
    normalizeConfig,
    normalizeRepoPath,
    projectKeyFromLocation
  };
  root.RdvPrefixConfig = prefixConfig;
  if (typeof module !== "undefined") {
    module.exports = prefixConfig;
  }
})(typeof globalThis !== "undefined" ? globalThis : window);
