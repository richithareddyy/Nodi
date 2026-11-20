"""Domain-dispatched baseline and backward-compatible evaluator imports."""

from .domain_types import Evaluator as Evaluator
from .domain_types import Intent as Intent
from .domains.procurement.intent import ProcurementEvaluator as ProcurementEvaluator


class DomainEvaluator:
    def evaluate(self, contract, action, prepared):
        from .domains import get_domain

        return get_domain(contract.purpose).evaluator.evaluate(contract, action, prepared)
