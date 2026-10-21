# Administração, API e CLI

Este guia descreve contratos implementados em `api.py`, `cli.py` e `api_cli.py`. `/openapi.json` é a referência final dos campos e respostas disponíveis na versão em execução.

## Autenticação

Há três contextos distintos:

| Contexto | Como autentica | Escopo |
| --- | --- | --- |
| Conta de escritório (`accounts`) | `POST /api/auth/login`; cookie HttpOnly `processwatch_session` | Escritórios vinculados à conta, um ativo por sessão |
| Credencial de serviço | `Authorization: Bearer pwsvc_…` | Um escritório e permissões concedidas à chave |
| Administrador global | `POST /api/platform/auth/login`; cookie HttpOnly `processwatch_platform_session` | Operações `/api/platform/*` |

O modo legado `token` continua disponível para instalação local e usa `PROCESSWATCH_ACCESS_TOKEN`. Ele não concede acesso às rotas globais. Em produção, `PROCESSWATCH_ENV=production` exige `PROCESSWATCH_AUTH_MODE=accounts`, cookie Secure e origem HTTPS. Requisições de escrita com sessão cookie precisam enviar `Origin` válido; a API não remove essa proteção para clientes externos.

Chaves de serviço são criadas somente por administrador autorizado. O token completo é devolvido na criação/rotação e não pode ser consultado depois; o banco guarda hash. Permissões implementadas: `processes:read`, `communications:read`, `oab:read`, `oab:manage`, `sync:request`, `jobs:read`, `communications:read_state:write`. Rotas não explicitamente autorizadas para chaves são negadas.

Exemplo de login por contas e consulta no PowerShell:

```powershell
$base = 'http://127.0.0.1:8000'
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$login = @{ email = 'admin@example.test'; password = 'SENHA_DIGITADA_LOCALMENTE' } | ConvertTo-Json
Invoke-RestMethod "$base/api/auth/login" -Method Post -ContentType 'application/json' -Body $login -WebSession $session
Invoke-RestMethod "$base/api/processes" -WebSession $session
```

Use senha real somente no prompt/local seguro; exemplos não são credenciais. Para integração entre servidores, use HTTPS e uma chave de serviço de menor privilégio:

```powershell
$headers = @{ Authorization = "Bearer $env:PROCESSWATCH_SERVICE_TOKEN" }
Invoke-RestMethod 'https://api.exemplo.interno/api/processes' -Headers $headers
```

## Grupos de rotas

As rotas listadas abaixo são registradas em `api.py`; detalhes de parâmetros e modelos podem ser consultados em `/docs` e `/openapi.json`.

### Saúde e acesso

| Método e rota | Finalidade |
| --- | --- |
| `GET /api/health` | Estado básico, configuração não secreta e indicação do scheduler da instância |
| `GET /api/scheduler/status` | Agendas e fila observadas no escritório ativo; exige autenticação normal |
| `GET /api/auth/mode` | Informa o modo configurado |
| `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/auth/logout` | Sessão de escritório |
| `POST /api/platform/auth/login`, `GET /api/platform/auth/me`, `POST /api/platform/auth/logout` | Sessão global |
| `POST /api/auth/recovery/request`, `POST /api/auth/recovery/complete` | Solicitação neutra e conclusão com token de recuperação de uso único |

O endpoint público de recuperação não envia e-mail: não há transporte SMTP integrado. Um administrador emite token por rota autenticada e o entrega fora do sistema por canal privado.

### Escritório, usuários e perfil

| Método e rota | Finalidade |
| --- | --- |
| `GET/PUT /api/office` | Consultar/atualizar cadastro do escritório ativo; escrita de administrador |
| `POST /api/office/activate`, `POST /api/office/deactivate`, `DELETE /api/office` | Ciclo de vida iniciado por `office_admin`; DELETE é exclusão definitiva e destrutiva |
| `GET/POST /api/admin/users`, `GET/PATCH/DELETE /api/admin/users/{user_id}` | Administração de usuários do escritório |
| `POST /api/admin/users/{user_id}/recovery` | Emitir token privado de recuperação |
| `GET/PUT /api/profile`, `POST /api/profile/password`, `DELETE /api/profile` | Perfil, troca de senha e encerramento da própria conta |
| `POST /api/auth/office` | Selecionar escritório ao qual a conta já está vinculada; não cria escritório |
| `GET/POST /api/service-credentials`, `POST /api/service-credentials/{id}/revoke`, `POST /api/service-credentials/{id}/rotate` | Gestão de chaves do escritório |

Os corpos de `PUT /api/office` incluem `name`, `email`, `phone`, `address`. O PATCH de usuário é parcial; campos omitidos preservam o valor. O servidor protege o último administrador ativo nas operações de vínculo/conta.

### Processos, eventos e comunicações

| Método e rota | Finalidade |
| --- | --- |
| `GET/POST /api/processes` | Listar e cadastrar processos no escritório |
| `GET /api/processes/overview`, `/api/processes/activity`, `/api/reports/summary` | Visão geral e atividade |
| `GET /api/processes/{number}/schedule`, `PUT /api/processes/{number}/schedule` | Consultar/alterar agenda recorrente do processo |
| `POST /api/processes/{number}/check`, `POST /api/processes/check-all` | Solicitar atualização |
| `GET /api/processes/{number}/timeline`, `/events`, `/runs`, `/sync` | Linha do tempo e histórico de coleta |
| `GET /api/processos/{number}`, `/partes`, `/movimentacoes`, `/intimacoes`, `/timeline` | Dados consolidados e fontes |
| `GET /api/processos/{number}/eventos-nao-lidos`; `POST .../ler` e `.../ler-todos` | Acompanhamento interno de leitura |
| `GET /api/communications`, `GET /api/communications/{number}/{source_id}`, `PATCH .../read` | Central de comunicações, detalhe e estado de leitura |
| `GET/POST /api/calendar`, `PATCH /api/calendar/{item_id}` | Itens do calendário do escritório |

Os parâmetros de filtro dependem da rota. Use `/docs` para o contrato exato da versão instalada, inclusive paginação e datas.

### Descoberta e monitoramento OAB

| Método e rota | Finalidade |
| --- | --- |
| `GET /api/discovery/capabilities` | Capacidades disponíveis |
| `POST /api/discovery/oab/search`, `GET /api/discovery/oab/results` | Pesquisa manual e leitura de resultados descobertos |
| `POST /api/discovery/oab/add` | Inclusão manual de processos selecionados na carteira |
| `POST/GET /api/monitoramentos/oab` | Criar/listar monitoramentos persistentes |
| `GET/PATCH/DELETE /api/monitoramentos/oab/{monitor_id}` | Consultar/alterar/remover monitoramento |
| `POST /api/monitoramentos/oab/{monitor_id}/atualizar` | Enfileirar atualização manual |
| `GET /api/monitoramentos/oab/{monitor_id}/comunicacoes` | Consultar comunicações relacionadas |

O monitoramento recorrente roda no worker sem a interface aberta. Pesquisa, publicação, cadastro de processo e ciência jurídica formal não são a mesma coisa. O estado `read` é interno ao ProcessWatch e não inicia prazo jurídico.

### Administração global

Todas as rotas `/api/platform/*` exigem cookie de sessão de administrador global válido. A autenticação não aceita sessão de escritório, token legado nem chave de serviço como substitutos.

| Grupo | Rotas principais |
| --- | --- |
| Autenticação global | `POST/GET /api/platform/auth/login`, `/me`, `/logout`; `POST /api/platform/auth/password` |
| Administradores globais | `GET/POST /api/platform/admins`; `GET/PATCH /api/platform/admins/{admin_id}`; status, sessões e redefinição delegada |
| Visão e escritórios | `GET /api/platform/overview`, `GET /api/platform/offices`, `GET/PATCH /api/platform/offices/{office_id}`, `PATCH .../status`, `POST /api/platform/offices` |
| Usuários de escritórios | `GET /api/platform/users`, rotas `/api/platform/offices/{office_id}/users...` |
| Jobs e integrações | `GET /api/platform/jobs`, `/api/platform/jobs/{job_kind}/{job_id}`, retries e `GET /api/platform/integrations` |
| Processos globais | `GET /api/platform/processes`, `GET /api/platform/processes/{office_id}/{number}`, ação explícita de refresh |
| Auditoria | `GET /api/platform/audit` |

Leituras globais de processos não exigem `purpose`; ficam auditadas automaticamente. Solicitar nova coleta é ação operacional e continua exigindo justificativa. A telemetria do worker pode retornar `unknown` quando a instância não possui probe em tempo real; um job antigo não prova que o worker está ativo.

## CLI

O executável Python é `processwatch`; `python -m processwatch` é equivalente para comandos do motor. O comando `processwatch-backup` é instalado separadamente pelo pacote. Use `processwatch --help` e os `--help` dos subcomandos para ver a sintaxe exata.

### Motor local

```text
processwatch [--db CAMINHO] [--org ESCRITORIO] add CNJ [--interval SEGUNDOS] [--no-fetch]
processwatch import-list ARQUIVO --portfolio NOME [--interval SEGUNDOS] [--fetch]
processwatch sync-djen CNJ [--start AAAA-MM-DD] [--end AAAA-MM-DD]
processwatch discover-oab OAB UF --start AAAA-MM-DD --end AAAA-MM-DD
processwatch reprocess-djen CNJ
processwatch check CNJ | timeline CNJ [--revisions] [--timezone FUSO]
processwatch runs CNJ | events CNJ [--after ID] | list | check-all | run-due
processwatch watch [--poll SEGUNDOS] [--djen]
processwatch web [--host 127.0.0.1] [--port 8000]
processwatch sources
```

Catálogo: `tpu-update`, `tpu-import`, `tpu-import-sheet`, `tpu-reprocess`, `tpu-show`. Reconciliação de eventos: `reconcile CNJ` e `review-link CNJ ID1 ID2 confirmed|rejected`. Consulte cada ajuda antes de executar uma operação que acesse fonte ou altere dados.

### Conta de escritório e chaves

```text
processwatch config show | config set-url ORIGEM
processwatch login [--url ORIGEM] | logout | whoami
processwatch office show | update [--name ... --email ... --phone ... --address ...]
processwatch users list | show ID | create | update ID | activate ID | deactivate ID | delete ID
processwatch account show | update | change-password | delete
processwatch api-key create | list | rotate ID | revoke ID
```

Login pede senha sem eco. No Windows, a sessão é guardada no Gerenciador de Credenciais. A persistência da sessão administrativa da CLI usa essa integração do Windows; para outros sistemas, use um cliente HTTP apropriado ou integração por chave de serviço. O comando global `processwatch platform-admin login` também usa armazenamento de credenciais Windows.

### Administração da plataforma

```text
processwatch platform-admin bootstrap --db CAMINHO [--email EMAIL]
processwatch platform-admin reset-owner-password --db CAMINHO [--email EMAIL]
processwatch platform-admin login [--url ORIGEM] | logout | whoami | change-password
processwatch platform-admin offices list | create | show ID | update ID | activate ID | deactivate ID
processwatch platform-admin users [--office-id ID] list
processwatch platform-admin users show|create|update|activate|deactivate|unlink|issue-recovery ...
processwatch platform-admin admins list|create|show|update|activate|deactivate|revoke-sessions|reset-password ...
processwatch platform-admin jobs [FILTROS] [show|retry KIND ID]
processwatch platform-admin integrations [--office-id ID]
processwatch platform-admin processes [--office-id ID] [--query TEXTO]
processwatch platform-admin processes refresh OFFICE CNJ
processwatch platform-admin oab-monitor refresh OFFICE MONITOR_ID
processwatch platform-admin audit
```

Provisionamento, redefinição e ações destrutivas pedem confirmação/senha quando apropriado. Operações HTTP exigem sessão global; `bootstrap` e `reset-owner-password` são procedimentos locais que abrem o SQLite apontado e devem ser executados somente no servidor com caminho verificado. Não passe senhas por argumentos.

## Swagger e OpenAPI

Na instalação local, use `/docs`, `/redoc` e `/openapi.json` na origem do servidor. Os bundles Swagger UI/ReDoc são servidos de `/assets/docs/`, para cumprir a CSP sem CDN. Os esquemas de autenticação declarados devem refletir o middleware real. O Swagger não cria credenciais: use uma sessão ou chave de serviço já autorizada e nunca salve segredo em captura compartilhada.
