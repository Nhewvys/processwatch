"""Consulta direta e conservadora ao formulário público do eproc/TRF4.

O conector segue somente o fluxo normal do formulário. Desafios de segurança
são retornados como bloqueio de acesso; nenhuma resolução automática é feita.
"""
from __future__ import annotations

import hashlib
import json
import re
import asyncio
import contextlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from ..cnj import format_number, normalize, tribunal
from .contracts import (CollectionRequest, ConnectorError, ConnectorEvidence,
                        ConnectorResult, ConnectorState, SourceCapabilities)
from .trf4_protection import OBSERVE, LABELS, classify

SOURCE_ID = "TRF4-EPROC-DIRECT"
SOURCE_LABEL = "eproc/TRF4"
SEARCH_URL = "https://consulta.trf4.jus.br/trf4/controlador.php?acao=consulta_processual_pesquisa"
ROOT = Path(__file__).resolve().parents[3]


def _challenge(title: str, body: str) -> bool:
    """Legacy text helper; live navigation uses structured DOM observations."""
    value = f"{title} {body}".casefold()
    return any(term in value for term in (
        "verify you are human", "verifique se você é humano",
        "verificação de segurança", "security check",
    ))


def _failure(state, code, message, stage, retryable=False):
    return ConnectorResult(SOURCE_ID, state, found=None,
        coverage={"status": state.value, "complete": False, "stage": stage,
                  "diagnostic": {"type": code, "stage": stage,
                     "observed_at": datetime.now(timezone.utc).isoformat(),
                     "state": state.value, "retryable": retryable}},
        evidence=(ConnectorEvidence(SOURCE_ID, SEARCH_URL, method="Pydoll public form",
                                    note="Consulta interrompida sem contornar controles de acesso."),),
        error=ConnectorError(SOURCE_ID, code, message, state, retryable, stage))


async def _observe_protection(tab, stage):
    response = await tab.execute_script(OBSERVE, return_by_value=True)
    signals = json.loads(response.get('result', {}).get('result', {}).get('value') or '{}')
    diagnostic = classify(signals, stage, await tab.current_url())
    if diagnostic is None:
        return None
    state = ConnectorState(diagnostic['state'])
    return ConnectorResult(SOURCE_ID, state, found=None,
        coverage={'status': state.value, 'complete': False, 'stage': stage,
                  'protection': diagnostic, 'page_opened': True, 'cnj_submitted': stage == 'search'},
        evidence=(ConnectorEvidence(SOURCE_ID, diagnostic['url'],
            retrieved_at=diagnostic['observed_at'], method='DOM observation',
            note=', '.join(diagnostic['evidence'])),),
        error=ConnectorError(SOURCE_ID, diagnostic['type'], LABELS[diagnostic['type']], state, False, stage))


def _decode_capture(raw: str, requested_number: str):
    try:
        capture = json.loads(raw)
    except (TypeError, ValueError):
        return None, "invalid_capture"
    if not isinstance(capture, dict) or not capture.get("ok"):
        return None, str((capture or {}).get("code") or "extraction_failed")
    data = capture.get("data")
    if not isinstance(data, dict):
        return None, "invalid_capture"
    try:
        returned = normalize(data.get("process_number") or "")
    except ValueError:
        return None, "process_identity_unverified"
    if returned != requested_number or tribunal(returned) != "trf4":
        return None, "process_identity_mismatch"
    movements = data.get("movements")
    if not isinstance(movements, list) or not movements:
        return None, "movement_table_not_recognized"
    normalized = []
    for item in movements:
        if not isinstance(item, dict) or not item.get("occurred_at") or not item.get("name"):
            continue
        original_id = item.get("source_id")
        if not original_id:
            original_id = hashlib.sha256(json.dumps(
                [returned, item.get("occurred_at"), item.get("name"), item.get("documents", [])],
                ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        normalized.append({"source": SOURCE_LABEL, "source_id": str(original_id),
                           "occurred_at": item["occurred_at"],
                           "date_original": item.get("date_original"),
                           "name": str(item["name"]),
                           "description": str(item["name"]),
                           "documents": [dict(doc, source=SOURCE_LABEL, metadata_only=True)
                                         for doc in item.get("documents", []) if isinstance(doc, dict)]})
    if not normalized:
        return None, "movement_table_not_recognized"
    pagination = data.get("pagination_status") or "unknown"
    if pagination != "unknown" and pagination != "complete":
        return {"number": returned, "tribunal": "TRF4", "source": SOURCE_LABEL,
                "movements": normalized, "coverage": {"complete": False,
                "pagination_status": pagination, "movement_count": len(normalized)}}, "pagination_incomplete"
    return {"number": returned, "tribunal": "TRF4", "source": SOURCE_LABEL,
            "class": data.get("class"), "subjects": data.get("subjects") or [],
            "court_organ": data.get("court_organ"), "situation": data.get("situation"),
            "movements": normalized,
            "coverage": {"complete": pagination == "complete",
                         "pagination_status": pagination,
                         "movement_count": len(normalized)}}, None


def _safe_capture_diagnostic(raw: str) -> dict | None:
    """Keep only bounded structural evidence when the browser extractor rejects a page."""
    try:
        value = json.loads(raw)
        source = value.get("diagnostic") if isinstance(value, dict) else None
        if not isinstance(source, dict):
            return None
        rejection = source.get("rejection") if isinstance(source.get("rejection"), dict) else {}
        allowed_labels = {"Originário", "Processo principal", "Processo", "Relacionado", "Vinculado",
                          "Referência", "Classe processual", "Classe", "Agravo de Instrumento",
                          "Agravo Interno", "Apelação", "Mandado de Segurança", "Procedimento Comum",
                          "Recurso Inominado", "Região de movimentações"}
        candidates = []
        for item in source.get("candidates", [])[:20]:
            if not isinstance(item, dict):
                continue
            candidates.append({
                "element_types": [tag for tag in item.get("element_types", []) if tag in {
                    "ARTICLE", "SECTION", "DIV", "H1", "H2", "H3", "H4", "P", "SPAN", "TR", "TD", "TH",
                    "DL", "DT", "DD", "HEADER", "MAIN", "FORM", "TABLE", "LI"}][:5],
                "labels_near_candidate": [label for label in item.get("labels_near_candidate", [])
                                          if label in allowed_labels][:6],
                "label_relation": item.get("label_relation") if item.get("label_relation") in {
                    "same_element", "same_field", "same_row", "sibling", "header", "none"} else "none",
                "in_header": bool(item.get("in_header")),
                "in_result_region": bool(item.get("in_result_region")),
                "related": bool(item.get("related")),
            })
        tables = []
        for item in source.get("tables", [])[:12]:
            if not isinstance(item, dict):
                continue
            tables.append({"element_type": "TABLE",
                           "row_count": max(0, min(10000, int(item.get("row_count", 0) or 0))),
                           "movement_region": bool(item.get("movement_region")),
                           "has_thead": bool(item.get("has_thead")),
                           "has_tbody": bool(item.get("has_tbody"))})
        return {
            "domain": "consulta.trf4.jus.br" if source.get("domain") == "consulta.trf4.jus.br" else "não reconhecido",
            "page_type": source.get("page_type") if source.get("page_type") in {
                "trf4_search", "trf4_result", "human_verification", "unsupported", "unknown"} else "unknown",
            "cnj_candidate_count": max(0, min(10000, int(source.get("cnj_candidate_count", 0) or 0))),
            "header_candidate_count": max(0, min(10000, int(source.get("header_candidate_count", 0) or 0))),
            "result_region_candidate_count": max(0, min(10000, int(source.get("result_region_candidate_count", 0) or 0))),
            "table_count": max(0, min(10000, int(source.get("table_count", 0) or 0))),
            "movement_region_count": max(0, min(10000, int(source.get("movement_region_count", 0) or 0))),
            "candidates": candidates, "tables": tables,
            "rejection": {"code": str(rejection.get("code", "diagnostic_unavailable"))[:80],
                          "message": str(rejection.get("message", "Diagnóstico estrutural da página."))[:180]},
        }
    except (TypeError, ValueError, AttributeError):
        return None


def _safe_destination(value: str | None) -> str | None:
    """Strip query, fragment, and userinfo from a browser-observed URL."""
    try:
        parsed = urlsplit(value or "")
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        path = re.sub(r"(?i)(?:\b\d{7,}\b|[A-Fa-f0-9]{32,})", "[redacted]", parsed.path)[:180]
        keys = sorted({key[:40] for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
        query_keys = "?" + "&".join(f"{key}=<redacted>" for key in keys[:12]) if keys else ""
        return f"{parsed.scheme}://{parsed.hostname}{path}{query_keys}"
    except (TypeError, ValueError):
        return None


_FORM_OBSERVER = r"""(() => {
  const d = window.__pwTrf4Diag = window.__pwTrf4Diag || {events:[], errors:[], mutationCount:0, addedTags:[]};
  if (d.installed) return true;
  d.installed = true;
  for (const type of ['input','change','blur','click','submit']) document.addEventListener(type, event => {
    const el = event.target;
    const item = {type, tag: el?.tagName || 'unknown', id: ['selFormaI','txtValorI','botaoEnviar'].includes(el?.id) ? el.id : '',
      input_type: ['INPUT','BUTTON'].includes(el?.tagName) ? String(el.type || '').slice(0,20) : '',
      value_length: typeof el?.value === 'string' ? Math.min(el.value.length, 100) : null,
      default_prevented: Boolean(event.defaultPrevented)};
    d.events.push(item); if (d.events.length > 40) d.events.shift();
    if (type === 'submit') setTimeout(() => { item.default_prevented = Boolean(event.defaultPrevented); }, 0);
  }, true);
  window.addEventListener('error', event => {
    const text = String(event.message || '');
    const kind = /TypeError/.test(text) ? 'TypeError' : /ReferenceError/.test(text) ? 'ReferenceError' : /SyntaxError/.test(text) ? 'SyntaxError' : 'script_error';
    d.errors.push({kind, line:Number(event.lineno)||null, column:Number(event.colno)||null, source:safeUrl(event.filename)}); if (d.errors.length > 20) d.errors.shift();
  }, true);
  window.addEventListener('unhandledrejection', event => {
    const text = String(event.reason?.name || '');
    const kind = ['TypeError','ReferenceError','SyntaxError'].includes(text) ? text : 'promise_rejection';
    d.errors.push({kind}); if (d.errors.length > 20) d.errors.shift();
  });
  new MutationObserver(records => {
    d.mutationCount += records.length;
    for (const record of records) for (const node of record.addedNodes || [])
      if (node.nodeType === 1 && d.addedTags.length < 20) d.addedTags.push(String(node.tagName || '').slice(0,20));
  }).observe(document.documentElement, {subtree:true,childList:true,attributes:true});
  function safeUrl(value) { try { const u=new URL(value,location.href); return u.hostname==='consulta.trf4.jus.br'?u.origin+u.pathname.slice(0,160):u.origin; } catch { return null; } }
  return true;
})()"""


_FORM_STATE = r"""JSON.stringify((() => {
  const select=document.querySelector('#selFormaI'), input=document.querySelector('#txtValorI'), button=document.querySelector('#botaoEnviar');
  const form=input?.form || button?.form || select?.form || null;
  const safePath=v=>{try{return new URL(v,location.href).pathname.slice(0,180)}catch{return null}};
  const messages=[];
  for (const el of document.querySelectorAll('[role=alert],.error,.erro,.alert,.mensagem,.validation,.invalid-feedback')) {
    const t=(el.innerText||'').toLocaleLowerCase('pt-BR'); if(!t.trim()) continue;
    const kind=/captcha|verifica.{0,15}seguran|cloudflare/.test(t)?'access_check':/obrigat|preench|informe/.test(t)?'required_field':/inv[aá]lid|incorret|formato/.test(t)?'invalid_input':/n[aã]o encontr|nenhum resultado|sem resultado/.test(t)?'no_results':'portal_message';
    if(!messages.includes(kind)) messages.push(kind);
  }
  const d=window.__pwTrf4Diag||{};
  const navigation=performance.getEntriesByType('navigation')[0];
  return {page:{url:safePath(location.href),title_category:/captcha|cloudflare|verify|verifica/i.test(document.title)?'security_or_verification':document.querySelector('#selFormaI')?'search_form':'other',
      form_count:document.forms.length,iframe_count:document.querySelectorAll('iframe').length,table_count:document.querySelectorAll('table').length,
      candidate_cnj_count:(document.body?.innerText.match(/\b\d{7}-\d{2}\.\d{4}\.4\.04\.\d{4}\b/g)||[]).length,
      document_response_status:Number(navigation?.responseStatus)||null,document_response_received:Boolean(navigation&&navigation.responseStart>0)},
    form:form?{id:['formConsulta','frmConsulta'].includes(form.id)?form.id:'',method:String(form.method||'get').toUpperCase(),action:safePath(form.action),target:['','_self','_blank'].includes(form.target)?(form.target||'_self'):'other',has_onsubmit:typeof form.onsubmit==='function'}:null,
    controls:[select,input,button].filter(Boolean).map(el=>({id:el.id,tag:el.tagName,type:String(el.type||'').slice(0,20),name:['selFormaI','txtValorI','botaoEnviar'].includes(el.id)?String(el.name||'').slice(0,40):'',required:Boolean(el.required),disabled:Boolean(el.disabled),visible:Boolean(el.offsetWidth||el.offsetHeight||el.getClientRects().length),selected_mode:el.id==='selFormaI'?el.value:null,value_length:typeof el.value==='string'?Math.min(el.value.length,100):null})),
    messages:messages.slice(0,10),events:(d.events||[]).slice(-30),js_errors:(d.errors||[]).slice(-10),dom_mutations:Math.min(Number(d.mutationCount)||0,10000),added_element_types:(d.addedTags||[]).slice(0,20)};
})())"""


class _SubmissionTrace:
    """Bounded, sanitized CDP and DOM trace for one public form submission."""
    def __init__(self):
        self.events = []
        self.network_events = []
        self.callback_ids = []
        self.browser_callback_ids = []
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.url_before = None
        self.url_after = None
        self.network_error = None
        self.submit_started = False
        self.submit_action_completed = False
        self.form_submit_event_confirmed = False
        self.fallback_submit_attempted = False
        self.network_log_offset = 0
        self.form_initial = None
        self.form_before_submit = None

    def _append(self, item):
        if len(self.events) < 60:
            self.events.append(item)

    def _append_network(self, item):
        if self.submit_started and len(self.network_events) < 40:
            self.network_events.append(item)

    def mark_submit(self, network_log_offset=0):
        self.network_events.clear()
        self.network_log_offset = network_log_offset
        self.submit_started = True

    async def install(self, browser, tab):
        from pydoll.protocol.network.events import NetworkEvent
        from pydoll.protocol.page.events import PageEvent
        from pydoll.protocol.runtime.events import RuntimeEvent
        from pydoll.protocol.target.events import TargetEvent
        from pydoll.commands.target_commands import TargetCommands

        await tab.enable_network_events()
        await tab.enable_page_events()
        await tab.enable_runtime_events()
        with contextlib.suppress(Exception):
            await browser.execute_command(TargetCommands.set_discover_targets(True))
        await tab.execute_script(_FORM_OBSERVER, return_by_value=True)
        self.form_initial = await self.form_state(tab)

    async def form_state(self, tab):
        response = await tab.execute_script(_FORM_STATE, return_by_value=True)
        raw = response.get('result', {}).get('result', {}).get('value')
        try:
            return json.loads(raw or '{}')
        except (TypeError, ValueError):
            return {'diagnostic_state':'dom_observation_unavailable'}

        def requested(event):
            p = event.get('params', {})
            req = p.get('request', {})
            dest = _safe_destination(req.get('url'))
            if dest:
                self._append_network({'event':'request','method':str(req.get('method',''))[:12],
                              'destination':dest,'resource_type':str(p.get('type',''))[:20]})

        def responded(event):
            p = event.get('params', {})
            response = p.get('response', {})
            dest = _safe_destination(response.get('url'))
            if dest:
                self._append_network({'event':'response','destination':dest,
                              'status':int(response.get('status',0) or 0),
                              'resource_type':str(p.get('type',''))[:20]})

        def navigated(event):
            frame = event.get('params', {}).get('frame', {})
            dest = _safe_destination(frame.get('url'))
            if dest:
                self._append({'event':'navigation','destination':dest,
                              'main_frame':not bool(frame.get('parentId'))})

        def js_exception(event):
            details = event.get('params', {}).get('exceptionDetails', {})
            text = str(details.get('text', ''))
            kind = next((name for name in ('TypeError','ReferenceError','SyntaxError') if name in text), 'script_error')
            self._append({'event':'javascript_error','kind':kind,
                          'line':max(0,int(details.get('lineNumber',0) or 0)),
                          'column':max(0,int(details.get('columnNumber',0) or 0)),
                          'source':_safe_destination(details.get('url'))})

        def target_created(event):
            target = event.get('params', {}).get('targetInfo', {})
            dest = _safe_destination(target.get('url'))
            self._append({'event':'new_target','target_type':str(target.get('type',''))[:20],
                          'destination':dest})

        self.callback_ids.extend([
            await tab.on(NetworkEvent.REQUEST_WILL_BE_SENT, requested),
            await tab.on(NetworkEvent.RESPONSE_RECEIVED, responded),
            await tab.on(PageEvent.FRAME_NAVIGATED, navigated),
            await tab.on(RuntimeEvent.EXCEPTION_THROWN, js_exception),
        ])
        try:
            self.browser_callback_ids.append(await browser.on(TargetEvent.TARGET_CREATED, target_created))
        except Exception:
            pass

    async def snapshot(self, tab, number):
        self.url_after = _safe_destination(await tab.current_url())
        page_state = await self.form_state(tab)
        observation = await _observe_search_submission(tab, number)
        page_state['field_matches_requested_cnj'] = bool(observation.get('search_value_matches'))
        portal_responses = [e for e in self.network_events
                            if e.get('event') == 'response' and
                            e.get('destination', '').startswith('https://consulta.trf4.jus.br/')]
        portal_requests = [e for e in self.network_events
                           if e.get('event') == 'request' and
                           e.get('destination', '').startswith('https://consulta.trf4.jus.br/')]
        safe_network_log = []
        try:
            logs = await tab.get_network_logs()
            for item in logs[self.network_log_offset:self.network_log_offset + 40]:
                params = item.get('params', {}) if isinstance(item, dict) else {}
                request = params.get('request', {})
                destination = _safe_destination(request.get('url'))
                if not destination:
                    continue
                safe_network_log.append({'event':'request',
                    'method':str(request.get('method',''))[:12], 'destination':destination,
                    'resource_type':str(params.get('type',''))[:20]})
        except Exception:
            pass
        logged_portal_requests = [e for e in safe_network_log
                                  if e['destination'].startswith('https://consulta.trf4.jus.br/')]
        if logged_portal_requests and not portal_requests:
            portal_requests = logged_portal_requests
        portal_document_requests = [e for e in portal_requests
                                    if e.get('resource_type') == 'Document' and
                                    e.get('destination', '').startswith('https://consulta.trf4.jus.br/trf4/controlador.php')]
        document_response_received = bool((page_state.get('page') or {}).get('document_response_received'))
        document_response_status = (page_state.get('page') or {}).get('document_response_status')
        return {'started_at':self.started_at,'url_before_submit':self.url_before,
                'url_after_submit':self.url_after,'page_state':page_state,
                'form_before_search_mode_change':self.form_initial,
                'form_immediately_before_submit':self.form_before_submit,
                'events':self.events[:60], 'submit_action_completed':self.submit_action_completed,
                'form_submit_event_confirmed':self.form_submit_event_confirmed,
                'submit_confirmed':bool(portal_document_requests),
                'fallback_submit_attempted':self.fallback_submit_attempted,
                'portal_request_confirmed':bool(portal_document_requests),
                'portal_server_response_received':bool(portal_responses or
                                                        (portal_document_requests and document_response_received)),
                'portal_response_statuses':([e['status'] for e in portal_responses[:10]] or
                                            ([document_response_status] if document_response_status else [])),
                'network_events':(self.network_events + safe_network_log)[:40]}

    async def close(self, browser, tab):
        for callback_id in self.callback_ids:
            with contextlib.suppress(Exception):
                await tab.remove_callback(callback_id)
        for callback_id in self.browser_callback_ids:
            with contextlib.suppress(Exception):
                await browser.remove_callback(callback_id)


async def _attach_protection_trace(result, trace, tab, number):
    diagnostic = await trace.snapshot(tab, number)
    if diagnostic.get('portal_request_confirmed'):
        # The portal document was replaced by the observed security page after
        # the confirmed GET. Chrome did not expose the HTTP status in this run.
        diagnostic['portal_server_response_received'] = True
        diagnostic['server_response_evidence'] = 'Página de desafio renderizada após a requisição; status HTTP não capturado.'
        result.coverage['cnj_submitted'] = True
    result.coverage['form_diagnostic'] = diagnostic
    return result


async def _observe_search_submission(tab, requested_number: str) -> dict:
    """Return only booleans/counts about the submitted public search page."""
    script = """JSON.stringify((() => {
      const body = document.body?.innerText || '';
      const value = (document.querySelector('#txtValorI')?.value || '').replace(/\\D/g, '');
      const cnjs = body.match(/\\b\\d{7}-\\d{2}\\.\\d{4}\\.4\\.04\\.\\d{4}\\b/g) || [];
      return {search_form_present: !!document.querySelector('#selFormaI, #txtValorI, #botaoEnviar'),
        search_value_matches: value === '__REQUESTED__',
        requested_number_visible: body.replace(/\\D/g, '').includes('__REQUESTED__'),
        cnj_candidate_count: cnjs.length, table_count: document.querySelectorAll('table').length,
        form_count: document.forms.length};
    })())""".replace("__REQUESTED__", requested_number)
    response = await tab.execute_script(script, return_by_value=True)
    raw = response.get("result", {}).get("result", {}).get("value")
    try:
        observed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"state": "observation_unavailable"}
    return {"state": "observed",
            "search_form_present": bool(observed.get("search_form_present")),
            "search_value_matches": bool(observed.get("search_value_matches")),
            "requested_number_visible": bool(observed.get("requested_number_visible")),
            "cnj_candidate_count": max(0, min(10000, int(observed.get("cnj_candidate_count", 0) or 0))),
            "table_count": max(0, min(10000, int(observed.get("table_count", 0) or 0))),
            "form_count": max(0, min(10000, int(observed.get("form_count", 0) or 0)))}


class EprocTRF4Connector:
    source_id = SOURCE_ID
    capabilities = SourceCapabilities(process_lookup=True, movements=True, pagination=True,
                                      browser_required=True, process_cover=True,
                                      document_metadata=True)

    def __init__(self, *, timeout_seconds=45, browser_factory=None):
        self.timeout_seconds = timeout_seconds
        self.browser_factory = browser_factory

    async def collect(self, request: CollectionRequest) -> ConnectorResult:
        if request.source != SOURCE_ID:
            return _failure(ConnectorState.NOT_VERIFIED, "unsupported_source",
                            "Identificador de fonte não corresponde ao conector eproc/TRF4.", "validation")
        try:
            number = normalize(request.number)
        except ValueError:
            return _failure(ConnectorState.NOT_VERIFIED, "invalid_cnj", "Número CNJ inválido.", "validation")
        if (tribunal(number) != "trf4" or
                (request.tribunal and request.tribunal.casefold() != "trf4") or
                (request.system and request.system.casefold() != "eproc")):
            return _failure(ConnectorState.NOT_VERIFIED, "routing_mismatch",
                            "Este conector aceita somente processos identificados como TRF4.", "validation")
        try:
            from pydoll import Chrome, ChromiumOptions
            from pydoll.constants import PageLoadState
        except ImportError:
            return _failure(ConnectorState.NOT_IMPLEMENTED, "browser_runtime_missing",
                            "Instale a opção Pydoll do ProcessWatch para habilitar a consulta direta.", "browser_start")

        options = ChromiumOptions()
        options.headless = False
        options.start_timeout = min(self.timeout_seconds, 20)
        options.page_load_state = PageLoadState.INTERACTIVE
        browser = self.browser_factory(options=options) if self.browser_factory else Chrome(options=options)
        stage = "browser_start"
        trace = None
        try:
            async with asyncio.timeout(self.timeout_seconds):
              async with browser:
                tab = await browser.start()
                stage = "open_search"
                await tab.go_to(SEARCH_URL, timeout=self.timeout_seconds)
                if urlsplit(await tab.current_url()).hostname != "consulta.trf4.jus.br":
                    return _failure(ConnectorState.UNAUTHORIZED, "unexpected_redirect",
                                    "O portal redirecionou para um endereço diferente da consulta oficial.", stage)
                protection = await _observe_protection(tab, 'form')
                if protection:
                    return protection
                trace = _SubmissionTrace()
                await trace.install(browser, tab)
                stage = "search"
                form_type = await tab.find(id="selFormaI", timeout=8, raise_exc=False)
                field = await tab.find(id="txtValorI", timeout=8, raise_exc=False)
                submit = await tab.find(id="botaoEnviar", timeout=8, raise_exc=False)
                if not form_type or not field or not submit:
                    return _failure(ConnectorState.LAYOUT_INCOMPATIBLE, "form_layout_changed",
                                    "O formulário público mudou e não foi possível identificar seus campos.", stage)
                await form_type.click()
                option = await tab.find(tag_name="option", value="NU", timeout=5, raise_exc=False)
                if not option:
                    return _failure(ConnectorState.LAYOUT_INCOMPATIBLE, "number_search_unavailable",
                                    "O formulário não apresentou a opção observada de pesquisa por número.", stage)
                await option.click()
                # The TRF4 form replaces its search input when the search mode
                # changes. Reacquire it after the change; the earlier element
                # handle becomes detached and typing into it does not populate
                # the current form.
                form_type = await tab.find(id="selFormaI", timeout=8, raise_exc=False)
                field = await tab.find(id="txtValorI", timeout=8, raise_exc=False)
                submit = await tab.find(id="botaoEnviar", timeout=8, raise_exc=False)
                if not form_type or not field or not submit:
                    return _failure(ConnectorState.LAYOUT_INCOMPATIBLE, "form_replaced_unexpectedly",
                                    "O formulário não apresentou os campos após selecionar a busca por número.", stage)
                if form_type.value and form_type.value != "NU":
                    return _failure(ConnectorState.LAYOUT_INCOMPATIBLE, "number_search_mode_not_selected",
                                    "O portal não confirmou o modo de pesquisa por número.", stage)
                await field.clear()
                await field.insert_text(format_number(number))
                submitted_value = await tab.execute_script(
                    "JSON.stringify((document.querySelector('#txtValorI')?.value||'').replace(/\\D/g,'') === "
                    f"'{number}')", return_by_value=True)
                if submitted_value.get("result", {}).get("result", {}).get("value") != "true":
                    return _failure(ConnectorState.LAYOUT_INCOMPATIBLE, "process_number_not_entered",
                                    "O formulário atual não confirmou o número CNJ antes do envio.", stage)
                trace.form_before_submit = await trace.form_state(tab)
                trace.url_before = _safe_destination(await tab.current_url())
                prior_logs = await tab.get_network_logs()
                trace.mark_submit(len(prior_logs))
                await submit.click()
                trace.submit_action_completed = True
                # This portal exposes a native GET form. If the physical click
                # returns without dispatching a DOM click/submit or creating a
                # request, use the browser's standard requestSubmit(button)
                # path once. It preserves constraint validation and the
                # portal's submit handlers; it is not a direct HTTP/API call.
                await asyncio.sleep(0.5)
                post_click_state = await trace.form_state(tab)
                observed_events = post_click_state.get('events') or []
                saw_submit_event = any(item.get('type') == 'submit' for item in observed_events)
                trace.form_submit_event_confirmed = saw_submit_event
                if not saw_submit_event and not trace.network_events:
                    trace.fallback_submit_attempted = True
                    with contextlib.suppress(Exception):
                        await tab.execute_script("""(() => {
                          const button=document.querySelector('#botaoEnviar');
                          const form=button?.form;
                          if (!form || typeof form.requestSubmit !== 'function') return false;
                          form.requestSubmit(button); return true;
                        })()""", return_by_value=True)
                submission_observation = {}
                for _ in range(12):
                    await asyncio.sleep(0.5)
                    protection = await _observe_protection(tab, stage)
                    if protection:
                        return await _attach_protection_trace(protection, trace, tab, number)
                    submission_observation = await _observe_search_submission(tab, number)
                    if (not submission_observation.get("search_form_present") or
                            submission_observation.get("requested_number_visible") or
                            submission_observation.get("table_count", 0) > 0):
                        break
                if urlsplit(await tab.current_url()).hostname != "consulta.trf4.jus.br":
                    failure = _failure(ConnectorState.UNAUTHORIZED, "unexpected_redirect",
                                       "O portal redirecionou após a pesquisa.", stage)
                    failure.coverage['form_diagnostic'] = await trace.snapshot(tab, number)
                    return failure
                if (submission_observation.get("search_form_present") and
                        submission_observation.get("search_value_matches") and
                        not submission_observation.get("requested_number_visible") and
                        submission_observation.get("table_count", 0) == 0):
                    failure = _failure(ConnectorState.LAYOUT_INCOMPATIBLE, "search_response_not_recognized",
                        "O formulário manteve o CNJ preenchido, mas não apresentou uma resposta de pesquisa reconhecível.", stage)
                    failure.coverage["search_submission"] = submission_observation
                    failure.coverage['form_diagnostic'] = await trace.snapshot(tab, number)
                    return failure
                protection = await _observe_protection(tab, stage)
                if protection:
                    return await _attach_protection_trace(protection, trace, tab, number)
                stage = "extract"
                identity = ROOT / "browser-extension" / "processwatch-chrome" / "content" / "trf4_identity.js"
                extractor = ROOT / "browser-extension" / "processwatch-chrome" / "content" / "trf4.js"
                await tab.execute_script(identity.read_text(encoding="utf-8"))
                await tab.execute_script(extractor.read_text(encoding="utf-8"))
                response = await tab.execute_script(
                    "JSON.stringify(globalThis.__processWatchCapture(null))", return_by_value=True)
                raw = response.get("result", {}).get("result", {}).get("value")
                data, issue = _decode_capture(raw, number)
                if issue == "pagination_incomplete":
                    return ConnectorResult(SOURCE_ID, ConnectorState.PARTIAL, found=True, data=data,
                        coverage=data["coverage"], limitations=("O portal indica que ainda há movimentações a carregar; a captura não é completa.",))
                if issue:
                    state = ConnectorState.PROCESS_NOT_FOUND if issue == "process_not_found" else ConnectorState.LAYOUT_INCOMPATIBLE
                    failure = _failure(state, issue, "Não foi possível validar a identidade e a estrutura dos movimentos retornados.", stage)
                    diagnostic = _safe_capture_diagnostic(raw)
                    if diagnostic:
                        failure.coverage["extractor_diagnostic"] = diagnostic
                    if submission_observation:
                        failure.coverage["search_submission"] = submission_observation
                    failure.coverage['form_diagnostic'] = await trace.snapshot(tab, number)
                    return failure
                data['coverage']['navigation_state'] = 'process_page_validated'
                if data["coverage"]["pagination_status"] != "complete":
                    data['coverage']['form_diagnostic'] = await trace.snapshot(tab, number)
                    return ConnectorResult(SOURCE_ID, ConnectorState.PARTIAL, found=True, data=data,
                        coverage=data["coverage"], limitations=("O extrator não confirmou o fim da paginação; resultado parcial.",))
                data['coverage']['form_diagnostic'] = await trace.snapshot(tab, number)
                return ConnectorResult(SOURCE_ID, ConnectorState.OPERATIONAL, found=True, data=data,
                    coverage=data["coverage"], evidence=(ConnectorEvidence(
                        SOURCE_ID, SEARCH_URL, method="Pydoll public form",
                        fields=("process_number", "movements", "document_metadata"),
                        note="Consulta direta; conteúdo original do portal validado pelo extrator TRF4."),))
        except TimeoutError:
            return _failure(ConnectorState.TIMEOUT, "timeout", "Tempo limite na consulta pública do TRF4.", stage, True)
        except Exception as exc:
            name = type(exc).__name__.casefold()
            detail = str(exc).casefold()
            if "429" in detail or "rate limit" in detail:
                return _failure(ConnectorState.RATE_LIMITED, "rate_limited", "O portal limitou temporariamente a consulta.", stage)
            if any(word in detail for word in ("connection", "network", "dns", "socket", "empty response")):
                return _failure(ConnectorState.COURT_UNAVAILABLE, "network_error", "Falha de conexão com a consulta pública do TRF4.", stage, True)
            return _failure(ConnectorState.COURT_UNAVAILABLE, "browser_error",
                            f"Falha do navegador ({name or 'erro'}); detalhes técnicos foram omitidos.", stage, True)
        finally:
            if trace is not None:
                with contextlib.suppress(Exception):
                    await trace.close(browser, tab)
