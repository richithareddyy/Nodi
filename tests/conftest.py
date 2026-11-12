"""Bounded local generation and deterministic CI; override for deeper exploration."""
import os
from hypothesis import settings

settings.register_profile("nodi", max_examples=60, stateful_step_count=60, deadline=None)
settings.register_profile("nodi-ci", max_examples=100, stateful_step_count=80,
                          deadline=None, derandomize=True, database=None)
settings.register_profile("nodi-deep", max_examples=300, stateful_step_count=150, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "nodi"))
