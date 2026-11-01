"""Quiz logic: validate what the model wrote, limit model calls, grade in code.

Nothing here talks to Google. The model call and the budget are passed in, so the
tests can run without a network connection or any cloud account.
"""
from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.budget import Budget, MemoryBudget

logger = logging.getLogger("coach")

TOPICS_DIR = Path(__file__).parent / "topics"
TOPICS = {
    "google-cloud-basics": {
        "title": "Google Cloud Basics",
        "file": "google-cloud-basics.md",
    },
}

QUESTIONS_PER_QUIZ = 5
CUSTOM = "custom"

Write = Callable[[str, str, int], list[dict]]


class TopicNotFound(Exception):
    """The requested topic is not one of the built-in topics."""


class DailyLimitReached(Exception):
    """The day's budget of model calls has been used."""


class VisitorLimitReached(Exception):
    """One visitor has made too many quizzes from their own material."""


class NoValidQuiz(Exception):
    """The model did not produce enough valid questions."""


class QuizNotFound(Exception):
    """The quiz has expired or the server has restarted."""


class WrongAnswerCount(Exception):
    """The number of answers does not match the number of questions."""


@dataclass(frozen=True)
class Limits:
    cooldown_seconds: int = 30
    attempts_per_quiz: int = 3
    quiz_lifetime_seconds: int = 3_600
    max_stored_quizzes: int = 500
    visitor_quizzes_per_hour: int = 6


def problems_with(question: dict) -> list[str]:
    """Return why a question can't be used. An empty list means it is valid."""
    problems = []
    text = str(question.get("question", "")).strip()
    options = question.get("options")
    answer = question.get("answer")
    explanation = str(question.get("explanation", "")).strip()

    if not text:
        problems.append("no question text")
    if not explanation:
        problems.append("no explanation")
    if not isinstance(options, list) or len(options) != 4:
        problems.append("must have exactly 4 options")
    else:
        cleaned = [str(option).strip() for option in options]
        if not all(cleaned):
            problems.append("an option is empty")
        if len(set(cleaned)) != 4:
            problems.append("options must all be different")
        if answer not in options:
            problems.append("answer is not one of the options")
    return problems


def keep_valid(questions: list[dict]) -> list[dict]:
    """Drop invalid and repeated questions, keeping the original order."""
    kept: list[dict] = []
    seen: set[str] = set()
    for question in questions:
        problems = problems_with(question)
        key = str(question.get("question", "")).strip().lower()
        if problems or key in seen:
            logger.warning("rejected question: %s", problems or ["repeated"])
            continue
        seen.add(key)
        kept.append(question)
    return kept


class Coach:
    def __init__(
        self,
        write: Write,
        budget: Budget | None = None,
        limits: Limits = Limits(),
        clock: Callable[[], float] = time.time,
        shuffle: Callable[[list], None] = random.shuffle,
    ):
        self._write = write
        self._budget = budget or MemoryBudget(clock=clock)
        self._limits = limits
        self._clock = clock
        self._shuffle = shuffle
        self._lock = threading.Lock()
        self._sample_lock = threading.Lock()
        self._quizzes: OrderedDict[str, dict] = OrderedDict()
        self._latest: dict[str, str] = {}
        self._visitors: dict[str, list[float]] = {}

    def topics(self) -> list[dict]:
        return [{"id": key, "title": t["title"]} for key, t in TOPICS.items()]

    def spend_call(self) -> None:
        """Pay for one model call, or stop if today's budget is used."""
        if not self._budget.spend():
            raise DailyLimitReached()

    def check_visitor(self, visitor: str) -> None:
        """Allow a limited number of quizzes per visitor per hour.

        This is held in memory, so it resets on a restart. The daily budget is the
        limit that protects the cost; this one stops one visitor using all of it.
        """
        now = self._clock()
        with self._lock:
            recent = [t for t in self._visitors.get(visitor, []) if now - t < 3_600]
            if len(recent) >= self._limits.visitor_quizzes_per_hour:
                self._visitors[visitor] = recent
                raise VisitorLimitReached()
            recent.append(now)
            self._visitors[visitor] = recent
            if len(self._visitors) > 5_000:
                self._visitors.clear()

    def new_quiz(self, topic: str) -> dict:
        """Return a quiz on a built-in topic. It never includes answers."""
        if topic not in TOPICS:
            raise TopicNotFound(topic)

        # One model call at a time for the sample topic. Anyone else waiting then
        # receives the quiz that call produced, which is what keeps a burst of
        # visitors from turning into a burst of model calls.
        with self._sample_lock:
            with self._lock:
                self._forget_expired()
                latest = self._quizzes.get(self._latest.get(topic, ""))
                age = self._clock() - latest["created"] if latest else None
                if latest and age < self._limits.cooldown_seconds:
                    return self._public(self._latest[topic], latest)

            pack = (TOPICS_DIR / TOPICS[topic]["file"]).read_text(encoding="utf-8")
            questions = self._write_questions(TOPICS[topic]["title"], pack)
            quiz_id = self._store(topic, questions)
            with self._lock:
                self._latest[topic] = quiz_id
                return self._public(quiz_id, self._quizzes[quiz_id])

    def quiz_from_material(self, material: str, title: str = "the material provided") -> dict:
        """Return a quiz on a visitor's own material. The material is not kept."""
        questions = self._write_questions(title, material)
        quiz_id = self._store(CUSTOM, questions)
        with self._lock:
            return self._public(quiz_id, self._quizzes[quiz_id])

    def grade(self, quiz_id: str, answers: list[str | None]) -> dict:
        """Mark the answers by comparing text. The model plays no part in this."""
        with self._lock:
            self._forget_expired()
            quiz = self._quizzes.get(quiz_id)
        if quiz is None:
            raise QuizNotFound(quiz_id)

        questions = quiz["questions"]
        if len(answers) != len(questions):
            raise WrongAnswerCount(f"{len(answers)} of {len(questions)}")

        results = []
        for question, given in zip(questions, answers):
            results.append(
                {
                    "question": question["question"],
                    "your_answer": given,
                    "correct_answer": question["answer"],
                    "correct": given == question["answer"],
                    "explanation": question["explanation"],
                }
            )
        return {
            "score": sum(result["correct"] for result in results),
            "total": len(results),
            "results": results,
        }

    def _write_questions(self, title: str, material: str) -> list[dict]:
        for attempt in range(1, self._limits.attempts_per_quiz + 1):
            self.spend_call()
            valid = keep_valid(self._write(title, material, QUESTIONS_PER_QUIZ))
            if len(valid) >= QUESTIONS_PER_QUIZ:
                chosen = valid[:QUESTIONS_PER_QUIZ]
                for question in chosen:
                    question["options"] = list(question["options"])
                    self._shuffle(question["options"])
                logger.info("quiz written on attempt %s", attempt)
                return chosen
            logger.warning("attempt %s gave %s valid questions", attempt, len(valid))
        raise NoValidQuiz(title)

    def _store(self, topic: str, questions: list[dict]) -> str:
        quiz_id = uuid.uuid4().hex
        with self._lock:
            self._quizzes[quiz_id] = {
                "topic": topic,
                "questions": questions,
                "created": self._clock(),
            }
            while len(self._quizzes) > self._limits.max_stored_quizzes:
                self._quizzes.popitem(last=False)
        return quiz_id

    def _forget_expired(self) -> None:
        oldest_allowed = self._clock() - self._limits.quiz_lifetime_seconds
        for quiz_id in [k for k, q in self._quizzes.items() if q["created"] < oldest_allowed]:
            del self._quizzes[quiz_id]

    @staticmethod
    def _public(quiz_id: str, quiz: dict) -> dict:
        return {
            "quiz_id": quiz_id,
            "topic": quiz["topic"],
            "questions": [
                {"question": q["question"], "options": q["options"]}
                for q in quiz["questions"]
            ],
        }
