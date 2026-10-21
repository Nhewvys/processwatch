from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "processwatch" / "web" / "index.html"
CSS = ROOT / "src" / "processwatch" / "web" / "ui-correction.css"


class DetailsReferenceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = WEB.read_text(encoding="utf-8") + (WEB.parent / "app.js").read_text(encoding="utf-8")
        cls.css = CSS.read_text(encoding="utf-8")

    def test_details_has_only_the_five_approved_tabs(self):
        tabs = re.findall(r'<button role="tab"[^>]*data-tab="([^"]+)"[^>]*>(.*?)</button>', self.html)
        detail = self.html[self.html.index('id="tab-gerais"'):self.html.index('id="panel-movimentos"')]
        self.assertEqual([key for key, _ in tabs], ["gerais", "movimentos", "intimacoes", "prazos", "monitoramento"])
        self.assertIn("Resumo", detail)
        self.assertNotIn('data-tab="partes"', self.html)

    def test_each_tab_targets_an_existing_panel(self):
        for panel, key in re.findall(r'<button role="tab" id="tab-[^"]+" aria-controls="([^"]+)"[^>]*data-tab="([^"]+)"', self.html):
            self.assertIn(f'id="{panel}"', self.html)
            self.assertIn(f'id="tab-{key}"', self.html)

    def test_case_summary_uses_process_scoped_data_and_honest_missing_values(self):
        self.assertIn("/api/processos/${number}/partes", self.html)
        self.assertIn("/api/processos/${number}/timeline", self.html)
        self.assertIn("/api/calendar?item_type=deadline", self.html)
        self.assertIn("Não informado pela fonte", self.html)
        self.assertIn("public_consultation_url", self.html)
        self.assertIn("Link público validado indisponível para este processo", self.html)

    def test_existing_back_button_returns_to_the_preserved_process_list(self):
        self.assertIn("back.addEventListener('click',()=>showView('processes'))", self.html)
        self.assertIn("back.id='detail-back'", self.html)

    def test_sidebar_and_topbar_styles_are_shared_across_views(self):
        self.assertIn("body.app-ready .brand img{display:block", self.css)
        self.assertIn("body.app-ready .top-select-shell{display:flex", self.css)
        self.assertIn("body.app-ready .top-icon .alert-dot{display:block}", self.css)
        self.assertNotIn("body.app-ready[data-current-view=overview] .top-select-shell", self.css)
        self.assertIn("body.app-ready[data-current-view=detail] .screen-host{margin-top:65px", self.css)

    def test_summary_timeline_uses_one_continuous_line_and_pole_icons(self):
        self.assertIn("case-timeline-list", self.html)
        self.assertIn("case-person-icon", self.html)
        self.assertIn("activePattern", self.html)
        self.assertIn("const activeParties=partyList.filter", self.html)
        self.assertIn("const passiveParties=partyList.filter", self.html)
        self.assertIn("item.party_summary", self.html)
        self.assertIn("summary?`${summary.name}", self.html)
        self.assertNotIn("${summary.label}", self.html)
        self.assertIn("Papel não confirmado", self.html)
        self.assertIn("const displayedPartyIds=new Set((parties.partes||[])", self.html)
        self.assertIn("displayedPartyIds.has(person.participant_id)", self.html)
        self.assertIn("Destinatários de comunicações", self.html)
        self.assertIn("'Destinatário da comunicação'", self.html)
        self.assertNotIn("código original ${code}", self.html)
        self.assertIn("function attributeText(general,key,value", self.html)
        self.assertIn("(divergência entre fontes)", self.html)
        self.assertNotIn("activeParties.slice(0,1)", self.html)
        self.assertNotIn("lawyers.slice(0,8)", self.html)
        self.assertIn(".case-timeline-list:before", self.css)
        self.assertNotIn(".case-move:after", self.css)
        self.assertIn(".case-person.polo-passivo .case-person-icon{background:#e8f2ff;color:#0878ea}", self.css)

    def test_topbar_notification_and_court_actions_have_real_behaviors(self):
        self.assertIn("notification-popover", self.html)
        self.assertIn("/api/processes/overview?unread_only=true", self.html)
        self.assertIn("public_consultation_url", self.html)
        self.assertIn("noopener,noreferrer", self.html)

    def test_details_uses_source_backed_value_and_tri_state_secrecy(self):
        self.assertIn("fieldText('valor_da_causa',general.valor_da_causa)", self.html)
        self.assertIn("fieldText('segredo_justica',general.segredo_justica,'Não informado')", self.html)
        self.assertIn("general.valor_da_causa_fonte", self.html)
        self.assertIn("general.segredo_justica_fonte", self.html)

    def test_court_access_and_document_actions_have_honest_fallbacks(self):
        self.assertIn("window.open(parsed.href,'_blank','noopener,noreferrer')", self.html)
        self.assertNotIn("if(!opened)", self.html)
        self.assertIn("Solicitação de abertura da consulta pública oficial enviada ao navegador", self.html)
        self.assertIn("A URL pública registrada não passou pela validação de segurança", self.html)
        self.assertIn("Referência documental ${doc.source_id}", self.html)
        self.assertIn("Link direto público não disponível", self.html)

    def test_activity_date_and_time_render_in_separate_bounded_elements(self):
        self.assertIn("function activityDateParts(value)", self.html)
        self.assertIn("time.append(node('time',parts.date),node('small',parts.time))", self.html)
        self.assertIn("grid-template-columns:39px minmax(0,1fr) minmax(76px,92px)", self.css)
        self.assertIn("#home-activity .activity-row .row-copy{min-width:0", self.css)

    def test_lawyer_rows_fill_content_column_and_keep_icon_provenance(self):
        self.assertIn("case-person advogado", self.html)
        self.assertIn("const lawyerCard=card('Advogados','users')", self.html)
        self.assertIn("#view-detail .case-person-copy{display:block;min-width:0;width:100%", self.css)

    def test_exports_state_their_format_and_purpose(self):
        self.assertIn("Exportação técnica JSON", self.html)
        self.assertIn("processwatch-dados-tecnicos-", self.html)
        self.assertIn("Exportar processos em CSV", self.html)

    def test_case_loads_ignore_stale_responses_and_notice_does_not_shift_page(self):
        self.assertIn("generation!==caseLoadGeneration", self.html)
        self.assertIn("generation!==portfolioLoadGeneration", self.html)
        self.assertIn("#notice{position:fixed", self.css)


if __name__ == "__main__":
    unittest.main()
