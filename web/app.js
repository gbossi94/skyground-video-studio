const files = [
  ["timeline.json", "Timeline", "Tagli e take selezionate"],
  ["captions.json", "Sottotitoli", "Parole e sincronizzazione"],
  ["cards.json", "Motion", "Card visive senza duplicare il parlato"],
  ["angles.json", "Angoli", "Inserti, durata e punto di ingresso"],
  ["brand.json", "Brand", "Colori e regole visive"],
  ["audio.json", "Audio", "Voce, musica ed effetti"],
  ["project.json", "Progetto", "Impostazioni generali"]
];
let projectId, currentFile = files[0][0], projects = [], etag = null, identity = null;
const $ = (id) => document.getElementById(id);

async function request(url, options) {
  const response = await fetch(url, options);
  const value = response.status === 204 ? {} : await response.json();
  if (!response.ok) {
    const error = new Error(value.error || "Richiesta non riuscita");
    error.status = response.status;
    error.body = value;
    // No session: either it expired, or this studio has never been claimed.
    // Both cases end at the same form, so open it rather than report an error
    // nobody can act on. Decided on the status, never on the message text.
    if (response.status === 401) showSignIn();
    throw error;
  }
  return {value, headers: response.headers};
}

const body = async (url, options) => (await request(url, options)).value;

let setupMode = false;

async function showSignIn() {
  // A freshly deployed studio has no account yet: the first visitor creates it,
  // and the door closes behind them.
  try {
    const setup = await body("/api/auth/setup");
    setupMode = Boolean(setup.required);
  } catch (error) {
    setupMode = false;
  }
  $("signin-intro").hidden = !setupMode;
  if (setupMode) {
    $("signin-intro").textContent = "Questo studio non ha ancora un account. Crea il primo: sarà l'amministratore.";
    $("signin-submit").textContent = "Crea l'account";
    $("password").setAttribute("autocomplete", "new-password");
  }
  $("signin").hidden = false;
}

function renderProjects() {
  $("projects").innerHTML = projects.map((p) => `<button class="project ${p.id === projectId ? "active" : ""}" data-id="${p.id}"><b>${p.name}</b><span>${p.status} · ${Math.round(p.canvas.duration)} sec</span></button>`).join("");
  document.querySelectorAll(".project").forEach((button) => button.onclick = () => openProject(button.dataset.id));
}

function renderTabs() {
  $("tabs").innerHTML = files.map(([name, label]) => `<button class="tab ${name === currentFile ? "active" : ""}" data-file="${name}">${label}</button>`).join("");
  document.querySelectorAll(".tab").forEach((button) => button.onclick = () => openFile(button.dataset.file));
}

function renderIdentity() {
  if (!identity) return;
  const local = identity.authMode === "open";
  $("identity").innerHTML = local
    ? "Studio locale"
    : `${identity.user.email}<button id="logout" type="button">Esci</button>`;
  const logout = $("logout");
  if (logout) logout.onclick = async () => {
    await fetch("/api/auth/logout", {method: "POST"});
    location.reload();
  };
}

async function openProject(id) {
  projectId = id;
  const project = projects.find((item) => item.id === id);
  $("title").textContent = project.name;
  $("preview").src = project.previewAvailable ? `/media/${id}/${project.files.preview}` : "";
  renderProjects();
  await Promise.all([openFile(currentFile), refreshStatus()]);
}

async function openFile(name) {
  currentFile = name;
  const meta = files.find(([file]) => file === name);
  $("file-title").textContent = meta[1];
  $("file-help").textContent = meta[2];
  renderTabs();
  const {value, headers} = await request(`/api/projects/${projectId}/files/${name}`);
  // The revision this text came from: sent back on save so a parallel edit is
  // reported instead of being overwritten.
  etag = (headers.get("ETag") || "").replace(/"/g, "") || null;
  $("editor").value = JSON.stringify(value, null, 2) + "\n";
  $("message").textContent = "";
}

async function refreshStatus() {
  const status = await body(`/api/projects/${projectId}/status`);
  const present = status.assets.filter((item) => item.present).length;
  const project = projects.find((item) => item.id === projectId);
  const details = await Promise.all(["cards.json", "angles.json", "captions.json"].map((file) => body(`/api/projects/${projectId}/files/${file}`)));
  const [cards, angles, captions] = details;
  $("metrics").innerHTML = `<div class="metric"><b>${Math.round(project.canvas.duration)}s</b><span>DURATA</span></div><div class="metric"><b>${angles.filter(a => a.enabled).length}</b><span>ANGOLI</span></div><div class="metric"><b>${cards.length}</b><span>MOTION CARD</span></div>`;
  $("health").textContent = status.problems.length ? `${status.problems.length} problemi` : "Progetto valido";
  $("health").className = `health ${status.problems.length ? "bad" : "ok"}`;
  $("issues").className = `issues ${status.problems.length ? "" : "ok"}`;
  $("issues").innerHTML = status.problems.length ? status.problems.map(p => `• ${p}`).join("<br>") : `Tutto coerente · ${present}/${status.assets.length} media disponibili · ${captions.length} parole`;
}

$("save").onclick = async () => {
  try {
    const value = JSON.parse($("editor").value);
    const headers = {"Content-Type": "application/json"};
    if (etag) headers["If-Match"] = etag;
    const {value: result, headers: responseHeaders} = await request(`/api/projects/${projectId}/files/${currentFile}`, {method: "PUT", headers, body: JSON.stringify(value)});
    etag = (responseHeaders.get("ETag") || "").replace(/"/g, "") || etag;
    // The fallback server reports no revision; keep the message honest either way.
    const revision = result.revision ? ` · revisione ${result.revision}` : "";
    $("message").textContent = result.problems.length ? `Salvato${revision} · ${result.problems.length} problemi da risolvere` : `Salvato e verificato${revision}`;
    await refreshStatus();
  } catch (error) {
    if (error.status === 409) {
      $("message").textContent = "Il file è cambiato nel frattempo: ricarico la versione salvata, rifai la modifica.";
      await openFile(currentFile);
      return;
    }
    $("message").textContent = `Errore: ${error.message}`;
  }
};

$("signin-form").onsubmit = async (event) => {
  event.preventDefault();
  $("signin-message").textContent = "";
  try {
    await body(setupMode ? "/api/auth/setup" : "/api/auth/login", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({email: $("email").value, password: $("password").value})
    });
    // After signing in the place to be is the editor, not this file panel:
    // landing here made a working studio look as if it had lost its editor.
    location.replace(WANTS_PANEL ? location.href : "/app/");
  } catch (error) {
    $("signin-message").textContent = error.message;
  }
};

/** `/?pannello` keeps this file panel; everything else signed in goes to the editor. */
const WANTS_PANEL = new URLSearchParams(location.search).has("pannello");

(async () => {
  try {
    identity = await body("/api/auth/me");
    if (!WANTS_PANEL) { location.replace("/app/"); return; }
    renderIdentity();
    projects = await body("/api/projects");
    if (!projects.length) throw new Error("Nessun progetto disponibile");
    await openProject(projects[0].id);
  } catch (error) {
    // A 401 has already opened the form: there is nothing to add on top of it.
    if (error.status !== 401) $("title").textContent = error.message;
  }
})();
