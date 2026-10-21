# Changelog

O ProcessWatch ainda não possui releases formais publicadas. `0.1.0` em `pyproject.toml` é a versão declarada do pacote, não uma publicação/tag confirmada. Este histórico registra a evolução pré-lançamento documentada no código, nos catálogos e nos relatórios consolidados; não é resultado de uma nova homologação de fontes.

## Desenvolvimento pré-lançamento — até outubro de 2026

### Dados e monitoramento

- Motor Python com SQLite, consulta DataJud por número CNJ, persistência de metadados e movimentações, CLI e interface FastAPI.
- Fila persistente de coleta, worker, estados de execução, retentativas e agendas por processo.
- Cliente DJEN para comunicações por processo, busca OAB/UF, paginação/janelas de consulta, descoberta e monitoramento recorrente persistente.
- Caixa de comunicações com estado interno lida/não lida. Esse estado é acompanhamento no ProcessWatch; não equivale a ciência jurídica formal.
- Catálogos versionados e integração local de códigos SGT/TPU. O catálogo instalado pode ser parcial; códigos não verificados não sustentam inferências.

### Consolidação processual

- Normalização de participantes e evidências por fonte, com separação de partes, advogados, destinatários e outras funções.
- Reconciliação de movimentos entre fontes, preservando detalhes, origem e estado de leitura dos eventos lógicos.
- Metadados consolidados por atributo, com conflito/proveniência explícitos quando a evidência não permite escolher um valor.
- Catálogo de fases com versão `2026-10-15.1` e regras conservadoras de situação. Não substitui decisão jurídica nem cálculo de prazo.

### Fontes e cobertura

- Provider DataJud e cliente DJEN integrados.
- PJe/TRF1 integrado para o escopo estreito `TRF1-PJE-JE` marcado como validado na matriz de 10/10/2026; `TRF1-PJE-1G` é compatibilidade de formulário sem validação independente equivalente.
- Fluxo eproc/TRF4 direto investigado, mas não considerado operacional diante da verificação observada. Extensão Chrome mantém importação assistida separada.
- Outras combinações de tribunal/sistema/grau ficam não verificadas ou não operacionais conforme `data/sources/courts.json`.

### Contas e administração

- Autenticação por contas e vínculos de escritório, sessões, funções de `office_admin` e `lawyer`, recuperação manual de conta e credenciais de serviço com permissões limitadas.
- Identidade global separada de usuários de escritório; owner bootstrap, administradores globais delegados, APIs e área `/platform-admin`.
- APIs de provisionamento, cadastro e ciclo de vida de escritórios, usuários, processos, jobs, integrações e auditoria.
- CLI cliente para operações existentes da API; comandos locais de bootstrap e recuperação do owner permanecem procedimentos de host.
- Proteções de ambiente e do escritório reservado `local` em produção.

### Segurança e distribuição

- Swagger/ReDoc com recursos locais e CSP restritiva; scripts da interface servidos localmente.
- Configuração de implantação com exemplos systemd, Nginx/HTTPS e backup verificado por SQLite.
- Build Python com setuptools e comandos `processwatch` e `processwatch-backup` registrados.
- Schema incremental atual: v18. O banco operacional não é incluído no pacote Python.

## Limitações conhecidas

- A disponibilidade e a cobertura dependem de cada fonte, combinação de tribunal e condições de acesso. A matriz é a referência da cobertura verificada.
- A integração direta automatizada PJe/TRF1 não possui validação ampla para todos os graus; TRF4 e demais conectores investigados não devem ser anunciados como coleta automática operacional sem atualização da evidência.
- Não há SMTP para envio automático de recuperação. Não há cálculo automático de prazos processuais, serviço de notificação ou cancelamento cooperativo de job em execução.
- A documentação de implantação fornece modelos; uma VPS real requer configuração e operação do host.
