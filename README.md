# ProcessWatch

O ProcessWatch acompanha processos judiciais por número CNJ, registra movimentações e comunicações disponíveis nas fontes configuradas e apresenta a carteira em uma interface web e em uma API. Ele mantém os dados em SQLite e executa consultas por uma fila persistente. A cobertura varia por fonte, tribunal, sistema, grau e condições de acesso; cadastro ou consulta vazia não prova que um processo ou publicação inexista.

Este manual descreve o código desta distribuição. O pacote declara versão `0.1.0` em `pyproject.toml`; não há indicação de release formal publicado. O schema SQLite atual do código é v18.

## O que está disponível

- Interface web para processos, movimentações, comunicações, monitoramentos e administração.
- CLI para cadastro e consulta operacional, além de operações administrativas pela API.
- Fila SQLite e worker persistente para coletas agendadas.
- Consultas DataJud por CNJ e DJEN por processo e OAB/UF, conforme configuração e disponibilidade das fontes.
- Monitoramentos recorrentes por processo e por OAB/UF.
- Contas por escritório, sessões, credenciais de serviço e administração global separada.
- Auditoria das operações administrativas e backup consistente do SQLite.

A cobertura do PJe/TRF1 é limitada ao que foi validado na matriz do projeto; outros graus podem aparecer como compatíveis sem validação independente. O eproc/TRF4 tem código experimental e fluxo de importação assistida pelo usuário, mas não é uma integração automática homologada. Veja [Fontes e conectores](docs/fontes-e-condicoes.md).

## Requisitos

- Python 3.11 ou superior.
- SQLite incluído na instalação do Python.
- Dependência base `httpx`; FastAPI e Uvicorn são instalados pelo extra `web`.
- Acesso de rede às fontes públicas para consultas externas.
- Chave DataJud configurada para chamadas ao DataJud. O DJEN usa a interface pública documentada no projeto.
- Playwright/Chromium é opcional e usado pelo fluxo de consulta PJe/TRF1 implementado.

## Instalação local no Windows

Abra PowerShell na pasta do projeto. Um ambiente virtual mantém as dependências separadas do Python do sistema:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[web]"
Copy-Item .env.example .env
```

`-e .` instala a CLI ligada ao código local, útil durante desenvolvimento. Para uma instalação fixa, use `python -m pip install .` no ambiente virtual. Ative o ambiente para chamar `processwatch` sem caminho completo:

```powershell
.\.venv\Scripts\Activate.ps1
processwatch --help
```

`Config.from_env()` lê um arquivo `.env` no diretório atual; variáveis já definidas no processo prevalecem. O projeto não carrega automaticamente `.env` de qualquer diretório do usuário. Copie `.env.example`, escolha um banco de desenvolvimento e ajuste os valores necessários. Nunca distribua o arquivo `.env` real.

Para usar contas no ambiente local, configure `PROCESSWATCH_AUTH_MODE=accounts` e crie um administrador de escritório com o script interativo:

```powershell
python .\scripts\create_admin.py --office-id local --office-name "Escritório local" --email admin@example.test
```

O script pede a senha sem exibi-la. Para habilitar a interface, inicie-a em loopback:

```powershell
processwatch web --host 127.0.0.1 --port 8000
```

Abra `http://127.0.0.1:8000`. O comando `web` inicia a API e um worker em segundo plano no mesmo processo. Para executar o worker no terminal sem a interface, use `processwatch watch`; ele precisa permanecer aberto para processar a fila.

Se for usar a consulta PJe/TRF1, instale a dependência extra e o Chromium:

```powershell
python -m pip install -e ".[court-browser]"
python -m playwright install chromium
```

## Instalação hospedada em Linux/VPS

Os exemplos em `deploy/` usam systemd e Nginx. Eles são modelos de implantação; não significam que uma VPS já tenha sido configurada. A API deve ficar em `127.0.0.1`, atrás de proxy HTTPS, com o banco em diretório persistente e restrito ao usuário do serviço.

Instale a partir de uma cópia controlada do código ou pacote aprovado, em ambiente próprio:

```bash
sudo install -d -o processwatch -g processwatch /opt/processwatch /var/lib/processwatch /var/backups/processwatch
sudo -u processwatch python3 -m venv /opt/processwatch/.venv
sudo -u processwatch /opt/processwatch/.venv/bin/pip install ".[web]"
```

Configure `/etc/processwatch/processwatch.env` a partir de `.env.production.example`, preenchendo segredos fora do repositório. A configuração de produção exige `PROCESSWATCH_ENV=production`, `PROCESSWATCH_AUTH_MODE=accounts`, cookies `Secure=1`, origem pública HTTPS e banco absoluto. O perfil hospedado bloqueia o escritório reservado `local`; não copie automaticamente o banco local para a VPS.

O primeiro administrador global é independente de qualquer escritório. Depois de instalar o código e definir o ambiente de produção, o operador autorizado cria essa identidade no banco novo:

```bash
/opt/processwatch/.venv/bin/processwatch platform-admin bootstrap \
  --db /var/lib/processwatch/processwatch.sqlite3
```

O comando solicita e confirma a senha no terminal. A instalação não cria automaticamente usuários, escritórios ou uma carteira `local`. Após o bootstrap, provisionamento de escritórios é feito por um `platform_admin` autenticado. Os arquivos systemd fornecidos executam API e worker juntos; o timer de backup diário é um serviço separado. Consulte [Implantação e operação](docs/preparacao-producao.md).

## Uso básico

Para adicionar um processo e enfileirar a coleta inicial:

```powershell
processwatch add 0000000-54.0000.4.04.0000
```

O número acima é esquemático e deve ser substituído por um CNJ válido autorizado para sua operação. Para só registrar, sem enfileirar, use `--no-fetch`. `list`, `timeline`, `runs`, `events`, `check`, `check-all` e `run-due` consultam a carteira local; algumas dessas operações fazem chamadas externas e devem ser utilizadas conscientemente.

Exemplos adicionais:

```powershell
processwatch sources
processwatch discover-oab 12345 SP --start 2026-10-01 --end 2026-10-10
processwatch sync-djen 0000000-54.0000.4.04.0000 --start 2026-10-01 --end 2026-10-10
processwatch timeline 0000000-54.0000.4.04.0000
```

`discover-oab` é uma pesquisa manual com período limitado; ela não cria um monitoramento recorrente. O monitoramento persistente por OAB é administrado na interface ou pela API. A inclusão manual de processos encontrados continua separada do resultado da pesquisa.

Para os comandos de contas, credenciais, escritórios, usuários e administração da plataforma, consulte [Administração, API e CLI](docs/administracao-api-cli.md) e [Administração da plataforma](docs/administracao-plataforma.md).

## API HTTP

Com a aplicação local, o OpenAPI está em `http://127.0.0.1:8000/openapi.json`, o Swagger em `/docs` e o ReDoc em `/redoc`. Os recursos de documentação são servidos localmente. Os grupos principais são:

| Grupo | Exemplos | Uso |
| --- | --- | --- |
| Saúde e autenticação | `GET /api/health`, `POST /api/auth/login`, `GET /api/auth/me` | Estado básico e sessão de escritório |
| Processos | `GET /api/processes`, `POST /api/processes`, `GET /api/processos/{number}` | Carteira, dados gerais e detalhe |
| Histórico | `/partes`, `/movimentacoes`, `/intimacoes`, `/timeline` | Participantes, movimentos e comunicações |
| Monitoramento | `/api/processes/{number}/schedule`, `/api/monitoramentos/oab` | Agendas por processo e OAB |
| Comunicações | `GET /api/communications` | Caixa de publicações, filtros e leitura |
| Escritório e usuários | `/api/office`, `/api/admin/users` | Administração do escritório autenticado |
| Credenciais de serviço | `/api/service-credentials` | Criar, listar, revogar e rotacionar chaves |
| Administração global | `/api/platform/*` | Operações de `platform_admin`, isoladas das contas dos escritórios |


Exemplo ilustrativo de resposta de `GET /api/health` (valores variam por instalação):

```json
{
  "status": "ok",
  "organization": "demo",
  "default_interval": 86400,
  "datajud_configured": false,
  "scheduler_active": false
}
```
No modo `accounts`, o navegador usa cookies de sessão HttpOnly. Clientes externos podem usar uma credencial de serviço Bearer com permissões limitadas, ou uma sessão de conta quando suportado pelo cliente. As consultas aceitam parâmetros documentados por rota: por exemplo, a caixa `GET /api/communications` filtra por `oab`, `uf`, `number`, `q`, `inicio`, `fim`, `read`, `monitored`, `kind`, `page` e `size`; o contrato vigente está em `/openapi.json`. As rotas globais exigem a sessão administrativa `processwatch_platform_session`; credenciais de escritório, tokens legados e chaves de serviço não concedem acesso global. Acesse a API por HTTPS fora do loopback e não coloque segredos na URL.

Exemplo de leitura autenticada por cookie no ambiente local:

```bash
curl -c cookies.txt -H "Content-Type: application/json" \
  -d '{"email":"admin@example.test","password":"SENHA_FORNECIDA_NO_TERMINAL"}' \
  http://127.0.0.1:8000/api/auth/login
curl -b cookies.txt http://127.0.0.1:8000/api/processes
```

Use um cliente que não registre o corpo de login no histórico compartilhado. Para integração servidor-servidor, prefira uma chave de serviço criada por administrador, com apenas as permissões necessárias. O contrato detalhado e os exemplos PowerShell estão em [Administração, API e CLI](docs/administracao-api-cli.md).

## Segurança, dados e backups

Escritórios e carteiras usam o mesmo arquivo SQLite, com escopo por identificador do escritório. O isolamento da API é aplicado no backend; `--org` é um seletor de contexto para comandos locais, não uma barreira contra quem possui acesso ao arquivo do banco. A identidade global fica em tabelas próprias e não é associada ao escritório `local`.

Use contas em instalações hospedadas. Mantenha segredos fora do repositório, limite acesso ao arquivo de ambiente e ao banco, habilite HTTPS, faça backups consistentes e teste a restauração. Não copie um SQLite ativo com um simples `Copy-Item`: o modo WAL pode conter transações ainda não incorporadas ao arquivo principal. Use `processwatch-backup` ou a API de backup SQLite. Consulte [Implantação e operação](docs/preparacao-producao.md).

## Atualizações e migrações

O código atual suporta schema v18 e aplica migrações incrementais ao abrir o repositório. Antes de atualizar um banco persistente, pare API e worker de forma controlada, crie backup verificável, ensaie a atualização em uma cópia e confira `user_version`, `integrity_check` e `foreign_key_check`. Nunca execute testes contra o banco operacional e não abra o banco principal com código experimental.

## Documentação

- [Arquitetura e fluxo de dados](docs/arquitetura.md)
- [Consolidação processual](docs/consolidacao-processual.md)
- [Fontes e conectores](docs/fontes-e-condicoes.md)
- [Administração, API e CLI](docs/administracao-api-cli.md)
- [Administração da plataforma](docs/administracao-plataforma.md)
- [Implantação, backup e manutenção](docs/preparacao-producao.md)
- [Histórico técnico consolidado](docs/HISTORICO_TECNICO.md)
- [Changelog](CHANGELOG.md)
- [Design System aprovado](docs/design-reference/Process_Watch_Design_System_v1/ESPECIFICACAO.md)

O histórico registra decisões e evidências das etapas anteriores, não um teste executado nesta versão. Para limitações atuais de cada fonte, consulte a matriz de fontes e os contratos do código.




