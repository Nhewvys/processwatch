# Administração global da plataforma

O papel `platform_admin` administra a instalação inteira, mas é uma identidade separada dos usuários de escritório. O primeiro owner é criado localmente por operador autorizado. Administradores globais delegados são criados pelo owner; um `office_admin`, a credencial de serviço ou o escritório legado `local` não recebe privilégios globais.

## Primeiro acesso e recuperação do owner

No servidor, configure o ambiente e use o banco novo ou banco escolhido deliberadamente:

```bash
processwatch platform-admin bootstrap --db /var/lib/processwatch/processwatch.sqlite3
```

O comando inicializa o repositório/schemas suportados, pede e confirma a senha sem eco e cria o owner se ainda não existir. Não execute sobre um banco operacional sem confirmar caminho e política de backup. Após o bootstrap, o owner autentica em `/platform-admin` ou na CLI global.

Se o owner perder acesso, a senha só pode ser recuperada por procedimento local, com acesso autorizado ao host e ao banco:

```bash
processwatch platform-admin reset-owner-password --db /var/lib/processwatch/processwatch.sqlite3
```

O comando exige confirmação do e-mail, solicita nova senha sem eco e revoga sessões anteriores. Não existe recuperação pública da identidade proprietária.

## Escritórios

As rotas globais são autenticadas por cookie `processwatch_platform_session`:

| Operação | Endpoint |
| --- | --- |
| Indicadores | `GET /api/platform/overview` |
| Listar e consultar | `GET /api/platform/offices`, `GET /api/platform/offices/{office_id}` |
| Provisionar escritório e primeiro administrador | `POST /api/platform/offices` |
| Editar cadastro parcialmente | `PATCH /api/platform/offices/{office_id}` |
| Ativar/desativar | `PATCH /api/platform/offices/{office_id}/status` com `{"active": true|false}` |

O identificador é imutável em edição. A alteração de estado é independente de usuários e preserva dados; regras operacionais ainda podem impedir a desativação enquanto há trabalho incompatível em execução. O escritório `local` é um namespace legado, não o owner. Em `production`, o backend bloqueia seu uso operacional e o omite das consultas globais de carteira.

O endpoint de edição aceita campos cadastrais entre `name`, `email`, `phone` e `address`. Somente campos enviados mudam; use string vazia para limpar contato opcional. Edição não ativa o escritório, não cria usuários e não mexe em processos.

## Usuários e vínculos

`GET /api/platform/users` lista contas e seus vínculos; `office_id` filtra. Para um escritório específico, as rotas ficam sob `/api/platform/offices/{office_id}/users`. O POST cria conta nova ou vincula uma identidade existente. O PATCH distingue campos do vínculo (`role`, `active`, `display_name`) de `account_active`, que afeta a conta em todos os escritórios. DELETE nessas rotas remove o vínculo daquele escritório, não apaga automaticamente vínculos de outros.

A proteção do último administrador ativo é aplicada nas operações de gestão de contas/vínculos. Essa regra não impede o owner global de ativar ou desativar um escritório que não possua administrador humano. Para recuperação de acesso de uma conta de escritório, as rotas `.../recovery` emitem token de uso único por prazo limitado; não há envio automático de e-mail.

## Identidades globais

Rotas em `/api/platform/admins` permitem listar, consultar, criar, atualizar e alterar estado de identidades globais, revogar sessões e redefinir senha de administrador delegado. O owner bootstrap não pode ser removido ou desativado por delegados. Somente o owner redefine senha de delegado; a API não redefine senha do owner. Mudanças de credenciais revogam sessões quando aplicável.

## Processos e operação

- `GET /api/platform/processes` pesquisa processos entre os escritórios disponíveis, com filtro de escritório e texto.
- `GET /api/platform/processes/{office_id}/{number}` consulta detalhes, histórico e comunicações.
- Leituras são auditadas automaticamente e não pedem `purpose`.
- Solicitar coleta é uma ação operacional diferente: `POST /api/platform/processes/{office_id}/{number}/refresh` exige propósito e cria/enfileira trabalho conforme as regras do motor.
- `GET /api/platform/jobs` tem filtros e paginação; `GET /api/platform/jobs/{job_kind}/{job_id}` retorna detalhes e histórico. Retry só é admitido para job falho compatível e não apaga a execução anterior.
- `GET /api/platform/integrations` resume verificações persistidas, erros sanitizados e telemetria quando disponível. Sem probe em tempo real, worker pode aparecer como `unknown`; uma execução antiga não prova que está ativo.
- `GET /api/platform/audit` consulta os registros administrativos.

Não há cancelamento forçado seguro de job em execução. A API não inicia nem encerra processos do sistema operacional. Consulte [Administração, API e CLI](administracao-api-cli.md) para contratos e comandos.

## Auditoria e segurança

Consultas globais e operações administrativas registram ator, ação, escopo, recurso e contexto disponíveis. Segredos não devem ser incluídos em finalidade, logs ou comandos. A API global não aceita sessão de escritório ou credenciais de serviço como autorização. Mudanças de estado e escrita por cookie seguem validação de origem/CSRF implementada no middleware.

## Desativar, encerrar e excluir

Ativar/desativar é reversível e preserva registros. Não existe endpoint global de exclusão definitiva de escritório nesta etapa. `DELETE /api/office` é a rota do próprio escritório e destrói dados vinculados, sujeitos a checagens de jobs, chaves estrangeiras e políticas internas. Trate-a como operação irreversível e não a use para arquivamento temporário.

## Referências

- Contrato HTTP e CLI: [Administração, API e CLI](administracao-api-cli.md)
- Ambientes, migrações e recuperação: [Preparação de produção](preparacao-producao.md)
- Fluxo interno: [Arquitetura](arquitetura.md)
