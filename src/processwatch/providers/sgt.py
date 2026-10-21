"""Small SOAP client for the official CNJ SGT public WebService."""
import xml.etree.ElementTree as ET

import httpx
from .contracts import SourceCapabilities


URL = "https://www.cnj.jus.br/sgt/sgt_ws.php"
NAMESPACE = URL
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"


class SgtError(RuntimeError):
    pass


class SgtClient:
    source_id = "SGT/TPU"
    capabilities = SourceCapabilities(catalog=True, hierarchy=True, complements=True)

    def __init__(self, client=None, timeout=20):
        self.client = client
        self.timeout = timeout

    def call(self, operation: str, **parameters):
        allowed = {"pesquisarItemPublicoWS", "getArrayDetalhesItemPublicoWS",
                   "getStringPaisItemPublicoWS", "getComplementoMovimentoWS", "getDataUltimaVersao"}
        if operation not in allowed:
            raise ValueError("Operação SGT não permitida.")
        envelope = ET.Element(f"{{{SOAP}}}Envelope")
        body = ET.SubElement(envelope, f"{{{SOAP}}}Body")
        action = ET.SubElement(body, f"{{{NAMESPACE}}}{operation}")
        for name, value in parameters.items():
            ET.SubElement(action, name).text = str(value)
        payload = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
        client = self.client or httpx.Client(follow_redirects=False)
        try:
            response = client.post(URL, content=payload, timeout=self.timeout,
                                   headers={"Content-Type": "text/xml; charset=utf-8",
                                            "SOAPAction": NAMESPACE + "#" + operation})
            if response.status_code != 200:
                raise SgtError(f"SGT indisponível (HTTP {response.status_code}).")
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError:
                raise SgtError("SGT retornou XML inválido.") from None
            if any(element.tag.endswith("}Fault") for element in root.iter()):
                raise SgtError("SGT retornou falha SOAP.")
            return root, response.content
        except httpx.HTTPError:
            raise SgtError("SGT indisponível por falha de rede ou timeout.") from None
        finally:
            if self.client is None:
                client.close()
