const files = [
  ["timeline.json", "Timeline", "Tagli e take selezionate"],
  ["captions.json", "Sottotitoli", "Parole e sincronizzazione"],
  ["cards.json", "Motion", "Card visive senza duplicare il parlato"],
  ["angles.json", "Angoli", "Inserti, durata e punto di ingresso"],
  ["brand.json", "Brand", "Colori e regole visive"],
  ["audio.json", "Audio", "Voce, musica ed effetti"],
  ["project.json", "Progetto", "Impostazioni generali"]
];
let projectId, currentFile = files[0][0], projects = [];
const $ = (id) => document.getElementById(id);

async function request(url, options) {
  const response = await fetch(url, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "Richiesta non riuscita");
  return value;
}

function renderProjects() {
  $("projects").innerHTML = projects.map((p) => `<button class="project ${p.id === projectId ? "active" : ""}" data-id="${p.id}"><b>${p.name}</b><span>${p.status} · ${Math.round(p.canvas.duration)} sec</span></button>`).join("");
  document.querySelectorAll(".project").forEach((button) => button.onclick = () => openProject(button.dataset.id));
}

function renderTabs() {
  $("tabs").innerHTML = files.map(([name, label]) => `<button class="tab ${name === currentFile ? "active" : ""}" data-file="${name}">${label}</button>`).join("");
  document.querySelectorAll(".tab").forEach((button) => button.onclick = () => openFile(button.dataset.file));
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
  const value = await request(`/api/projects/${projectId}/files/${name}`);
  $("editor").value = JSON.stringify(value, null, 2) + "\n";
  $("message").textContent = "";
}

async function refreshStatus() {
  const status = await request(`/api/projects/${projectId}/status`);
  const present = status.assets.filter((item) => item.present).length;
  const project = projects.find((item) => item.id === projectId);
  const details = await Promise.all(["cards.json", "angles.json", "captions.json"].map((file) => request(`/api/projects/${projectId}/files/${file}`)));
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
    const result = await request(`/api/projects/${projectId}/files/${currentFile}`, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(value)});
    $("message").textContent = result.problems.length ? `Salvato · ${result.problems.length} problemi da risolvere` : "Salvato e verificato";
    await refreshStatus();
  } catch (error) {
    $("message").textContent = `Errore: ${error.message}`;
  }
};

(async () => {
  try {
    projects = await request("/api/projects");
    if (!projects.length) throw new Error("Nessun progetto disponibile");
    await openProject(projects[0].id);
  } catch (error) {
    $("title").textContent = error.message;
  }
})();
