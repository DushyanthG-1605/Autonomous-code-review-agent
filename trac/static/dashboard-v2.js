(() => {
  "use strict";

  const TOKEN_KEY = "trac-project-credentials";
  const THEME_KEY = "trac-theme";
  const state = {
    tokens: {},
    projects: [],
    activeProject: "",
    devices: {},
    tasks: [],
    events: [],
    page: "overview",
    currentDevice: null,
    streamControllers: new Map(),
    streamEvents: new Set(),
    pairingTimer: null
  };
  const pageInfo = {
    overview: ["Overview", "ENGINEERING INTELLIGENCE", "A live view of project progress, meaningful activity, and attention items."],
    activity: ["Live Activity", "REAL-TIME PROJECT SIGNAL", "Authenticated engineering events, delivered from the TRAC event stream."],
    projects: ["Projects", "PROJECT PORTFOLIO", "Project activity and work visible to your connected credentials."],
    tasks: ["Tasks", "WORK TRACKING", "Task status and reported progress, separate from verified engineering evidence."],
    integrate: ["Integrate Skill", "UNIVERSAL INTEGRATION", "Use one consent-based workflow with compatible AI coding environments."],
    connections: ["Connections", "AUTHORIZED ACCESS", "Devices and integrations authorized for the connected project."],
    settings: ["Settings", "PREFERENCES", "Profile, appearance, privacy, and browser-session access."]
  };
  const taskStatuses = [
    ["todo", "To Do"], ["in_progress", "In Progress"], ["blocked", "Blocked"],
    ["in_review", "In Review"], ["done", "Done"]
  ];
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[char]);
  const projectToken = projectId => state.tokens[String(projectId)];
  const selectedProject = () => state.projects.find(project => project.id === state.activeProject);

  function readTokens() {
    try {
      const parsed = JSON.parse(sessionStorage.getItem(TOKEN_KEY) || "{}");
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch {
      sessionStorage.removeItem(TOKEN_KEY);
      return {};
    }
  }

  function saveTokens() {
    sessionStorage.setItem(TOKEN_KEY, JSON.stringify(state.tokens));
  }

  async function api(path, token, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        Accept: "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {})
      }
    });
    if (!response.ok) {
      let detail = `Request failed (${response.status})`;
      try {
        const body = await response.json();
        detail = body.detail || detail;
      } catch {}
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    return response;
  }

  function setTheme(theme, persist = false) {
    const preference = ["light", "dark", "system"].includes(theme) ? theme : "system";
    document.documentElement.dataset.theme = preference === "system"
      ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
      : preference;
    document.documentElement.dataset.themePreference = preference;
    $("#brandLogo").src = document.documentElement.dataset.theme === "dark"
      ? "/static/trac-logo.svg"
      : "/static/trac-logo-light.svg";
    $("#themeSelect").value = preference;
    if (persist) localStorage.setItem(THEME_KEY, preference);
  }

  function showGate(message = "") {
    $("#credentialGate").classList.remove("hidden");
    $("#dashboard").classList.add("hidden");
    $("#gateError").textContent = message;
    $("#gateError").classList.toggle("hidden", !message);
  }

  function showDashboard() {
    $("#credentialGate").classList.add("hidden");
    $("#dashboard").classList.remove("hidden");
  }

  function updateConnectionUI(connected, message) {
    const pill = $("#connectionPill");
    pill.classList.toggle("connected", connected);
    pill.querySelector("span").textContent = message;
    $("#streamStatus").textContent = message;
    $(".live-dot").classList.toggle("connected", connected);
  }

  function setError(message = "") {
    const node = $("#pageError");
    node.textContent = message;
    node.classList.toggle("hidden", !message);
  }

  function projectName(projectId) {
    return state.projects.find(project => project.id === projectId)?.name || "Project";
  }

  async function addCredential(token, { persist = true } = {}) {
    const cleanToken = token.trim();
    if (!cleanToken) throw new Error("Enter a project credential.");
    const response = await api("/v1/me/device", cleanToken);
    const device = await response.json();
    const projectResponse = await api(`/v1/projects/${encodeURIComponent(device.project_id)}`, cleanToken);
    const project = await projectResponse.json();
    state.tokens[String(project.id)] = cleanToken;
    state.devices[String(project.id)] = device;
    const existing = state.projects.filter(item => item.id !== project.id);
    state.projects = [...existing, project].sort((a, b) => a.name.localeCompare(b.name));
    state.activeProject = project.id;
    if (persist) saveTokens();
    showDashboard();
    renderProjectSelect();
    await refreshData();
    return project;
  }

  async function restoreCredentials() {
    state.tokens = readTokens();
    const valid = {};
    const failures = [];
    for (const [projectId, token] of Object.entries(state.tokens)) {
      try {
        const device = await (await api("/v1/me/device", token)).json();
        if (String(device.project_id) !== String(projectId)) continue;
        const project = await (await api(`/v1/projects/${encodeURIComponent(projectId)}`, token)).json();
        valid[projectId] = token;
        state.devices[projectId] = device;
        state.projects.push(project);
      } catch (error) {
        failures.push(error.message);
      }
    }
    state.tokens = valid;
    saveTokens();
    state.projects.sort((a, b) => a.name.localeCompare(b.name));
    if (!state.projects.some(project => project.id === state.activeProject)) {
      state.activeProject = state.projects[0]?.id || "";
    }
    if (!state.projects.length) {
      showGate(failures.length ? `Could not restore a saved project credential: ${failures[0]}` : "");
      updateConnectionUI(false, "Disconnected");
      return;
    }
    showDashboard();
    renderProjectSelect();
    await refreshData();
  }

  function renderProjectSelect() {
    const select = $("#projectFilter");
    const selected = state.activeProject;
    select.innerHTML = `<option value="">All connected projects</option>` + state.projects
      .map(project => `<option value="${esc(project.id)}">${esc(project.name)}</option>`).join("");
    select.value = selected;
  }

  async function loadProjectData(project) {
    const token = projectToken(project.id);
    const device = state.devices[project.id];
    const output = { project, token, device, overview: null, tasks: [], events: [], devices: [], errors: [] };
    const jobs = [
      api(`/v1/projects/${encodeURIComponent(project.id)}/tasks`, token)
        .then(async response => { output.tasks = await response.json(); })
        .catch(error => output.errors.push(error.message)),
      api("/v1/events?limit=100", token)
        .then(async response => {
          output.events = await response.json();
          output.cursor = response.headers.get("X-TRAC-Event-Cursor") || "0";
        })
        .catch(error => output.errors.push(error.message)),
      api(`/v1/projects/${encodeURIComponent(project.id)}/overview`, token)
        .then(async response => { output.overview = await response.json(); })
        .catch(() => {}),
      api(`/v1/projects/${encodeURIComponent(project.id)}/devices`, token)
        .then(async response => { output.devices = await response.json(); })
        .catch(() => {})
    ];
    await Promise.all(jobs);
    return output;
  }

  async function refreshData() {
    if (!state.projects.length) return;
    showDashboard();
    $("#pageContent").innerHTML = `<div class="grid stats-grid">${Array.from({ length: 4 }, () => '<div class="skeleton"></div>').join("")}</div><div class="skeleton"></div>`;
    setError("");
    try {
      const projectsToLoad = state.activeProject
        ? state.projects.filter(project => project.id === state.activeProject)
        : state.projects;
      const data = await Promise.all(projectsToLoad.map(loadProjectData));
      state.loadedProjects = data;
      state.tasks = data.flatMap(item => item.tasks);
      state.events = data.flatMap(item => item.events);
      state.devicesByProject = Object.fromEntries(data.map(item => [item.project.id, item.devices]));
      state.currentDevice = data.find(item => item.device)?.device || null;
      const failures = data.flatMap(item => item.errors);
      setError(failures.length ? `Some authorized project data could not be loaded: ${[...new Set(failures)].join("; ")}` : "");
      renderPage();
      startStreams(data);
    } catch (error) {
      setError(error.message);
      $("#pageContent").innerHTML = `<div class="empty-state"><strong>Project data is unavailable.</strong>${esc(error.message)}</div>`;
    }
  }

  function renderPage() {
    const [title, eyebrow, description] = pageInfo[state.page];
    $("#pageTitle").textContent = title;
    $("#currentPageLabel").textContent = title;
    $("#pageEyebrow").textContent = eyebrow;
    $("#pageDescription").textContent = description;
    $$(".nav-item").forEach(button => button.classList.toggle("active", button.dataset.page === state.page));
    const root = $("#pageContent");
    const views = {
      overview: renderOverview,
      activity: renderActivity,
      projects: renderProjects,
      tasks: renderTasks,
      integrate: renderIntegrate,
      connections: renderConnections,
      settings: renderSettings
    };
    root.innerHTML = views[state.page]();
    bindPageActions();
  }

  function renderOverview() {
    const projects = state.loadedProjects || [];
    const tasks = projects.flatMap(item => item.tasks);
    const events = visibleEvents(projects.flatMap(item => item.events)).sort(byRecent).slice(0, 7);
    const active = tasks.filter(task => task.status === "in_progress").length;
    const completed = tasks.filter(task => task.status === "done").length;
    const blocked = tasks.filter(task => task.status === "blocked").length;
    const cards = [
      ["Connected projects", projects.length, "Authorized project scopes"],
      ["In progress", active, "Tasks reported in progress"],
      ["Completed", completed, "Tasks marked done"],
      ["Blocked", blocked, blocked ? "Requires attention" : "No blocked tasks"]
    ];
    return `<div class="grid stats-grid">${cards.map(([label, value, note]) => `
      <article class="stat-card"><span class="stat-label">${esc(label)}</span><strong class="stat-value">${value}</strong><span class="stat-note">${esc(note)}</span></article>`).join("")}</div>
      <div class="grid two-column">
        <section class="panel"><div class="panel-heading"><h2>Recent engineering activity</h2><button class="button button-quiet" data-go="activity">View all</button></div>${events.length ? eventList(events) : empty("No recorded events", "Real authorized events will appear here.")}</section>
        <section class="panel"><div class="panel-heading"><h2>Project progress</h2><button class="button button-quiet" data-go="projects">Projects</button></div>${projects.length ? projects.map(item => projectProgress(item.project)).join("") : empty("No connected projects", "Add a project credential to load project data.")}</section>
      </div>
      <section class="panel" style="margin-top:16px"><div class="panel-heading"><h2>Items requiring attention</h2></div>${blocked ? eventList(tasks.filter(task => task.status === "blocked").map(task => ({
        event_type: "task.blocked", actor_display_name: task.assignee_display_name, project_id: task.project_id,
        task_id: task.id, metadata: { note: task.title }, received_at: task.updated_at, source: "task"
      }))) : empty("Nothing needs attention", "No blocked tasks are visible in the selected projects.")}</section>`;
  }

  function projectProgress(project) {
    const data = state.loadedProjects.find(item => item.project.id === project.id);
    const overview = data?.overview;
    const tasks = data?.tasks || [];
    const counts = overview?.task_counts || countStatuses(tasks);
    const total = overview?.total_tasks ?? tasks.length;
    const done = counts.done || 0;
    const percent = total ? Math.round((done / total) * 100) : 0;
    const lastActivity = data?.events.slice().sort(byRecent)[0];
    return `<button class="project-card" data-select-project="${esc(project.id)}">
      <span class="badge">${esc(project.project_key || "PROJECT")}</span><h3>${esc(project.name)}</h3>
      <p class="small muted">Head: ${esc(project.project_head_name || "Not specified")} · ${project.member_count ?? "—"} members</p>
      <p class="small muted" style="margin:0 0 12px">Members: ${esc((project.members || []).map(member => member.display_name).join(", ") || "No members listed")}</p>
      <div class="progress-track" aria-label="${percent}% of visible tasks complete"><div class="progress-fill" style="width:${percent}%"></div></div>
      <div class="project-stats"><span>${done}/${total} done</span><span>${counts.blocked || 0} blocked</span></div>
      <p class="small muted" style="margin:12px 0 0">Last activity: ${lastActivity ? esc(dateText(lastActivity.occurred_at || lastActivity.received_at)) : "No recorded activity"}</p>
    </button>`;
  }

  function renderProjects() {
    const projects = state.loadedProjects || [];
    if (!projects.length) return empty("No projects in this view", "Connect an authorized project credential to see its portfolio.");
    const active = projects.find(item => item.project.id === state.activeProject);
    return `<div class="grid project-grid">${projects.map(item => projectProgress(item.project)).join("")}</div>
      ${active ? `<div class="grid two-column" style="margin-top:16px">
        <section class="panel"><div class="panel-heading"><h2>${esc(active.project.name)} tasks</h2><button class="button button-quiet" data-go="tasks">Open board</button></div>
          ${active.tasks.length ? active.tasks.slice(0, 8).map(task => `<button class="task-card" data-task-id="${esc(task.id)}"><span class="badge status-badge status-${esc(task.status)}">${esc(task.status)}</span><h3>${esc(task.title)}</h3><div class="task-footer"><span>${esc(task.assignee_display_name || "Unassigned")}</span><time>${esc(dateText(task.updated_at))}</time></div></button>`).join("") : empty("No visible tasks", "No tasks are assigned to this developer or available to this credential.")}</section>
        <section class="panel"><div class="panel-heading"><h2>Recent project activity</h2><button class="button button-quiet" data-go="activity">Live feed</button></div>${eventList(visibleEvents(active.events).slice(0, 12)) || empty("No recorded activity", "Events appear after they are authorized and persisted.")}</section>
      </div>` : ""}`;
  }

  function renderTasks() {
    const tasks = state.tasks;
    if (!tasks.length) return empty("No visible tasks", "The connected credential may only show tasks assigned to its developer, or this project has no tasks.");
    return `<div class="task-board">${taskStatuses.map(([status, title]) => {
      const inColumn = tasks.filter(task => task.status === status);
      return `<section class="task-column" aria-label="${title} tasks"><div class="column-title">${title}<span class="column-count">${inColumn.length}</span></div>
        ${inColumn.map(task => `<button class="task-card" data-task-id="${esc(task.id)}">
          <span class="badge">${esc(task.external_key || task.priority || "Task")}</span>
          <h3>${esc(task.title)}</h3><p>${esc(task.assignee_display_name || "Unassigned")}</p>
          ${task.progress_percent != null ? `<div class="progress-track" aria-label="${task.progress_percent}%${task.progress_is_estimate ? " estimated" : ""}"><div class="progress-fill" style="width:${Math.max(0, Math.min(100, task.progress_percent))}%"></div></div>` : ""}
          <div class="task-footer"><span>${task.progress_percent == null ? "No progress reported" : `${task.progress_percent}%${task.progress_is_estimate ? " estimate" : ""}`}</span><time>${esc(dateText(task.updated_at))}</time></div>
        </button>`).join("") || `<p class="small muted">No tasks</p>`}</section>`;
    }).join("")}</div>`;
  }

  function renderActivity() {
    const events = visibleEvents(state.events);
    return `<div class="filters" aria-label="Activity filters">
      <label class="sr-only" for="activityProjectFilter">Project</label><select class="control" id="activityProjectFilter"><option value="">All loaded projects</option>${(state.loadedProjects || []).map(item => `<option value="${esc(item.project.id)}">${esc(item.project.name)}</option>`).join("")}</select>
      <label class="sr-only" for="actorFilter">Developer</label><input class="control" id="actorFilter" placeholder="Filter developer">
      <label class="sr-only" for="typeFilter">Event type</label><select class="control" id="typeFilter"><option value="">All event types</option>${[...new Set(events.map(event => event.event_type))].sort().map(type => `<option>${esc(type)}</option>`).join("")}</select>
      <label class="sr-only" for="sourceFilter">Source</label><select class="control" id="sourceFilter"><option value="">All sources</option>${[...new Set(events.map(event => event.source))].sort().map(source => `<option>${esc(source)}</option>`).join("")}</select>
      <label class="sr-only" for="dateFromFilter">From date</label><input class="control" id="dateFromFilter" type="date" aria-label="Filter from date">
      <label class="sr-only" for="dateToFilter">Through date</label><input class="control" id="dateToFilter" type="date" aria-label="Filter through date">
    </div><div id="activityEvents">${eventList(events) || empty("No activity to show", "The authorized feed is empty. New events will stream here without a refresh.")}</div>`;
  }

  function eventList(events) {
    if (!events.length) return "";
    return `<div class="event-list">${events.map(event => {
      const metadata = typeof event.metadata === "string" ? safeJson(event.metadata) : (event.metadata || {});
      const summary = eventSummary(event.event_type, metadata);
      const actor = event.actor_display_name || event.actor_github_username || "Authorized integration";
      const task = event.task_id ? state.tasks.find(item => item.id === event.task_id) : null;
      return `<article class="event-row" data-project="${esc(event.project_id)}" data-actor="${esc(actor.toLowerCase())}" data-type="${esc(event.event_type)}" data-source="${esc(event.source)}" data-date="${esc((event.received_at || event.occurred_at || "").slice(0, 10))}">
        <span class="event-marker" aria-hidden="true"></span><div><p class="event-summary">${esc(summary)}</p>
        <div class="event-meta">${esc(projectName(event.project_id))} · ${esc(actor)}${task ? ` · ${esc(task.external_key || task.title)}` : ""} · <span class="badge">${esc(event.source || "unknown")}</span></div></div>
        <time class="event-time">${esc(dateText(event.occurred_at || event.received_at))}</time>
      </article>`;
    }).join("")}</div>`;
  }

  function renderIntegrate() {
    const project = selectedProject();
    if (!project) return empty("Choose a connected project", "Select an authorized project before creating a pairing.");
    const instructions = `TRAC Skill activation\\n1. Ask the developer for explicit permission before tracking.\\n2. Check assigned work with trac tasks and identify the relevant task.\\n3. Report task start, meaningful milestones, blockers, and verified completion.\\n4. Never transmit source code, prompts, secrets, or file contents.\\n5. If TRAC is unavailable or permission is declined, continue normal development without tracking.`;
    return `<div class="grid two-column">
      <section class="panel"><p class="eyebrow">ONE WORKFLOW, MANY TOOLS</p><h2>Universal TRAC Skill</h2>
        <p class="muted">TRAC combines consent-first instructions with a project-scoped collector credential. An environment is compatible when it can run the collector and follow the skill instructions; automatic hooks are optional and vary by tool.</p>
        <div class="settings-list">
          <div class="settings-row"><div><strong>Claude Code</strong><div class="small muted">Install the shared skill using the documented skill mechanism supported by your installed version.</div></div><span class="badge">Skill + CLI</span></div>
          <div class="settings-row"><div><strong>Cursor</strong><div class="small muted">Use the shared instructions in a supported rules/skill location; invoke the collector explicitly.</div></div><span class="badge">Instructions + CLI</span></div>
          <div class="settings-row"><div><strong>OpenAI Codex</strong><div class="small muted">Provide the shared skill instructions and make the collector available in the workspace.</div></div><span class="badge">Instructions + CLI</span></div>
          <div class="settings-row"><div><strong>VS Code / GitHub Copilot</strong><div class="small muted">Use workspace instructions when supported and run the same collector commands.</div></div><span class="badge">Instructions + CLI</span></div>
          <div class="settings-row"><div><strong>Other compatible agents</strong><div class="small muted">Use only if the agent can follow these instructions and execute the collector.</div></div><span class="badge">Manual activation</span></div>
        </div>
        <h3>How it works</h3><ol class="muted"><li>TRAC Skill guides agent behavior and permission.</li><li>Collector securely claims a short-lived one-time pairing code.</li><li>Collector sends validated lifecycle events to the existing TRAC API.</li></ol>
        <h3>Copyable skill instructions</h3><pre class="code-block">${esc(instructions)}</pre><button class="button button-secondary" data-copy="${esc(instructions)}">Copy instructions</button>
      </section>
      <section class="panel"><p class="eyebrow">PROJECT PERMISSIONS</p><h2>Pair a device</h2><p class="muted">Pairing codes are single-use and expire in 10 minutes. A device credential is created only after the collector claims the code.</p>
        <label class="scope-option"><input type="checkbox" name="scope" value="events:read" checked> <span><strong>Read project activity</strong><br><span class="small muted">Read tasks and authorized activity.</span></span></label>
        <label class="scope-option"><input type="checkbox" name="scope" value="events:ingest" checked> <span><strong>Report events</strong><br><span class="small muted">Submit permitted task lifecycle events.</span></span></label>
        <button id="createPairing" class="button button-primary">Create one-time pairing</button>
        <div id="pairingResult" class="hidden" style="margin-top:16px"></div>
        <p class="small muted" style="margin-top:18px">Do not share the pairing code publicly. The code itself is not a long-lived credential.</p>
      </section></div>`;
  }

  function renderConnections() {
    const project = selectedProject();
    const devices = project ? (state.devicesByProject?.[project.id] || []) : [];
    if (!project) return empty("Choose a connected project", "Connections are scoped to a project.");
    if (!devices.length) return `<section class="panel">${empty("No device inventory available", "Only project heads can view and revoke project devices.")}</section>`;
    return `<div class="grid connection-grid">${devices.map(device => `<article class="connection-card">
      <div class="panel-heading"><div><h3>${esc(device.label || "Unnamed device")}</h3><div class="small muted">${esc(device.display_name || "Unknown developer")}</div></div><span class="badge">${esc(device.status)}</span></div>
      <div class="device-meta"><span>Project: ${esc(device.project_name)}</span><span>Created: ${esc(dateText(device.created_at))}</span><span>Last reported: ${esc(device.last_reported_at ? dateText(device.last_reported_at) : "Never")}</span><span>Auth: ${esc(device.auth_mode || "device")}</span><div class="scope-list">${(device.scopes || []).map(scope => `<span class="badge">${esc(scope)}</span>`).join("")}</div></div>
      ${device.status !== "revoked" ? `<button class="button button-danger" data-revoke-device="${esc(device.device_id)}">Revoke access</button>` : ""}
    </article>`).join("")}</div>`;
  }

  function renderSettings() {
    const device = state.currentDevice;
    if (!device) return empty("Profile unavailable", "Connect an authorized project credential to load your profile.");
    const eventVisibility = sessionStorage.getItem("trac-event-visibility") || "authorized";
    return `<section class="panel"><div class="panel-heading"><h2>Profile and privacy</h2></div><div class="settings-list">
      <div class="settings-row"><div><strong>Developer</strong><div class="small muted">${esc(device.display_name || device.user_id)}${device.github_username ? ` · @${esc(device.github_username)}` : ""}</div></div><span class="badge">${esc(device.role || "member")}</span></div>
      <div class="settings-row"><div><strong>Current project</strong><div class="small muted">${esc(device.project_name || "Project")}</div></div><span class="badge">${esc(device.project_key || "")}</span></div>
      <div class="settings-row"><div><strong>Appearance</strong><div class="small muted">Choose a light, dark, or system theme.</div></div><select class="control" id="settingsTheme"><option value="system">System</option><option value="light">Light</option><option value="dark">Dark</option></select></div>
      <div class="settings-row"><div><strong>Event visibility</strong><div class="small muted">The API enforces project/task access; this preference only affects this browser view.</div></div><select class="control" id="eventVisibility"><option value="authorized">Authorized events</option><option value="mine">My reported events</option></select></div>
      <div class="settings-row"><div><strong>Browser credentials</strong><div class="small muted">Project credentials remain in session storage and are removed when disconnected.</div></div><button class="button button-danger" id="disconnectButton">Disconnect all</button></div>
      <div class="settings-row"><div><strong>Server-side device management</strong><div class="small muted">Project heads can review and revoke authorized devices.</div></div><button class="button button-secondary" data-go="connections">Manage devices</button></div>
    </div></section>`;
  }

  function bindPageActions() {
    $$("[data-go]").forEach(button => button.addEventListener("click", () => navigate(button.dataset.go)));
    $$("[data-select-project]").forEach(button => button.addEventListener("click", () => {
      state.activeProject = button.dataset.selectProject;
      $("#projectFilter").value = state.activeProject;
      refreshData();
    }));
    $$("[data-task-id]").forEach(button => button.addEventListener("click", () => openTask(button.dataset.taskId)));
    $$("[data-copy]").forEach(button => button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(button.dataset.copy);
        button.textContent = "Copied";
      } catch {
        button.textContent = "Clipboard unavailable";
      }
    }));
    $("#createPairing")?.addEventListener("click", createPairing);
    $$("[data-revoke-device]").forEach(button => button.addEventListener("click", () => revokeDevice(button.dataset.revokeDevice)));
    ["activityProjectFilter", "actorFilter", "typeFilter", "sourceFilter", "dateFromFilter", "dateToFilter"].forEach(id => {
      $(`#${id}`)?.addEventListener("input", applyActivityFilters);
      $(`#${id}`)?.addEventListener("change", applyActivityFilters);
    });
    $("#settingsTheme")?.addEventListener("change", event => setTheme(event.target.value, true));
    $("#settingsTheme") && ($("#settingsTheme").value = document.documentElement.dataset.themePreference || "system");
    $("#eventVisibility")?.addEventListener("change", event => {
      sessionStorage.setItem("trac-event-visibility", event.target.value);
      renderPage();
    });
    $("#eventVisibility") && ($("#eventVisibility").value = sessionStorage.getItem("trac-event-visibility") || "authorized");
    $("#disconnectButton")?.addEventListener("click", disconnectAll);
  }

  function navigate(page) {
    if (!pageInfo[page]) return;
    state.page = page;
    renderPage();
    $("#sidebar").classList.remove("open");
    $("#menuButton").setAttribute("aria-expanded", "false");
  }

  async function openTask(taskId) {
    const task = state.tasks.find(item => item.id === taskId);
    if (!task) return;
    try {
      const detail = await (await api(`/v1/tasks/${encodeURIComponent(taskId)}`, projectToken(task.project_id))).json();
      const linked = state.events.filter(event => event.task_id === taskId);
      $("#taskDialogContent").innerHTML = `<p class="eyebrow">TASK DETAIL</p><h2>${esc(detail.title)}</h2>
        <p class="muted">${esc(detail.description || "No description provided.")}</p>
        <div class="project-stats"><span class="badge status-badge status-${esc(detail.status)}">${esc(detail.status)}</span><span>Priority: ${esc(detail.priority)}</span><span>Assignee: ${esc(detail.assignee_display_name || "Unassigned")}</span></div>
        <section class="panel" style="margin-top:16px"><h3>Task updates</h3>${detail.updates?.length ? detail.updates.map(update => `<article class="event-row"><span class="event-marker"></span><div><p class="event-summary">${esc(update.update_type)}${update.progress_is_estimate ? " · estimate" : ""}</p><div class="event-meta">${esc(update.actor_display_name || "Developer")} · ${esc(update.note || "No note")}</div></div><time class="event-time">${esc(dateText(update.timestamp))}</time></article>`).join("") : empty("No task updates", "No status or progress updates have been recorded.")}</section>
        <section class="panel" style="margin-top:14px"><h3>Verified engineering evidence</h3><p class="small muted">These are persisted events associated with this task. They are shown separately from reported progress and status updates.</p>${linked.length ? eventList(linked) : empty("No linked engineering evidence", "GitHub, CI, or agent events appear here only when explicitly associated with this task.")}</section>`;
      $("#taskDialog").showModal();
    } catch (error) {
      setError(error.message);
    }
  }

  async function createPairing() {
    const project = selectedProject();
    if (!project) return;
    const button = $("#createPairing");
    button.disabled = true;
    button.textContent = "Creating…";
    try {
      const scopes = $$('input[name="scope"]:checked').map(input => input.value);
      if (!scopes.length) throw new Error("Select at least one permission scope.");
      const result = await (await api("/v1/pairings", projectToken(project.id), {
        method: "POST",
        body: JSON.stringify({ project_id: project.id, scopes })
      })).json();
      const box = $("#pairingResult");
      box.classList.remove("hidden");
      box.innerHTML = `<div class="pairing-code" aria-label="One-time pairing code">${esc(result.pairing_code)}</div>
        <p class="small muted">Expires ${esc(dateText(result.expires_at))}. This code is shown once; the server stores only its hash.</p>
        <p><button class="button button-secondary" data-copy="${esc(result.pairing_code)}">Copy pairing code</button></p>
        <div class="message" id="pairingStatus" role="status">Waiting for the collector to claim this code…</div>`;
      box.querySelector("[data-copy]").addEventListener("click", async event => {
        try { await navigator.clipboard.writeText(result.pairing_code); event.currentTarget.textContent = "Copied"; }
        catch { event.currentTarget.textContent = "Clipboard unavailable"; }
      });
      clearInterval(state.pairingTimer);
      state.pairingTimer = setInterval(async () => {
        try {
          const status = await (await api(`/v1/pairings/${encodeURIComponent(result.pairing_id)}`, projectToken(project.id))).json();
          const statusBox = $("#pairingStatus");
          if (!statusBox) { clearInterval(state.pairingTimer); return; }
          statusBox.textContent = status.status === "claimed"
            ? `Connected: ${status.device_label || "device"} claimed the pairing.`
            : `Pairing status: ${status.status}.`;
          if (status.status !== "pending") {
            clearInterval(state.pairingTimer);
            await loadDevices(project);
            if (state.page === "connections") renderPage();
          }
        } catch (error) {
          const statusBox = $("#pairingStatus");
          if (statusBox) statusBox.textContent = `Pairing verification unavailable: ${error.message}`;
          clearInterval(state.pairingTimer);
        }
      }, 2500);
    } catch (error) {
      const box = $("#pairingResult");
      box.classList.remove("hidden");
      box.innerHTML = `<div class="message error">${esc(error.message)}</div>`;
    } finally {
      button.disabled = false;
      button.textContent = "Create one-time pairing";
    }
  }

  async function loadDevices(project) {
    state.devicesByProject[project.id] = await (await api(`/v1/projects/${encodeURIComponent(project.id)}/devices`, projectToken(project.id))).json();
  }

  async function revokeDevice(deviceId) {
    const project = selectedProject();
    if (!project || !confirm("Revoke this device's project access? This cannot be undone.")) return;
    try {
      await api(`/v1/projects/${encodeURIComponent(project.id)}/devices/${encodeURIComponent(deviceId)}`, projectToken(project.id), { method: "DELETE" });
      const target = state.devicesByProject[project.id]?.find(item => String(item.device_id) === String(deviceId));
      if (target) {
        target.status = "revoked";
        target.revoked_at = new Date().toISOString();
      }
      if (String(state.currentDevice?.device_id) === String(deviceId)) {
        state.streamControllers.get(project.id)?.abort();
        state.streamControllers.delete(project.id);
        delete state.tokens[project.id];
        saveTokens();
        state.tasks = state.tasks.filter(item => item.project_id !== project.id);
        state.events = state.events.filter(item => item.project_id !== project.id);
        const projectData = state.loadedProjects?.find(item => item.project.id === project.id);
        if (projectData) {
          projectData.tasks = [];
          projectData.events = [];
          projectData.overview = null;
        }
        state.currentDevice = Object.entries(state.devices)
          .filter(([projectId]) => Boolean(state.tokens[projectId]))
          .map(([, item]) => item)[0] || null;
        updateConnectionUI(false, "Credential revoked");
        setError("This project credential was revoked. Add another authorized credential to reconnect.");
        renderPage();
        return;
      }
      await loadDevices(project);
      renderPage();
    } catch (error) {
      setError(error.message);
    }
  }

  function applyActivityFilters() {
    const project = $("#activityProjectFilter")?.value || "";
    const actor = ($("#actorFilter")?.value || "").trim().toLowerCase();
    const type = $("#typeFilter")?.value || "";
    const source = $("#sourceFilter")?.value || "";
    const from = $("#dateFromFilter")?.value || "";
    const through = $("#dateToFilter")?.value || "";
    $$(".event-row", $("#activityEvents")).forEach(row => {
      const visible = (!project || row.dataset.project === project)
        && (!actor || row.dataset.actor.includes(actor))
        && (!type || row.dataset.type === type)
        && (!source || row.dataset.source === source)
        && (!from || row.dataset.date >= from)
        && (!through || row.dataset.date <= through);
      row.classList.toggle("hidden", !visible);
    });
  }

  function startStreams(projectData) {
    for (const [key, controller] of state.streamControllers) {
      if (!projectData.some(item => item.project.id === key)) {
        controller.abort();
        state.streamControllers.delete(key);
      }
    }
    for (const project of projectData) {
      if (state.streamControllers.has(project.project.id)) continue;
      const controller = new AbortController();
      state.streamControllers.set(project.project.id, controller);
      consumeStream(project, controller.signal);
    }
    updateConnectionUI(projectData.length > 0, projectData.length ? "Connected" : "Disconnected");
  }

  async function consumeStream(project, signal) {
    let cursor = project.cursor || "0";
    while (!signal.aborted) {
      try {
        const response = await fetch("/v1/events/stream", {
          headers: { Authorization: `Bearer ${project.token}`, "Last-Event-ID": cursor, Accept: "text/event-stream" },
          signal
        });
        if (!response.ok || !response.body) throw new Error(`Stream failed (${response.status})`);
        updateConnectionUI(true, "Live");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!signal.aborted) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let boundary;
          while ((boundary = buffer.indexOf("\n\n")) >= 0) {
            const frame = buffer.slice(0, boundary).replace(/\r/g, "");
            buffer = buffer.slice(boundary + 2);
            const eventId = frame.match(/^id:\s*(.+)$/m)?.[1]?.trim();
            const eventType = frame.match(/^event:\s*(.+)$/m)?.[1]?.trim();
            const data = frame.match(/^data:\s*(.+)$/m)?.[1];
            if (!data || !eventId || eventType === "heartbeat") continue;
            cursor = eventId;
            if (state.streamEvents.has(eventId)) continue;
            state.streamEvents.add(eventId);
            if (state.streamEvents.size > 2000) state.streamEvents.delete(state.streamEvents.values().next().value);
            const parsed = safeJson(data);
            if (!parsed) continue;
            state.events = [parsed, ...state.events.filter(item => item.event_id !== parsed.event_id)].slice(0, 500);
            const eventProject = state.loadedProjects?.find(item => item.project.id === parsed.project_id);
            if (eventProject) {
              eventProject.events = [parsed, ...eventProject.events.filter(item => item.event_id !== parsed.event_id)].slice(0, 200);
            }
            if (parsed.task_id) {
              const task = await api(`/v1/tasks/${encodeURIComponent(parsed.task_id)}`, project.token)
                .then(response => response.json()).catch(() => null);
              if (task) {
                state.tasks = [task, ...state.tasks.filter(item => item.id !== task.id)];
                const projectData = state.loadedProjects?.find(item => item.project.id === task.project_id);
                if (projectData) {
                  projectData.tasks = [task, ...projectData.tasks.filter(item => item.id !== task.id)];
                  if (projectData.overview?.task_counts) {
                    projectData.overview.task_counts = countStatuses(projectData.tasks);
                    projectData.overview.total_tasks = projectData.tasks.length;
                  }
                }
              }
            }
            if (state.page === "activity") {
              const activity = $("#activityEvents");
              const visibleEvents = visibleEventsForActivity();
              if (activity) activity.innerHTML = eventList(visibleEvents) || empty("No activity to show", "The authorized feed is empty. New events will stream here without a refresh.");
              applyActivityFilters();
            } else if (state.page === "overview" || state.page === "projects") {
              renderPage();
            }
          }
        }
        if (!signal.aborted) updateConnectionUI(false, "Reconnecting");
      } catch (error) {
        if (signal.aborted) return;
        updateConnectionUI(false, "Reconnecting");
      }
      if (!signal.aborted) await new Promise(resolve => setTimeout(resolve, 1800));
    }
  }

  function eventSummary(type, metadata) {
    const notes = metadata.note || metadata.reason || metadata.workflow_name || metadata.check_name || "";
    const descriptions = {
      "task.started": "Started a task",
      "task.progress": `Reported ${metadata.progress ?? "updated"}% progress`,
      "task.blocked": "Reported a blocker",
      "task.completed": `Reported task completion (${metadata.outcome || "done"})`,
      "git.commit": "Recorded a commit",
      "git.pull_request.opened": "Opened a pull request",
      "git.pull_request.updated": "Updated a pull request",
      "git.pull_request.merged": "Merged a pull request",
      "test.completed": `Tests ${metadata.result || "completed"}`,
      "build.completed": `Build ${metadata.result || "completed"}`,
      "session.started": "Started a development session",
      "session.ended": "Ended a development session"
    };
    return `${descriptions[type] || type}${notes ? ` — ${notes}` : ""}`;
  }

  function countStatuses(tasks) {
    return tasks.reduce((result, task) => {
      result[task.status] = (result[task.status] || 0) + 1;
      return result;
    }, {});
  }

  function visibleEvents(events) {
    return sessionStorage.getItem("trac-event-visibility") === "mine"
      ? events.filter(event => event.actor_id === state.devices[event.project_id]?.user_id)
      : events;
  }

  function visibleEventsForActivity() {
    return visibleEvents(state.events);
  }

  function byRecent(a, b) {
    return new Date(b.received_at || b.occurred_at || 0) - new Date(a.received_at || a.occurred_at || 0);
  }

  function dateText(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? "—" : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  }

  function empty(title, note) {
    return `<div class="empty-state"><strong>${esc(title)}</strong>${esc(note)}</div>`;
  }

  function safeJson(value) {
    try { return JSON.parse(value); } catch { return null; }
  }

  function disconnectAll() {
    state.streamControllers.forEach(controller => controller.abort());
    state.streamControllers.clear();
    sessionStorage.removeItem(TOKEN_KEY);
    state.tokens = {};
    state.projects = [];
    state.tasks = [];
    state.events = [];
    state.activeProject = "";
    updateConnectionUI(false, "Disconnected");
    showGate();
  }

  function bindGlobalActions() {
    $$(".nav-item").forEach(button => button.addEventListener("click", () => navigate(button.dataset.page)));
    $("#projectFilter").addEventListener("change", event => {
      state.activeProject = event.target.value;
      state.streamControllers.forEach(controller => controller.abort());
      state.streamControllers.clear();
      refreshData();
    });
    $("#refreshButton").addEventListener("click", refreshData);
    $("#addCredentialButton").addEventListener("click", () => showGate("Add a credential for another project. Existing project credentials remain in this session."));
    $("#credentialForm").addEventListener("submit", async event => {
      event.preventDefault();
      try {
        const project = await addCredential($("#credentialInput").value);
        $("#credentialInput").value = "";
        $("#gateError").classList.add("hidden");
        state.activeProject = project.id;
        renderProjectSelect();
      } catch (error) {
        $("#gateError").textContent = error.message;
        $("#gateError").classList.remove("hidden");
      }
    });
    $("#devLoginForm").addEventListener("submit", async event => {
      event.preventDefault();
      try {
        const response = await api("/v1/dev/login", null, {
          method: "POST",
          body: JSON.stringify({
            github_username: $("#githubUsername").value.trim(),
            project_id: $("#projectIdInput").value.trim()
          })
        });
        const result = await response.json();
        await addCredential(result.access_token);
      } catch (error) {
        $("#gateError").textContent = error.message;
        $("#gateError").classList.remove("hidden");
      }
    });
    $("#themeSelect").addEventListener("change", event => setTheme(event.target.value, true));
    $("#menuButton").addEventListener("click", () => {
      const open = $("#sidebar").classList.toggle("open");
      $("#menuButton").setAttribute("aria-expanded", String(open));
    });
  }

  async function init() {
    const preference = localStorage.getItem(THEME_KEY) || "system";
    setTheme(preference);
    matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
      if ((localStorage.getItem(THEME_KEY) || "system") === "system") setTheme("system");
    });
    bindGlobalActions();
    await restoreCredentials();
  }

  init();
})();
