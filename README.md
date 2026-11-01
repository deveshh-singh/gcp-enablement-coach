# Enablement Coach

Turns study notes into a short quiz, then scores it and explains each answer. Gemini
writes the questions on Google Cloud, code checks them, and code does the marking.

This is an early version with one built-in topic, Google Cloud Basics. The longer-term
aim is to turn any team's documents into quizzes and practice conversations.

## How it works

```
Browser
   │  POST /api/quiz, POST /api/grade
   ▼
Cloud Run service "coach-api" (FastAPI, asia-south1), runs as a service account
   │  no key file: Cloud Run supplies short-lived credentials
   ▼
Gemini on Google Cloud (response schema: a fixed JSON shape)
```

- **The model's output is checked, not trusted.** Each question needs four different
  options and an answer that is one of them. If too few pass, the app tries again, up
  to three times. Options are then shuffled, because Gemini tends to list the correct
  answer first.
- **Answers stay on the server** until the quiz is submitted, and marking compares text
  in code. The model plays no part in the score.
- **Model calls are limited.** Quizzes requested within 30 seconds share one model call,
  and there is a daily limit of 200 calls. With a single instance, that is enough to
  keep the cost low. The daily count is held in memory, so it resets when the instance
  restarts. The 30-second rule still applies, so a restart can't cause a flood of calls.
  Moving the count to a database is the next step.
- **User input never reaches the model.** Topics come from a fixed list, which removes
  the risk of prompt injection in this version.

## What isn't built yet

Sign-in, per-user limits, saved progress, chat, uploading your own documents and a
custom domain. There is no sign-in in this version, so anyone with the link can use it.

## Run it locally

Needs Python 3.12 or later and `gcloud auth application-default login`.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
GOOGLE_CLOUD_PROJECT=your-project GOOGLE_CLOUD_LOCATION=asia-south1 \
  .venv/bin/uvicorn app.main:app --reload
```

The tests use a fake model, so they need no cloud account.

## Layout

| Path | What it does |
|---|---|
| `app/coach.py` | Validation, limits and marking. No cloud code, so it can be tested on its own |
| `app/gemini.py` | The only file that calls Gemini |
| `app/main.py` | The web layer: routes and error messages |
| `app/topics/` | Study notes for the built-in topic |
| `static/index.html` | The page |
