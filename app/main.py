"""Web layer: turns HTTP requests into calls on the Coach, and errors into replies."""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import sources
from app.budget import Budget, FirestoreBudget, MemoryBudget
from app.coach import (
    Coach,
    DailyLimitReached,
    NoValidQuiz,
    QuizNotFound,
    TopicNotFound,
    VisitorLimitReached,
    WrongAnswerCount,
)
from app.gemini import Gemini, Unreadable

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("coach")

STATIC = Path(__file__).parent.parent / "static"

# Cloud Run reserves some paths ending in "z", so the health check is not /healthz.
HEALTH_PATH = "/api/health"


class Reader(Protocol):
    def read_link(self, link: str) -> str: ...
    def read_pdf(self, data: bytes) -> str: ...


class QuizRequest(BaseModel):
    topic: str = Field(max_length=64)


class TextRequest(BaseModel):
    text: str = Field(max_length=200_000)


class LinkRequest(BaseModel):
    link: str = Field(max_length=2_000)


class GradeRequest(BaseModel):
    quiz_id: str = Field(max_length=64)
    answers: list[str | None] = Field(max_length=20)


def visitor_of(request: Request) -> str:
    # Google adds the real address as the last entry. Earlier entries come from
    # the visitor's own request, so they can't be trusted.
    forwarded = request.headers.get("x-forwarded-for", "")
    last = forwarded.split(",")[-1].strip()
    return last or (request.client.host if request.client else "unknown")


@contextmanager
def friendly_errors():
    """Turn known problems into plain messages. Never show internal details."""
    try:
        yield
    except HTTPException:
        raise
    except TopicNotFound:
        raise HTTPException(404, "That topic isn't available.")
    except sources.TooLittleMaterial:
        raise HTTPException(422, "There isn't enough text to write questions from. Please use at least a few sentences.")
    except sources.FileTooLarge:
        raise HTTPException(413, "That file is too large. The limit is 5 MB.")
    except sources.UnsupportedFile:
        raise HTTPException(415, "Please use a PDF, Word (.docx), .txt or .md file.")
    except sources.BadLink:
        raise HTTPException(422, "That doesn't look like a public web address.")
    except Unreadable:
        raise HTTPException(422, "Couldn't read that. Some sites block automated reading, so try copying the text and pasting it instead.")
    except VisitorLimitReached:
        raise HTTPException(429, "You've made several quizzes in the last hour. Please try again a little later.")
    except DailyLimitReached:
        raise HTTPException(429, "Today's limit has been reached. Please try again tomorrow.")
    except NoValidQuiz:
        raise HTTPException(502, "The questions didn't pass the checks. Please try again.")
    except Exception:
        # Logged without the visitor's material, which must not reach the logs.
        logger.exception("quiz failed")
        raise HTTPException(502, "Couldn't write a quiz just now. Please try again.")


def default_budget() -> Budget:
    # Local runs have no Firestore unless asked for. Cloud Run sets K_SERVICE itself.
    return FirestoreBudget() if os.environ.get("K_SERVICE") else MemoryBudget()


def create_app(coach: Coach | None = None, reader: Reader | None = None) -> FastAPI:
    gemini = Gemini()
    coach = coach or Coach(gemini.write_questions, default_budget())
    reader = reader or gemini
    app = FastAPI(title="Enablement Coach", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; base-uri 'none'; form-action 'none'"
        )
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")

    @app.get(HEALTH_PATH)
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/topics")
    def topics() -> list[dict]:
        return coach.topics()

    # Plain `def`, not `async def`: FastAPI runs these on worker threads, so a
    # slow model call doesn't hold up other requests.
    @app.post("/api/quiz")
    def quiz(body: QuizRequest) -> dict:
        with friendly_errors():
            return coach.new_quiz(body.topic)

    @app.post("/api/quiz/text")
    def quiz_from_text(body: TextRequest, request: Request) -> dict:
        with friendly_errors():
            material = sources.clean_material(body.text)
            coach.check_visitor(visitor_of(request))
            return coach.quiz_from_material(material)

    @app.post("/api/quiz/link")
    def quiz_from_link(body: LinkRequest, request: Request) -> dict:
        with friendly_errors():
            link = sources.check_link(body.link)
            coach.check_visitor(visitor_of(request))
            coach.spend_call()
            return coach.quiz_from_material(sources.clean_material(reader.read_link(link)))

    @app.post("/api/quiz/file")
    def quiz_from_file(request: Request, file: UploadFile = File()) -> dict:
        with friendly_errors():
            name = file.filename or ""
            data = file.file.read(sources.MAX_FILE_BYTES + 1)
            kind = sources.kind_of_file(name, data)
            if kind == sources.PDF:
                coach.check_visitor(visitor_of(request))
                coach.spend_call()
                text = reader.read_pdf(data)
            else:
                text = sources.text_from_file(name, data)
                sources.clean_material(text)
                coach.check_visitor(visitor_of(request))
            return coach.quiz_from_material(sources.clean_material(text))

    @app.post("/api/grade")
    def grade(body: GradeRequest) -> dict:
        try:
            return coach.grade(body.quiz_id, body.answers)
        except QuizNotFound:
            raise HTTPException(404, "This quiz has expired. Please start a new one.")
        except WrongAnswerCount:
            raise HTTPException(422, "Please send one answer for each question.")

    return app


app = create_app()
