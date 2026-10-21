"""Human-readable movement titles without altering source facts or legal nature."""
import json

from .tpu import display_name


def movement_presentation(row, enrichment=None):
    original = json.loads(row["complements"]) if isinstance(row["complements"], str) else row["complements"]
    interpreted = (enrichment or {}).get("complements") or []
    visible = []
    for index, complement in enumerate(original):
        meaning = interpreted[index] if index < len(interpreted) else {}
        official = meaning.get("official_value") if meaning.get("validated") else None
        source_text = complement.get("nome") or complement.get("descricao")
        if not source_text:
            source_text = json.dumps(complement, ensure_ascii=False, sort_keys=True)
        text = str(official or source_text or "").strip()
        if not text:
            continue
        visible.append({"text": text, "origin": "SGT/TPU" if official else "DataJud",
                        "validated": bool(official), "original": complement})
    base = display_name(row["code"], row["name"])
    # Capitalization affects presentation only; the raw text remains untouched.
    labels = [part["text"][0].upper() + part["text"][1:] for part in visible]
    return {"title": base + (" — " + "; ".join(labels) if labels else ""),
            "base_name": base, "complement_display": visible}
