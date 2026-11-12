"""Backward-compatible bounded monitor mission drafting."""
import re
import time
from ...models import Contract


def draft(mission: str, agent_id: str, now: int | None = None) -> Contract:
    """Deliberately bounded parser: rejects unsupported missions instead of guessing."""
    match = re.fullmatch(r"(?:Buy|Purchase|Find and purchase) (?:a )?monitor under \$(\d{1,6})(?:\.(\d{2}))?\.?", mission, re.I)
    if not match:
        raise ValueError('supported draft: "Buy a monitor under $300."; review the returned contract before issuing')
    cents = int(match[1]) * 100 + int(match[2] or 0)
    return Contract(agent_id=agent_id, mission=mission, spending_limit=cents,
                    maximum_transaction=cents, approval_threshold=min(cents, 25000),
                    expires_at=(int(time.time()) if now is None else now) + 3600)
