"""Server-owned plugin registry. Agents cannot supply import paths or plugins."""
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import Domain


@lru_cache(maxsize=8)
def get_domain(name: str) -> "Domain":
    if name in ("procurement", "monitor_procurement"):
        from .procurement.domain import ProcurementDomain
        return ProcurementDomain()
    if name in ("research", "research_assistant"):
        from .research.domain import ResearchDomain
        return ResearchDomain()
    if name in ("sandbox", "sandbox_readonly"):
        from .readonly import ReadOnlyDomain
        return ReadOnlyDomain()
    if name in ("payments", "mock_payment"):
        from .payments import PaymentDomain
        return PaymentDomain()
    raise ValueError("unknown domain or purpose")
