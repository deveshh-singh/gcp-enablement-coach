"""Write the stored sample quizzes with real Gemini.

Run this by hand now and then, read the result, and commit it:

    GOOGLE_CLOUD_PROJECT=<project> GOOGLE_CLOUD_LOCATION=asia-south1 \
        python -m scripts.make_samples

The app itself never writes these. Guests only read the files, which is why the
guest pages make no model calls.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from app.budget import MemoryBudget
from app.coach import SAMPLES_DIR, Coach, keep_valid
from app.gemini import Gemini

HERE = Path(__file__).parent
TOPIC_PACK = HERE.parent / "app" / "topics" / "google-cloud-basics.md"
SAMPLES = {
    "google-cloud-basics": (TOPIC_PACK, "Google Cloud Basics"),
    "sample-job-posting": (HERE / "sample-posting.md", "the material provided"),
}


def real_address(link: str) -> str:
    """Gemini's source links go through a Google redirect that may not last, which
    is fine for a quiz kept for an hour but not for a file kept for months."""
    return httpx.head(link, timeout=20).headers.get("location", link)


def lasting(job: dict | None) -> dict | None:
    if job:
        job["sources"] = [{**s, "url": real_address(s["url"])} for s in job["sources"]]
    return job


def main() -> None:
    gemini = Gemini()
    coach = Coach(gemini.write_questions, MemoryBudget(), preparer=gemini, samples={})
    SAMPLES_DIR.mkdir(exist_ok=True)
    for name, (source, title) in SAMPLES.items():
        quiz = coach.quiz_from_material(source.read_text(encoding="utf-8"), "samples", title)
        full = coach.full_quiz(quiz["quiz_id"])
        sample = {"questions": keep_valid(full["questions"]), "job": lasting(full["job"])}
        path = SAMPLES_DIR / f"{name}.json"
        path.write_text(json.dumps(sample, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{path.name}: {len(sample['questions'])} questions, job: {bool(sample['job'])}")


if __name__ == "__main__":
    main()
