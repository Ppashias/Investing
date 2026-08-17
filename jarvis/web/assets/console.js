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

  function paintApprovals() {
    const list = $("approvalList");
    const count = $("approvalCount");
    if (!list) return;
    clear(list);
    if (count) count.textContent = String(state.approvals.size);

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
    paintMode(state.mode, state.stopped);

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
  });

  paintMode("SAFE", false);
  paintApprovals();
})();
