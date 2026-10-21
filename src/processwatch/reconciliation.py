"""Cross-source relationships without merging independent source events."""
import json


def propose_explicit_links(repository, org, number):
    """Create review candidates only for an explicit movement ID in DJEN raw data."""
    movements = repository.timeline(org, number)
    by_original_id = {}
    for row in movements:
        original_id = json.loads(row["raw"]).get("id")
        if original_id is not None:
            by_original_id.setdefault(str(original_id), []).append("datajud:" + row["fingerprint"])
    proposals = 0
    for communication in repository.communications(org, number):
        original = communication.get("original") or {}
        reference = next((original[key] for key in ("idMovimento", "id_movimento", "movimento_id")
                          if original.get(key) is not None), None)
        if reference is None:
            continue
        matches = by_original_id.get(str(reference), [])
        if len(matches) != 1:
            continue
        repository.propose_relation(org, number, matches[0], "djen:" + communication["source_id"],
                                    "Identificador de movimento explícito na comunicação; confirmar sem fundir eventos.")
        proposals += 1
    return proposals
