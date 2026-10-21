# Laboratório local de OCR documental e estados de acesso

Este é um laboratório experimental separado do produto ProcessWatch. Os ambientes virtuais locais usados em experimentos anteriores não acompanham mais o repositório; os comandos abaixo descrevem os scripts preservados e exigem recriar um ambiente e instalar suas dependências antes da execução. O laboratório não contém adaptadores para CAPTCHA de produção, ocultação de automação ou interação com portais judiciais.

## Executar a página de demonstração

Na raiz do projeto, em PowerShell, após preparar um ambiente Python para as dependências do laboratório:

~~~powershell
python -m labs.challenge_lab.server
~~~

Abra http://127.0.0.1:8765. A página principal tem cartões simulados. A página /turnstile-test usa somente as chaves dummy públicas documentadas pela Cloudflare; o widget sempre aprova e não exibe nem supera um desafio real. A rota de validação envia somente o token dummy à API Siteverify oficial de teste.

## Pydoll e Turnstile dummy

Execute a prova Pydoll no Chrome instalado:

~~~powershell
$env:PYTHONPATH = (Get-Location).Path
python .\labs\challenge_lab\pydoll_turnstile_smoke.py
~~~

O caso de timeout navega para uma página local sem widget e verifica que a espera termina e o navegador fecha:

~~~powershell
$env:PYTHONPATH = (Get-Location).Path
python .\labs\challenge_lab\pydoll_turnstile_smoke.py --timeout-test
~~~

## Turnstile interativo oficial de teste

A página `/turnstile-interactive-test` usa a sitekey oficial `3x00000000000000000000FF`, que força o fluxo interativo para teste. Nodriver e SeleniumBase foram exercitados no widget real da Cloudflare, servido no localhost, com validação Siteverify do laboratório em `test_mode`. Isso não comprova acesso a Turnstile de produção nem autorização para automatizar portais judiciais.

Os adaptadores exigem loopback e a sitekey de teste; não fazem parte do motor ProcessWatch. Os ambientes opcionais `nodriver` e `seleniumbase` utilizados anteriormente foram descartados e não acompanham o repositório. Os scripts permanecem como material experimental, mas exigem instalar suas dependências separadamente. Para repetir, na raiz do repositório e com Chrome instalado:

~~~powershell
$env:PYTHONPATH = (Get-Location).Path
$env:PROCESSWATCH_TEST_CHROME = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
python labs\challenge_lab\nodriver_turnstile_interactive.py
python labs\challenge_lab\seleniumbase_turnstile_interactive.py
~~~

A proteção local do adaptador está em [turnstile_local_interaction.py](turnstile_local_interaction.py). O histórico técnico consolidado fica em [HISTORICO_TECNICO](../../docs/HISTORICO_TECNICO.md); este laboratório é independente do motor ProcessWatch.

## reCAPTCHA e hCaptcha em chaves oficiais dummy

O servidor oferece `/recaptcha-test` e `/hcaptcha-test`. Ambos usam somente chaves oficiais de integração automatizada, encaminham o token ao `siteverify` do fornecedor e seguem para uma página local de sucesso após validação. Essas chaves aprovam sem desafio visual. O caso de token inválido é enviado diretamente ao Siteverify e não imprime a resposta secreta.

Com Chrome instalado e conexão à internet:

~~~powershell
$env:PYTHONPATH = (Get-Location).Path
python .\labs\challenge_lab\pydoll_vendor_test_keys.py --provider both
~~~

Use `--provider recaptcha` ou `--provider hcaptcha` para testar um só fluxo. O teste adicional de timeout abre uma página local sem widget e confirma que a espera limitada termina; não representa expiração real. O Google documenta que suas chaves v2 de teste aceitam todas as verificações, enquanto o hCaptcha documenta a rejeição de um token inválido. Os resultados históricos do laboratório não são validação de integração judicial; veja o [histórico técnico consolidado](../../docs/HISTORICO_TECNICO.md).

## OCR de texto comum

O teste cria 20 imagens locais de texto comum, números e digitalizações levemente degradadas. Não gera nem lê imagens CAPTCHA:

~~~powershell
python .\labs\challenge_lab\ocr_benchmark.py
~~~

O executável Tesseract fica em labs\challenge_lab\tesseract; nenhuma dependência foi adicionada ao ambiente principal.

Não instale ddddocr ou PaddleOCR neste laboratório para avaliar ou resolver imagens de CAPTCHA. O conjunto autorizado do Tesseract é texto documental comum, e as páginas de fornecedor usam as chaves dummy, sem desafios.

## Limites

CapSkip e Anti-Captcha não são instalados ou chamados: são solucionadores de barreiras de acesso, e o serviço CapSkip também requer licença comercial. O OCR do laboratório é destinado a texto documental e não deve ser aplicado a desafios de controle de acesso.



