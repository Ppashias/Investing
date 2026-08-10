/* JARVIS command center.
 *
 * Talks to the real backend — there is no mock path here. Every panel is
 * driven by an API response, and anything not implemented server-side is
 * absent from the UI rather than stubbed, per the Phase 1 rule against fake
 * capabilities.
 *
 * All DOM insertion uses textContent, never innerHTML with server data. The
 * Phase 0 audit found remote-data-to-innerHTML in the existing dashboard; the
 * same mistake here would be worse, because this page holds an access token.
 */
"use strict";

(function () {
  const TOKEN_KEY = "jarvis.token";
  const MAX_ACTIVITY = 200;

  const state = {
    token: localStorage.getItem(TOKEN_KEY) || "",
    conversationId: null,
    sending: false,
    activity: [],
    streamAbort: null,
  };

  const $ = (id) => document.getElementById(id);
  const el = {
    messages: $("messages"),
    input: $("input"),
    composer: $("composer"),
    sendBtn: $("sendBtn"),
    thinking: $("thinking"),
    thinkingLabel: $("thinkingLabel"),
    convTitle: $("convTitle"),
    newConvBtn: $("newConvBtn"),
    providerBadge: $("providerBadge"),
    connBadge: $("connBadge"),
    authGate: $("authGate"),
    tokenInput: $("tokenInput"),
    tokenSave: $("tokenSave"),
    memoryList: $("memoryList"),
    memoryCounts: $("memoryCounts"),
    memorySearch: $("memorySearch"),
    memoryType: $("memoryType"),
    memoryNotice: $("memoryNotice"),
    exportMemory: $("exportMemory"),
    memoryModal: $("memoryModal"),
    memType: $("memType"),
    memContent: $("memContent"),
    memFacts: $("memFacts"),
    memHistory: $("memHistory"),
    memSave: $("memSave"),
    memArchive: $("memArchive"),
    memDelete: $("memDelete"),
    memClose: $("memClose"),
    documentList: $("documentList"),
    knowledgeCounts: $("knowledgeCounts"),
    knowledgeNotice: $("knowledgeNotice"),
    sourceList: $("sourceList"),
    uploadDoc: $("uploadDoc"),
    docFile: $("docFile"),
    authError: $("authError"),
    activityList: $("activityList"),
    activityCount: $("activityCount"),
    clearActivity: $("clearActivity"),
    taskList: $("taskList"),
    taskCounts: $("taskCounts"),
    refreshTasks: $("refreshTasks"),
    toolList: $("toolList"),
    systemInfo: $("systemInfo"),
    confirmModal: $("confirmModal"),
    confirmTitle: $("confirmTitle"),
    confirmBody: $("confirmBody"),
    confirmArgs: $("confirmArgs"),
    confirmRisk: $("confirmRisk"),
    confirmIrreversible: $("confirmIrreversible"),
    confirmApprove: $("confirmApprove"),
    confirmDeny: $("confirmDeny"),
  };

  /* ── api ────────────────────────────────────────────────────────────── */

  async function api(path, options) {
    const opts = options || {};
    const headers = { "Content-Type": "application/json" };
    if (state.token) headers.Authorization = "Bearer " + state.token;

    let response;
    try {
      response = await fetch("/api" + path, {
        method: opts.method || "GET",
        headers: headers,
        body: opts.body ? JSON.stringify(opts.body) : undefined,
      });
    } catch (err) {
      setConnection(false);
      throw new Error("Cannot reach JARVIS. Is the server running?");
    }

    if (response.status === 401 || response.status === 503) {
      const body = await response.json().catch(() => ({}));
      if (response.status === 401 || /token/i.test(body.detail || "")) {
        showAuthGate(body.detail || "Authentication required.");
        throw new Error("unauthorized");
      }
    }

    const text = await response.text();
    const data = text ? JSON.parse(text) : {};
    if (!response.ok) {
      const err = new Error(
        (data.error && data.error.message) || data.detail || "Request failed"
      );
      err.code = data.error && data.error.code;
      throw err;
    }
    return data;
  }

  /* ── auth ───────────────────────────────────────────────────────────── */

  function showAuthGate(message) {
    el.authGate.hidden = false;
    if (message) {
      el.authError.textContent = message;
      el.authError.hidden = false;
    }
    el.tokenInput.focus();
  }

  el.tokenSave.addEventListener("click", async () => {
    const value = el.tokenInput.value.trim();
    if (!value) return;
    state.token = value;
    localStorage.setItem(TOKEN_KEY, value);
    el.authError.hidden = true;
    try {
      await api("/system/status");
      el.authGate.hidden = true;
      boot();
    } catch (err) {
      el.authError.textContent = "That token was not accepted.";
      el.authError.hidden = false;
    }
  });
  el.tokenInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") el.tokenSave.click();
  });

  /* ── rendering helpers ──────────────────────────────────────────────── */

  function node(tag, className, text) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function clearEmptyState() {
    const empty = el.messages.querySelector(".empty-state");
    if (empty) empty.remove();
  }

  function addMessage(role, text, options) {
    const opts = options || {};
    clearEmptyState();

    const wrap = node("div", "msg " + role + (opts.error ? " error" : ""));
    wrap.appendChild(node("div", "msg-role", role === "user" ? "YOU" : "JARVIS"));
    wrap.appendChild(node("div", "msg-body", text));

    if (opts.toolCalls && opts.toolCalls.length) {
      const chips = node("div", "toolchips");
      opts.toolCalls.forEach((call) => {
        chips.appendChild(
          node("span", "toolchip" + (call.is_error ? " err" : ""), call.tool)
        );
      });
      wrap.appendChild(chips);
    }
    if (opts.meta) wrap.appendChild(node("div", "msg-meta", opts.meta));

    el.messages.appendChild(wrap);
    el.messages.scrollTop = el.messages.scrollHeight;
    return wrap;
  }

  function formatMeta(response) {
    const parts = [];
    if (response.model) parts.push(response.model);
    if (response.usage && response.usage.output_tokens) {
      parts.push(
        response.usage.input_tokens + " in / " + response.usage.output_tokens + " out"
      );
    }
    if (response.duration_ms) parts.push(Math.round(response.duration_ms) + "ms");
    if (response.warnings && response.warnings.length) {
      parts.push("⚠ " + response.warnings.join(", "));
    }
    return parts.join(" · ");
  }

  /* ── chat ───────────────────────────────────────────────────────────── */

  el.composer.addEventListener("submit", (e) => {
    e.preventDefault();
    send();
  });

  el.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  el.input.addEventListener("input", () => {
    el.input.style.height = "auto";
    el.input.style.height = Math.min(el.input.scrollHeight, 180) + "px";
  });

  async function send() {
    const text = el.input.value.trim();
    if (!text || state.sending) return;

    state.sending = true;
    el.sendBtn.disabled = true;
    el.input.value = "";
    el.input.style.height = "auto";

    addMessage("user", text);
    setThinking(true, "thinking…");

    try {
      const response = await api("/chat", {
        method: "POST",
        body: { message: text, conversation_id: state.conversationId },
      });

      state.conversationId = response.conversation_id;
      if (response.conversation_id) {
        el.convTitle.textContent = response.conversation_id.slice(0, 14) + "…";
      }

      if (response.status === "needs_confirmation") {
        addMessage("assistant", response.text, { meta: formatMeta(response) });
        openConfirmation(response.pending_confirmation);
      } else {
        addMessage("assistant", response.text, {
          error: response.status === "error",
          toolCalls: response.tool_calls,
          meta: formatMeta(response),
        });
      }
      refreshTasks();
    } catch (err) {
      if (err.message !== "unauthorized") {
        addMessage("assistant", err.message, { error: true });
      }
    } finally {
      setThinking(false);
      state.sending = false;
      el.sendBtn.disabled = false;
      el.input.focus();
    }
  }

  function setThinking(on, label) {
    el.thinking.hidden = !on;
    if (label) el.thinkingLabel.textContent = label;
  }

  el.newConvBtn.addEventListener("click", () => {
    state.conversationId = null;
    el.messages.replaceChildren();
    const empty = node("div", "empty-state");
    empty.appendChild(node("p", null, "New conversation."));
    empty.appendChild(node("span", null, "Previous conversations are kept and retrievable via the API."));
    el.messages.appendChild(empty);
    el.convTitle.textContent = "new";
  });

  /* ── confirmation ───────────────────────────────────────────────────── */

  let pendingConfirmation = null;

  function openConfirmation(confirmation) {
    if (!confirmation) return;
    pendingConfirmation = confirmation;

    el.confirmTitle.textContent = confirmation.title || "Confirm this action";
    el.confirmBody.textContent = confirmation.body || "";
    el.confirmArgs.textContent = JSON.stringify(confirmation.arguments || {}, null, 2);
    el.confirmRisk.textContent = confirmation.risk_level || "MEDIUM";
    el.confirmRisk.className = "risk " + (confirmation.risk_level || "MEDIUM");
    el.confirmIrreversible.hidden = confirmation.reversible !== false;
    el.confirmModal.hidden = false;
  }

  async function decide(approved) {
    if (!pendingConfirmation) return;
    const id = pendingConfirmation.id;
    el.confirmModal.hidden = true;
    pendingConfirmation = null;

    try {
      await api("/confirmations/" + encodeURIComponent(id) + "/decide", {
        method: "POST",
        body: { approved: approved },
      });
      if (approved) {
        addMessage("assistant", "Approved. Ask me to continue and I will carry it out.", {
          meta: "confirmation " + id.slice(0, 12),
        });
      } else {
        addMessage("assistant", "Cancelled — I have not done that.", {
          meta: "confirmation " + id.slice(0, 12),
        });
      }
    } catch (err) {
      addMessage("assistant", err.message, { error: true });
    }
  }

  el.confirmApprove.addEventListener("click", () => decide(true));
  el.confirmDeny.addEventListener("click", () => decide(false));

  /* ── tabs ───────────────────────────────────────────────────────────── */

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("sel"));
      tab.classList.add("sel");
      const wanted = tab.dataset.tab;
      document.querySelectorAll(".tabpanel").forEach((panel) => {
        panel.hidden = panel.dataset.panel !== wanted;
      });
      if (wanted === "tasks") refreshTasks();
      if (wanted === "tools") refreshTools();
      if (wanted === "system") refreshSystem();
      if (wanted === "memory") refreshMemory();
      if (wanted === "knowledge") refreshKnowledge();
    });
  });

  /* ── activity ───────────────────────────────────────────────────────── */

  const KIND_CLASS = {
    REQUEST_COMPLETED: "ok",
    REQUEST_FAILED: "bad",
    ERROR: "bad",
    TOOL_CALL: "tool",
    MODEL_CALL: "model",
    PERMISSION_DECISION: "tool",
    CONFIRMATION_REQUESTED: "tool",
  };

  function renderActivity(entry) {
    const li = node("li");
    const kind = node("span", "act-kind " + (KIND_CLASS[entry.kind] || ""),
      (entry.kind || "").replace("REQUEST_", "").slice(0, 12));
    const summary = node("span", "act-summary", entry.summary || "");
    const time = node("span", "act-time",
      entry.created_at ? new Date(entry.created_at).toTimeString().slice(0, 8) : "");
    li.appendChild(kind);
    li.appendChild(summary);
    li.appendChild(time);
    return li;
  }

  function pushActivity(entry) {
    state.activity.unshift(entry);
    if (state.activity.length > MAX_ACTIVITY) state.activity.pop();
    el.activityList.prepend(renderActivity(entry));
    while (el.activityList.children.length > MAX_ACTIVITY) {
      el.activityList.lastChild.remove();
    }
    el.activityCount.textContent = state.activity.length + " events";
  }

  el.clearActivity.addEventListener("click", () => {
    state.activity = [];
    el.activityList.replaceChildren();
    el.activityCount.textContent = "0 events";
  });

  /* The stream is consumed with fetch + ReadableStream rather than
   * EventSource. EventSource cannot set request headers, which would force the
   * access token into the query string — the exact flaw the Phase 0 audit
   * raised against the existing dashboard (tokens in URLs reach server logs,
   * browser history, and Referer headers). Parsing SSE frames by hand is a
   * small price for keeping the credential in an Authorization header.
   */
  async function connectActivityStream() {
    if (state.streamAbort) state.streamAbort.abort();
    const abort = new AbortController();
    state.streamAbort = abort;

    const headers = { Accept: "text/event-stream" };
    if (state.token) headers.Authorization = "Bearer " + state.token;

    try {
      const response = await fetch("/api/activity/stream", {
        headers: headers,
        signal: abort.signal,
      });
      if (!response.ok || !response.body) throw new Error("stream unavailable");
      setConnection(true);

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });

        // Frames are separated by a blank line.
        let split;
        while ((split = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          handleFrame(frame);
        }
      }
      setConnection(false);
    } catch (err) {
      if (abort.signal.aborted) return;
      setConnection(false);
      // Reconnect with a fixed delay. A backoff ladder is not worth it for a
      // loopback connection whose failure mode is "the server restarted".
      setTimeout(connectActivityStream, 3000);
    }
  }

  function handleFrame(frame) {
    let eventName = "message";
    const dataLines = [];
    frame.split("\n").forEach((line) => {
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      // lines starting with ':' are heartbeat comments — ignored
    });
    if (eventName !== "activity" || !dataLines.length) return;
    try {
      pushActivity(JSON.parse(dataLines.join("\n")));
    } catch (_) { /* a malformed frame must not kill the stream */ }
  }

  function setConnection(ok) {
    el.connBadge.textContent = ok ? "live" : "offline";
    el.connBadge.className = "badge " + (ok ? "ok" : "bad");
  }

  /* ── tasks ──────────────────────────────────────────────────────────── */

  async function refreshTasks() {
    try {
      const data = await api("/tasks?limit=50");
      el.taskList.replaceChildren();

      const open = (data.counts.TODO || 0) + (data.counts.IN_PROGRESS || 0) +
        (data.counts.WAITING || 0) + (data.counts.BLOCKED || 0);
      el.taskCounts.textContent = open + " open · " + data.tasks.length + " total";

      if (!data.tasks.length) {
        const li = node("li", "dim", "No tasks yet.");
        el.taskList.appendChild(li);
        return;
      }
      data.tasks.forEach((task) => {
        const li = node("li");
        const row = node("div", "task-row");
        row.appendChild(node("span", "task-title", task.title));
        row.appendChild(node("span", "task-status st-" + task.status, task.status));
        li.appendChild(row);

        const bits = [task.priority];
        if (task.due_at) bits.push("due " + task.due_at.slice(0, 10));
        li.appendChild(node("div", "task-meta", bits.join(" · ")));
        el.taskList.appendChild(li);
      });
    } catch (_) { /* rail failures must not disturb the conversation */ }
  }

  el.refreshTasks.addEventListener("click", refreshTasks);

  /* ── tools ──────────────────────────────────────────────────────────── */

  async function refreshTools() {
    try {
      const data = await api("/tools");
      el.toolList.replaceChildren();
      data.tools.forEach((tool) => {
        const li = node("li", tool.enabled ? "" : "tool-off");
        const row = node("div", "tool-row");
        row.appendChild(node("span", "tool-name", tool.name));
        row.appendChild(
          node("span", "tool-cap cap-" + tool.capability, tool.capability)
        );
        li.appendChild(row);

        const bits = ["risk " + tool.risk_level];
        if (tool.requires_confirmation) bits.push("always asks");
        if (!tool.reversible) bits.push("irreversible");
        if (!tool.enabled) bits.push("disabled");
        li.appendChild(node("div", "tool-desc", bits.join(" · ")));
        li.appendChild(node("div", "tool-desc", tool.description));
        el.toolList.appendChild(li);
      });
    } catch (_) { /* ignore */ }
  }

  /* ── system ─────────────────────────────────────────────────────────── */

  async function refreshSystem() {
    try {
      const [status, permissions] = await Promise.all([
        api("/system/status"),
        api("/permissions"),
      ]);
      el.systemInfo.replaceChildren();

      el.systemInfo.appendChild(node("h4", null, "PROVIDERS"));
      status.providers.forEach((provider) => {
        const kv = node("div", "kv");
        kv.appendChild(node("span", null, provider.display_name));
        kv.appendChild(
          node("span", null, provider.configured ? "configured" : "no credentials")
        );
        el.systemInfo.appendChild(kv);
      });

      el.systemInfo.appendChild(node("h4", null, "MODELS BY TASK"));
      Object.entries(status.settings.models).forEach(([task, model]) => {
        const kv = node("div", "kv");
        kv.appendChild(node("span", null, task));
        kv.appendChild(node("span", null, model));
        el.systemInfo.appendChild(kv);
      });

      el.systemInfo.appendChild(node("h4", null, "PERMISSION DEFAULTS"));
      Object.entries(permissions.defaults).forEach(([capability, mode]) => {
        const kv = node("div", "kv");
        kv.appendChild(node("span", null, capability));
        kv.appendChild(node("span", null, mode));
        el.systemInfo.appendChild(kv);
      });

      el.systemInfo.appendChild(node("h4", null, "GRANTS"));
      permissions.grants.forEach((grant) => {
        const kv = node("div", "kv");
        kv.appendChild(node("span", null, grant.capability + " " + grant.resource_scope));
        kv.appendChild(node("span", null, grant.mode));
        el.systemInfo.appendChild(kv);
      });

      el.systemInfo.appendChild(node("h4", null, "NOT IMPLEMENTED"));
      [
        "Memory & semantic search (Phase 2)",
        "File access (Phase 3)",
        "Computer control (Phase 4)",
        "Browser control (Phase 5)",
        "Specialised agents (Phase 6)",
      ].forEach((item) => {
        el.systemInfo.appendChild(node("div", "dim", "· " + item));
      });
    } catch (_) { /* ignore */ }
  }

  /* ── memory ─────────────────────────────────────────────────────────── */

  /* Every field shown here comes from the API. Confidence and importance are
     rendered as bands rather than numbers because nobody reading their own
     memory wants to interpret 0.63, and provenance is shown whenever the
     record has it — "where did this come from" is the question that decides
     whether to trust a memory. */

  let memorySearchTimer = null;

  function memoryQuery() {
    const params = new URLSearchParams({ limit: "60" });
    const q = el.memorySearch.value.trim();
    if (q) params.set("q", q);
    if (el.memoryType.value) params.set("type", el.memoryType.value);
    return params.toString();
  }

  async function refreshMemory() {
    try {
      const data = await api("/memories?" + memoryQuery());
      const stats = await api("/memories/stats");
      el.memoryList.replaceChildren();

      el.memoryCounts.textContent =
        stats.total_active + " active · " + data.total + " shown";

      if (!el.memoryType.options.length || el.memoryType.options.length === 1) {
        Object.keys(stats.by_type).sort().forEach((type) => {
          const opt = document.createElement("option");
          opt.value = type;
          opt.textContent = type.replace(/_/g, " ").toLowerCase();
          el.memoryType.appendChild(opt);
        });
      }

      /* Say plainly when retrieval is lexical. A user comparing results
         against expectations deserves to know the search cannot match
         paraphrases rather than concluding the memory is missing. */
      if (!stats.semantic_search) {
        el.memoryNotice.textContent =
          "Search is lexical only — it matches wording, not meaning. Configure " +
          "an embedding endpoint (JARVIS_EMBEDDING_BASE_URL) for semantic recall.";
        el.memoryNotice.hidden = false;
      } else {
        el.memoryNotice.hidden = true;
      }

      if (!data.memories.length) {
        el.memoryList.appendChild(node("li", "dim", "Nothing remembered yet."));
        return;
      }
      data.memories.forEach((memory) => {
        el.memoryList.appendChild(memoryRow(memory));
      });
    } catch (_) { /* rail failures must not disturb the conversation */ }
  }

  function memoryRow(memory) {
    const li = node("li");
    li.addEventListener("click", () => openMemory(memory.id));

    const top = node("div", "mem-top");
    top.appendChild(node("span", "mem-text", memory.summary || memory.content));
    top.appendChild(node("span", "mem-kind", memory.type.replace(/_/g, " ")));
    li.appendChild(top);

    const meta = node("div", "mem-meta");
    meta.appendChild(node("span", "band " + memory.confidence_band,
      memory.confidence_band.toLowerCase()));
    meta.appendChild(node("span", "band " + memory.importance_band,
      memory.importance_band.toLowerCase()));
    if (memory.status !== "ACTIVE") {
      meta.appendChild(node("span", "st-" + memory.status, memory.status.toLowerCase()));
    }
    if (memory.pinned) meta.appendChild(node("span", "mem-flag", "pinned"));
    if (memory.tainted) meta.appendChild(node("span", "mem-flag", "external"));
    if (memory.provenance) meta.appendChild(node("span", null, memory.provenance));
    if (memory.updated_at) meta.appendChild(node("span", null, memory.updated_at.slice(0, 10)));
    li.appendChild(meta);

    /* Proposed memories are the confirmation prompt from §14, inline where
       the memory is rather than in a separate queue. */
    if (memory.status === "PROPOSED") {
      const actions = node("div", "mem-actions");
      const yes = node("button", "primary small", "Remember");
      const no = node("button", "ghost small", "Don't");
      yes.addEventListener("click", (e) => { e.stopPropagation(); confirmMemory(memory.id, true); });
      no.addEventListener("click", (e) => { e.stopPropagation(); confirmMemory(memory.id, false); });
      actions.appendChild(yes);
      actions.appendChild(no);
      li.appendChild(actions);
    }
    return li;
  }

  async function confirmMemory(id, approved) {
    try {
      await api("/memories/" + encodeURIComponent(id) + "/confirm", {
        method: "POST",
        body: { approved },
      });
      refreshMemory();
    } catch (err) {
      addMessage("assistant", err.message, { error: true });
    }
  }

  let openMemoryId = null;

  async function openMemory(id) {
    try {
      const memory = await api("/memories/" + encodeURIComponent(id));
      openMemoryId = id;
      el.memType.textContent = memory.type.replace(/_/g, " ");
      el.memContent.value = memory.content;

      el.memFacts.replaceChildren();
      const facts = [
        ["confidence", memory.confidence_band.toLowerCase()],
        ["importance", memory.importance_band.toLowerCase()],
        ["status", memory.status.toLowerCase()],
        ["source", (memory.source || "").toLowerCase()],
        ["subject", memory.subject || "—"],
        ["revision", String(memory.revision)],
        ["recalled", memory.access_count + "×"],
        ["created", (memory.created_at || "").slice(0, 10)],
      ];
      if (memory.provenance) facts.push(["from", memory.provenance]);
      facts.forEach(([label, value]) => {
        const span = node("span");
        span.appendChild(node("b", null, label + ": "));
        span.appendChild(document.createTextNode(value));
        el.memFacts.appendChild(span);
      });

      el.memHistory.replaceChildren();
      memory.history.forEach((entry) => {
        const when = (entry.at || "").slice(0, 16).replace("T", " ");
        el.memHistory.appendChild(
          node("li", null, when + "  " + entry.kind.toLowerCase() +
            " by " + entry.actor + (entry.note ? " — " + entry.note : ""))
        );
      });

      el.memoryModal.hidden = false;
    } catch (err) {
      addMessage("assistant", err.message, { error: true });
    }
  }

  function closeMemory() {
    el.memoryModal.hidden = true;
    openMemoryId = null;
  }

  el.memClose.addEventListener("click", closeMemory);

  el.memSave.addEventListener("click", async () => {
    if (!openMemoryId) return;
    try {
      await api("/memories/" + encodeURIComponent(openMemoryId), {
        method: "PATCH",
        body: { content: el.memContent.value.trim() },
      });
      closeMemory();
      refreshMemory();
    } catch (err) {
      addMessage("assistant", err.message, { error: true });
    }
  });

  el.memArchive.addEventListener("click", async () => {
    if (!openMemoryId) return;
    try {
      await api("/memories/" + encodeURIComponent(openMemoryId) + "/archive",
        { method: "POST" });
      closeMemory();
      refreshMemory();
    } catch (err) {
      addMessage("assistant", err.message, { error: true });
    }
  });

  el.memDelete.addEventListener("click", async () => {
    if (!openMemoryId) return;
    /* Confirmed in the browser because it is the one irreversible action the
       UI offers: archive is a click away and recoverable, this is not. */
    if (!window.confirm("Delete this memory permanently? Archiving is reversible; this is not.")) return;
    try {
      await api("/memories/" + encodeURIComponent(openMemoryId), { method: "DELETE" });
      closeMemory();
      refreshMemory();
    } catch (err) {
      addMessage("assistant", err.message, { error: true });
    }
  });

  el.memorySearch.addEventListener("input", () => {
    window.clearTimeout(memorySearchTimer);
    memorySearchTimer = window.setTimeout(refreshMemory, 220);
  });
  el.memoryType.addEventListener("change", refreshMemory);

  el.exportMemory.addEventListener("click", async () => {
    try {
      const archive = await api("/memories/export/archive");
      const blob = new Blob([JSON.stringify(archive, null, 2)],
        { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "jarvis-memory.json";
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      addMessage("assistant", err.message, { error: true });
    }
  });

  /* ── knowledge ──────────────────────────────────────────────────────── */

  async function refreshKnowledge() {
    try {
      const data = await api("/knowledge/documents?limit=60");
      const sources = await api("/knowledge/sources");

      el.documentList.replaceChildren();
      el.knowledgeCounts.textContent =
        data.stats.documents + " docs · " + data.stats.chunks + " chunks · " +
        data.stats.chunks_embedded + " embedded";

      if (!sources.roots.length) {
        el.knowledgeNotice.textContent =
          "No directories are approved for ingestion. Set JARVIS_KNOWLEDGE_ROOTS " +
          "to ingest from disk; uploads work regardless.";
        el.knowledgeNotice.hidden = false;
      } else {
        el.knowledgeNotice.hidden = true;
      }

      if (!data.documents.length) {
        el.documentList.appendChild(node("li", "dim", "No documents ingested."));
      }
      data.documents.forEach((doc) => {
        const li = node("li");
        const row = node("div", "src-row");
        row.appendChild(node("span", "doc-title", doc.title));
        row.appendChild(node("span", "doc-status ds-" + doc.status, doc.status));
        li.appendChild(row);

        const bits = [doc.chunk_count + " chunks"];
        if (doc.media_type) bits.push(doc.media_type.split("/").pop());
        if (doc.provenance) bits.push(doc.provenance);
        if (doc.error) bits.push(doc.error);
        li.appendChild(node("div", "doc-meta", bits.join(" · ")));
        el.documentList.appendChild(li);
      });

      /* Sources render from capability flags, not from hard-coded names.
         Obsidian shows as planned because the API says implemented:false —
         the UI is not asserting anything the backend has not. */
      el.sourceList.replaceChildren();
      sources.sources.forEach((source) => {
        const li = node("li");
        const row = node("div", "src-row");
        row.appendChild(node("span", "src-name", source.name));
        const state = source.implemented
          ? (source.connected ? "connected" : "not connected")
          : "planned";
        row.appendChild(node("span",
          "src-state " + (source.connected ? "on" : "off"), state));
        li.appendChild(row);
        li.appendChild(node("div", "src-detail", source.detail));
        el.sourceList.appendChild(li);
      });

      const unsupported = sources.formats.filter((f) => !f.available)
        .map((f) => f.key);
      const supported = sources.formats.filter((f) => f.available)
        .map((f) => f.key);
      const li = node("li");
      li.appendChild(node("div", "src-detail",
        "Readable: " + supported.join(", ") +
        (unsupported.length ? " · Not supported: " + unsupported.join(", ") : "")));
      el.sourceList.appendChild(li);
    } catch (_) { /* ignore */ }
  }

  el.uploadDoc.addEventListener("click", () => el.docFile.click());

  el.docFile.addEventListener("change", async () => {
    const file = el.docFile.files && el.docFile.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    try {
      /* Not through api(): FormData must set its own multipart boundary, so
         the JSON content-type that helper applies would corrupt the body. */
      const headers = {};
      if (state.token) headers.Authorization = "Bearer " + state.token;
      const response = await fetch("/api" + "/knowledge/ingest/upload", {
        method: "POST", headers, body: form,
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error((body.error && body.error.message) || body.detail ||
          "Ingestion failed");
      }
      addMessage("assistant",
        "Ingested " + file.name + " — " + body.chunks + " chunks, " +
        body.embedded + " embedded." +
        (body.warnings && body.warnings.length ? "\n" + body.warnings.join("\n") : ""));
      refreshKnowledge();
    } catch (err) {
      addMessage("assistant", err.message, { error: true });
    } finally {
      el.docFile.value = "";
    }
  });

  /* ── boot ───────────────────────────────────────────────────────────── */

  async function boot() {
    try {
      const status = await api("/system/status");
      const configured = status.providers.filter((p) => p.configured);
      if (configured.length) {
        el.providerBadge.textContent = configured[0].key + " · " +
          status.settings.models.conversation;
        el.providerBadge.className = "badge ok";
      } else {
        el.providerBadge.textContent = "no AI provider";
        el.providerBadge.className = "badge warn";
        addMessage(
          "assistant",
          "No AI provider is configured, so I cannot answer conversationally yet.\n\n" +
          "Set ANTHROPIC_API_KEY in your .env (or the OS keychain) and restart me. " +
          "Tasks, tools, permissions, and activity all work without it.",
          { error: true }
        );
      }

      if (status.embeddings && !status.embeddings.semantic) {
        /* Not an error, so not an error message — but the user should not
           have to discover from result quality that recall is lexical. */
        console.info("JARVIS: " + status.embeddings.description);
      }

      const activity = await api("/activity?limit=50");
      activity.activity.reverse().forEach(pushActivity);

      connectActivityStream();
      refreshTasks();
    } catch (err) {
      if (err.message !== "unauthorized") {
        setConnection(false);
        addMessage("assistant", err.message, { error: true });
      }
    }
  }

  boot();
})();
