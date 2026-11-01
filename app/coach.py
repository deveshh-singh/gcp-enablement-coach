"""Quiz logic: validate what the model wrote, limit model calls, grade in code.

Nothing here talks to Google. The model call is passed in as `generate`, so the
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

logger = logging.getLogger("coach")

TOPICS_DIR = Path(__file__).parent / "topics"
TOPICS = {
    "google-cloud-basics": {
        "title": "Google Cloud Basics",
        "file": "google-cloud-basics.md",
    },
}

QUESTIONS_PER_QUIZ = 5
SECONDS_PER_DAY = 86_400

Generate = Callable[[str, str, int], list[dict]]


class TopicNotFound(Exception):
    """The requested topic is not one of the built-in topics."""


class DailyLimitReached(Exception):
    """The day's budget of model calls has been used."""


class NoValidQuiz(Exception):
    """The model did not produce enough valid questions."""


class QuizNotFound(Exception):
    """The quiz has expired or the server has restarted."""


class WrongAnswerCount(Exception):
    """The number of answers does not match the number of questions."""


@dataclass(frozen=True)
class Limits:
    daily_model_calls: int = 200
    cooldown_seconds: int = 30
    attempts_per_quiz: int = 3
    quiz_lifetime_seconds: int = 3_600
    max_stored_quizzes: int = 500


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
        generate: Generate,
        limits: Limits = Limits(),
        clock: Callable[[], float] = time.time,
        shuffle: Callable[[list], None] = random.shuffle,
    ):
        self._generate = generate
        self._limits = limits
        self._clock = clock
        self._shuffle = shuffle
        self._lock = threading.Lock()
        self._quizzes: OrderedDict[str, dict] = OrderedDict()
        self._latest: dict[str, str] = {}
        self._calls_today = 0
        self._day = self._today()

    def _today(self) -> int:
        return int(self._clock() // SECONDS_PER_DAY)

    def topics(self) -> list[dict]:
        return [{"id": key, "title": t["title"]} for key, t in TOPICS.items()]

    def new_quiz(self, topic: str) -> dict:
        """Return a quiz for players. It never includes answers."""
        if topic not in TOPICS:
            raise TopicNotFound(topic)

        # One lock means one model call at a time. Anyone else waiting then
        # receives the quiz that call produced, which is what keeps a burst of
        # visitors from turning into a burst of model calls.
        with self._lock:
            self._forget_expired()
            latest = self._quizzes.get(self._latest.get(topic, ""))
            age = self._clock() - latest["created"] if latest else None
            if latest and age < self._limits.cooldown_seconds:
                return self._public(self._latest[topic], latest)

            questions = self._write_questions(topic)
            quiz_id = uuid.uuid4().hex
            quiz = {"topic": topic, "questions": questions, "created": self._clock()}
            self._quizzes[quiz_id] = quiz
            self._latest[topic] = quiz_id
            while len(self._quizzes) > self._limits.max_stored_quizzes:
                self._quizzes.popitem(last=False)
            return self._public(quiz_id, quiz)

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

    def _write_questions(self, topic: str) -> list[dict]:
        pack = (TOPICS_DIR / TOPICS[topic]["file"]).read_text(encoding="utf-8")
        for attempt in range(1, self._limits.attempts_per_quiz + 1):
            self._spend_one_call()
            valid = keep_valid(self._generate(TOPICS[topic]["title"], pack, QUESTIONS_PER_QUIZ))
            if len(valid) >= QUESTIONS_PER_QUIZ:
                chosen = valid[:QUESTIONS_PER_QUIZ]
                for question in chosen:
                    question["options"] = list(question["options"])
                    self._shuffle(question["options"])
                logger.info("quiz written on attempt %s; %s calls today", attempt, self._calls_today)
                return chosen
            logger.warning("attempt %s gave %s valid questions", attempt, len(valid))
        raise NoValidQuiz(topic)

    def _spend_one_call(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._calls_today = 0
        if self._calls_today >= self._limits.daily_model_calls:
            raise DailyLimitReached()
        self._calls_today += 1

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
