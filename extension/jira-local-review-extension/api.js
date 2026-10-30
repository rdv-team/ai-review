(function attachApi(root) {
  "use strict";

  const DEFAULT_PORT = 8765;
  const DEFAULT_TIMEOUT_SEC = 1800;

  function requiredConfigFields(config) {
    return ["repoPath", "reviewsRoot", "branch", "remote"].filter((field) => {
      return !config || typeof config[field] !== "string" || !config[field].trim();
    });
  }

  function isConfigReady(config) {
    return requiredConfigFields(config).length === 0;
  }

  function buildReviewRequest({ context, ide, force, timeoutSec, init, runner, userInstruction }) {
    const payload = {
      issueKey: context.issueKey,
      projectKey: context.projectKey || context.issueKey.split("-", 1)[0],
      ide,
      force: Boolean(force),
      timeoutSec: Number(timeoutSec || DEFAULT_TIMEOUT_SEC),
      jira: {
        url: context.url,
        title: context.title || "",
        description: context.description || ""
      }
    };
    if (runner) {
      payload.runner = runner;
    }
    if (init && Object.keys(init).length > 0) {
      payload.init = init;
    }
    if (typeof userInstruction === "string" && userInstruction.length > 0) {
      payload.userInstruction = userInstruction;
    }
    return payload;
  }

  class LocalReviewApi {
    constructor(options = {}) {
      this.port = options.port || DEFAULT_PORT;
      this.token = options.token || "";
      this.host = options.host || "127.0.0.1";
    }

    baseUrl() {
      return `http://${this.host}:${this.port}`;
    }

    async request(path, options = {}) {
      const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
      if (this.token) {
        headers["X-Review-Tasks-Token"] = this.token;
      }
      const response = await fetch(`${this.baseUrl()}${path}`, Object.assign({}, options, { headers }));
      const body = await response.json();
      if (!response.ok) {
        const error = new Error(body && body.error ? body.error.message : response.statusText);
        error.status = response.status;
        error.body = body;
        throw error;
      }
      return body;
    }

    health() {
      return this.request("/health", { method: "GET" });
    }

    getPrefixConfig(prefix) {
      return this.request(`/prefix-config/${encodeURIComponent(prefix)}`, { method: "GET" });
    }

    savePrefixConfig(prefix, config) {
      return this.request(`/prefix-config/${encodeURIComponent(prefix)}`, {
        method: "PUT",
        body: JSON.stringify(config)
      });
    }

    getReviewByIssue(issueKey) {
      return this.request(`/reviews/by-issue/${encodeURIComponent(issueKey)}`, { method: "GET" });
    }

    discoverMergeRequestsByIssue(issueKey) {
      return this.request(`/gitlab/merge-requests/by-issue/${encodeURIComponent(issueKey)}`, { method: "GET" });
    }

    createReview(payload) {
      return this.request("/reviews", { method: "POST", body: JSON.stringify(payload) });
    }

    refreshStatus(jobId) {
      return this.request(`/reviews/${encodeURIComponent(jobId)}/status`, { method: "GET" });
    }

    getReport(jobId) {
      return this.request(`/reviews/${encodeURIComponent(jobId)}/report`, { method: "GET" });
    }

    getReasoning(jobId) {
      return this.request(`/reviews/${encodeURIComponent(jobId)}/reasoning`, { method: "GET" });
    }

    openAgentLog(jobId) {
      return this.request(`/reviews/${encodeURIComponent(jobId)}/open-agent-log`, { method: "POST" });
    }

    openReviewBsl(jobId) {
      return this.request(`/reviews/${encodeURIComponent(jobId)}/open-review-bsl`, { method: "POST" });
    }

    openChanges(jobId) {
      return this.request(`/reviews/${encodeURIComponent(jobId)}/open-changes`, { method: "POST" });
    }

    openJobLog(jobId, logRef) {
      return this.request(`/reviews/${encodeURIComponent(jobId)}/open-log`, {
        method: "POST",
        body: JSON.stringify({ logRef })
      });
    }

    cancel(jobId) {
      return this.request(`/reviews/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
    }
  }

  const api = { DEFAULT_PORT, DEFAULT_TIMEOUT_SEC, LocalReviewApi, requiredConfigFields, isConfigReady, buildReviewRequest };
  root.RdvLocalReviewApi = api;
  if (typeof module !== "undefined") {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : window);
