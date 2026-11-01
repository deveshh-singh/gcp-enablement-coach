"""The only file that talks to Gemini."""
from __future__ import annotations

import json
import os

from google import genai
from google.genai import types
from pydantic import BaseModel

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
TIMEOUT_MS = 60_000


class Question(BaseModel):
    question: str
    options: list[str]
    answer: str
    explanation: str


class QuizDraft(BaseModel):
    questions: list[Question]


PROMPT = """You write multiple-choice questions for people learning {title}.

Use only the study notes below. Do not use any other knowledge.

Write {count} questions. For each one:
- Test understanding of a concept, not memory of a sentence.
- Give exactly 4 different options, of similar length, with one correct.
- Set "answer" to the exact text of the correct option.
- Give a one or two sentence explanation of why that answer is right.
- Use plain British English.

<study_notes>
{pack}
</study_notes>"""


class GeminiWriter:
    """Writes questions with Gemini. The client is built on first use, so
    importing this file needs no cloud credentials."""

    def __init__(self) -> None:
        self._client: genai.Client | None = None

    def _get_client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(
                vertexai=True,
                project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                location=os.environ.get("GOOGLE_CLOUD_LOCATION", "asia-south1"),
                http_options=types.HttpOptions(timeout=TIMEOUT_MS),
            )
        return self._client

    def __call__(self, title: str, pack: str, count: int) -> list[dict]:
        response = self._get_client().models.generate_content(
            model=MODEL,
            contents=PROMPT.format(title=title, pack=pack, count=count),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=QuizDraft,
            ),
        )
        return json.loads(response.text)["questions"]
