/* Strict, DOM-independent process identity resolution for the TRF4 capture. */
(() => {
  const pattern = /\b\d{7}-\d{2}\.\d{4}\.4\.04\.\d{4}\b/g;
  const SAFE_LABELS = [
    [/origin[áa]ri[oa]/i, "Originário"], [/processo principal/i, "Processo principal"],
    [/processo/i, "Processo"], [/relacionad[oa]/i, "Relacionado"], [/vinculad[oa]/i, "Vinculado"],
    [/refer[eê]ncia/i, "Referência"], [/classe processual/i, "Classe processual"], [/classe/i, "Classe"],
    [/agravo de instrumento/i, "Agravo de Instrumento"], [/agravo interno/i, "Agravo Interno"],
    [/apela[çc][ãa]o/i, "Apelação"], [/mandado de seguran[çc]a/i, "Mandado de Segurança"],
    [/procedimento comum/i, "Procedimento Comum"], [/recurso inominado/i, "Recurso Inominado"],
    [/movimenta[çc][ãa]o|andamento|hist[oó]rico/i, "Região de movimentações"],
  ];
  const digitsOnly = (value) => String(value || "").replace(/\D/g, "");
  const RELATED = /origin[áa]ri[oa]|relacionad[oa]|dependente|vinculad[oa]|refer[eê]ncia|derivad[oa]|apensad[oa]|mencionad[oa] em movimenta[çc][ãa]o/i;
  const MAIN_LABEL = /(?:^|\b)(?:processo principal|processo|n[uú]mero do processo)\s*[:#-]?/i;
  const CLASS_LABEL = /agravo de instrumento|agravo interno|apela[çc][ãa]o|mandado de seguran[çc]a|procedimento comum|execu[çc][ãa]o|recurso inominado|embargos|habeas corpus/i;
  const isValidCnj = (value) => {
    const digits = digitsOnly(value);
    if (digits.length !== 20) return false;
    let remainder = 0;
    for (const digit of `${digits.slice(0, 7)}${digits.slice(9)}00`) {
      remainder = (remainder * 10 + Number(digit)) % 97;
    }
    return Number(digits.slice(7, 9)) === 98 - remainder;
  };
  const canonical = (value) => digitsOnly(value);

  function classifyCandidate({ number, localText = "", fieldText = "", siblingLabels = [], headerText = "", elementTypes = [], cssClasses = [], labelRelation = "none" }) {
    const local = String(localText).slice(0, 300);
    const field = String(fieldText).slice(0, 500);
    const siblings = (Array.isArray(siblingLabels) ? siblingLabels : []).filter((value) => typeof value === "string").join(" ").slice(0, 300);
    const rawHeader = String(headerText).slice(0, 500);
    const headerNumbers = [...new Set(findNumbers(rawHeader).map(canonical))];
    const header = headerNumbers.length === 0 || (headerNumbers.length === 1 && headerNumbers[0] === canonical(number)) ? rawHeader : "";
    // Ancestors containing multiple CNJs are deliberately absent from this evidence.
    const associatedText = `${local} ${field} ${siblings}`;
    const related = RELATED.test(associatedText);
    const mainLabel = MAIN_LABEL.test(associatedText);
    const heading = /^(H[1-4]|HEADER)$/.test(elementTypes[0] || "") && CLASS_LABEL.test(`${local} ${header}`);
    const resultRegion = !related && (mainLabel || heading || (/process|capa|resultado|identifica/i.test(cssClasses.join(" ")) && isValidCnj(number)));
    return {
      number, related, isHeader: !related && (mainLabel || heading), isResultRegion: resultRegion,
      labels: safeLabels(`${associatedText} ${header}`), elementTypes: [...new Set(elementTypes)].slice(0, 5),
      cssClasses: [...new Set(cssClasses)].slice(0, 6), labelRelation,
    };
  }

  function resolveMainProcess(candidates, requestedNumbers = [], manualNumber = null) {
    const requested = [...new Set(requestedNumbers.map(canonical).filter(Boolean))];
    if (requested.length > 1) return { ok: false, code: "ambiguous_search", message: "A pesquisa contém mais de um número CNJ; confira a consulta antes de capturar." };

    const invalidHeader = candidates.find((item) => item.isHeader && item.number && !isValidCnj(item.number) && !item.related);
    if (invalidHeader) return { ok: false, code: "invalid_cnj", message: "O número CNJ do cabeçalho do resultado é inválido; a captura foi bloqueada." };

    const main = [...new Set(candidates
      .filter((item) => item.isHeader && !item.related && isValidCnj(item.number))
      .map((item) => canonical(item.number)))];
    if (manualNumber) {
      const selected = canonical(manualNumber);
      const eligible = [...new Set(candidates.filter((item) => item.isResultRegion && !item.related && isValidCnj(item.number))
        .map((item) => canonical(item.number)))];
      if (!isValidCnj(selected) || !eligible.includes(selected)) {
        return { ok: false, code: "manual_selection_invalid", message: "O número selecionado não pertence a uma região estruturada de resultado elegível." };
      }
      if (main.length === 1 && main[0] !== selected) {
        return { ok: false, code: "process_mismatch", message: "O processo selecionado diverge do cabeçalho principal identificado; a captura foi bloqueada." };
      }
      if (requested.length === 1 && requested[0] !== selected) {
        return { ok: false, code: "process_mismatch", message: "O processo selecionado diverge do número consultado; a captura foi bloqueada." };
      }
      return { ok: true, processNumber: selected, selectedManually: true };
    }
    if (!main.length) return { ok: false, code: "main_process_not_identified", message: "Não identifiquei com segurança o processo principal no cabeçalho estruturado do resultado. O diagnóstico local pode mostrar candidatos de regiões estruturadas para seleção assistida." };
    if (main.length > 1) return { ok: false, code: "ambiguous_main_process", message: "A página contém mais de um processo principal no cabeçalho; a captura automática foi bloqueada." };
    if (requested.length === 1 && requested[0] !== main[0]) {
      return { ok: false, code: "process_mismatch", message: "O processo principal exibido no resultado diverge do número pesquisado; confira o resultado antes de capturar." };
    }
    return { ok: true, processNumber: main[0] };
  }

  function findNumbers(value) {
    return [...String(value || "").matchAll(pattern)].map((match) => match[0]);
  }

  function parseMovementCells(cells) {
    const tokenPattern = /\b(\d{1,2}\/\d{1,2}\/\d{4}(?:\s+(?:[àa]s\s*)?\d{1,2}:\d{2}(?::\d{2})?)?|\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?)\b/;
    const values = (Array.isArray(cells) ? cells : []).map((cell) => String(cell?.text || "").replace(/\s+/g, " ").trim());
    let dateIndex = -1, parsed = null;
    for (let index = 0; index < values.length; index++) {
      const match = values[index].match(tokenPattern);
      if (!match) continue;
      const original = match[1];
      const br = original.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})(?:\s+(?:[àa]s\s*)?(\d{1,2}):(\d{2})(?::(\d{2}))?)?$/);
      const iso = original.match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?$/);
      let year, month, day, hour, minute, second;
      if (br) { [, day, month, year, hour, minute, second] = br; month = month.padStart(2,"0"); day = day.padStart(2,"0"); }
      else if (iso) [, year, month, day, hour, minute, second] = iso;
      else continue;
      const check = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)));
      if (check.getUTCFullYear() !== Number(year) || check.getUTCMonth() + 1 !== Number(month) || check.getUTCDate() !== Number(day)) continue;
      if (hour && (Number(hour) > 23 || Number(minute) > 59 || Number(second || 0) > 59)) continue;
      const time = hour ? `T${hour.padStart(2,"0")}:${minute}:${second || "00"}` : "";
      parsed = { index, original, token: original, iso: `${year}-${month}-${day}${time}` };
      dateIndex = index; break;
    }
    if (!parsed) return null;
    let timeIndex = -1;
    if (!parsed.iso.includes("T")) {
      timeIndex = values.findIndex((value, index) => index !== dateIndex && /^\s*(?:[àa]s\s*)?\d{1,2}:\d{2}(?::\d{2})?\s*$/.test(value));
      if (timeIndex >= 0) {
        const time = values[timeIndex].match(/(\d{1,2}):(\d{2})(?::(\d{2}))?/);
        if (Number(time[1]) > 23 || Number(time[2]) > 59 || Number(time[3] || 0) > 59) return null;
        parsed.iso += `T${time[1].padStart(2,"0")}:${time[2]}:${time[3] || "00"}`;
        parsed.original += ` ${time[0]}`;
      }
    }
    const name = values.map((value, index) => {
      if (index === timeIndex) return "";
      return index === dateIndex ? value.replace(parsed.token, "").replace(/\b[àa]s\s*$/, "").trim() : value;
    }).filter(Boolean).join(" — ").replace(/\s+/g, " ").trim();
    if (!name || name.length > 2000 || !/[A-Za-zÀ-ÿ]{3}/.test(name) || /^(?:data|hora|movimenta[çc][ãa]o|evento|descri[çc][ãa]o|andamento)$/i.test(name)) return null;
    return { occurred_at: parsed.iso, date_original: parsed.original, name, date_index: dateIndex, time_index: timeIndex };
  }

  function isMovementTable({ headers = "", hint = "", rows = [] } = {}) {
    const headingCue = /moviment|evento|hist[oó]rico|andamento|descri/i.test(`${headers} ${hint}`);
    if (headingCue) return true;
    const rowTexts = (Array.isArray(rows) ? rows : []).map((row) => (Array.isArray(row.cells) ? row.cells : []).map((cell) => cell.text || "").join(" "));
    const datedRows = rowTexts.filter((text) => parseMovementCells([{ text }]) || /\b\d{1,2}\/\d{1,2}\/\d{4}\b|\b\d{4}-\d{2}-\d{2}\b/.test(text));
    return datedRows.length >= 2 && /juntad|expedi|conclus|prazo|remet|distribu|senten|decis|intima|certid|arquiv|despacho|peti[çc][ãa]o|publica|audi[eê]ncia/i.test(rowTexts.join(" "));
  }

  function safeLabels(value) {
    const text = String(value || "").slice(0, 500);
    return [...new Set(SAFE_LABELS.filter(([pattern]) => pattern.test(text)).map(([, label]) => label))].slice(0, 6);
  }

  function sanitizeDiagnostic(input) {
    const source = input && typeof input === "object" ? input : {};
    const allowedCodes = new Set(["unsupported_page", "human_verification", "main_process_not_identified", "ambiguous_main_process", "ambiguous_search", "process_mismatch", "invalid_cnj", "manual_selection_invalid", "movement_table_not_recognized", "ambiguous_movement_table", "no_movements", "identity_parser_missing", "capture_ready"]);
    const rawCandidates = Array.isArray(source.candidates) ? source.candidates : [];
    const safeTags = new Set(["ARTICLE", "SECTION", "DIV", "H1", "H2", "H3", "H4", "P", "SPAN", "TR", "TD", "TH", "DL", "DT", "DD", "HEADER", "MAIN", "FORM", "TABLE", "LI"]);
    const candidates = rawCandidates.slice(0, 30).map((item) => ({
      number: isValidCnj(item.number) ? String(item.number).slice(0, 25) : "CNJ inválido",
      element_types: [...new Set((Array.isArray(item.elementTypes) ? item.elementTypes : []).filter((tag) => safeTags.has(tag)))].slice(0, 5),
      css_classes: [...new Set((Array.isArray(item.cssClasses) ? item.cssClasses : []).filter((name) => /^[a-zA-Z_-][a-zA-Z0-9_-]{0,39}$/.test(name) && !/(?:token|cookie|session|sessao|cpf|cnpj|senha|auth|user|usuario)/i.test(name)))].slice(0, 6),
      label_relation: ["same_element", "same_field", "same_row", "sibling", "header", "none"].includes(item.labelRelation) ? item.labelRelation : "none",
      labels_near_candidate: safeLabels((Array.isArray(item.labels) ? item.labels : []).join(" ")),
      in_header: Boolean(item.isHeader), in_result_region: Boolean(item.isResultRegion),
      related: Boolean(item.related), eligible_for_manual_selection: Boolean(item.isResultRegion && !item.related && isValidCnj(item.number)),
    }));
    const tables = (Array.isArray(source.tables) ? source.tables : []).slice(0, 20).map((item) => ({
      element_type: "TABLE", row_count: Math.max(0, Math.min(10000, Number(item.rowCount) || 0)),
      movement_region: Boolean(item.movementRegion), has_thead: Boolean(item.hasThead), has_tbody: Boolean(item.hasTbody),
      rows: (Array.isArray(item.rows) ? item.rows : []).slice(0, 6).map((row) => ({
        cell_count: Math.max(0, Math.min(50, Number(row.cellCount) || 0)),
        cell_types: [...new Set((Array.isArray(row.cellTypes) ? row.cellTypes : []).filter((tag) => tag === "TD" || tag === "TH"))].slice(0, 10),
        has_colspan: Boolean(row.hasColspan), has_rowspan: Boolean(row.hasRowspan),
        has_date: Boolean(row.hasDate), has_time: Boolean(row.hasTime), link_count: Math.max(0, Math.min(100, Number(row.linkCount) || 0)),
      })),
    }));
    const code = allowedCodes.has(source.rejectionCode) ? source.rejectionCode : "diagnostic_unavailable";
    const messages = {
      unsupported_page: "Abra a consulta processual oficial do TRF4.",
      human_verification: "O portal apresentou uma verificação humana.",
      main_process_not_identified: "O cabeçalho principal não foi identificado com segurança.",
      ambiguous_main_process: "Há mais de um processo principal candidato.", ambiguous_search: "A pesquisa contém mais de um CNJ.",
      process_mismatch: "O CNJ do resultado diverge do CNJ consultado.", invalid_cnj: "O CNJ apresentado no cabeçalho é inválido.",
      manual_selection_invalid: "O número selecionado não pertence a uma região elegível.",
      movement_table_not_recognized: "A tabela de movimentações não foi reconhecida.",
      ambiguous_movement_table: "Há tabelas de movimentações igualmente prováveis.", no_movements: "Nenhuma movimentação foi identificada.",
      identity_parser_missing: "O verificador local da extensão não foi carregado.", diagnostic_unavailable: "Diagnóstico local disponível.",
      capture_ready: "Cabeçalho e estrutura do resultado identificados; nenhum dado foi enviado.",
    };
    return {
      domain: source.domain === "consulta.trf4.jus.br" ? source.domain : "não reconhecido",
      page_type: ["trf4_search", "trf4_result", "human_verification", "unsupported", "unknown"].includes(source.pageType) ? source.pageType : "unknown",
      cnj_candidate_count: Math.max(0, Math.min(10000, Number(source.candidateCount) || 0)),
      header_candidate_count: Math.max(0, Math.min(10000, rawCandidates.filter((item) => item.isHeader).length)),
      result_region_candidate_count: Math.max(0, Math.min(10000, rawCandidates.filter((item) => item.isResultRegion).length)),
      candidates, table_count: Math.max(0, Math.min(10000, Number(source.tableCount ?? source.tables?.length) || 0)),
      tables, movement_region_count: Math.max(0, Math.min(10000, Number(source.movementRegionCount ?? tables.filter((item) => item.movement_region).length) || 0)),
      rejection: { code, message: messages[code] },
    };
  }

  const api = { isValidCnj, canonical, resolveMainProcess, classifyCandidate, findNumbers, parseMovementCells, isMovementTable, safeLabels, sanitizeDiagnostic };
  globalThis.ProcessWatchTrf4Identity = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
