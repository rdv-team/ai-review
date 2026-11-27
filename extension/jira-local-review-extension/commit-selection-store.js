(function attachCommitSelection(root) {
  "use strict";

  const STORAGE_KEY = "rdv:commit-selection";
  const SHA = /^[a-f0-9]{40}$/i;

  function fail(code) {
    const error = new Error(code);
    error.code = code;
    throw error;
  }

  function identity(value) {
    if (Array.isArray(value)) return JSON.stringify(value.map((item) => JSON.parse(identity(item))));
    if (value && typeof value === "object") {
      return JSON.stringify(Object.fromEntries(Object.keys(value).sort().map((key) => [key, JSON.parse(identity(value[key]))])));
    }
    return JSON.stringify(value === undefined ? null : value);
  }

  function normalizeBoundaries(values) {
    if (!Array.isArray(values)) fail("invalid_boundaries");
    const paths = new Set();
    return values.map((value) => {
      if (!value || typeof value.repoPath !== "string" || !value.repoPath.trim() || !SHA.test(value.sha)) fail("invalid_boundaries");
      const normalizedPath = value.repoPath.replace(/\\/g, "/").replace(/\/+$/, "");
      const pathKey = /^(?:[a-z]:\/|\/\/)/i.test(normalizedPath) ? normalizedPath.toLowerCase() : normalizedPath;
      if (paths.has(pathKey)) fail("duplicate_repository");
      paths.add(pathKey);
      return { repoPath: value.repoPath, sha: value.sha.toLowerCase() };
    });
  }

  function selectionState(repositories, boundaries) {
    const selected = new Map(boundaries.map((item) => [item.repoPath, item.sha.toLowerCase()]));
    let count = 0;
    const missing = [];
    const paths = new Set(repositories.map((repo) => repo.repoPath));
    for (const path of selected.keys()) if (!paths.has(path)) missing.push(path);
    for (const repo of repositories) {
      const sha = selected.get(repo.repoPath);
      if (!sha) {
        count += repo.commits.length;
        continue;
      }
      const commit = repo.commits.find((item) => item.sha.toLowerCase() === sha);
      if (!commit) missing.push(repo.repoPath);
      else if (!Number.isInteger(commit.afterCount) || commit.afterCount < 0) fail("invalid_candidates");
      else count += commit.afterCount;
    }
    return { count, missing, canApply: missing.length === 0 && count > 0 };
  }

  // One instance belongs to the service worker. Storage.session survives worker
  // suspension, while the promise chain serializes revision checks and writes.
  function createStore(storage, makeId = () => root.crypto.randomUUID()) {
    let queue = Promise.resolve();
    const retired = new Set();
    async function execute(command, input) {
      if (!storage || typeof storage.setAccessLevel !== "function") fail("storage_unavailable");
      await storage.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
      const stored = await storage.get(STORAGE_KEY);
      let draft = stored[STORAGE_KEY] || null;
      if (command === "current") return draft && !retired.has(draft.draftId) && draft.context.issueKey === input.issueKey ? draft : null;
      if (command === "issue") {
        if (draft && draft.context.issueKey !== input.issueKey) await storage.remove(STORAGE_KEY);
        return null;
      }
      if (command === "consume" && (!draft || draft.draftId !== input.draftId)) return null;
      if (command === "activate") {
        if (!input.context || !root.RdvIssue.ISSUE_RE.test(input.context.issueKey || "")) fail("invalid_issue_key");
        if (!draft || retired.has(draft.draftId) || identity(draft.context) !== identity(input.context)) {
          draft = { draftId: makeId(), revision: 0, context: input.context, boundaries: [] };
          await storage.set({ [STORAGE_KEY]: draft });
        }
        return draft;
      }
      if (!draft || retired.has(draft.draftId) || draft.draftId !== input.draftId) fail("stale_draft");
      if (command === "read") return draft;
      if (command === "consume") {
        retired.add(draft.draftId);
        await storage.remove(STORAGE_KEY);
        return null;
      }
      if (command !== "apply") fail("invalid_command");
      if (input.revision !== draft.revision) fail("revision_conflict");
      draft = { ...draft, revision: draft.revision + 1, boundaries: normalizeBoundaries(input.boundaries) };
      await storage.set({ [STORAGE_KEY]: draft });
      return draft;
    }
    return function command(name, input = {}) {
      const result = queue.then(() => execute(name, input));
      queue = result.catch(() => undefined);
      return result;
    };
  }

  async function request(runtime, command, input = {}) {
    const response = await runtime.sendMessage({ type: "commitSelection", command, ...input });
    if (!response || !response.ok) fail(response && response.error || "storage_unavailable");
    return response.draft;
  }

  const api = { STORAGE_KEY, identity, normalizeBoundaries, selectionState, createStore, request };
  root.RdvCommitSelection = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : window);
