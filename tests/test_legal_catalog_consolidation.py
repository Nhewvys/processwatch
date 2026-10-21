import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

from processwatch.legal_catalog import (
    SgtClassCatalogService, find_class, load_roles, validate_class_catalog,
)
from processwatch.participants import (
    canonical_participants, explicit_text_roles, normalize_procedural_function, participant_summary,
)


NUMBER = "10068509020254013305"
WEB = Path(__file__).resolve().parents[1] / "src/processwatch/web/index.html"


class LegalCatalogConsolidationTests(unittest.TestCase):
    def test_sgt_class_export_keeps_original_xml_and_detects_tampering(self):
        class FakeSgt:
            def call(self, operation, **params):
                if operation == "getDataUltimaVersao":
                    payload = b"<root><return>12/09/2026</return></root>"
                elif operation == "pesquisarItemPublicoWS":
                    self_params = params
                    assert self_params == {"tipoTabela": "C", "tipoPesquisa": "C", "valorPesquisa": "2"}
                    payload = (b"<root><Item><cod_item>2</cod_item><nome>PROCESSO CIVIL</nome>"
                               b"<cod_item_pai>1</cod_item_pai><dscGlossario>Classe raiz</dscGlossario>"
                               b"</Item></root>")
                elif operation == "getArrayDetalhesItemPublicoWS":
                    assert params["tipoItem"] == "C"
                    payload = b"<root><item><key>natureza</key><value>civil</value></item></root>"
                else:
                    assert operation == "getStringPaisItemPublicoWS" and params["tipoItem"] == "C"
                    payload = b"<root><return>1</return></root>"
                return ET.fromstring(payload), payload

        document = SgtClassCatalogService(FakeSgt()).fetch_codes(["2"])
        self.assertEqual(validate_class_catalog(document)["coverage"]["confirmed_codes"], 1)
        self.assertEqual(find_class(document, "2")[0]["details"]["natureza"], "civil")
        self.assertFalse(document["coverage"]["national_complete"])
        document["items"][0]["name"] = "NOME INVENTADO"
        with self.assertRaises(ValueError):
            validate_class_catalog(document)

    def test_versioned_catalog_preserves_specific_recourse_terms(self):
        catalog = load_roles()
        self.assertGreaterEqual(len(catalog["roles"]), 35)
        for label, result in (("AGRAVANTE", "agravante"), ("APELADO", "apelado"),
                              ("EMBARGANTE", "embargante"), ("REQUERENTE", "requerente"),
                              ("PERITO", "perito")):
            with self.subTest(label=label):
                mapped = normalize_procedural_function("PJe/TRF1", label)
                self.assertEqual(mapped["procedural_function"], result)
                self.assertEqual(mapped["function_code_original"], label)
                self.assertEqual(mapped["role_catalog_version"], catalog["version"])

    def test_anchored_communication_roles_and_ambiguous_prose(self):
        text = ("AGRAVANTE: Maria Exemplo\nAGRAVADO: Instituto Exemplo\n"
                "ADVOGADO: Pedro Exemplo\nNão é o autor: Outra Pessoa\n"
                "AUTOR: Não informado\nRÉU: Empresa A; Empresa B")
        hits = explicit_text_roles(text, "pub-1")
        self.assertEqual([row["procedural_function"] for row in hits],
                         ["agravante", "agravado", "advogado"])
        self.assertTrue(all(row["evidence"]["source_id"] == "pub-1" for row in hits))

    def test_explicit_role_and_unique_recipient_share_publication_identity(self):
        class Repo:
            def participants(self, org, number):
                return [{"participant_id": "recipient-1", "original_name": "Maria Exemplo",
                         "normalized_name": "maria exemplo", "kind": "destinatário",
                         "source": "DJEN_PUBLICACAO", "evidence_ids": ["pub-1"]}]

            def communications(self, org, number):
                return [{"source_id": "pub-1", "text_plain": "AGRAVANTE: Maria Exemplo"}]

        people = canonical_participants(Repo(), "office", NUMBER)
        self.assertEqual(len(people), 1)
        self.assertEqual(set(people[0]["kinds"]), {"destinatário", "parte"})
        self.assertEqual(people[0]["procedural_function"], "agravante")
        self.assertEqual(people[0]["function_mapping_status"], "explicit_communication_line")
        self.assertEqual(people[0]["mention_count"], 2)

    def test_lawyer_never_substitutes_party_and_recipient_is_safe_fallback(self):
        lawyer = {"participant_id": "lawyer-1", "original_name": "Advogada Exemplo",
                  "kind": "advogado", "kinds": ["advogado"]}
        self.assertIsNone(participant_summary([lawyer])["name"])
        recipient = {"participant_id": "recipient-1", "original_name": "Pessoa Exemplo",
                     "kind": "destinatário", "kinds": ["destinatário"]}
        result = participant_summary([lawyer, recipient])
        self.assertEqual(result["name"], "Pessoa Exemplo")
        self.assertEqual(result["label"], "Destinatário DJEN")

    def test_portfolio_status_displays_monitoring_and_failure_only(self):
        html = WEB.read_text(encoding="utf-8") + (WEB.parent / "app.js").read_text(encoding="utf-8")
        self.assertIn("const statusText=item.attention?'Falha na consulta':item.schedule?.enabled?'Em monitoramento':'Sem monitoramento'", html)
        self.assertNotIn("const situationLabel=item.situation_state", html)
        self.assertIn("if(item.situation)status.title", html)


if __name__ == "__main__":
    unittest.main()
