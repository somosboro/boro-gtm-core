"""Single import point that ensures every ORM table is registered on Base.

Alembic autogenerate and ``Base.metadata.create_all`` both depend on the
mappers having been imported at least once.
"""

from __future__ import annotations

from boro_gtm.core.db import Base
from boro_gtm.discovery.domain import models as discovery_models  # noqa: F401
from boro_gtm.market_intelligence.domain import models as mi_models  # noqa: F401
from boro_gtm.research.domain import models as research_models  # noqa: F401
from boro_gtm.strategy.domain import models as strategy_models  # noqa: F401

__all__ = [
    "Base",
    "mi_models",
    "strategy_models",
    "discovery_models",
    "research_models",
]
