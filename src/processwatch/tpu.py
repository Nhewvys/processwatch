"""Small, explicitly verified TPU enrichment table.

Keep source labels separately; never infer case status from one movement.
"""

VERIFIED_MOVEMENTS = {
    "246": "Arquivamento definitivo",
}


def enrich_movement(row: dict, catalog_item: dict) -> dict:
    """Interpret only complement codes explicitly listed by this SGT version."""
    import json

    raw_complements = json.loads(row["complements"]) if isinstance(row["complements"], str) else row["complements"]
    known = {str(part["code"]): part for part in catalog_item.get("complements", [])}
    interpreted = []
    for original in raw_complements:
        code = str(original.get("codigo", ""))
        definition = known.get(code)
        value = original.get("valor")
        official = None
        if definition and value is not None:
            official = definition.get("values", {}).get(str(value))
        interpreted.append({"original": original, "definition": definition["name"] if definition else None,
                            "official_value": official, "validated": bool(definition and official is not None)})
    return {"version": catalog_item["version"], "code": catalog_item["code"],
            "official_name": catalog_item["name"], "official_description": catalog_item.get("description"),
            "parent_codes": catalog_item.get("parents", []), "source": catalog_item["source"],
            "complements": interpreted}


def display_name(code: str, original: str) -> str:
    return VERIFIED_MOVEMENTS.get(str(code), original)
