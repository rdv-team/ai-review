(function changesActionMain(root) {
  "use strict";

  function canOpenChanges(status) {
    return Boolean(status && typeof status.jobId === "string" && status.jobId.trim()
      && Array.isArray(status.availableActions) && status.availableActions.includes("open_changes"));
  }

  async function openChanges(apiClient, jobId, button, feedbackFn, options = {}) {
    if (!apiClient || !jobId || !button || button.disabled) {
      return false;
    }
    const showFeedback = feedbackFn || root.RdvUiFeedback.flashButtonFeedback;
    button.disabled = true;
    try {
      await apiClient.openChanges(jobId);
      showFeedback(button, { success: true, successLabel: options.successLabel || "✓" });
      if (options.onSuccess) options.onSuccess();
      return true;
    } catch (error) {
      showFeedback(button, { success: false, failLabel: options.failLabel || "×" });
      if (options.onError) options.onError(error);
      return false;
    }
  }

  const api = { canOpenChanges, openChanges };
  root.RdvChangesAction = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : window);
