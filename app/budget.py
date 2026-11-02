"""The daily limits on model calls.

The whole app gets 200 units a day, which protects the bill, and each signed-in
user gets 30 calls, so one person can't use up everyone's share. These have to
survive restarts, which is why production keeps the counts in Firestore.

A web search takes 10 units from the app's budget but counts as one call for the
user. The budget follows the cost; the user's limit follows how much they asked for.
"""
from __future__ import annotations

import datetime as dt
import random
import threading
import time
from typing import Callable, Protocol

DAILY_MODEL_CALLS = 200
DAILY_USER_CALLS = 30
SECONDS_PER_DAY = 86_400

# Requests arriving together can make Firestore abort each other's transactions.
TRANSACTION_ATTEMPTS = 10

OK = "ok"
DAY_USED = "day_used"
USER_USED = "user_used"


class Budget(Protocol):
    def spend(self, user: str, units: int = 1) -> str:
        """Pay for a call. Return OK, or DAY_USED or USER_USED if it's refused.
        A refused call costs nothing."""
        ...


def decide(day_used: int, user_used: int, units: int, daily_calls: int, user_calls: int) -> str:
    # The user's own limit comes first, so that their message is about them.
    if user_used + 1 > user_calls:
        return USER_USED
    if day_used + units > daily_calls:
        return DAY_USED
    return OK


def retry_when_contended(attempt: Callable[[], str], sleep: Callable[[float], None] = time.sleep) -> str:
    """Run a transaction again if Firestore aborts it.

    The library retries a failed save, but not a read that was aborted. If it never
    clears, the error propagates and the quiz is refused, as with any Firestore fault.
    """
    from google.api_core.exceptions import Aborted

    for tries_left in range(TRANSACTION_ATTEMPTS - 1, -1, -1):
        try:
            return attempt()
        except Aborted:
            if not tries_left:
                raise
            # A random wait, so that two aborted requests don't collide again.
            sleep(random.uniform(0.02, 0.2))
    raise AssertionError("unreachable")


class MemoryBudget:
    """For tests and local runs. The counts are lost when the process stops."""

    def __init__(
        self,
        daily_calls: int = DAILY_MODEL_CALLS,
        clock: Callable[[], float] = time.time,
        user_calls: int = DAILY_USER_CALLS,
    ):
        self._daily_calls = daily_calls
        self._user_calls = user_calls
        self._clock = clock
        self._lock = threading.Lock()
        self._day = int(clock() // SECONDS_PER_DAY)
        self._used = 0
        self._users: dict[str, int] = {}

    def spend(self, user: str, units: int = 1) -> str:
        with self._lock:
            today = int(self._clock() // SECONDS_PER_DAY)
            if today != self._day:
                self._day, self._used, self._users = today, 0, {}
            user_used = self._users.get(user, 0)
            result = decide(self._used, user_used, units, self._daily_calls, self._user_calls)
            if result == OK:
                self._used += units
                self._users[user] = user_used + 1
            return result


class FirestoreBudget:
    """Keeps one document per day and one per user per day, updated in a single
    transaction so that two requests arriving together can't both take the last
    call. The user's document holds their Firebase ID, never their name or email.

    If Firestore can't be reached, the error propagates and the quiz is refused.
    Failing closed is deliberate: with no way to count, no calls should be made.
    """

    def __init__(
        self,
        daily_calls: int = DAILY_MODEL_CALLS,
        user_calls: int = DAILY_USER_CALLS,
        collection: str = "usage",
    ):
        self._daily_calls = daily_calls
        self._user_calls = user_calls
        self._collection = collection
        self._client = None

    def spend(self, user: str, units: int = 1) -> str:
        from google.cloud import firestore

        if self._client is None:
            self._client = firestore.Client()

        date = dt.datetime.now(dt.timezone.utc).date().isoformat()
        day = self._client.collection(self._collection).document(date)
        person = day.collection("users").document(user)
        daily_calls, user_calls = self._daily_calls, self._user_calls

        @firestore.transactional
        def take(transaction) -> str:
            # Firestore needs every read in a transaction to come before any write.
            day_used = (day.get(transaction=transaction).to_dict() or {}).get("model_calls", 0)
            user_used = (person.get(transaction=transaction).to_dict() or {}).get("model_calls", 0)
            result = decide(day_used, user_used, units, daily_calls, user_calls)
            if result == OK:
                transaction.set(day, {"model_calls": day_used + units})
                transaction.set(person, {"model_calls": user_used + 1})
            return result

        return retry_when_contended(lambda: take(self._client.transaction()))
