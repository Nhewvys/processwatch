# Histórico técnico consolidado

Este documento reúne decisões e evidências descritas ao longo do desenvolvimento do ProcessWatch. É um registro histórico: números de testes, consultas e contagens abaixo dos relatórios antigos não são uma validação executada nesta consolidação. A árvore atual não contém histórico Git acessível; quando a data não está registrada no código ou em um artefato preservado, o texto não inventa uma data ou versão.

## Escopo do produto

O projeto começou como motor Python local de acompanhamento de processos públicos e evoluiu para uma aplicação com API, interface, fila de jobs, contas por escritório e administração global. A interface é consumidora da API; o motor, a CLI e o worker usam os mesmos serviços e repositório. O banco SQLite é compartilhado fisicamente entre escritórios, com dados separados pelo escopo de escritório. O escritório legado `local` não é identidade administrativa global.

## Evolução técnica

### Núcleo de processos

O núcleo mantém processos por CNJ, consulta DataJud quando configurado, persiste metadados e movimentos e permite inspeção pela CLI. A arquitetura usa providers separados do domínio, repositório SQLite e configuração por ambiente. O pacote inclui o schema suportado e aplica migrações incrementais; o schema atual no código é v18.

### Fila e execução recorrente

A execução foi separada do ciclo HTTP por uma fila SQLite com jobs, estados, leases e retentativas. A inclusão de processo enfileira a coleta sem esperar por consultas externas. `processwatch web` inicia API e worker em conjunto; `watch` executa o worker isoladamente; `run-due` é uma passagem pontual. Agendas por processo começam desabilitadas até ativação. A telemetria operacional distingue probe atual de último registro histórico.

### DJEN e OAB

Foi introduzido cliente DJEN para buscar comunicações por processo e OAB/UF, com filtros temporais e paginação. A pesquisa manual de descoberta é distinta do cadastro persistente de monitoramento. Monitoramentos ativos são processados pelo worker e comunicações podem ser armazenadas antes de o CNJ ser incluído na carteira. A associação posterior é separada da criação do processo. Leitura/não leitura é um estado interno e não uma afirmação de ciência jurídica ou início de prazo.

### PJe/TRF1 e expansão de fontes

O conector `PJe/TRF1` extrai dados da consulta pública por meio de fluxo de navegador configurável. A matriz do projeto registra validação real restrita a `TRF1-PJE-JE` em 10/10/2026. O código também admite uma combinação de primeiro grau por compatibilidade do formulário; isso não amplia a evidência de validação. O sistema interrompe ou registra estado de acesso quando a fonte exige interação.

eproc/TRF4 e outros portais foram examinados. As tentativas diretas do TRF4 encontraram mecanismo de verificação e não produziram dados suficientes para classificar o conector como operacional. Uma extensão Chrome foi criada como importação explicitamente assistida pelo usuário. TJSP/e-SAJ, Projudi/TJPR e outras combinações permanecem bloqueadas, investigadas ou não verificadas conforme `data/sources/courts.json`.

### Consolidação e proveniência

As iterações de timeline levaram à manutenção de eventos lógicos com fontes múltiplas, datas de evento/primeira observação/leitura e histórico preservado. A consolidação evita eliminar eventos apenas por coincidência temporal; identidade textual e temporal é tratada por regras de reconciliação.

Participantes também passaram a ser construídos como identidades e menções com categorias e evidências distintas. A regra de produto essencial é preservar o nome e a proveniência, sem converter destinatário automaticamente em autor ou réu nem classificar advogado como parte por padrão. A API separa listas de partes, profissionais e destinatários. Mapeamentos desconhecidos permanecem sem significado atribuído.

Metadados de processo são resolvidos por atributo e contexto. A fonte não vence universalmente por nome; datas comparáveis, escopo e concordância influenciam a decisão. Conflitos sem evidência suficiente são mantidos como conflitos com candidatos, em vez de esconder os valores de origem. Situação derivada usa catálogo e sinais explicitamente reconhecidos; os códigos TPU 22, 246, 849 e 893 são tratados separadamente. Um nome de movimento não é prova de decisão, prazo ou encerramento jurídico.

### Contas e isolamento

O sistema evoluiu de autenticação local por token para modo de contas com usuários, vínculos por escritório, papéis, sessões e credenciais de serviço. As credenciais de serviço são escopadas e têm permissões. Operações globais usam identidades e sessões próprias (`platform_admin`), sem promover `office_admin` ou o escritório `local` a administrador global.

O ciclo global inclui bootstrap local do owner, administradores delegados, provisionamento de escritório, gestão de vínculos, consulta global de dados, auditoria e consultas operacionais de jobs. O owner não é redefinido por endpoint público. A recuperação é local ao servidor. A interface `/platform-admin` é independente da área de escritório.

### Schema e ambiente

As migrações foram incrementais e o código atual estabelece `PRAGMA user_version=18`. Migrações persistentes têm procedimentos de backup/validação no repositório. A configuração separa `principal`, `development`, `test`, `homologation` e `production`; caminhos de banco são validados para impedir que testes/homologações abram o banco primário. Produção exige contas, HTTPS/cookie seguro e bloqueia o namespace `local` no backend.

## Segurança operacional e recuperação

O utilitário `processwatch-backup` usa a API de backup SQLite, que inclui transações confirmadas do WAL, e valida integridade e foreign keys antes de finalizar o arquivo. Os modelos systemd executam API e worker como serviço e agendam backup diário. A restauração exige parada controlada, validação de cópia, substituição com cópia completa e conferência pós-início. A aplicação não inclui o banco operacional no pacote.

## Evidências preservadas no repositório

- `data/sources/courts.json`: matriz versionada de tribunal, sistema, grau e evidência registrada; última verificação indicada no arquivo: 10/10/2026.
- `data/djen-openapi.yml`: contrato DJEN mantido para desenvolvimento.
- `data/tpu/` e `data/legal/`: catálogos locais versionados; conferir a referência/data dentro de cada documento.
- `tests/`: verificações automatizadas reutilizáveis. Resultados históricos dos relatórios de fase não são repetidos como execução atual.
- `docs/design-reference/Process_Watch_Design_System_v1/`: referência visual extraída preservada.

## Limitações e pendências confirmadas no código

- Não existe transporte SMTP para recuperação automática por e-mail.
- Não existe cálculo jurídico de prazos, distribuição de tarefas ou aviso externo.
- Não existe cancelamento cooperativo de job já em execução.
- Sem probe em tempo real, a API não afirma que worker está ativo somente com base em uma execução passada.
- Cobertura de consulta de tribunal depende da matriz e não é nacionalmente homogênea.
- As condições vigentes das fontes e autorizações devem ser revistas antes de qualquer uso recorrente ou redistribuição.

## Como interpretar o arquivo

`CHANGELOG.md` resume marcos do desenvolvimento pré-lançamento. Este histórico explica as decisões e limites. Para instalar e operar, use o [README](../README.md) e os guias técnicos atuais; relatórios de fases antigos foram consolidados e retirados para evitar instruções concorrentes.
