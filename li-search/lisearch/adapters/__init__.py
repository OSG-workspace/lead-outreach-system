"""Provider registry. Order here IS the priority order used by `fire`."""
from __future__ import annotations

from typing import Any, Dict, List

from .base import Adapter
from .exa import ExaAdapter
from .pdl import PDLAdapter
from .coresignal import CoresignalAdapter
from .ddgs_engine import DdgsAdapter
from .openweb import OpenWebAdapter

# Keyed indexes first (best recall per query), then the free multi-engine
# index, then the single-engine stdlib fallback. A fire dedupes across all of
# them, so running every available provider only ever adds rows.
ALL = [ExaAdapter, CoresignalAdapter, PDLAdapter, DdgsAdapter, OpenWebAdapter]

# Kept for callers that import it; the real default is default_providers(),
# which is "everything that can actually run right now". The old default
# (exa, coresignal, pdl) produced four zero-lead fires on 2026-09-01 because
# none of the three had a key and the free providers were opt-in.
DEFAULT_ENABLED = [c.name for c in ALL]


def build(names: List[str], config: Dict[str, Any]) -> List[Adapter]:
    by_name = {c.name: c for c in ALL}
    out = []
    for n in names:
        cls = by_name.get(n)
        if not cls:
            raise SystemExit("unknown provider %r; known: %s" % (n, ", ".join(by_name)))
        out.append(cls(config.get(n, {})))
    return out


def default_providers(config: Dict[str, Any]) -> List[str]:
    """Every provider that is available right now, in priority order. Keyed
    providers count only when their key is set; the free ones always count.
    The operator asked for as many leads as possible, and a provider that costs
    nothing and can run should not need a flag to be included."""
    return [c.name for c in ALL if c(config.get(c.name, {})).available()]
