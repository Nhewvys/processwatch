# Process Watch — Design System e Especificação de Interfaces
**Versão:** 1.0 · **Data:** 12/10/2026 · **Status:** especificação de design aprovado, não implementação

> Fonte de verdade visual: as nove imagens aprovadas nesta conversa, reproduzidas na pasta `referencias/`. São protótipos com dados fictícios, não capturas do sistema funcional.

## 1. Princípios de experiência
- Interface que orienta usuários leigos por hierarquia visual e ações explícitas.
- Painel gerencial prioritário, com alertas e tarefas operacionais integrados.
- Linguagem jurídica clara; ocultar detalhes técnicos do motor em áreas avançadas.
- Consistência entre telas; evitar ações escondidas e preservar contexto de navegação.
- Estados de carregamento, vazio, erro e sucesso explícitos; nunca confundir indisponibilidade com valor zero.

## 2. Identidade visual
A marca original é multicolorida; a interface utiliza o verde-azulado como principal, o verde para sucesso, o laranja para atenção e o vermelho para erro/vencimento. A logo original deve ser preservada; os protótipos ainda exibem símbolo provisório.

| Token | Valor de referência | Uso |
|---|---|---|
| `brand.primary` | `#249F86` | Ações primárias, seleção e links |
| `brand.deep` | `#063D34` (aproximado) | Sidebar verde-escuro |
| `status.success` | `#53B85E` | Confirmação e monitoramento ativo |
| `status.warning` | `#F59A23` | Prazos próximos e atenção |
| `status.danger` | `#D92735` | Erro, vencimento e exclusão |
| `surface.page` | `#F7F9FA` | Fundo da aplicação |
| `surface.card` | `#FFFFFF` | Cartões, tabelas e painéis |
| `text.primary` | `#17212B` | Títulos e corpo |
| `text.secondary` | `#74808B` | Legendas e metadados |

**Nota:** tokens iniciais extraídos da discussão e aproximação visual; conferir contraste WCAG e amostrar cores finais na fase de implementação.

## 3. Navegação e responsividade
- Desktop amplo (referência ≥1280 px): sidebar expandido com ícones e rótulos, recolhível manualmente.
- Largura intermediária (referência 768–1279 px): sidebar compacto com ícones e nomes acessíveis por tooltip/foco.
- Celular (referência ≤767 px): sidebar oculto e acessível por botão que abre gaveta.
- Pontos de quebra são propostas, sujeitos a testes com conteúdo real.
- Rotas conceituais: Visão Geral, Processos, Monitoramentos, Comunicações, Agenda, Prazos, Relatórios, Configurações. Detalhes do Processo é uma subpágina de Processos, **não** substitui a listagem.
- Fluxo: Processos → Meus Processos → Detalhes do Processo → Voltar com filtros e paginação preservados.

## 4. Componentes reutilizáveis
- Sidebar e topbar; busca global; filtros e seletor de período.
- Cartões KPI, gráficos, tabela com ordenação e paginação, badges semânticos.
- Abas, botões primários/secundários, menus de ação, drawer lateral, modais de confirmação.
- Linha do tempo, calendário, formulário com validação, notificações e feedback assíncrono.
- Estados: skeleton/carregando, vazio com próximo passo, falha recuperável, sucesso, sem permissão, dados desatualizados.
- Ações perigosas exigem confirmação; status devem combinar cor, texto e ícone.

## 5. Telas aprovadas
### 01. Visão Geral
Seis indicadores: total de processos, monitoramentos ativos, movimentações novas, intimações novas, prazos próximos e falhas de consulta. Gráficos gerenciais, filtros por período e tribunal, alertas e atividades recentes. Indicadores clicáveis levam a listas filtradas.
**Referência:** `referencias/a_wide_clean_modern_desktop_dashboard_ui_screens.png`

### 02. Meus Processos
Tela inicial ao clicar em Processos no sidebar. Carteira completa em tabela, alternância para cartões, busca por CNJ/parte/cliente, filtros por advogado/tribunal/situação/status, paginação, exportação e ações rápidas. Ao voltar de Detalhes, preservar busca, filtros e página.
**Referência:** `referencias/wide_screenshot_of_a_desktop_web_application_ui_m.png`

### 03. Detalhes do Processo
Página aberta a partir de um processo selecionado. Cabeçalho com CNJ, tribunal, situação e monitoramento. Abas Resumo, Movimentações, Intimações, Prazos e Monitoramento. Resumo com partes, advogados, histórico recente e alertas; dados técnicos na aba Monitoramento.
**Referência:** `referencias/a_wide_clean_modern_ui_screenshot_of_a_legal_cas.png`

### 04. Monitoramentos
Listagem com status, tribunal, frequência, última consulta, próxima execução e ações de executar/pausar/retomar/editar. Novo monitoramento abre formulário lateral com CNJ e informações essenciais; opções avançadas condicionadas à capacidade real do motor.
**Referência:** `referencias/wide_ui_screenshot_of_a_web_application_dashboard.png`

### 05. Comunicações e Intimações
Tabela com pesquisa, filtros e estados de leitura, painel lateral de conteúdo, documentos, prazos relacionados e acesso ao processo. Leitura interna nunca equivale a ciência jurídica oficial.
**Referência:** `referencias/a_wide_clean_modern_web_app_dashboard_ui_screen.png`

### 06. Agenda Jurídica
Modos mês, semana, dia e lista; audiências, reuniões, compromissos e prazos visualmente diferenciados. Painel de detalhes, vínculo opcional com processo, ações de edição e lembretes conforme recursos implementados.
**Referência:** `referencias/a_wide_clean_modern_web_app_dashboard_screenshot.png`

### 07. Prazos Processuais
Categorias obrigatórias: Vencidos, Próximos 7 dias, Em aberto, Confirmados, Todos e Descartados. Filtros, tabela, detalhes laterais, histórico, responsável e ações. Identificar origem manual/automática e conferência jurídica; não presumir cálculo validado.
**Referência:** `referencias/wide_screenshot_of_a_web_application_dashboard_ui.png`

### 08. Relatórios
Indicadores e gráficos de processos, tribunais, movimentações, prazos, comunicações e produtividade. Filtros por período, tribunal, cliente e advogado; geração e exportação PDF/XLSX/CSV como capacidade planejada a confirmar.
**Referência:** `referencias/a_wide_clean_dashboard_ui_screenshot_in_portugues.png`

### 09. Configurações
Seções de Geral, Escritório, Usuários/Advogados, Notificações, Monitoramentos, Tribunais, Integrações, Aparência e Segurança. Parâmetros avançados separados; administração global da plataforma em produto/painel distinto.
**Referência:** `referencias/a_clean_modern_web_application_ui_screenshot_in_po.png`

## 6. Regras de dados e segurança
- Escopo atual: um único escritório e uma carteira compartilhada.
- Escopo futuro: isolamento real por escritório/usuário no backend e banco de dados; filtros visuais não são controle de acesso.
- Dashboard administrativo da plataforma é separado do dashboard jurídico.
- Dados fictícios de protótipos não podem ser tratados como dados reais.
- Publicação/intimação marcada como lida é controle interno, não ato de ciência oficial.
- Prazo calculado ou sugerido automaticamente deve indicar fonte, regras aplicadas e necessidade de validação profissional.
- Frequência, canais de notificação, relatórios e cálculos avançados devem ser confrontados com o motor antes da implementação.

## 7. Diretrizes de implementação
- Manter Python/FastAPI no backend. Frontend pode ser HTML/CSS/JavaScript sem React; avaliar HTMX/Alpine.js apenas se simplificar a experiência.
- Definir design tokens em CSS, componentes reutilizáveis e layouts responsivos.
- API deve expor estados de atualização, erro e timestamps para indicadores e monitoramentos.
- Usar paginação no servidor para carteiras grandes; filtros, acessibilidade de teclado e estados de foco.
- Priorizar performance percebida e real, com carregamento progressivo e feedback imediato.

## 8. Checklist de aceite antes do código
- [ ] Conferir fidelidade de cada tela com sua imagem aprovada.
- [ ] Substituir símbolo provisório pela logomarca original.
- [ ] Validar responsividade em desktop, tablet e celular.
- [ ] Verificar contraste, foco, teclado e textos de status.
- [ ] Mapear cada campo e ação à API/motor existentes; marcar lacunas como planejadas.
- [ ] Validar fluxos de navegação, inclusive retorno à lista com filtros preservados.
- [ ] Confirmar estados de erro, vazio, carregamento e indisponibilidade de fonte.

## 9. Referências
As nove imagens aprovadas e a logomarca original acompanham o pacote na pasta `referencias/`.