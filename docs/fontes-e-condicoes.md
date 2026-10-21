# Fontes judiciais, conectores e limites

Este documento distingue provider presente no código de fonte efetivamente consultável. A matriz completa e versionada fica em `data/sources/courts.json`; `processwatch sources` mostra o inventário disponível na instalação. Estado operacional só vale para a combinação de tribunal, sistema e grau registrada e para o escopo de validação descrito.

## DataJud/CNJ

O provider `providers/datajud.py` consulta o endpoint por tribunal usando o alias mantido na matriz e autenticação `Authorization: APIKey ...`. Configure `DATAJUD_API_KEY` a partir do canal oficial. A resposta pode trazer metadados e movimentos; os campos e a atualização variam. Ausência de participantes, documento ou situação na resposta não prova que essa informação inexista na fonte ou nos autos.

O projeto não afirma cobertura operacional de todos os tribunais só porque há aliases no inventário. Para cada CNJ, o alias precisa existir e a consulta depende de autorização, disponibilidade e contrato vigente. Consulte a documentação oficial atual antes de alterar frequência ou finalidade. O acesso aos dados e seu uso têm condições próprias; não se presume licença para redistribuição ou exploração comercial.

## DJEN

`providers/djen.py` usa a API de comunicações do DJEN. O código suporta consulta por número de processo e busca por OAB/UF com intervalo, paginação e janelas limitadas. A busca manual de descoberta não equivale ao monitoramento persistente. Monitoramentos OAB ativos são salvos e consumidos pelo worker, e publicações podem existir mesmo se o processo ainda não estiver na carteira.

Publicação, intimação identificada, destinatário e movimento processual são entidades diferentes. A fonte pode retornar destinatários, advogados e campos de polo. A interpretação de `destinatarios[].polo` não foi confirmada no contrato público consultado pelo projeto; valores originais permanecem evidência sem conversão automática para autor/réu. Uma consulta vazia significa somente que aquele filtro e intervalo não retornaram documentos.

Configurações principais: `DJEN_INITIAL_DATE`, `DJEN_WINDOW_DAYS`, `DJEN_OVERLAP_DAYS`, `DJEN_SYNC_INTERVAL`, `DJEN_MIN_INTERVAL` e `DJEN_AUTO_SYNC`. Consulte `.env.example`; os limites devem respeitar o contrato e condições atuais do serviço.

## PJe/TRF1

Existe um provider baseado no formulário público do PJe/TRF1 (`providers/pje_trf1.py`), com parsing de capa, participantes e movimentos e uso opcional de Playwright. A matriz marca `TRF1-PJE-JE` como validado para um escopo estreito. O conector também reconhece `TRF1-PJE-1G` por compatibilidade do formulário, mas essa combinação não possui validação independente equivalente. Isso não autoriza generalizar cobertura para todos os processos ou coleta em escala. Desafios humanos e respostas incompletas não são tratados como ausência de processo.

O histórico de documentos e movimentos depende dos links e campos que a página efetivamente entrega. O sistema não deve inventar URL pública de documento quando só existe referência ou consulta geral.

## TRF4 e outros tribunais

Há código experimental para eproc/TRF4 e uma extensão Chrome para importação assistida explícita. O acesso direto observado foi interrompido por mecanismo de verificação; o fluxo direto não é integração automática operacional nem entra no agendamento recorrente. A extensão usa uma página aberta pelo próprio usuário e deve ser entendida como importação assistida, sujeita à compatibilidade da página.

TRF2/eproc, TRF3/PJe, TRF5/PJe, Projudi/TJPR e e-SAJ/TJSP foram investigados em escopos descritos na matriz, mas não são conectores automáticos operacionais. Em particular, o TJSP informa restrição a robôs/ferramentas similares; não automatize sem um canal autorizado. Os tribunais não listados como validados permanecem não verificados mesmo que a API nacional DataJud possa fornecer dados.

## Catálogos auxiliares

SGT/TPU é um catálogo de códigos e nomes, não uma fonte de acompanhamento de processos. As cópias versionadas de classes e movimentos disponíveis em `data/` podem ser parciais. Só códigos conferidos são usados para regras específicas de situação; nomes de movimentos não devem ser convertidos em decisões judiciais.

## Referências mantidas pelo projeto

- [API Pública DataJud — CNJ](https://www.cnj.jus.br/sistemas/datajud/api-publica/)
- [Documentação DataJud](https://datajud-wiki.cnj.jus.br/api-publica/)
- [OpenAPI/portal do DJEN](https://hcomunicaapi.pje.jus.br/swagger/index.html)
- [Matriz de capacidades por tribunal](../data/sources/courts.json)
- [Contratos e normalização](consolidacao-processual.md)

Condições de acesso, documentação e políticas dos provedores podem mudar. Verifique-as novamente antes de ativar tráfego recorrente ou alterar a finalidade do tratamento.
