"""Company name -> internal-policy document, deterministically.

A fuzzy or embedding match here would be a liability: "Shriram Finance" and "Shri Ram
Finance Corporation" are different companies, as are "Muthoot Finance" and "Muthoot
MCred". A lookalike must fail loudly with suggestions, never silently resolve.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from regtech.manifest import ManifestRow, load_manifest

_STOP = {"limited", "ltd", "private", "pvt", "the", "company", "co", "india", "of", "and"}

# Short names people actually type. Each must map to exactly one policy.
ALIASES = {
    "mahindra finance": "fpc-mahindra-finance",
    "m&m finance": "fpc-mahindra-finance",
    "mmfsl": "fpc-mahindra-finance",
    "chola": "fpc-cholamandalam",
    "l&t finance": "fpc-lnt-microloans",
    "lnt finance": "fpc-lnt-microloans",
    "nissan renault": "fpc-nrfsi",
    "nrfsi": "fpc-nrfsi",
    "tcl": "fpc-tata-capital",
}


class EntityNotFound(LookupError):
    def __init__(self, query: str, suggestions: list[str]):
        self.query, self.suggestions = query, suggestions
        hint = f" Did you mean: {'; '.join(suggestions)}? (Only an exact company name is accepted.)" if suggestions else ""
        super().__init__(f"No Fair Practices Code in the corpus for {query!r}.{hint}")


def tokens(name: str) -> list[str]:
    name = name.lower().replace("&", " & ")
    return [t for t in re.findall(r"[a-z0-9&]+", name) if t not in _STOP]


@dataclass(frozen=True)
class Entity:
    doc_id: str
    name: str
    row: ManifestRow


def policies() -> list[Entity]:
    return [Entity(r.doc_id, r.entity_name, r) for r in load_manifest() if r.doc_type == "internal_policy"]


def resolve(query: str) -> Entity:
    """Exact alias, or every query token found in exactly one company's name (token-set match)."""
    ents = policies()
    by_id = {e.doc_id: e for e in ents}
    q = " ".join(query.lower().split())
    if q in ALIASES:
        return by_id[ALIASES[q]]
    if q in by_id:
        return by_id[q]
    qt = set(tokens(query))
    if not qt:
        raise EntityNotFound(query, [])
    matches = [e for e in ents if qt <= set(tokens(e.name))]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise EntityNotFound(query, [e.name for e in matches][:3])
    squash = {"".join(tokens(e.name)): e.name for e in ents}
    close = difflib.get_close_matches("".join(tokens(query)), list(squash), n=3, cutoff=0.6)
    raise EntityNotFound(query, [squash[c] for c in close])
