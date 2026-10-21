"""Read-only browser observations; never interacts with security challenges."""
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .contracts import ConnectorState

OBSERVE = r"""JSON.stringify((() => {
 const visible = e => !!e && !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
 const any = s => Array.from(document.querySelectorAll(s)).some(visible);
 const text = (document.body?.innerText || '').toLowerCase();
 const frames = Array.from(document.querySelectorAll('iframe'));
 return {
   turnstile_frame: frames.some(e => visible(e) && /^https:\/\/challenges\.cloudflare\.com\//.test(e.src) && /turnstile/.test(e.src)),
   turnstile_container: any('.cf-turnstile'),
   turnstile_response_field: !!document.querySelector('[name="cf-turnstile-response"]'),
   cf_challenge_form: any('#challenge-form, #cf-challenge-running, #challenge-running'),
   cf_challenge_config: typeof window._cf_chl_opt === 'object',
   cf_challenge_script: Array.from(document.scripts).some(e => /\/cdn-cgi\/challenge-platform\//.test(e.src)),
   verification_message: /verify you are human|verifique se você é humano|verificação de segurança|verificando.*navegador|verifying you are human|checking your browser/.test(text),
   challenge_title: /just a moment|um momento|attention required/i.test(document.title),
   access_denied: /sorry, you have been blocked|access denied|acesso negado|acesso bloqueado/.test(text),
   cf_block_marker: /cloudflare ray id/.test(text),
   portal_captcha: any('input[name*="captcha" i]:not([type="hidden"]), input[id*="captcha" i]:not([type="hidden"])') && any('img[src*="captcha" i], img[id*="captcha" i]'),
   authentication_form: any('input[type="password"]'),
   search_form: any('#selFormaI') && any('#txtValorI') && any('#botaoEnviar')
 };
})())"""

LABELS = {
 'cloudflare_turnstile': 'Cloudflare Turnstile: widget presente; interação automática não autorizada para este portal.',
 'cloudflare_managed_challenge': 'Página de desafio gerenciado pelo Cloudflare; consulta interrompida.',
 'access_restricted': 'O portal apresentou restrição de acesso.',
 'portal_captcha': 'O formulário apresentou CAPTCHA próprio do portal.',
 'authentication_required': 'O portal apresentou formulário de autenticação.',
 'unknown_challenge': 'Verificação detectada, mas sem evidência suficiente para identificar o mecanismo.',
}

def classify(signals, stage, url, *, http_status=None):
    """Passive assets alone are not evidence of an active challenge."""
    evidence = sorted(k for k, v in signals.items() if v is True and k in {
        'turnstile_frame', 'turnstile_container', 'turnstile_response_field',
        'cf_challenge_form', 'cf_challenge_config', 'cf_challenge_script',
        'verification_message', 'challenge_title', 'access_denied', 'cf_block_marker',
        'portal_captcha', 'authentication_form', 'search_form'})
    s = set(evidence)
    kind = None
    state = ConnectorState.CAPTCHA
    if http_status in (401, 403) or 'access_denied' in s:
        kind, state = 'access_restricted', ConnectorState.UNAUTHORIZED
    elif 'turnstile_frame' in s or {'turnstile_container', 'turnstile_response_field'} <= s:
        kind = 'cloudflare_turnstile'
    elif (s & {'cf_challenge_form', 'cf_challenge_config'}) and (s & {'challenge_title', 'verification_message', 'cf_challenge_script'}):
        kind = 'cloudflare_managed_challenge'
    elif 'portal_captcha' in s:
        kind = 'portal_captcha'
    elif 'authentication_form' in s:
        kind, state = 'authentication_required', ConnectorState.AUTHENTICATION_REQUIRED
    elif s & {'verification_message', 'challenge_title'}:
        kind, state = 'unknown_challenge', ConnectorState.HUMAN_VERIFICATION
    if kind is None:
        return None
    parsed = urlsplit(url)
    return {'type': kind, 'stage': stage,
            'url': f'{parsed.scheme}://{parsed.hostname}{parsed.path}',
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'evidence': evidence, 'http_status': http_status,
            'state': state.value, 'retryable': False,
            'pydoll_compatibility': 'widget_detected_not_validated' if kind == 'cloudflare_turnstile' else 'not_established',
            'automatic_interaction_authorized': False}
