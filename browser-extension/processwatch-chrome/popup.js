const $ = (id) => document.getElementById(id);
let capture = null;
let preview = null;

function setStatus(message, kind = "") {
  $("status").textContent = message;
  $("status").className = kind;
}

function apiBase() {
  const value = $("api-url").value.trim().replace(/\/$/, "");
  const url = new URL(value);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.port !== "8000" ||
      url.pathname !== "" || url.search || url.hash || url.username || url.password) {
    throw new Error("Use o endereço local padrão http://127.0.0.1:8000.");
  }
  return value;
}

async function api(path, token, options = {}) {
  const method = options.method || "POST";
  const response = await fetch(`${apiBase()}${path}`, {
    method,
    headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json",
      ...(options.nonce ? { "X-ProcessWatch-Nonce": options.nonce } : {}) },
    ...(method === "GET" ? {} : { body: options.body === undefined ? "{}" : JSON.stringify(options.body) }),
    cache: "no-store", credentials: "omit", redirect: "error",
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.detail || `Falha HTTP ${response.status}.`);
  return result;
}

async function analyzeCurrentTab(manualNumber = null) {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab?.id || !tab.url) throw new Error("Não foi possível identificar a aba atual.");
  const current = new URL(tab.url);
  if (current.protocol !== "https:" || current.hostname !== "consulta.trf4.jus.br") {
    throw new Error("Página não suportada. Abra a consulta oficial https://consulta.trf4.jus.br.");
  }
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content/trf4_identity.js", "content/trf4.js"] });
  const [result] = await chrome.scripting.executeScript({ target: { tabId: tab.id },
    func: (selected) => globalThis.__processWatchCapture(selected), args: [manualNumber] });
  return result?.result;
}

function showDiagnostic(diagnostic, allowManual = false) {
  const panel = $("diagnostic-panel");
  const safe = diagnostic && typeof diagnostic === "object" ? diagnostic : {
    domain: "não reconhecido", page_type: "unknown", cnj_candidate_count: 0,
    header_candidate_count: 0, result_region_candidate_count: 0, candidates: [], tables: [],
    movement_region_count: 0, rejection: { code: "diagnostic_unavailable", message: "Diagnóstico local indisponível." },
  };
  $("diagnostic-json").value = JSON.stringify(safe, null, 2);
  panel.hidden = false;
  const list = $("candidate-list");
  list.replaceChildren();
  const choices = allowManual && Array.isArray(safe.candidates)
    ? safe.candidates.filter((item) => item.eligible_for_manual_selection && item.number !== "CNJ inválido") : [];
  $("manual-candidates").hidden = choices.length === 0;
  for (const candidate of choices) {
    const card = document.createElement("section"); card.className = "candidate";
    const description = document.createElement("p");
    const labels = candidate.labels_near_candidate.join(" · ") || "Sem rótulo próximo identificado";
    description.textContent = `${candidate.number} — ${labels} (${candidate.element_types.join(", ") || "estrutura não identificada"})`;
    const button = document.createElement("button"); button.type = "button"; button.textContent = "Selecionar como processo principal";
    button.addEventListener("click", async () => {
      if (!window.confirm(`Confirma que ${candidate.number} é o processo principal exibido? A importação só será aceita se esse processo já estiver cadastrado na carteira local.`)) return;
      button.disabled = true;
      try {
        const outcome = await analyzeCurrentTab(candidate.number);
        if (!outcome?.ok || String(outcome.data?.process_number || "").replace(/\D/g, "") !== candidate.number.replace(/\D/g, "")) {
          showDiagnostic(outcome?.diagnostic, false);
          throw new Error(outcome?.message || "A seleção não passou pela validação do processo.");
        }
        capture = outcome.data; preview = { ...outcome.preview, selection_mode: "Seleção assistida confirmada" };
        displayPreview(preview);
        setStatus("Processo selecionado. Confira a prévia; a carteira será validada novamente antes do envio.", "success");
      } catch (error) { setStatus(error.message || "Não foi possível validar essa seleção.", "error"); }
      finally { button.disabled = false; }
    });
    card.append(description, button); list.append(card);
  }
}

function displayPreview(data) {
  const entries = [
    ["Tribunal", data.tribunal], ["Processo", data.process_number],
    ["Movimentações", String(data.movement_count)], ["Mais recente", data.newest || "Não identificada"],
    ["Mais antiga", data.oldest || "Não identificada"], ["Links documentais", String(data.document_links)],
    ["Paginação", "Possivelmente incompleta; somente itens atualmente visíveis serão importados"],
    ...(data.selection_mode ? [["Identificação", data.selection_mode]] : []),
  ];
  const list = $("summary");
  list.replaceChildren();
  for (const [label, value] of entries) {
    const dt = document.createElement("dt"); dt.textContent = label;
    const dd = document.createElement("dd"); dd.textContent = value;
    list.append(dt, dd);
  }
  $("preview").hidden = false;
}

$("diagnose").addEventListener("click", async () => {
  $("diagnose").disabled = true;
  try {
    const outcome = await analyzeCurrentTab();
    showDiagnostic(outcome?.diagnostic || outcome?.identity_diagnostic, outcome?.code === "main_process_not_identified");
    if (outcome?.ok) setStatus("Diagnóstico concluído. A captura não foi enviada.");
    else setStatus(outcome?.message || "Diagnóstico concluído; confira os detalhes e candidatos.", "error");
  } catch (error) { setStatus(error.message || "Não foi possível diagnosticar a página.", "error"); }
  finally { $("diagnose").disabled = false; }
});

$("copy-diagnostic").addEventListener("click", async () => {
  const field = $("diagnostic-json");
  try {
    await navigator.clipboard.writeText(field.value);
    setStatus("JSON sanitizado copiado. Ele permanece local até você compartilhá-lo.", "success");
  } catch {
    field.focus(); field.select();
    const copied = document.execCommand("copy");
    setStatus(copied ? "JSON sanitizado copiado." : "Selecione e copie o JSON exibido.", copied ? "success" : "error");
  }
});

$("capture").addEventListener("click", async () => {
  capture = null; preview = null; $("preview").hidden = true;
  $("capture").disabled = true;
  try {
    const outcome = await analyzeCurrentTab();
    if (!outcome?.ok) {
      if (outcome?.diagnostic) showDiagnostic(outcome.diagnostic, outcome.code === "main_process_not_identified");
      throw new Error(outcome?.message || "A página não pôde ser capturada.");
    }
    capture = outcome.data; preview = outcome.preview;
    displayPreview(preview);
    setStatus("Captura pronta. Revise a prévia e confirme antes de enviar.");
  } catch (error) {
    setStatus(error.message || "Falha ao capturar a página.", "error");
  } finally { $("capture").disabled = false; }
});

$("import").addEventListener("click", async () => {
  const token = $("token").value.trim();
  if (!capture) return setStatus("Capture uma página compatível antes de importar.", "error");
  if (!token) return setStatus("Informe o código de acesso local do ProcessWatch.", "error");
  $("import").disabled = true;
  try {
    const watched = await api("/api/processes", token, { method: "GET" });
    const selectedDigits = capture.process_number.replace(/\D/g, "");
    if (!Array.isArray(watched) || !watched.some((item) => String(item.number || "").replace(/\D/g, "") === selectedDigits)) {
      throw new Error("O CNJ selecionado não está cadastrado na carteira local. A importação foi bloqueada.");
    }
    const issued = await api("/api/extension/trf4/nonce", token);
    const result = await api("/api/extension/trf4/queue-import", token,
      { nonce: issued.nonce, body: capture });
    setStatus(`Captura enviada ao worker: ${result.movement_count} movimentações. Estado: ${result.job_status}. Paginação: ${result.pagination_status}; captura completa não confirmada. Acompanhe a fonte TRF4 na interface local.`, "success");
    capture = null; preview = null; $("preview").hidden = true;
  } catch (error) {
    setStatus(error.message || "Não foi possível importar. Confira se o ProcessWatch está ativo e tente novamente.", "error");
  } finally { $("import").disabled = false; }
});

$("open").addEventListener("click", async () => {
  try { await chrome.tabs.create({ url: apiBase() }); }
  catch (error) { setStatus(error.message || "Não foi possível abrir o ProcessWatch local.", "error"); }
});
