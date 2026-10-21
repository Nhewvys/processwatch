# Implantação, backup e manutenção

Este é um guia para preparar uma instalação Linux usando os modelos em `deploy/`. Não há instalador automático nem deploy remoto implementado. Revise os caminhos e o domínio antes de habilitar os serviços.

## Perfil local e hospedado

`PROCESSWATCH_ENV` aceita `principal`, `development`, `test`, `homologation` e `production`. O perfil local preserva compatibilidade com o escritório reservado `local`. O perfil `production` exige `PROCESSWATCH_AUTH_MODE=accounts`, origem HTTPS, cookies Secure e caminho explícito do banco; o backend bloqueia o escritório `local`. Não importe automaticamente a base local para produção.

Uma instalação hospedada começa com banco novo. Escritórios e carteiras reais são criados pelo provisionamento global autenticado. A identidade global é inicializada separadamente por `processwatch platform-admin bootstrap`; não use conta de escritório como owner.

## Configuração

O processo lê variáveis de ambiente. `Config.from_env()` também lê `.env` do diretório atual usando a biblioteca padrão; variáveis já definidas no processo prevalecem. A unit systemd usa `EnvironmentFile=/etc/processwatch/processwatch.env`, portanto a produção não depende de `.env` no diretório do código.

**Não compartilhe nem versione o arquivo de produção.** Restrinja permissões e preencha, conforme necessário:

```dotenv
PROCESSWATCH_ENV=production
PROCESSWATCH_AUTH_MODE=accounts
PROCESSWATCH_DB=/var/lib/processwatch/processwatch.sqlite3
PROCESSWATCH_PRIMARY_DB=/var/lib/processwatch/processwatch.sqlite3
PROCESSWATCH_SESSION_COOKIE_SECURE=1
PROCESSWATCH_SESSION_DAYS=14
PROCESSWATCH_SESSION_IDLE_MINUTES=60
PROCESSWATCH_PUBLIC_ORIGIN=https://processwatch.example.com
DATAJUD_API_KEY=
DJEN_AUTO_SYNC=0
```

`.env.production.example` é um modelo sem segredo. Não defina token legado como mecanismo de login hospedado. Defina chaves externas somente no host autorizado.

## Arquivos de implantação

- `deploy/systemd/processwatch.service` inicia `processwatch web --host 127.0.0.1 --port 8000`. A API e o worker rodam juntos.
- `deploy/nginx/processwatch.conf.example` redireciona HTTP para HTTPS e encaminha o tráfego local ao Uvicorn.
- `deploy/systemd/processwatch-backup.service` chama `processwatch-backup` com origem e destino explícitos.
- `deploy/systemd/processwatch-backup.timer` agenda backup diário.

Os arquivos usam `/opt/processwatch`, `/var/lib/processwatch`, `/var/backups/processwatch` e usuário de sistema `processwatch`. Se os caminhos mudarem, atualize service, environment file, proxy e política de acesso em conjunto. Mantenha Uvicorn acessível somente pelo proxy local; não exponha a porta 8000 diretamente.

## Instalação de referência

Os comandos abaixo descrevem a configuração esperada pelos arquivos `deploy/`; execute-os em uma VPS sob controle do operador:

```bash
sudo useradd --system --home /opt/processwatch --shell /usr/sbin/nologin processwatch
sudo install -d -o processwatch -g processwatch /opt/processwatch /var/lib/processwatch
sudo install -d -o root -g processwatch -m 0750 /etc/processwatch
sudo install -d -o processwatch -g processwatch -m 0700 /var/backups/processwatch
sudo -u processwatch python3 -m venv /opt/processwatch/.venv
sudo -u processwatch /opt/processwatch/.venv/bin/pip install ".[web]"
```

Coloque o código/pacote aprovado em `/opt/processwatch`, configure `/etc/processwatch/processwatch.env`, instale os modelos systemd e o proxy HTTPS, então crie o primeiro owner apontando explicitamente ao banco novo:

```bash
sudo -u processwatch /opt/processwatch/.venv/bin/processwatch platform-admin bootstrap \
  --db /var/lib/processwatch/processwatch.sqlite3
```

O bootstrap pede a senha de forma interativa. Em seguida, habilite as units revisadas e autentique em `/platform-admin` para provisionar escritórios. Nenhum comando deste guia foi executado durante a documentação.

## Backup consistente

SQLite em modo WAL pode ter transações confirmadas ainda no arquivo `-wal`. Não copie apenas o arquivo `.sqlite3` enquanto o serviço estiver ativo. Use o backup nativo:

```bash
/opt/processwatch/.venv/bin/processwatch-backup \
  --database /var/lib/processwatch/processwatch.sqlite3 \
  --directory /var/backups/processwatch
```

O utilitário usa a API de backup SQLite, verifica `integrity_check` e `foreign_key_check` antes de publicar o backup e exige caminhos absolutos. Guarde backups em volume separado/restrito e aplique retenção fora da aplicação. Faça também cópia fora do host conforme a política de continuidade.

## Restauração

1. Planeje janela de manutenção e pare serviço API/worker com encerramento controlado; confirme que não há job em execução.
2. Preserve o banco atual e os arquivos auxiliares como cópia de diagnóstico.
3. Valide o backup selecionado em cópia, com `PRAGMA integrity_check`, `PRAGMA foreign_key_check` e `PRAGMA user_version`.
4. Restaure o backup validado para o caminho configurado, com permissões do usuário `processwatch`.
5. Inicie o serviço e confira health, autenticação, consultas locais e logs antes de reabrir o uso.

Não misture o banco principal com arquivos WAL/SHM de outra cópia. A substituição do banco deve ser feita com o serviço parado, usando a cópia completa produzida pela API do SQLite.

## Atualização do código e migrações

O schema atual é v18. Ao abrir um banco antigo, o repositório executa migrações incrementais e verifica backup antes de aplicar a atualização. Para atualização operacional:

1. Revise a versão do código e migrações.
2. Interrompa API e worker em janela controlada.
3. Produza e valide backup independente.
4. Faça ensaio em cópia fiel do banco.
5. Compare escritórios, usuários, processos, movimentos, comunicações, monitoramentos, jobs e históricos.
6. Inicie a versão nova e valide `user_version`, `integrity_check`, `foreign_key_check`, login e endpoints necessários.
7. Preserve o backup anterior até encerrar a janela de observação.

Nunca use banco operacional em testes. O validador de caminhos reconhece ambiente e banco primário para bloquear configurações inseguras, mas isso não substitui confirmar o caminho real antes de iniciar um processo.

## Manutenção e diagnóstico

- `GET /api/health`: saúde básica da API e configuração sem segredos.
- `GET /api/scheduler/status`: agendas e fila visíveis no escopo atual.
- `GET /api/platform/jobs` e `/api/platform/integrations`: diagnóstico global com sessão `platform_admin`.
- `processwatch sources`: matriz de fontes instalada.
- `processwatch list`, `timeline`, `runs` e `events`: inspeção local da carteira selecionada.
- `processwatch-backup`: criar backup verificado.

Logs de processo vão para journal quando os modelos systemd são usados. Verifique primeiro a causa e o estado do banco; não apague jobs, WAL ou registros históricos como tentativa genérica de recuperação.

## Limites

Os modelos não configuram DNS, certificados, firewall, SMTP, monitoramento externo, armazenamento fora do host nem política de retenção. A API de recuperação de conta não envia e-mail. A disponibilidade das fontes e as autorizações para consulta recorrente devem ser verificadas com cada provedor. O ambiente de produção exige operação e monitoramento do host por sua equipe.
