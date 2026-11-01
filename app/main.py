"""Web layer: turns HTTP requests into calls on the Coach, and errors into replies."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.coach import (
    Coach,
    DailyLimitReached,
    NoValidQuiz,
    QuizNotFound,
    TopicNotFound,
    WrongAnswerCount,
)
from app.gemini import GeminiWriter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("coach")

INDEX_PAGE = Path(__file__).parent.parent / "static" / "index.html"

# Cloud Run reserves some paths ending in "z", so the health check is not /healthz.
HEALTH_PATH = "/api/health"


class QuizRequest(BaseModel):
    topic: str = Field(max_length=64)


class GradeRequest(BaseModel):
    quiz_id: str = Field(max_length=64)
    answers: list[str | None] = Field(max_length=20)


def create_app(coach: Coach | None = None) -> FastAPI:
    coach = coach or Coach(GeminiWriter())
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
        return FileResponse(INDEX_PAGE)

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
        try:
            return coach.new_quiz(body.topic)
        except TopicNotFound:
            raise HTTPException(404, "That topic isn't available.")
        except DailyLimitReached:
            raise HTTPException(429, "Today's question limit has been reached. Please try tomorrow.")
        except NoValidQuiz:
            raise HTTPException(502, "The questions didn't pass the checks. Please try again.")
        except Exception:
            logger.exception("quiz generation failed")
            raise HTTPException(502, "Couldn't write a quiz just now. Please try again.")

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
