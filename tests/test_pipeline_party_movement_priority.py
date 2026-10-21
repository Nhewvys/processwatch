import tempfile
import unittest
from pathlib import Path

from processwatch.api import create_app
from processwatch.cnj import format_number
from processwatch.config import Config
from processwatch.consolidation import consolidate
from processwatch.domain import Participant
from processwatch.engine import Engine
from processwatch.event_reconciliation import reconcile_movements
from processwatch.participants import (canonical_participants, explicit_text_roles,
                                       normalize_pole, normalized_name, participant_summary)
from processwatch.providers.pje_trf1 import _participant_identity, parse_detail_html
from processwatch.repository import SQLiteRepository
from processwatch.routing import route_process


NUMBER = "10068509020254013305"


class NoNetwork:
    def fetch(self, *args, **kwargs):
        raise AssertionError("Uma rota local de leitura não pode consultar fontes externas")


class PartyIdentityPipelineTests(unittest.TestCase):
    def test_pje_parser_keeps_cpf_cnpj_role_and_document_href(self):
        cpf = _participant_identity("ANA EXEMPLO (AUTOR) CPF: 123.456.789-01", "ativo")
        cnpj = _participant_identity("AGRAVANTE: EMPRESA EXEMPLO LTDA CNPJ: 12.345.678/0001-99", "ativo")
        representative = _participant_identity("IARA EXEMPLO (REPRESENTANTE)", "outros")
        self.assertEqual((cpf["cpf"], cpf["procedural_function"], cpf["procedural_pole"]),
                         ("12345678901", "autor", "ativo"))
        self.assertEqual((cnpj["cnpj"], cnpj["procedural_function"], cnpj["function_scope"]),
                         ("12345678000199", "agravante", "recurso"))
        self.assertEqual(representative["procedural_function"], "representante legal")
        html = f'''<html><body><div>Número Processo</div>{format_number(NUMBER)}
        <table id="j_id150:processoPartesPoloAtivoResumidoTableBinding"><tr><td>
        ANA EXEMPLO (AUTOR) CPF: 123.456.789-01</td><td>Ativo</td></tr></table>
        <table id="j_id150:processoDocumentoGridTab"><tr><td>Despacho
        <a title="Visualizar" href="https://pje1g-consultapublica.trf1.jus.br/doc?id=77">Abrir</a>
        </td><td>Despacho</td></tr></table></body></html>'''
        parsed = parse_detail_html(html, NUMBER)
        self.assertEqual(parsed["participants"][0]["cpf"], "12345678901")
        self.assertEqual(parsed["documents"][0]["url"],
            "https://pje1g-consultapublica.trf1.jus.br/doc?id=77")
        self.assertFalse(parsed["documents"][0]["public_link_validated"])

    def test_explicit_role_patterns_and_lawyer_relation_are_normalized(self):
        cases = {
            "AUTOR: ANA EXEMPLO": ("autor", "ativo"),
            "AGRAVADO: EMPRESA EXEMPLO": ("agravado", "passivo"),
            "APELANTE ANA EXEMPLO": ("apelante", "ativo"),
            "ANA EXEMPLO (EMBARGADO)": ("embargado", "passivo"),
            "EMPRESA EXEMPLO - APELADO": ("apelado", "passivo"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                rows = explicit_text_roles(text, "comunicacao-1")
                self.assertTrue(rows)
                self.assertEqual((rows[0]["procedural_function"], rows[0]["procedural_pole"]), expected)
        lawyers = explicit_text_roles("Advogado do autor: ANA ADVOGADA", "comunicacao-2")
        self.assertEqual(len(lawyers), 1)
        self.assertEqual(lawyers[0]["kind"], "advogado")
        self.assertEqual(lawyers[0]["relationships"][0]["role"], "autor")
        self.assertNotIn("do autor", lawyers[0]["name"].casefold())

    def test_djen_caption_extracts_both_parties_and_oab_without_promoting_expert_questions(self):
        text = ("PROCESSO: 0000000-27.0000.4.05.0000 - PROCEDIMENTO DO JUIZADO ESPECIAL "
                "CÍVEL (436) AUTOR: ANA EXEMPLO Advogado do(a) AUTOR: LIA ADVOGADA - BA68297 "
                "REU: INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS ATO ORDINATÓRIO "
                "PERITO: Médico responda aos quesitos abaixo. Colheu dados do histórico do(a) "
                "periciando(a)? Procurou determinar a veracidade?")
        rows = explicit_text_roles(text, "publication-1")
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["ANA EXEMPLO"]["procedural_function"], "autor")
        self.assertEqual(by_name["ANA EXEMPLO"]["procedural_pole"], "ativo")
        defendant = next(row for row in rows if row["name"].startswith("INSTITUTO NACIONAL"))
        self.assertEqual(defendant["procedural_function"], "réu")
        self.assertEqual(defendant["procedural_pole"], "passivo")
        lawyer = by_name["LIA ADVOGADA"]
        self.assertEqual((lawyer["kind"], lawyer["oab"], lawyer["uf"]), ("advogado", "68297", "BA"))
        self.assertEqual(lawyer["relationships"][0]["role"], "autor")
        self.assertFalse(any(row["kind"] == "perito" for row in rows))

    def test_datajud_pole_codes_are_source_and_field_specific(self):
        self.assertEqual(normalize_pole("DataJud", "partes[].polo", "AT")["polo_normalized"], "ativo")
        self.assertEqual(normalize_pole("DataJud", "partes[].polo", "PA")["polo_normalized"], "passivo")
        self.assertEqual(normalize_pole("DataJud", "partes[].polo", "TC")["polo_normalized"], "terceiro")
        self.assertIsNone(normalize_pole("DJEN_PUBLICACAO", "destinatarios[].polo", "A")["polo_normalized"])

    def test_repository_collapses_fifteen_mentions_and_keeps_all_communications(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(str(Path(folder) / "parties.sqlite3"))
            try:
                repo.register("local", NUMBER, "trf1", 24, 100)
                communications = []
                for index in range(15):
                    participant = Participant(
                        participant_id=f"recipient-{index}", number=NUMBER,
                        original_name="ANA EXEMPLO", normalized_name=normalized_name("ANA EXEMPLO"),
                        kind="destinatário", role=None, source="DJEN_PUBLICACAO",
                        evidence_id=f"communication-{index}",
                        communication_role="destinatário da comunicação").model_dump()
                    communications.append({"source_id": f"communication-{index}",
                        "number": NUMBER, "participants": [participant], "text_plain": "",
                        "original": {"id": f"communication-{index}"}})
                repo.save_communications("local", NUMBER, communications, status="complete")
                mentions = repo.participants("local", NUMBER)
                consolidated = canonical_participants(repo, "local", NUMBER)
                self.assertEqual(len(repo.communications("local", NUMBER)), 15)
                self.assertEqual(len(mentions), 1)
                self.assertEqual(mentions[0]["mention_count"], 15)
                self.assertEqual(len(consolidated), 1)
                self.assertEqual(consolidated[0]["mention_count"], 15)
                self.assertEqual(len(consolidated[0]["evidence_ids"]), 15)
            finally:
                repo.close()

    def test_unique_pje_cover_is_authority_and_same_name_has_one_presentation(self):
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "api.sqlite3")
            repo = SQLiteRepository(db)
            try:
                repo.register("local", NUMBER, "trf1", 24, 100)
                recipient = Participant(
                    participant_id="djen-recipient", number=NUMBER,
                    original_name="ANA EXEMPLO", normalized_name=normalized_name("ANA EXEMPLO"),
                    kind="destinatário", source="DJEN_PUBLICACAO", evidence_id="djen-1",
                    communication_role="destinatário da comunicação").model_dump()
                repo.save_communications("local", NUMBER, [{"source_id": "djen-1", "number": NUMBER,
                    "participants": [recipient], "text_plain": "AUTOR: ANA EXEMPLO",
                    "original": {"id": "djen-1"}}], status="complete")
                snapshot = {"number": NUMBER, "tribunal": "TRF1", "system": "PJe", "degree": "1º grau",
                    "participants": [
                        {"name": "ANA EXEMPLO (AUTOR)", "kind": "parte", "role": "AUTOR",
                         "polo": "ativo", "cpf": "12345678901", "document_type": "cpf",
                         "document_number": "12345678901", "source": "PJe/TRF1"},
                        {"name": "EMPRESA EXEMPLO (RÉU)", "kind": "parte", "role": "RÉU",
                         "polo": "passivo", "source": "PJe/TRF1"},
                        {"name": "ADVOGADA EXEMPLO", "kind": "advogado", "role": "ADVOGADO",
                         "oab": "12345", "uf": "BA", "source": "PJe/TRF1"}],
                    "movements": [{"source_id": "pje-1", "occurred_at": "2026-10-01T12:00:00+00:00",
                        "name": "Juntada de petição inicial", "source": "PJe/TRF1", "documents": []}]}
                repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", snapshot,
                                         status="consulta concluída", coverage={"participants": 3})
                app = create_app(repo, Engine(repo, NoNetwork()),
                                 config=Config(db=db, access_token="pipeline-test"))
                route = next(item for item in app.routes
                             if getattr(item, "path", None) == "/api/processos/{number}/partes")
                operation = route.endpoint(number=format_number(NUMBER))
                try:
                    operation.send(None)
                except StopIteration as completed:
                    payload = completed.value
                else:
                    self.fail("A função de leitura da rota /partes deve concluir sem operações externas.")
                # Presentation joins case-scoped names while preserving the
                # constituent identities and the authoritative cover role.
                self.assertEqual(len(payload["partes"]), 2)
                self.assertEqual(len(payload["advogados"]), 1)
                active = next(item for item in payload["partes"]
                              if item["nome"] == "ANA EXEMPLO" and item["fonte"] == "PJe/TRF1")
                self.assertEqual(active["polo_canonico"], "ativo")
                self.assertEqual(active["cpf"], "12345678901")
                self.assertEqual(active["fonte"], "PJe/TRF1")
                author_cards = [item for item in payload["participants"] if item["original_name"] == "ANA EXEMPLO"]
                self.assertEqual(len(author_cards), 1)
                self.assertNotIn("destinatário", author_cards[0]["kinds"])
                self.assertIn("DJEN_PUBLICACAO", active["fontes"])
                self.assertGreaterEqual(len(active["identidades_origem"]), 2)
                self.assertEqual(participant_summary(payload["participants"])["name"], "ANA EXEMPLO")
                self.assertEqual(repo.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                repo.close()

    def test_first_degree_trf1_routes_to_shared_form_without_claiming_independent_validation(self):
        routing = route_process(NUMBER, {"tribunal": "TRF1", "sistema": {"nome": "PJe"},
                                         "grau": "1º grau"})
        self.assertEqual(routing["selected_connector"], "TRF1-PJE-1G")
        self.assertEqual(routing["status"], "routed_compatible_unvalidated")
        self.assertTrue(any(item.get("validation") == "not independently validated for this degree"
                            for item in routing["evidence"]))


class MovementCrossSourceDeduplicationTests(unittest.TestCase):
    def test_same_timestamp_high_similarity_uses_pje_text_and_keeps_both_sources(self):
        timestamp = "2026-10-01T12:00:00+00:00"
        datajud = {"id": "dj-1", "number": NUMBER, "tipo": "movimentacao", "fonte": "DataJud",
                   "occurred_at": timestamp, "titulo": "Petição inicial juntada", "documents": []}
        pje = {"id": "pje-1", "number": NUMBER, "tipo": "movimentacao", "fonte": "PJe/TRF1",
               "occurred_at": timestamp, "titulo": "Juntada da petição inicial", "documents": [
                   {"source_id": "doc-1", "label": "Petição inicial", "url": None}]}
        merged = reconcile_movements([datajud, pje])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["titulo"], pje["titulo"])
        self.assertEqual(merged[0]["fontes"], ["DataJud", "PJe/TRF1"])
        self.assertEqual(len(merged[0]["source_details"]), 2)
        self.assertIn("high_text_similarity", merged[0]["equivalence"]["evidence"])

    def test_same_time_but_different_legal_acts_remain_separate(self):
        timestamp = "2026-10-01T12:00:00+00:00"
        left = {"id": "dj-1", "number": NUMBER, "tipo": "movimentacao", "fonte": "DataJud",
                "occurred_at": timestamp, "titulo": "Juntada de contestação", "documents": []}
        right = {"id": "pje-1", "number": NUMBER, "tipo": "movimentacao", "fonte": "PJe/TRF1",
                 "occurred_at": timestamp, "titulo": "Decisão sobre tutela de urgência", "documents": []}
        self.assertEqual(len(reconcile_movements([left, right])), 2)


if __name__ == "__main__":
    unittest.main()
