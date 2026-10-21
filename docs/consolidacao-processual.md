# Consolidação processual: identidade, evidência e movimentos

O ProcessWatch conserva registros por fonte e constrói uma visão para a API. A consolidação tenta aproveitar evidências sem transformar ausência de dado em conclusão jurídica. As funções centrais ficam em `participants.py`, `consolidation.py`, `event_reconciliation.py` e `process_phases.py`.

## Participantes

Cada menção mantém nome original, identidade de apresentação, categoria, função/polo quando explicitamente sustentados, proveniência e referências às evidências. `canonical_participants()` coleta dados de snapshots e repositório; `presentation_participants()` agrupa a visão de apresentação. A rota `/api/processos/{number}/partes` separa `partes`, `advogados` e `destinatarios`.

Critérios de identidade são deliberadamente diferentes de papel jurídico:

- Identificadores estáveis iguais, documento igual ou OAB/UF compatível são evidências fortes, sujeitos às validações de conflito implementadas.
- Nome normalizado igual e contexto compatível podem consolidar menções no mesmo processo, conservando todas as referências.
- Similaridade de nome, abreviações e iniciais podem ser candidatos de apresentação conforme as regras do código; não são prova universal de identidade.
- Um advogado é categoria profissional e não deve ser contado como parte só por aparecer na mesma publicação.
- Destinatário é papel na comunicação. Ele só integra uma lista de parte quando há evidência processual separada; a API pode manter a mesma pessoa em mais de uma categoria se as fontes sustentarem ambos os vínculos.

As qualificações do catálogo legal podem produzir função e polo a partir de evidência estruturada ou textual explícita. O código original e a proveniência são preservados. Para `destinatarios[].polo` do DJEN, o mapeamento A/P para o campo específico da API não está confirmado; o valor original fica disponível sem ser convertido automaticamente em polo canônico. Códigos desconhecidos não recebem significado inventado.

Snapshots de tribunal são considerados em seu contexto e a API lê o histórico pertinente de snapshots; isso não significa precedência universal de um provedor para todos os atributos. O resolvedor de metadados compara classe, assunto, distribuição, órgão, grau, valor e flags por atributo, campo original, escopo e data disponível. Valores iguais acumulam proveniência; conflito sem data comparável suficiente permanece visível como conflito e não apaga as evidências.

## Movimentações e situação

`reconcile_movements()` associa movimentos equivalentes quando os critérios temporais e textuais suportam uma identidade lógica, mantendo fontes e detalhes. Descrições distintas no mesmo horário permanecem separadas quando a evidência não permite uni-las. A descrição preferida não substitui a versão original de cada fonte.

`process_phases.py` usa um catálogo de apresentação versionado (atualmente `2026-10-15.1`). Ele combina códigos TPU já verificados no projeto com reconhecedores explícitos de título; não é um catálogo oficial completo de todas as fases. `consolidation.derive_situation()` é uma regra separada que deriva apenas situações a partir de códigos verificados (`22`, `246`, `849`, `893`). Baixa, arquivamento, reativação e desarquivamento não são tratados como sinônimos. A situação derivada guarda evento, código, fonte e data.

O resultado não determina prazo, trânsito em julgado ou encerramento jurídico por inferência livre. Publicação DJEN, movimento e estado de monitoramento são objetos distintos. A classificação de uma fase por título é evidência de interface e deve ser interpretada conforme data, contexto, escopo e eventos posteriores.

## Contrato da API

- `GET /api/processos/{number}`: dados gerais, atributos com proveniência e diagnóstico de fontes.
- `GET /api/processos/{number}/partes`: participantes, partes, advogados, destinatários e cobertura.
- `GET /api/processos/{number}/movimentacoes`: movimentos consolidados e respectivos detalhes por fonte.
- `GET /api/processos/{number}/intimacoes`: publicações/comunicações disponíveis para o processo.
- `GET /api/processos/{number}/timeline`: eventos combinados e estado interno de leitura.

As rotas normais exigem autenticação e escopo de escritório conforme o modo configurado. A interface apresenta uma projeção desses contratos; ela não é a fonte canônica dos dados.

## Limites conhecidos

O conteúdo varia entre fontes. DataJud nem sempre traz partes, documentos ou flags. DJEN pode fornecer destinatário e OAB sem vínculo de representação. PJe pode estar indisponível ou exigir interação/validação. Registros não encontrados não provam inexistência. Para cobertura comprovada por combinação tribunal/sistema/grau, consulte [Fontes e condições](fontes-e-condicoes.md) e `data/sources/courts.json`.
