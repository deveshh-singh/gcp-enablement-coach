"""The daily budget of model calls.

Anyone with the link can use this app, so this limit protects the bill. It has to
survive restarts, which is why production keeps the count in Firestore.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Callable, Protocol

DAILY_MODEL_CALLS = 200
SECONDS_PER_DAY = 86_400


class Budget(Protocol):
    def spend(self) -> bool:
        """Use one call. Return False if today's budget has run out."""
        ...


class MemoryBudget:
    """For tests and local runs. The count is lost when the process stops."""

    def __init__(
        self,
        daily_calls: int = DAILY_MODEL_CALLS,
        clock: Callable[[], float] = time.time,
    ):
        self._daily_calls = daily_calls
        self._clock = clock
        self._lock = threading.Lock()
        self._day = int(clock() // SECONDS_PER_DAY)
        self._used = 0

    def spend(self) -> bool:
        with self._lock:
            today = int(self._clock() // SECONDS_PER_DAY)
            if today != self._day:
                self._day, self._used = today, 0
            if self._used >= self._daily_calls:
                return False
            self._used += 1
            return True


class FirestoreBudget:
    """Keeps one document per day, updated in a transaction so that two requests
    arriving together can't both take the last call.

    If Firestore can't be reached, the error propagates and the quiz is refused.
    Failing closed is deliberate: with no way to count, no calls should be made.
    """

    def __init__(self, daily_calls: int = DAILY_MODEL_CALLS):
        self._daily_calls = daily_calls
        self._client = None

    def spend(self) -> bool:
        from google.cloud import firestore

        if self._client is None:
            self._client = firestore.Client()

        day = dt.datetime.now(dt.timezone.utc).date().isoformat()
        document = self._client.collection("usage").document(day)
        daily_calls = self._daily_calls

        @firestore.transactional
        def take_one(transaction) -> bool:
            snapshot = document.get(transaction=transaction)
            used = (snapshot.to_dict() or {}).get("model_calls", 0)
            if used >= daily_calls:
                return False
            transaction.set(document, {"model_calls": used + 1})
            return True

        return take_one(self._client.transaction())
