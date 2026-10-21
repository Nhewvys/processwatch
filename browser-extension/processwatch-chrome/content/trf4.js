/* Runs in Chrome's isolated world only after an explicit popup action. */
globalThis.__processWatchCapture = (manualSelectedNumber = null) => {
  const allowedHost = "consulta.trf4.jus.br";
  if (location.protocol !== "https:" || location.hostname !== allowedHost) {
    return { ok: false, code: "unsupported_page", message: "Abra a consulta processual oficial do TRF4.", diagnostic: globalThis.ProcessWatchTrf4Identity?.sanitizeDiagnostic({ domain: location.hostname, pageType: "unsupported", rejectionCode: "unsupported_page" }) };
  }
  const bodyText = document.body?.innerText || "";
  const title = `${document.title} ${bodyText.slice(0, 1600)}`.toLocaleLowerCase("pt-BR");
  if (/cloudflare|verify you are human|verifique se você é humano|aguarde.*verifica|cf-challenge|challenge-platform/.test(title) ||
      document.querySelector("#challenge-form, iframe[src*='challenges.cloudflare.com'], [id*='cf-chl']")) {
    return { ok: false, code: "human_verification", message: "O portal está exibindo uma verificação humana. Conclua-a no próprio portal e tente novamente.", diagnostic: globalThis.ProcessWatchTrf4Identity?.sanitizeDiagnostic({ domain: location.hostname, pageType: "human_verification", candidateCount: globalThis.ProcessWatchTrf4Identity?.findNumbers(bodyText).length || 0, rejectionCode: "human_verification" }) };
  }
  const identity = globalThis.ProcessWatchTrf4Identity;
  if (!identity) return { ok: false, code: "identity_parser_missing", message: "O verificador seguro do processo não foi carregado. Recarregue a extensão no Chrome." };
  const cnjRegex = /\b\d{7}-\d{2}\.\d{4}\.4\.04\.\d{4}\b/g;
  const requestedNumbers = [];
  try {
    for (const value of new URL(location.href).searchParams.values()) requestedNumbers.push(...identity.findNumbers(value));
  } catch { /* URL parameters are corroborating evidence only. */ }
  for (const input of document.querySelectorAll("input[name], input[id]")) {
    if (/process|numero|cnj/i.test(`${input.name || ""} ${input.id || ""}`)) requestedNumbers.push(...identity.findNumbers(input.value));
  }

  const visible = (element) => !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
  const tableIsMovement = (table) => {
    const rows = [...table.querySelectorAll("tr")].filter((row) => row.closest("table") === table);
    const headerRows = rows.slice(0, 3).filter((row) => [...row.children].some((cell) => cell.tagName === "TH") ||
      /\b(data|hora|movimento|movimenta[çc][ãa]o|evento|descri[çc][ãa]o|andamento)\b/i.test(row.innerText || ""));
    const headers = headerRows.map((row) => row.innerText || "").join(" ");
    const hint = `${table.id || ""} ${table.className || ""} ${table.getAttribute("aria-label") || ""}`;
    const rowSummaries = rows.map((row) => ({ cells: [...row.children].filter((cell) => cell.tagName === "TD" || cell.tagName === "TH")
      .map((cell) => ({ text: cell.innerText || "" })) }));
    return identity.isMovementTable({ headers, hint, rows: rowSummaries });
  };
  const isMovementContext = (element) => {
    for (let current = element; current && current !== document.body; current = current.parentElement) {
      if (current.tagName === "TABLE") {
        if (tableIsMovement(current)) return true;
      }
    }
    return false;
  };
  const scopes = [...document.querySelectorAll("body *")].filter((element) => visible(element) && identity.findNumbers(element.innerText || "").length);
  const identityCandidates = [];
  for (const element of scopes) {
    if (isMovementContext(element)) continue;
    const ownText = (element.innerText || "").replace(/\s+/g, " ").trim();
    const ownNumbers = identity.findNumbers(ownText);
    if (!ownNumbers.length) continue;
    // Prefer the innermost element carrying the number. Ancestors are context only
    // when they contain this candidate alone; a shared section cannot label every CNJ.
    const atomicNumbers = ownNumbers.filter((number) => ![...element.children].some((child) => identity.findNumbers(child.innerText || "").includes(number)));
    if (!atomicNumbers.length) continue;
    const fieldFor = (number) => {
      let field = element;
      for (let parent = element.parentElement, depth = 0; parent && parent !== document.body && depth < 5; parent = parent.parentElement, depth++) {
        if (isMovementContext(parent)) break;
        const text = (parent.innerText || "").replace(/\s+/g, " ").trim();
        const nums = [...new Set(identity.findNumbers(text))];
        if (nums.length === 1 && nums[0] === number && text.length <= 500) { field = parent; break; }
      }
      return field;
    };
    const classTokens = (node) => [...(node?.classList || [])].slice(0, 12);
    for (const number of ownNumbers) {
      if (!atomicNumbers.includes(number)) continue;
      const field = fieldFor(number);
      const fieldText = (field.innerText || "").replace(/\s+/g, " ").trim().slice(0, 500);
      const parent = field.parentElement;
      // Sibling text is useful only when the parent describes this CNJ alone.
      // A shared container holding the main and origin process must not lend
      // the "Originário" label to both candidates.
      const parentNumbers = parent ? [...new Set(identity.findNumbers(parent.innerText || ""))] : [];
      const siblingLabels = parent && parentNumbers.length === 1 && parentNumbers[0] === number
        ? [...parent.children].filter((sibling) => sibling !== field &&
          !identity.findNumbers(sibling.innerText || "").length).map((sibling) => (sibling.innerText || "").replace(/\s+/g, " ").trim().slice(0, 100)).filter(Boolean).slice(0, 6)
        : [];
      let header = null;
      for (let node = field; node && node !== document.body; node = node.parentElement) {
        const tag = node.tagName;
        const text = (node.innerText || "").replace(/\s+/g, " ").trim();
        if (identity.findNumbers(text).length !== 1 || !identity.findNumbers(text).includes(number)) continue;
        if (/^(H[1-4]|HEADER)$/.test(tag) || /process|capa|resultado|cabecalho|identifica/i.test(`${node.id || ""} ${node.className || ""}`)) { header = node; break; }
      }
      const headerText = header ? (header.innerText || "").replace(/\s+/g, " ").trim().slice(0, 500) : "";
      const localText = (element.innerText || "").replace(/\s+/g, " ").trim().slice(0, 300);
      const labelRelation = identity.safeLabels(localText).length ? "same_element" : siblingLabels.length ? "sibling" : identity.safeLabels(fieldText).length ? "same_field" : header ? "header" : "none";
      const elementTypes = [element.tagName, ...(field !== element ? [field.tagName] : []), ...(header && header !== field ? [header.tagName] : [])];
      identityCandidates.push(identity.classifyCandidate({ number, localText, fieldText, siblingLabels, headerText,
        elementTypes, cssClasses: [...classTokens(element), ...classTokens(field), ...classTokens(header)], labelRelation }));
    }
  }
  const candidateMap = new Map();
  for (const candidate of identityCandidates) {
    const key = candidate.number;
    const occurrences = candidateMap.get(key) || [];
    occurrences.push(candidate);
    candidateMap.set(key, occurrences);
  }
  const candidateScore = (item) => item.isHeader && !item.related ? 3 : item.isResultRegion && !item.related ? 2 : !item.related ? 1 : 0;
  const candidates = [...candidateMap.values()].map((occurrences) => {
    const best = [...occurrences].sort((left, right) => candidateScore(right) - candidateScore(left))[0];
    // Keep the strongest local evidence for labels. Unioning every mention can
    // reintroduce a shared "Originário" label into the main-process diagnostic.
    return { ...best,
      isHeader: occurrences.some((item) => item.isHeader && !item.related),
      isResultRegion: occurrences.some((item) => item.isResultRegion && !item.related),
      related: occurrences.every((item) => item.related),
      elementTypes: [...new Set(occurrences.flatMap((item) => item.elementTypes))].slice(0, 5),
    };
  });
  const resolvedIdentity = identity.resolveMainProcess(candidates, requestedNumbers, manualSelectedNumber);
  const allTables = [...document.querySelectorAll("table")];
  const tablesForDiagnostic = allTables.map((table) => {
    const rows = [...table.querySelectorAll("tr")].filter((row) => row.closest("table") === table);
    return { rowCount: rows.length, movementRegion: tableIsMovement(table), hasThead: Boolean(table.querySelector("thead")),
      hasTbody: Boolean(table.querySelector("tbody")), rows: rows.slice(0, 6).map((row) => {
        const cells = [...row.children].filter((cell) => cell.tagName === "TD" || cell.tagName === "TH");
        const text = cells.map((cell) => cell.innerText || "").join(" ");
        return { cellCount: cells.length, cellTypes: cells.map((cell) => cell.tagName),
          hasColspan: cells.some((cell) => Number(cell.getAttribute("colspan") || 1) > 1),
          hasRowspan: cells.some((cell) => Number(cell.getAttribute("rowspan") || 1) > 1),
          hasDate: /\b\d{1,2}\/\d{1,2}\/\d{4}\b|\b\d{4}-\d{2}-\d{2}\b/.test(text), hasTime: /\b\d{1,2}:\d{2}(?::\d{2})?\b/.test(text),
          linkCount: row.querySelectorAll("a[href]").length };
      }) };
  });
  const pageType = candidates.some((item) => item.isHeader) ? "trf4_result" : (tablesForDiagnostic.some((item) => item.movementRegion) ? "trf4_result" : "trf4_search");
  const makeDiagnostic = (code) => identity.sanitizeDiagnostic({ domain: location.hostname, pageType,
    candidateCount: identity.findNumbers(bodyText).length, candidates, tables: tablesForDiagnostic,
    tableCount: allTables.length, movementRegionCount: tablesForDiagnostic.filter((item) => item.movementRegion).length,
    rejectionCode: code });
  const diagnostic = makeDiagnostic(resolvedIdentity.ok ? "capture_ready" : resolvedIdentity.code);
  if (!resolvedIdentity.ok) {
    return { ok: false, code: resolvedIdentity.code, message: resolvedIdentity.message, diagnostic };
  }
  const processNumber = resolvedIdentity.processNumber;
  const rowData = (row) => {
    const cells = [...row.children].filter((cell) => (cell.tagName === "TH" || cell.tagName === "TD") && visible(cell));
    const parsed = identity.parseMovementCells(cells.map((cell) => ({ text: cell.innerText || "" })));
    if (!parsed) return null;
    const documents = [...row.querySelectorAll("a[href]")].filter(visible).map((anchor) => {
      let url = null;
      try {
        const target = new URL(anchor.getAttribute("href"), location.href);
        if (target.protocol === "https:" && target.hostname === allowedHost && target.pathname.startsWith("/trf4/") && !target.search && !target.hash) url = target.href;
      } catch { /* Keep metadata only for unsafe/relative links. */ }
      return { source_id: anchor.getAttribute("data-id") || null,
        description: (anchor.innerText || anchor.getAttribute("aria-label") || "").trim().slice(0,300) || null,
        url };
    });
    const stableId = row.getAttribute("data-id") || row.getAttribute("id") || null;
    return { source_id: stableId && /^[A-Za-z0-9_.:-]{1,160}$/.test(stableId) ? stableId : null,
      occurred_at: parsed.occurred_at, date_original: parsed.date_original, name: parsed.name, documents };
  };
  const tables = [...document.querySelectorAll("table")].filter(visible);
  const movementCandidates = tables.map((table) => {
    const headerText = [...table.querySelectorAll("thead th, thead td, tr:first-child th")]
      .map((cell) => cell.innerText || "").join(" ").toLocaleLowerCase("pt-BR");
    const tableHint = `${table.id} ${table.className} ${table.getAttribute("aria-label") || ""}`.toLocaleLowerCase("pt-BR");
    const movementLabel = tableIsMovement(table);
    const rows = [...table.querySelectorAll("tr")].filter((row) => row.closest("table") === table).map(rowData).filter(Boolean);
    return { table, headerText, tableHint, rows, movementLabel };
  }).filter((item) => item.movementLabel && item.rows.length);
  if (!movementCandidates.length) return { ok: false, code: "movement_table_not_recognized",
    message: "Não reconheci uma tabela de movimentações nesta página.", diagnostic: makeDiagnostic("movement_table_not_recognized") };
  movementCandidates.sort((a,b) => b.rows.length - a.rows.length);
  if (movementCandidates.length > 1 && movementCandidates[0].rows.length === movementCandidates[1].rows.length) {
    return { ok: false, code: "ambiguous_movement_table", message: "Encontrei mais de uma tabela igualmente provável; a importação foi cancelada.", diagnostic: makeDiagnostic("ambiguous_movement_table") };
  }
  const selected = movementCandidates[0];
  const movements = selected.rows;
  if (!movements.length) return { ok: false, code: "no_movements", message: "Nenhuma movimentação foi identificada.", diagnostic: makeDiagnostic("no_movements") };
  const labels = [...document.querySelectorAll("tr")].filter(visible).map((row) => {
    const cells = [...row.querySelectorAll(":scope > th, :scope > td")].filter(visible);
    if (cells.length < 2) return null;
    const label = (cells[0].innerText || "").trim().replace(/\s*:?\s*$/, "").toLocaleLowerCase("pt-BR");
    const value = (cells.slice(1).map((cell) => cell.innerText || "").join(" ")).trim();
    if (!value || value.length > 500) return null;
    if (/^classe( processual)?$/.test(label)) return ["class", value];
    if (/^(?:unidade|[óo]rg[ãa]o julgador|ju[ií]zo)$/.test(label)) return ["court_organ", value];
    if (/^situa[çc][ãa]o$/.test(label)) return ["situation", value];
    if (/^assunto(s)?$/.test(label)) return ["subjects", value.split(/\s*[;,]\s*/).filter(Boolean).slice(0,50)];
    return null;
  }).filter(Boolean);
  const metadata = Object.fromEntries(labels);
  const pageTextLower = bodyText.toLocaleLowerCase("pt-BR");
  const loadMoreVisible = [...document.querySelectorAll("button,a,input[type=button],input[type=submit]")]
    .filter(visible).some((item) => /carregar mais|mais movimenta|pr[oó]ximas movimenta|ver mais/.test(`${item.innerText||""} ${item.value||""}`.toLocaleLowerCase("pt-BR")));
  return { ok: true, data: { schema_version: 1, source: "TRF4", capture_mode: "chrome_extension_assisted",
      extractor_version: "1.3.3", process_number: processNumber, page_host: location.hostname, tribunal: "TRF4",
      captured_at: new Date().toISOString(), class: metadata.class || null,
      court_organ: metadata.court_organ || null, situation: metadata.situation || null,
      subjects: metadata.subjects || [], movements, documents: [],
      pagination_status: loadMoreVisible ? "possibly_incomplete" : "unknown" },
    preview: { tribunal: "TRF4", process_number: processNumber, movement_count: movements.length,
      oldest: movements.map((item) => item.date_original).sort()[0],
      newest: movements.map((item) => item.date_original).sort().at(-1),
      document_links: movements.reduce((sum,item) => sum + item.documents.filter((doc) => doc.url).length,0),
      pagination_status: loadMoreVisible ? "possibly_incomplete" : "unknown" },
    diagnostics: { table_id: selected.table.id || null, headers: [...selected.table.querySelectorAll("thead th, thead td, tr:first-child th")].map((cell) => (cell.innerText||"").trim().slice(0,80)), visible_rows: movements.length,
      load_more_control_visible: loadMoreVisible, page_text_mentions_challenge: /cloudflare|captcha|verifica[çc][ãa]o de seguran[çc]a/.test(pageTextLower) },
    identity_diagnostic: identity.sanitizeDiagnostic({ domain: location.hostname, pageType, candidateCount: identity.findNumbers(bodyText).length,
      candidates, tables: tablesForDiagnostic, tableCount: allTables.length,
      movementRegionCount: tablesForDiagnostic.filter((item) => item.movementRegion).length, rejectionCode: "capture_ready" }) };
};
