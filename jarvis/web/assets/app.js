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
    obsState: $("obsState"),
    obsFacts: $("obsFacts"),
    obsDetail: $("obsDetail"),
    obsCapabilities: $("obsCapabilities"),
    obsConnectForm: $("obsConnectForm"),
    obsVaultPath: $("obsVaultPath"),
    obsAllowWrites: $("obsAllowWrites"),
    obsAllowDeletes: $("obsAllowDeletes"),
    obsConnect: $("obsConnect"),
    obsTest: $("obsTest"),
    obsSync: $("obsSync"),
    obsDisconnect: $("obsDisconnect"),
    obsNotice: $("obsNotice"),
    obsConflicts: $("obsConflicts"),
    obsConflictList: $("obsConflictList"),
    obsAudit: $("obsAudit"),
    computerState: $("computerState"),
    refreshComputer: $("refreshComputer"),
    stopBtn: $("stopBtn"),
    stopState: $("stopState"),
    computerNotice: $("computerNotice"),
    computerMode: $("computerMode"),
    scopeList: $("scopeList"),
    computerCurrent: $("computerCurrent"),
    observeBtn: $("observeBtn"),
    screenPreview: $("screenPreview"),
    auditList: $("auditList"),
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

  /* The console panels call the API through this rather than holding the
     token themselves. One holder means one place re-authentication has to
     reach, and no second copy to leave in a closure somewhere. */
  window.jarvisApi = api;

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

  /* Announce an assistant turn for anything that wants to react to it —
     today, the voice module reading it aloud.

     A CustomEvent rather than a direct call keeps the two files independent:
     this one does not know whether voice.js loaded, and voice.js failing
     cannot stop a reply from rendering. It also means the reply is on screen
     before anything is spoken, which is the right order — the screen is the
     record and the speech is a convenience. */
  function announce(role, text) {
    if (role !== "assistant" || !text) return;
    document.dispatchEvent(
      new CustomEvent("jarvis:reply", { detail: { text: text } })
    );
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
    // After the DOM insertion, so the reply is on screen before anything is
    // spoken. The screen is the record; the speech is a convenience.
    announce(role, text);
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
        body: { approved: approved, channel: "ui" },
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
      if (wanted === "computer") refreshComputer();
      if (wanted === "console") {
        document.dispatchEvent(new CustomEvent("jarvis:authenticated"));
      }
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
  /* One reader, parameterised. The activity feed and the command centre want
     different vocabularies off the same bus — raw records for the audit view,
     the closed console vocabulary for the panels — and two hand-written
     readers would drift in exactly the way SSE frame parsing drifts: quietly,
     in the buffering. */
  async function openStream(path, wanted, onPayload) {
    const abort = new AbortController();
    state.streamAborts = state.streamAborts || [];
    state.streamAborts.push(abort);

    const headers = { Accept: "text/event-stream" };
    if (state.token) headers.Authorization = "Bearer " + state.token;

    try {
      const response = await fetch("/api" + path, {
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
          handleFrame(frame, wanted, onPayload);
        }
      }
      setConnection(false);
    } catch (err) {
      if (abort.signal.aborted) return;
      setConnection(false);
      // Reconnect with a fixed delay. A backoff ladder is not worth it for a
      // loopback connection whose failure mode is "the server restarted".
      setTimeout(() => openStream(path, wanted, onPayload), 3000);
    }
  }

  function handleFrame(frame, wanted, onPayload) {
    let eventName = "message";
    const dataLines = [];
    frame.split("\n").forEach((line) => {
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      // lines starting with ':' are heartbeat comments — ignored
    });
    if (eventName !== wanted || !dataLines.length) return;
    try {
      onPayload(JSON.parse(dataLines.join("\n")));
    } catch (_) { /* a malformed frame must not kill the stream */ }
  }

  /* Two streams off one bus: the audit feed keeps the raw records, the console
     panels get the filtered vocabulary the server defines. Classifying on the
     client instead would put a second copy of the event schema in JavaScript,
     and the copy that drifts is always the one nobody tests. */
  function startStreams() {
    for (const abort of state.streamAborts || []) abort.abort();
    state.streamAborts = [];
    openStream("/activity/stream", "activity", pushActivity);
    openStream("/activity/console", "console", (payload) => {
      document.dispatchEvent(
        new CustomEvent("jarvis:console", { detail: payload })
      );
    });
    document.dispatchEvent(new CustomEvent("jarvis:authenticated"));
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

  /* One dropdown per role. Worth being clear about why this control is
     allowed to exist, given the rule that the frontend never widens what
     JARVIS may do: a model id chooses *who does the thinking*, not what it is
     permitted to do with the answer. Every tool call the new model makes still
     goes through ToolExecutor and the permission engine, and is refused in
     exactly the same cases. There is deliberately no control here for a
     provider, a base url, a key or a capability — the PATCH schema cannot even
     express one.

     The options come from the server's list of models a configured provider
     can actually call, never from a list written here. A dropdown offering
     something that fails on selection is worse than a short dropdown. */
  function modelPicker(role, models) {
    const row = node("div", "kv model-picker");
    row.appendChild(node("span", null, role.role));

    const select = document.createElement("select");
    select.className = "model-select";
    select.id = "model_" + role.role;
    // Named for a screen reader, since the visible label is a sibling span
    // rather than a <label for>.
    select.setAttribute("aria-label", "Model for " + role.role);

    const fallback = models.defaults[role.role] || "";
    const asDefault = document.createElement("option");
    asDefault.value = "";
    asDefault.textContent = "default (" + fallback + ")";
    select.appendChild(asDefault);

    models.available.forEach((model) => {
      const option = document.createElement("option");
      option.value = model.id;
      // Price is on the label because the expensive choice sits on the
      // reasoning path, which every turn touches. Finding that out from a bill
      // is a poor way to find it out.
      let label = model.id;
      if (model.runs_locally) label += "  · local";
      else if (model.input_price_per_mtok) {
        label += "  · $" + model.input_price_per_mtok + "/Mtok";
      }
      option.textContent = label;
      select.appendChild(option);
    });

    select.value = role.source === "preference" ? role.model : "";

    select.addEventListener("change", async () => {
      const wanted = select.value || null;
      select.disabled = true;
      try {
        await api("/system/models", {
          method: "PATCH",
          body: { role: role.role, model: wanted },
        });
      } catch (err) {
        // Re-read rather than trusting the optimistic value: a refused change
        // must not leave the dropdown showing a model that is not in use.
        pushActivity({
          kind: "ERROR",
          summary: "Could not change the model: " + err.message,
          created_at: new Date().toISOString(),
        });
      }
      select.disabled = false;
      refreshSystem();
    });

    row.appendChild(select);
    return row;
  }

  async function refreshSystem() {
    try {
      const [status, permissions, models] = await Promise.all([
        api("/system/status"),
        api("/permissions"),
        api("/system/models"),
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
      models.roles.forEach((role) => {
        el.systemInfo.appendChild(modelPicker(role, models));
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

    await refreshObsidian();
  }

  /* ── Obsidian ───────────────────────────────────────────────────────────
     Every value rendered here comes from /api/obsidian/status, which walks
     the vault on each request. Nothing below caches "connected" — a vault
     that has gone away reads ERROR on the next refresh, which is the point
     of the panel. */

  async function refreshObsidian() {
    let status;
    try {
      status = await api("/obsidian/status");
    } catch (_) {
      return;
    }

    el.obsState.textContent = status.state;
    el.obsState.className =
      "src-state " + (status.connected ? "on" : "off");
    el.obsDetail.textContent = status.detail || "";

    const config = status.config || {};
    el.obsFacts.replaceChildren();
    const facts = [
      ["Vault", (status.vault && status.vault.name) || "—"],
      ["Path", config.vault_path || "—"],
      ["Connection", config.connection_type || "—"],
      ["Last connection", stamp(config.last_connected_at)],
      ["Last sync", stamp(config.last_synced_at)],
      ["Indexed notes", String(config.indexed_notes ?? 0)],
      ["Writes", config.allow_writes ? "allowed" : "off"],
      ["Deletes", config.allow_deletes ? "allowed" : "off"],
    ];
    if (config.last_error) facts.push(["Last error", config.last_error]);
    facts.forEach(([label, value]) => {
      const row = node("div", "sys-row");
      row.appendChild(node("span", "sys-key", label));
      row.appendChild(node("span", "sys-val", value));
      el.obsFacts.appendChild(row);
    });

    /* Capabilities are rendered from what the API reports, not from a fixed
       list. A read-only vault genuinely does not offer CREATE, and showing a
       greyed-out button for it would be claiming a capability that is not
       there. */
    el.obsCapabilities.replaceChildren();
    (status.capabilities || []).forEach((capability) => {
      el.obsCapabilities.appendChild(node("span", "obs-cap", capability));
    });
    if (!status.capabilities || !status.capabilities.length) {
      el.obsCapabilities.appendChild(
        node("span", "dim", "No capabilities — nothing is connected.")
      );
    }

    el.obsConnectForm.hidden = status.connected;
    el.obsConnect.hidden = status.connected;
    el.obsTest.hidden = !status.configured;
    el.obsSync.hidden = !status.connected;
    el.obsDisconnect.hidden = !status.configured;

    if (status.connected) {
      await refreshObsidianConflicts();
      await refreshObsidianAudit();
    } else {
      el.obsConflicts.hidden = true;
      el.obsAudit.replaceChildren();
    }
  }

  function stamp(value) {
    return value ? new Date(value).toLocaleString() : "never";
  }

  async function refreshObsidianConflicts() {
    try {
      const body = await api("/obsidian/conflicts");
      el.obsConflicts.hidden = !body.count;
      el.obsConflictList.replaceChildren();
      body.conflicts.forEach((conflict) => {
        const li = node("li");
        li.appendChild(node("div", "obs-conflict-path", conflict.note_path));
        const actions = node("div", "obs-actions");
        [
          ["Keep Obsidian", "keep_obsidian"],
          ["Keep JARVIS", "keep_jarvis"],
          ["Merge", "merge"],
          ["Cancel", "cancel"],
        ].forEach(([label, resolution]) => {
          const button = node("button", "ghost small", label);
          button.addEventListener("click", async () => {
            try {
              const result = await api(
                "/obsidian/conflicts/resolve?path=" +
                  encodeURIComponent(conflict.note_path),
                { method: "POST", body: { resolution: resolution } }
              );
              notice(el.obsNotice, result.detail || result.message);
              await refreshObsidian();
            } catch (err) {
              notice(el.obsNotice, err.message);
            }
          });
          actions.appendChild(button);
        });
        li.appendChild(actions);
        el.obsConflictList.appendChild(li);
      });
    } catch (_) { /* ignore */ }
  }

  async function refreshObsidianAudit() {
    try {
      const body = await api("/obsidian/audit?limit=15");
      el.obsAudit.replaceChildren();
      if (!body.entries.length) {
        el.obsAudit.appendChild(node("li", "dim", "Nothing yet."));
      }
      body.entries.forEach((entry) => {
        const li = node("li");
        const row = node("div", "src-row");
        row.appendChild(node("span", "audit-kind", entry.operation || "—"));
        row.appendChild(node("span", "audit-outcome ao-" + entry.status,
          entry.status));
        li.appendChild(row);
        li.appendChild(node("div", "audit-detail", entry.summary));
        el.obsAudit.appendChild(li);
      });
    } catch (_) { /* ignore */ }
  }

  function notice(element, message) {
    element.textContent = message || "";
    element.hidden = !message;
  }

  el.obsConnect.addEventListener("click", async () => {
    const path = el.obsVaultPath.value.trim();
    if (!path) {
      notice(el.obsNotice, "Enter the folder your vault lives in.");
      return;
    }
    try {
      const result = await api("/obsidian/connect", {
        method: "POST",
        body: {
          vault_path: path,
          allow_writes: el.obsAllowWrites.checked,
          allow_deletes: el.obsAllowDeletes.checked,
        },
      });
      notice(el.obsNotice,
        "Connected to " + result.vault + " — " + result.notes + " notes found.");
      await refreshObsidian();
    } catch (err) {
      notice(el.obsNotice, err.message);
    }
  });

  el.obsTest.addEventListener("click", async () => {
    try {
      const result = await api("/obsidian/test", { method: "POST" });
      notice(el.obsNotice, result.connected
        ? "Reachable — " + result.notes + " notes."
        : result.detail);
      await refreshObsidian();
    } catch (err) {
      notice(el.obsNotice, err.message);
    }
  });

  el.obsSync.addEventListener("click", async () => {
    notice(el.obsNotice, "Syncing…");
    try {
      const result = await api("/obsidian/sync", { method: "POST" });
      notice(el.obsNotice,
        result.indexed + " new, " + result.updated + " updated, " +
        result.removed + " removed, " + result.skipped + " unchanged" +
        (result.conflicts.length
          ? ", " + result.conflicts.length + " conflict(s) to resolve"
          : "."));
      await refreshKnowledge();
    } catch (err) {
      notice(el.obsNotice, err.message);
    }
  });

  el.obsDisconnect.addEventListener("click", async () => {
    try {
      await api("/obsidian/disconnect", { method: "POST" });
      /* Deliberately not offering "and delete everything I learned" as a
         one-click action here: disconnecting a source and destroying the
         index are different intentions. The API supports it explicitly. */
      notice(el.obsNotice,
        "Disconnected. Your vault is untouched and indexed notes are kept.");
      await refreshObsidian();
    } catch (err) {
      notice(el.obsNotice, err.message);
    }
  });

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

  /* ── computer ───────────────────────────────────────────────────────── */

  /* Everything here renders from /api/computer/status. The panel asserts
     nothing on its own: if the backend says a capability is unavailable, the
     reason shown is the backend's, so the UI cannot drift into claiming
     something works when it does not. */

  let computerStatus = null;

  async function refreshComputer() {
    try {
      const status = await api("/computer/status");
      computerStatus = status;
      renderStopState(status.emergency_stop);
      renderComputerState(status);
      renderScopes(status);
      renderCurrent(status);
      await refreshAudit();
    } catch (_) { /* rail failures must not disturb the conversation */ }
  }

  function renderStopState(stop) {
    el.stopBtn.textContent = stop.engaged ? "STOPPED — RESUME" : "STOP JARVIS";
    el.stopBtn.classList.toggle("engaged", stop.engaged);
    if (stop.engaged) {
      el.stopState.textContent =
        stop.reason + " · " + stop.blocked_count + " action(s) blocked since.";
      el.stopState.hidden = false;
    } else {
      el.stopState.hidden = true;
    }
  }

  function renderComputerState(status) {
    const display = status.capabilities.display;
    el.computerState.textContent = status.connected
      ? display.kind + " " + display.width + "x" + display.height
      : "not connected";

    /* The notes come from capability detection, so the panel says exactly
       what the backend found — including that a display is virtual. */
    const notes = status.capabilities.notes || [];
    if (!status.connected || notes.length) {
      el.computerNotice.textContent = status.connected
        ? notes.join(" ")
        : "No display is available. " + notes.join(" ");
      el.computerNotice.hidden = false;
    } else {
      el.computerNotice.hidden = true;
    }
    el.computerMode.value = status.policy.mode;
  }

  function renderScopes(status) {
    const enabled = new Set(status.policy.enabled_scopes);
    const auto = new Set(status.policy.auto_scopes);
    const forbidden = new Set(status.policy.forbidden_scopes);
    const all = status.policy.available_scopes.concat(status.policy.forbidden_scopes);

    el.scopeList.replaceChildren();
    all.sort().forEach((scope) => {
      const li = node("li");
      if (forbidden.has(scope)) li.className = "forbidden";
      li.appendChild(node("span", "scope-name", scope.toLowerCase()));

      ["enabled", "auto"].forEach((which) => {
        const label = node("label");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.checked = which === "enabled" ? enabled.has(scope) : auto.has(scope);
        box.disabled = forbidden.has(scope);
        box.addEventListener("change", () => updateScope(scope, which, box.checked));
        label.appendChild(box);
        label.appendChild(document.createTextNode(which));
        li.appendChild(label);
      });
      el.scopeList.appendChild(li);
    });
  }

  async function updateScope(scope, which, on) {
    if (!computerStatus) return;
    const enabled = new Set(computerStatus.policy.enabled_scopes);
    const auto = new Set(computerStatus.policy.auto_scopes);
    const target = which === "enabled" ? enabled : auto;
    if (on) target.add(scope); else target.delete(scope);
    /* Mirrors the server rule rather than relying on the round trip, so the
       checkbox does not flicker into an impossible state. */
    if (which === "enabled" && !on) auto.delete(scope);

    try {
      await api("/computer/permissions", {
        method: "PATCH",
        body: { enabled_scopes: [...enabled], auto_scopes: [...auto] },
      });
      refreshComputer();
    } catch (err) {
      addMessage("assistant", err.message, { error: true });
      refreshComputer();
    }
  }

  el.computerMode.addEventListener("change", async () => {
    try {
      await api("/computer/permissions", {
        method: "PATCH", body: { mode: el.computerMode.value },
      });
      refreshComputer();
    } catch (err) {
      addMessage("assistant", err.message, { error: true });
    }
  });

  function renderCurrent(status) {
    el.computerCurrent.replaceChildren();
    const rows = [
      ["backend", status.backend],
      ["application", status.current_application || "—"],
      ["window", status.active_window ? status.active_window.title : "—"],
      ["task", status.active_task ? status.active_task.description : "—"],
      ["step", status.active_task ? (status.active_task.current_step || "—") : "—"],
      ["planner", status.reasoner_available ? "available" : "no model provider"],
      ["file roots", String(status.filesystem.allowed_paths.length)],
      ["screenshots held", String(status.screenshots.held)],
    ];
    rows.forEach(([key, value]) => {
      const div = node("div", "kv");
      div.appendChild(node("span", null, key));
      div.appendChild(node("span", null, value));
      el.computerCurrent.appendChild(div);
    });
  }

  async function refreshAudit() {
    try {
      const data = await api("/computer/audit?limit=40");
      el.auditList.replaceChildren();
      if (!data.entries.length) {
        el.auditList.appendChild(node("li", "dim", "Nothing yet."));
        return;
      }
      data.entries.forEach((entry) => {
        const li = node("li");
        const top = node("div", "audit-top");
        top.appendChild(node("span", "audit-kind", entry.kind));
        top.appendChild(
          node("span", "audit-outcome ao-" + entry.outcome, entry.outcome)
        );
        li.appendChild(top);

        const bits = [];
        bits.push((entry.at || "").slice(11, 19));
        bits.push(entry.actor);
        if (entry.reason) bits.push(entry.reason);
        const meta = node("div", "audit-meta");
        meta.appendChild(node("span", "risk-" + entry.risk, entry.risk + " "));
        meta.appendChild(document.createTextNode(bits.join(" · ")));
        li.appendChild(meta);

        if (entry.verification && entry.verification !== "UNVERIFIED") {
          li.appendChild(node("div", "audit-meta", "verify: " + entry.verification));
        }
        el.auditList.appendChild(li);
      });
    } catch (_) { /* ignore */ }
  }

  el.stopBtn.addEventListener("click", async () => {
    const engaged = computerStatus && computerStatus.emergency_stop.engaged;
    try {
      /* Not through api(): the stop must not queue behind anything, and the
         helper's error handling could swallow it. Direct fetch, minimal
         path, no dependence on prior state. */
      const headers = { "Content-Type": "application/json" };
      if (state.token) headers.Authorization = "Bearer " + state.token;
      const response = await fetch("/api/computer/" + (engaged ? "resume" : "stop"), {
        method: "POST",
        headers,
        body: engaged ? null : JSON.stringify({ reason: "User pressed STOP" }),
      });
      const body = await response.json();
      renderStopState(body);
      addMessage(
        "assistant",
        engaged
          ? "Emergency stop released. Computer actions can run again."
          : "Emergency stop engaged. No computer actions will run until you release it.",
        { error: !engaged }
      );
      refreshComputer();
    } catch (err) {
      addMessage("assistant", "Could not reach the stop endpoint: " + err.message,
                 { error: true });
    }
  });

  el.refreshComputer.addEventListener("click", refreshComputer);

  el.observeBtn.addEventListener("click", async () => {
    try {
      const data = await api("/computer/observe?include_image=true");
      const image = data.image;
      if (image && image.base64) {
        el.screenPreview.src = "data:image/png;base64," + image.base64;
        el.screenPreview.hidden = false;
      } else {
        el.screenPreview.hidden = true;
        addMessage("assistant", "Observed — the screen is unchanged since last time.");
      }
      refreshComputer();
    } catch (err) {
      addMessage("assistant", err.message, { error: true });
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

      if (status.computer && status.computer.emergency_stop) {
        addMessage(
          "assistant",
          "The emergency stop is engaged from a previous session. No computer "
          + "actions will run until you release it in the Computer tab.",
          { error: true }
        );
      }

      const activity = await api("/activity?limit=50");
      activity.activity.reverse().forEach(pushActivity);

      startStreams();
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
