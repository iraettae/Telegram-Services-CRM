"""
tg_combiner — Spintax parser.
Supports nested constructions: {Hello|{Hi|Hey}}, {how are you|what's up}?
"""

import re
import random


_SPINTAX_RE = re.compile(r"\{([^{}]+)\}")


def spin(text: str) -> str:
    """Recursively resolve all spintax blocks and return a unique variant."""
    def _pick(match: re.Match) -> str:
        options = match.group(1).split("|")
        return random.choice(options)

    # Keep resolving until no more braces remain (handles nesting)
    while _SPINTAX_RE.search(text):
        text = _SPINTAX_RE.sub(_pick, text)

    return text
