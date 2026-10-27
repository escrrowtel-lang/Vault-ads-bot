"""Per-account queue: priority for replies while respecting Telegram cooldowns.

No parallel sending from the same account. Incoming DMs may overtake queued group
messages, but cannot bypass the account's existing minimum gap or FloodWait.
"""
import asyncio
import time


class AccountPacer:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._next_allowed: dict[str, float] = {}
        self._urgent_waiters: dict[str, int] = {}

    async def run(self, account_id: str, min_gap_seconds: float, send, *, priority=False, gap_on_error=True):
        account_id = str(account_id)
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        registered_urgent = bool(priority)
        if registered_urgent:
            self._urgent_waiters[account_id] = self._urgent_waiters.get(account_id, 0) + 1
        try:
            while True:
                async with lock:
                    if not priority and self._urgent_waiters.get(account_id, 0):
                        # Let a waiting incoming-DM reply take the next available slot.
                        pass
                    else:
                        remaining = self._next_allowed.get(account_id, 0.0) - time.monotonic()
                        if remaining > 0:
                            # Release the lock frequently so an arriving DM can be
                            # noticed during a long group-message pacing delay.
                            await asyncio.sleep(min(remaining, 0.05))
                            continue
                        if registered_urgent:
                            self._urgent_waiters[account_id] -= 1
                            registered_urgent = False
                        succeeded = False
                        try:
                            result = await send()
                            succeeded = True
                            return result
                        finally:
                            # Successful sends use the normal pacing gap.  Callers may
                            # opt out of charging the gap for failed/skipped targets so
                            # the next target can be attempted immediately.
                            if succeeded or gap_on_error:
                                self._next_allowed[account_id] = (time.monotonic()
                                                                  + max(0, min_gap_seconds))
                await asyncio.sleep(0.01)
        finally:
            if registered_urgent:
                self._urgent_waiters[account_id] -= 1
