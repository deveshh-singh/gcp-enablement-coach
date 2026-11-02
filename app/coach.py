"""Quiz logic: validate what the model wrote, limit model calls, grade in code.

Nothing here talks to Google. The model call and the budget are passed in, so the
tests can run without a network connection or any cloud account.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from app.budget import DAY_USED, USER_USED, Budget, MemoryBudget
from app.sources import MIN_MATERIAL_CHARS, Unreadable

logger = logging.getLogger("coach")

SAMPLES_DIR = Path(__file__).parent / "samples"

QUESTIONS_PER_QUIZ = 5
CUSTOM = "custom"

# A web search costs more than a normal model call and can run several queries, so
# it takes 10 from the daily budget of 200. That allows 20 searches a day at most.
SEARCH_COST = 10

# Below this length, a page read has probably caught a loading screen.
MIN_PAGE_CHARS = 600
MIN_NOTES_CHARS = 200
MAX_TOPICS = 5
MAX_SOURCES = 6

Write = Callable[[str, str, int], list[dict]]


class LinkReader(Protocol):
    def read_link(self, link: str) -> str: ...
    def find_link(self, link: str) -> str: ...


class JobPreparer(Protocol):
    def analyse(self, material: str) -> dict: ...
    def research(self, role: str, topics: list[str]) -> dict: ...
    def write_job_questions(self, role: str, notes: str, count: int) -> list[dict]: ...


class SampleNotFound(Exception):
    """The requested quiz is not one of the stored samples."""


class DailyLimitReached(Exception):
    """The day's budget of model calls has been used."""


class UserLimitReached(Exception):
    """This user has used their share of today's model calls."""


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
    attempts_per_quiz: int = 3
    quiz_lifetime_seconds: int = 3_600
    max_stored_quizzes: int = 500
    visitor_quizzes_per_hour: int = 6
    link_reads: int = 2


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


def load_samples(folder: Path = SAMPLES_DIR) -> dict[str, dict]:
    """Read the stored sample quizzes. A bad file stops the app from starting,
    because a guest should never meet a broken question."""
    samples = {}
    for path in sorted(folder.glob("*.json")):
        sample = json.loads(path.read_text(encoding="utf-8"))
        if len(keep_valid(sample["questions"])) != QUESTIONS_PER_QUIZ:
            raise ValueError(f"{path.name} needs {QUESTIONS_PER_QUIZ} valid questions")
        samples[path.stem] = {"topic": "sample", "questions": sample["questions"], "job": sample["job"]}
    return samples


class Coach:
    def __init__(
        self,
        write: Write,
        budget: Budget | None = None,
        limits: Limits = Limits(),
        clock: Callable[[], float] = time.time,
        shuffle: Callable[[list], None] = random.shuffle,
        preparer: JobPreparer | None = None,
        samples: dict[str, dict] | None = None,
    ):
        self._write = write
        self._preparer = preparer
        self._budget = budget or MemoryBudget(clock=clock)
        self._limits = limits
        self._clock = clock
        self._shuffle = shuffle
        self._lock = threading.Lock()
        self._quizzes: OrderedDict[str, dict] = OrderedDict()
        self._samples = load_samples() if samples is None else samples
        self._visitors: dict[str, list[float]] = {}

    def spend_call(self, user: str, units: int = 1) -> None:
        """Pay for a model call, or stop if a daily limit has been reached."""
        result = self._budget.spend(user, units)
        if result == DAY_USED:
            raise DailyLimitReached()
        if result == USER_USED:
            raise UserLimitReached()

    def sample(self, sample_id: str) -> dict:
        """Return a stored quiz, with no answers. This makes no model call and
        keeps nothing, so anyone can call it as often as they like."""
        if sample_id not in self._samples:
            raise SampleNotFound(sample_id)
        return self._public(sample_id, self._samples[sample_id])

    def read_link(self, reader: LinkReader, link: str, user: str) -> str:
        """Read a page, allowing for sites that only sometimes come back whole.

        Pages built by scripts can return nothing, part of the page or all of it
        from one try to the next. So this tries more than once, keeps the longest
        result, and finally asks Google Search for the page instead.
        """
        best = ""
        for _ in range(self._limits.link_reads):
            self.spend_call(user)
            try:
                text = reader.read_link(link)
            except Unreadable:
                continue
            if len(text.strip()) >= MIN_PAGE_CHARS:
                return text
            best = max(best, text, key=len)

        self.spend_call(user, SEARCH_COST)
        try:
            best = max(best, reader.find_link(link), key=len)
        except Unreadable:
            logger.info("search found nothing for a link")
        if len(best.strip()) < MIN_MATERIAL_CHARS:
            raise Unreadable()
        return best

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

    def quiz_from_material(self, material: str, user: str, title: str = "the material provided") -> dict:
        """Return a quiz on a visitor's own material. The material is not kept.

        A job posting gets a different quiz: practice on the skills the job needs,
        researched on the web, rather than questions about the posting's wording.
        """
        job = self._job_in(material, user)
        if job:
            notes = self._research(job, user)
            if notes:
                job["sources"] = notes["sources"]
                questions = self._write_questions(
                    job["role"], notes["notes"], user, self._preparer.write_job_questions
                )
                return self._stored_quiz(questions, job)
        return self._stored_quiz(self._write_questions(title, material, user), None)

    def _stored_quiz(self, questions: list[dict], job: dict | None) -> dict:
        quiz_id = self._store(CUSTOM, questions, job)
        with self._lock:
            return self._public(quiz_id, self._quizzes[quiz_id])

    def _job_in(self, material: str, user: str) -> dict | None:
        """Return the role and key skills if this is a job posting.

        These come from the visitor's material and go into a search prompt, so
        they are cut to a fixed number of short strings first.
        """
        if self._preparer is None:
            return None
        self.spend_call(user)
        try:
            found = self._preparer.analyse(material)
        except Exception:
            logger.exception("couldn't analyse the material")
            return None

        role = " ".join(str(found.get("role", "")).split())[:120]
        topics = [" ".join(str(t).split())[:80] for t in found.get("topics", [])]
        topics = [t for t in topics if t][:MAX_TOPICS]
        if not found.get("is_job_posting") or not role or not topics:
            return None
        return {
            "role": role,
            "topics": topics,
            "sources": [],
            "closed": bool(found.get("is_closed")),
        }

    def _research(self, job: dict, user: str) -> dict | None:
        """Search the web for the skills. If that fails, the caller falls back to
        an ordinary quiz on the posting, which is better than no quiz."""
        self.spend_call(user, SEARCH_COST)
        try:
            notes = self._preparer.research(job["role"], job["topics"])
        except Exception:
            logger.exception("research failed")
            return None
        if len(notes.get("notes", "").strip()) < MIN_NOTES_CHARS:
            return None
        return {
            "notes": notes["notes"],
            "sources": [
                {"title": str(s["title"])[:100], "url": s["url"]}
                for s in notes.get("sources", [])
                if str(s.get("url", "")).startswith("https://")
            ][:MAX_SOURCES],
        }

    def grade(self, quiz_id: str, answers: list[str | None]) -> dict:
        """Mark the answers by comparing text. The model plays no part in this."""
        quiz = self._samples.get(quiz_id) or self.full_quiz(quiz_id)
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
            "job": quiz.get("job"),
        }

    def full_quiz(self, quiz_id: str) -> dict:
        """Return a stored quiz with its answers. Never send this to a browser."""
        with self._lock:
            self._forget_expired()
            quiz = self._quizzes.get(quiz_id)
        if quiz is None:
            raise QuizNotFound(quiz_id)
        return quiz

    def _write_questions(
        self, title: str, material: str, user: str, write: Write | None = None
    ) -> list[dict]:
        write = write or self._write
        for attempt in range(1, self._limits.attempts_per_quiz + 1):
            self.spend_call(user)
            valid = keep_valid(write(title, material, QUESTIONS_PER_QUIZ))
            if len(valid) >= QUESTIONS_PER_QUIZ:
                chosen = valid[:QUESTIONS_PER_QUIZ]
                for question in chosen:
                    question["options"] = list(question["options"])
                    self._shuffle(question["options"])
                logger.info("quiz written on attempt %s", attempt)
                return chosen
            logger.warning("attempt %s gave %s valid questions", attempt, len(valid))
        raise NoValidQuiz(title)

    def _store(self, topic: str, questions: list[dict], job: dict | None = None) -> str:
        quiz_id = uuid.uuid4().hex
        with self._lock:
            self._quizzes[quiz_id] = {
                "topic": topic,
                "questions": questions,
                "created": self._clock(),
                "job": job,
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
            "job": quiz.get("job"),
            "questions": [
                {"question": q["question"], "options": q["options"]}
                for q in quiz["questions"]
            ],
        }
