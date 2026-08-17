/* The command centre.
 *
 * Five panels driven by one event stream, plus the mode banner and the stop.
 *
 * ## The rule this file exists to obey
 *
 * The front end is the console, not the authority. It displays state, requests
 * actions, receives events and requests approvals. It never decides. Concretely
 * that means:
 *
 * - Every action here is an authenticated REST call the backend authorises
 *   exactly as it would the same action taken autonomously. There is no path
 *   from a button to a tool handler that skips ToolExecutor.
 * - There is no control that spawns an agent, widens a ceiling, edits a grant,
 *   or clears taint. Those are not missing features; their absence is the
 *   design. Spawning goes through `spawn_agent`, which is a tool, and so meets
 *   the permission engine and the confirmation flow like anything else.
 * - Pause/resume/cancel *withdraw* authority rather than exercise it, which is
 *   why they are plain endpoints. Requiring an approval to stop work would be
 *   an approval-fatigue trap at precisely the wrong moment.
 *
 * ## Every insertion is textContent
 *
 * Event summaries are prose JARVIS wrote; detail values can carry page-authored
 * text — an element name, a page title. Both reach these panels. `node()` sets
 * textContent and there is no innerHTML in this file, which is asserted by a
 * test rather than left as an intention.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const root = $("consoleRoot");
  if (!root) return;

  /* Bounded, because this runs for as long as the tab is open. A console that
     accumulates every event since Tuesday is a memory leak with a scrollbar. */
  const MAX_ROWS = 200;

  const state = {
    events: [],
    approvals: new Map(),
    jobs: [],
    mode: "SAFE",
    stopped: false,
  };

  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }

  function clear(el) {
    while (el && el.firstChild) el.removeChild(el.firstChild);
  }

  /* ── mode ───────────────────────────────────────────────────────────────
     Impossible to miss, as asked. The banner carries the word as well as the
     colour: someone who cannot distinguish amber from red still has to be able
     to tell AUTONOMOUS from LOCKDOWN, and that is the single most consequential
     distinction on the page. */

  function paintMode(mode, stopped) {
    const banner = $("modeBanner");
    if (!banner) return;
    const shown = stopped ? "LOCKDOWN" : String(mode || "SAFE").toUpperCase();
    banner.textContent = stopped
      ? "LOCKDOWN — emergency stop engaged, nothing will run"
      : "MODE " + shown;
    banner.dataset.mode = shown;
  }

  /* ── the feed ───────────────────────────────────────────────────────────── */

  function addEvent(payload) {
    state.events.unshift(payload);
    if (state.events.length > MAX_ROWS) state.events.length = MAX_ROWS;

    const list = $("consoleFeed");
    if (!list) return;

    const row = node("li", "con-row" + (payload.loud ? " loud" : ""));
    row.appendChild(node("span", "con-kind", payload.event));
    row.appendChild(node("span", "con-summary", payload.summary));
    if (payload.status) {
      row.appendChild(node("span", "con-status ao-" + payload.status,
                           payload.status));
    }
    list.insertBefore(row, list.firstChild);
    while (list.children.length > MAX_ROWS) list.removeChild(list.lastChild);
  }

  /* ── approvals ──────────────────────────────────────────────────────────── */

  /* One place computes the numbers and one place draws them. A rendering bug
     in the reactor must not be able to take the panel's data with it, and a
     ring that disagrees with the number printed beside it would be worse than
     no ring at all. */
  function publishReading() {
    const set = (id, value) => {
      const el = $(id);
      if (el) el.textContent = String(value);
    };
    const reading = {
      approvals: state.approvals.size,
      jobs: state.jobs.filter((j) => j.state === "RUNNING").length,
      denials: state.denials || 0,
      mode: state.stopped ? "LOCKDOWN" : state.mode,
    };
    set("readApprovals", reading.approvals);
    set("readJobs", reading.jobs);
    set("readDenials", reading.denials);
    document.dispatchEvent(
      new CustomEvent("jarvis:reading", { detail: reading })
    );
  }

  function paintApprovals() {
    const list = $("approvalList");
    const count = $("approvalCount");
    if (!list) return;
    clear(list);
    if (count) count.textContent = String(state.approvals.size);
    publishReading();

    if (!state.approvals.size) {
      list.appendChild(node("li", "empty-state", "Nothing is waiting on you."));
      return;
    }
    for (const item of state.approvals.values()) {
      const row = node("li", "approval-row");
      row.appendChild(node("div", "approval-title", item.summary));
      const facts = node("div", "approval-facts");
      // Impact first: "can I take this back" is the question a person is
      // actually answering, and burying it under the tool name inverts that.
      if (item.detail.impact) {
        facts.appendChild(node("span", "impact impact-" + item.detail.impact,
                               item.detail.impact));
      }
      if (item.tool) facts.appendChild(node("span", "dim", item.tool));
      if (item.detail.reason) {
        facts.appendChild(node("span", "dim", item.detail.reason));
      }
      row.appendChild(facts);
      row.appendChild(node("div", "dim small",
                           "Decide in the approval dialog when it appears, or "
                           + "in the Confirmations view."));
      list.appendChild(row);
    }
  }

  /* ── jobs ───────────────────────────────────────────────────────────────── */

  async function refreshJobs() {
    if (!window.jarvisApi) return;
    try {
      const data = await window.jarvisApi("/agents");
      state.jobs = data.jobs || [];
    } catch (_) {
      return;              // unauthorised or offline; the badge already says so
    }
    paintJobs();
  }

  function paintJobs() {
    publishReading();
    const list = $("jobList");
    if (!list) return;
    clear(list);
    if (!state.jobs.length) {
      list.appendChild(node("li", "empty-state", "No background work."));
      return;
    }
    for (const job of state.jobs) {
      const row = node("li", "job-row");
      row.appendChild(node("span", "job-state st-" + job.state, job.state));
      row.appendChild(node("span", "job-title", job.title));
      row.appendChild(node("span", "dim small",
        "step " + job.progress.step + "/" + job.budget.max_steps
        + " · " + job.elapsed_seconds + "s"));
      if (job.tainted) row.appendChild(node("span", "mem-flag", "TAINTED"));

      const actions = node("div", "job-actions");
      for (const action of ["pause", "resume", "cancel"]) {
        const button = node("button", "ghost small", action);
        button.addEventListener("click", () => controlJob(job.job_id, action));
        actions.appendChild(button);
      }
      row.appendChild(actions);
      list.appendChild(row);
    }
  }

  async function controlJob(jobId, action) {
    if (!window.jarvisApi) return;
    try {
      await window.jarvisApi(
        "/agents/jobs/" + encodeURIComponent(jobId) + "/" + action,
        { method: "POST" }
      );
    } catch (_) { /* the feed will show the refusal */ }
    refreshJobs();
  }

  /* ── security ───────────────────────────────────────────────────────────── */

  async function refreshSecurity() {
    if (!window.jarvisApi) return;
    let data;
    try {
      data = await window.jarvisApi("/security");
    } catch (_) {
      return;
    }

    state.stopped = !!(data.emergency_stop && data.emergency_stop.engaged);
    state.denials = data.denied_last_24h || 0;
    paintMode(state.mode, state.stopped);
    publishReading();

    if (data.browser) paintBrowser(data.browser);

    const list = $("securityList");
    if (!list) return;
    clear(list);

    const rows = [
      ["Emergency stop", state.stopped ? "ENGAGED" : "clear"],
      ["Denied (24h)", String(data.denied_last_24h)],
      ["Running jobs", String(data.running_jobs)],
      ["Browser: localhost", data.browser.allow_localhost ? "allowed" : "refused"],
      ["Browser: private networks",
       data.browser.allow_private_networks ? "allowed" : "refused"],
      ["Browser pages open", String(data.browser.pages_open)],
      ["Desktop backend", data.computer.backend],
    ];
    for (const [label, value] of rows) {
      const row = node("li", "sec-row");
      row.appendChild(node("span", "sec-label", label));
      row.appendChild(node("span", "sec-value", value));
      list.appendChild(row);
    }

    // Grants are read-only here on purpose. Editing authorisation from the
    // console would make the console the authority, which is the one thing
    // this design says it is not.
    const grants = $("grantList");
    if (grants) {
      clear(grants);
      for (const grant of data.grants) {
        const row = node("li", "sec-row");
        row.appendChild(node("span", "sec-label",
                             grant.capability + " " + grant.scope));
        row.appendChild(node("span", "sec-value cap-" + grant.mode, grant.mode));
        grants.appendChild(row);
      }
      if (!data.grants.length) {
        grants.appendChild(node("li", "empty-state", "No explicit grants."));
      }
    }
  }

  /* ── browser ────────────────────────────────────────────────────────────
     Read-only, and the panel says so. "Take control" is deliberately not a
     button here: handing a human the keyboard on a page JARVIS opened means
     the human's next click happens in a context the policy engine authorised
     for JARVIS, and there is no way for the backend to tell the two apart
     afterwards. Closing the page is the honest version of that, and it is a
     tool call. */

  function paintBrowser(browser) {
    const list = $("browserList");
    if (!list) return;
    clear(list);

    const rows = [
      ["Running", browser.running ? "yes" : "not started"],
      ["Pages open", String(browser.pages_open)],
      ["Reaches localhost", browser.allow_localhost ? "allowed" : "refused"],
      ["Reaches private networks",
       browser.allow_private_networks ? "allowed" : "refused"],
      ["Window", browser.headless ? "hidden" : "visible"],
      ["Storage", browser.persists_storage ? "persists" : "discarded on exit"],
    ];
    for (const [label, value] of rows) {
      const row = node("li", "sec-row");
      row.appendChild(node("span", "sec-label", label));
      row.appendChild(node("span", "sec-value", value));
      list.appendChild(row);
    }
    for (const page of browser.pages || []) {
      const row = node("li", "sec-row");
      row.appendChild(node("span", "sec-label", page.page_id));
      // Page-authored. textContent, like everything else here.
      row.appendChild(node("span", "sec-value", page.url));
      list.appendChild(row);
    }
  }

  /* ── computer ───────────────────────────────────────────────────────────
     Observation is pull-based, because §35 says do not continuously record the
     screen. A live screenshot feed would be a recording, and a recording of
     somebody's desktop is the thing this system most has to not become.

     The request goes through /computer/observe, which runs the action through
     the policy engine like any other — so a machine with no display, or a user
     without the SCREEN scope, gets a refusal rather than a blank panel. */

  async function observe() {
    const status = $("computerStatus");
    const image = $("computerShot");
    const windows = $("windowList");
    if (!window.jarvisApi || !status) return;

    status.textContent = "observing…";
    let data;
    try {
      data = await window.jarvisApi("/computer/observe?include_image=true");
    } catch (error) {
      // Named, because "observation failed" does not tell anyone whether the
      // machine has no display or they lack the permission.
      status.textContent = error.message || "observation refused";
      return;
    }

    status.textContent = data.active_window
      ? "active: " + (data.active_window.title || data.active_window.application)
      : "no active window";

    if (image) {
      if (data.screenshot_id) {
        image.hidden = false;
        image.src = "/api/computer/screenshot/" + encodeURIComponent(data.screenshot_id);
      } else {
        image.hidden = true;
      }
    }

    if (windows) {
      clear(windows);
      // The structured half, which the brief asks to complement the picture
      // rather than be replaced by it: a named window survives the desktop
      // moving, and a screenshot does not.
      for (const win of data.windows || []) {
        const row = node("li", "sec-row");
        row.appendChild(node("span", "sec-label", win.title || "(untitled)"));
        row.appendChild(node("span", "sec-value", win.application || ""));
        windows.appendChild(row);
      }
      if (!(data.windows || []).length) {
        windows.appendChild(node("li", "empty-state", "No windows reported."));
      }
    }
  }

  const observeBtn = $("console_observeBtn");
  if (observeBtn) observeBtn.addEventListener("click", observe);

  /* ── memory ─────────────────────────────────────────────────────────────
     Three states, and the distinction is the whole panel:

       PROPOSED  JARVIS inferred it and is asking. Not in play yet.
       TAINTED   it came from, or was extracted on a turn that read, something
                 JARVIS did not author — a web page, a document, a note.
       TRUSTED   everything else.

     There is deliberately no control that turns a tainted memory into a
     trusted one. The backend refuses it too — MemoryService.update() drops a
     falsy `tainted` and logs, and the API's update schema has no such field at
     all — so this is the third of three layers rather than the only one. Taint
     is a fact about where a claim came from, and approving the claim does not
     change where it came from. */

  function trustOf(memory) {
    if (memory.status === "PROPOSED") return "proposed";
    return memory.tainted ? "tainted" : "trusted";
  }

  async function refreshMemory() {
    if (!window.jarvisApi) return;
    let data;
    try {
      data = await window.jarvisApi("/memories?limit=25");
    } catch (_) {
      return;
    }
    paintMemory(data.memories || []);
  }

  function paintMemory(memories) {
    const list = $("trustList");
    if (!list) return;
    clear(list);

    if (!memories.length) {
      list.appendChild(node("li", "empty-state", "Nothing remembered yet."));
      return;
    }

    // Proposed first, then tainted, then trusted. Ordering by what needs a
    // decision rather than by recency: the panel exists to be acted on.
    const rank = { proposed: 0, tainted: 1, trusted: 2 };
    const sorted = memories.slice().sort(
      (a, b) => rank[trustOf(a)] - rank[trustOf(b)]
    );

    for (const memory of sorted) {
      const trust = trustOf(memory);
      const row = node("li", "mem-trust-row trust-" + trust);
      row.appendChild(node("span", "trust-tag", trust));
      row.appendChild(node("span", "mem-text", memory.content));

      const facts = node("div", "mem-facts");
      facts.appendChild(node("span", "dim", memory.type));
      facts.appendChild(node("span", "dim", "source " + memory.source));
      facts.appendChild(node("span", "dim",
                             "confidence " + memory.confidence_band));
      // Provenance, so "why is this distrusted?" is answerable here rather
      // than by replaying the turn it came from.
      if (memory.meta && memory.meta.tainted_request_id) {
        facts.appendChild(node("span", "dim",
                               "from request " + memory.meta.tainted_request_id));
      }
      if (memory.provenance && memory.provenance.label) {
        facts.appendChild(node("span", "dim", memory.provenance.label));
      }
      row.appendChild(facts);

      const actions = node("div", "mem-actions");
      if (memory.status === "PROPOSED") {
        actions.appendChild(memoryButton("keep", memory.id, "confirm"));
        actions.appendChild(memoryButton("reject", memory.id, "archive"));
      } else {
        actions.appendChild(memoryButton("forget", memory.id, "archive"));
      }
      row.appendChild(actions);
      list.appendChild(row);
    }
  }

  function memoryButton(label, memoryId, action) {
    const button = node("button", "ghost small", label);
    button.addEventListener("click", async () => {
      if (!window.jarvisApi) return;
      try {
        await window.jarvisApi(
          "/memories/" + encodeURIComponent(memoryId) + "/" + action,
          { method: "POST" }
        );
      } catch (_) { /* the feed will show the refusal */ }
      refreshMemory();
    });
    return button;
  }

  /* ── the stream ─────────────────────────────────────────────────────────── */

  function onEvent(payload) {
    addEvent(payload);

    if (payload.event === "approval.required") {
      state.approvals.set(payload.detail.confirmation_id || payload.request_id,
                          payload);
      paintApprovals();
    } else if (payload.event === "approval.granted"
               || payload.event === "approval.rejected") {
      state.approvals.delete(payload.detail.confirmation_id
                             || payload.request_id);
      paintApprovals();
    } else if (payload.event === "emergency_stop") {
      refreshSecurity();
    } else if (payload.event.indexOf("task.") === 0) {
      refreshJobs();
    } else if (payload.event.indexOf("memory.") === 0) {
      refreshMemory();
    }
  }

  // app.js owns the authenticated stream reader and exposes this hook. Sharing
  // its reader rather than opening a second one keeps the token in exactly one
  // place, and means a re-auth fixes both panels at once.
  document.addEventListener("jarvis:console", (event) => {
    if (event.detail) onEvent(event.detail);
  });

  document.addEventListener("jarvis:authenticated", () => {
    refreshJobs();
    refreshSecurity();
    refreshMemory();
  });

  paintMode("SAFE", false);
  paintApprovals();
})();
