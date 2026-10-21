# Arquitetura e fluxo de dados

Este guia descreve o caminho que os dados percorrem no ProcessWatch. O motor é um pacote Python independente; a interface web, a CLI e a API usam os mesmos serviços e repositório.

## Componentes

| Componente | Responsabilidade |
| --- | --- |
| `config.py` | Carrega configuração do processo e de `.env` no diretório atual. |
| `cli.py` / `api_cli.py` | Comandos locais do motor e cliente da API administrativa. |
| `api.py` | API FastAPI, autenticação, autorização, auditoria e páginas web. |
| `engine.py` | Fluxo de aplicação que coordena registro e consulta de processos. |
| `repository.py` | Persistência SQLite, migrações, escopo por escritório e consultas. |
| `orchestrator.py`, `jobs.py`, `worker.py` | Fila persistente, agendamento, leases, tentativas e execução. |
| `providers/` | Adaptadores DataJud, DJEN e integrações de tribunal. |
| `consolidation.py`, `participants.py`, `event_reconciliation.py` | Normalização e visão de dados com proveniência. |
| `web/` | Interface de escritório, interface global e recursos estáticos locais. |

## Fluxo de uma coleta

1. Um processo ou monitoramento OAB é cadastrado ou atualizado por uma ação autorizada.
2. O orquestrador determina as fontes aplicáveis e grava jobs no SQLite. A requisição HTTP retorna sem aguardar uma consulta longa.
3. O worker adquire um job com lease e chama os providers habilitados.
4. Respostas brutas e dados normalizados são persistidos com identificadores, horário e fonte. Um erro de fonte não apaga a última coleta válida.
5. A consolidação combina movimentos, comunicações, snapshots e metadados no contexto do processo.
6. A API serializa os registros para a interface ou para clientes autorizados. A UI não deve inferir papel jurídico a partir de texto sem evidência explícita.

O comando `processwatch web` inicia FastAPI e um worker em segundo plano no mesmo processo. `processwatch watch` executa o worker sem interface; `processwatch run-due` faz uma passagem pontual pelos processos vencidos. Na configuração systemd fornecida, o serviço `web` executa API e worker juntos. Não inicie uma segunda cópia contra o mesmo banco sem planejar concorrência e ownership da fila.

## Banco e isolamento

O repositório utiliza um único arquivo SQLite configurado por `PROCESSWATCH_DB` ou `--db`. Escritórios e carteiras são linhas e escopos nesse banco compartilhado; não existe um arquivo SQLite por escritório. Processos, movimentos, comunicações, agendas, jobs, usuários e credenciais mantêm o identificador do escopo conforme o modelo.

O modo hospedado bloqueia o escritório reservado `local` no backend e usa contas vinculadas a escritórios. `--org` em comandos locais escolhe um namespace, mas não substitui autenticação nem protege contra acesso direto ao arquivo SQLite. A identidade `platform_admin` e suas sessões ficam separadas de usuários e vínculos de escritório.

O schema suportado pelo código é v18. O repositório aplica migrações incrementais, com backup e verificação implementados para bancos persistentes antigos. Consulte os procedimentos antes de abrir uma base operacional com uma versão diferente do código.

## APIs de fonte e persistência

Cada provider tem capacidade própria. DataJud fornece os registros que sua API disponibiliza para o CNJ consultado; DJEN fornece comunicações e resultados de busca por OAB; conectores de tribunal só podem ser considerados disponíveis para as combinações declaradas na matriz e comprovadas por validação. Uma capability declarada não equivale a consulta bem-sucedida.

As coletas guardam evidências suficientes para rastrear fonte e resultado. A resposta bruta usada para reconciliação pode ficar no banco. O produto não deve expor payload bruto ou identificadores internos como se fossem informação jurídica interpretada.

## Interfaces e observabilidade

A interface principal é servida por `/`; a administração global independente é servida por `/platform-admin`. Ambos usam os recursos estáticos do pacote. `/docs`, `/redoc` e `/openapi.json` apresentam o contrato HTTP. Respostas incluem métricas locais de duração da requisição e consultas SQLite nos cabeçalhos `Server-Timing` e `X-ProcessWatch-*`; isso mede uma requisição, não a saúde histórica de uma integração.

Detalhes dos providers e regras de consolidação estão em [Fontes e condições](fontes-e-condicoes.md) e [Consolidação processual](consolidacao-processual.md). Operação e implantação estão em [Preparação de produção](preparacao-producao.md).
