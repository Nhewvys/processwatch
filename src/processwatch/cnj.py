"""Numeração CNJ: módulo 97 base 10, Resolução CNJ 65/2008."""
import re

STATES = "AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SE SP TO".split()


def normalize(value: str) -> str:
    value = value.strip()
    if not (re.fullmatch(r"[0-9]{20}", value) or
            re.fullmatch(r"[0-9]{7}-[0-9]{2}\.[0-9]{4}\.[0-9]\.[0-9]{2}\.[0-9]{4}", value)):
        raise ValueError("Use 20 dígitos ou NNNNNNN-DD.AAAA.J.TR.OOOO.")
    n = re.sub(r"[-.]", "", value)
    expected = 98 - int(n[:7] + n[9:] + "00") % 97
    if int(n[7:9]) != expected:
        raise ValueError("Dígito verificador CNJ inválido.")
    return n


def format_number(n: str) -> str:
    return f"{n[:7]}-{n[7:9]}.{n[9:13]}.{n[13]}.{n[14:16]}.{n[16:]}"


def tribunal(n: str) -> str:
    """Identifica tribunal de origem; o roteador valida o alias DataJud."""
    segment, tr = n[13], int(n[14:16])
    if segment == "1" and tr == 0:
        return "stf"
    if segment == "2" and tr == 0:
        return "cnj"
    if segment == "4" and 1 <= tr <= 6:
        return f"trf{tr}"
    if segment == "5" and 1 <= tr <= 24:
        return f"trt{tr}"
    if segment == "8" and 1 <= tr <= 27:
        return "tjdft" if tr == 7 else "tj" + STATES[tr-1].lower()
    if segment == "6" and 1 <= tr <= 27:
        return "tre-dft" if tr == 7 else "tre-" + STATES[tr-1].lower()
    if segment == "3" and tr == 0:
        return "stj"
    if segment == "5" and tr == 0:
        return "tst"
    if segment == "6" and tr == 0:
        return "tse"
    if segment == "7" and 0 <= tr <= 12:
        return "stm"
    if segment == "9" and tr in (13, 21, 26):
        return {13: "tjmmg", 21: "tjmrs", 26: "tjmsp"}[tr]
    raise ValueError("Segmento/tribunal CNJ não reconhecido.")
