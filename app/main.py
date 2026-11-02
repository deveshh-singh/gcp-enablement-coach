"""Web layer: turns HTTP requests into calls on the Coach, and errors into replies."""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app import sources
from app.auth import FirebaseTokens, NotSignedIn, Tokens, bearer_token
from app.budget import Budget, FirestoreBudget, MemoryBudget
from app.coach import (
    Coach,
    DailyLimitReached,
    NoValidQuiz,
    QuizNotFound,
    SampleNotFound,
    UserLimitReached,
    VisitorLimitReached,
    WrongAnswerCount,
)
from app.gemini import Gemini

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("coach")

STATIC = Path(__file__).parent.parent / "static"

# Cloud Run reserves some paths ending in "z", so the health check is not /healthz.
HEALTH_PATH = "/api/health"

# The public page is on Cloudflare and calls this service directly, so a slow quiz
# doesn't hit Cloudflare's time limit and visitors keep their own addresses.
PUBLIC_SITE = "https://coach.devesh-singh.com"
CLOUD_RUN_URL = "https://coach-api-391985624624.asia-south1.run.app"

# Guests can reach only these. They take nothing from the visitor for the model and
# make no model calls, so everything else under /api/ needs a sign-in by default.
GUEST_PATHS = {HEALTH_PATH, "/api/grade"}
GUEST_PREFIX = "/api/samples/"

# Sign-in needs Firebase's code, Google's sign-in helper and Firebase's sign-in
# frame. The same list is in static/_headers for the copy of the page on Cloudflare.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline' https://www.gstatic.com https://apis.google.com; "
    "connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com; "
    "frame-src https://enablement-coach-ds26.firebaseapp.com; "
    "base-uri 'none'; form-action 'none'"
)


class Reader(Protocol):
    def read_link(self, link: str) -> str: ...
    def find_link(self, link: str) -> str: ...
    def read_pdf(self, data: bytes) -> str: ...


class TextRequest(BaseModel):
    text: str = Field(max_length=200_000)


class LinkRequest(BaseModel):
    link: str = Field(max_length=2_000)


class GradeRequest(BaseModel):
    quiz_id: str = Field(max_length=64)
    answers: list[str | None] = Field(max_length=20)


def needs_sign_in(path: str) -> bool:
    if not path.startswith("/api/"):
        return False
    return path not in GUEST_PATHS and not path.startswith(GUEST_PREFIX)


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
    except sources.TooLittleMaterial:
        raise HTTPException(422, "There isn't enough text to write questions from. Please use at least a few sentences.")
    except sources.FileTooLarge:
        raise HTTPException(413, "That file is too large. The limit is 5 MB.")
    except sources.UnsupportedFile:
        raise HTTPException(415, "Please use a PDF, Word (.docx), .txt or .md file.")
    except sources.BadLink:
        raise HTTPException(422, "That doesn't look like a public web address.")
    except sources.ModelBusy:
        raise HTTPException(503, "Google's AI service is busy right now. Please try again in a minute.")
    except sources.Unreadable:
        raise HTTPException(422, "Couldn't read that. Some sites block automated reading, so try copying the text and pasting it instead.")
    except UserLimitReached:
        raise HTTPException(429, "You've used today's allowance. Please try again tomorrow.")
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


def create_app(
    coach: Coach | None = None,
    reader: Reader | None = None,
    tokens: Tokens | None = None,
) -> FastAPI:
    gemini = Gemini()
    coach = coach or Coach(gemini.write_questions, default_budget(), preparer=gemini)
    reader = reader or gemini
    tokens = tokens or FirebaseTokens()
    app = FastAPI(title="Enablement Coach", docs_url=None, redoc_url=None, openapi_url=None)

    # Runs before the request body is read, so a stranger's upload is never processed.
    @app.middleware("http")
    async def require_sign_in(request, call_next):
        if not needs_sign_in(request.url.path):
            return await call_next(request)
        try:
            token = bearer_token(request.headers.get("authorization", ""))
            # A worker thread, because the first check fetches Google's public keys.
            request.state.user = await run_in_threadpool(tokens.user_of, token)
        except NotSignedIn:
            return JSONResponse(
                {"detail": "Please sign in to use your own material."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        except Exception:
            logger.exception("couldn't check a sign-in")
            return JSONResponse(
                {"detail": "Sign-in can't be checked right now. Please try again in a minute."},
                status_code=503,
            )
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        return response

    # Browsers let only this site call the API from another address.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[PUBLIC_SITE],
        allow_methods=["GET", "POST"],
        allow_headers=["authorization", "content-type"],
    )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")

    @app.get(HEALTH_PATH)
    def health() -> dict:
        return {"status": "ok"}

    @app.get(GUEST_PREFIX + "{sample_id}")
    def sample(sample_id: str) -> dict:
        try:
            return coach.sample(sample_id)
        except SampleNotFound:
            raise HTTPException(404, "That sample isn't available.")

    # Plain `def`, not `async def`: FastAPI runs these on worker threads, so a
    # slow model call doesn't hold up other requests.
    @app.post("/api/quiz/text")
    def quiz_from_text(body: TextRequest, request: Request) -> dict:
        with friendly_errors():
            material = sources.clean_material(body.text)
            coach.check_visitor(visitor_of(request))
            return coach.quiz_from_material(material, request.state.user)

    @app.post("/api/quiz/link")
    def quiz_from_link(body: LinkRequest, request: Request) -> dict:
        with friendly_errors():
            link = sources.check_link(body.link)
            coach.check_visitor(visitor_of(request))
            text = coach.read_link(reader, link, request.state.user)
            return coach.quiz_from_material(sources.clean_material(text), request.state.user)

    @app.post("/api/quiz/file")
    def quiz_from_file(request: Request, file: UploadFile = File()) -> dict:
        with friendly_errors():
            name = file.filename or ""
            data = file.file.read(sources.MAX_FILE_BYTES + 1)
            kind = sources.kind_of_file(name, data)
            if kind == sources.PDF:
                coach.check_visitor(visitor_of(request))
                coach.spend_call(request.state.user)
                text = reader.read_pdf(data)
                if len(text.strip()) < sources.MIN_MATERIAL_CHARS:
                    raise sources.Unreadable()
            else:
                text = sources.text_from_file(name, data)
                sources.clean_material(text)
                coach.check_visitor(visitor_of(request))
            return coach.quiz_from_material(sources.clean_material(text), request.state.user)

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
