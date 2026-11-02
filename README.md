# Enablement Coach

Turns a document, a web page or your own notes into a five-question quiz. Give it a
job posting and it researches the key skills instead, so you can practise for the role.
Gemini writes the questions on Google Cloud, code checks them, and code does the marking.

**Try it:** https://coach.devesh-singh.com

Anyone can take the sample quizzes without signing in. Using your own material needs a
Google sign-in. The longer-term aim is to turn any team's documents into quizzes and
practice conversations.

## How it works

```
Page on Cloudflare (coach.devesh-singh.com)
   │  guest: a stored sample quiz
   │  signed in with Google through Firebase: upload a file, paste a link, paste text
   ▼  the browser calls the API directly; CORS lets only this site in
Cloud Run service "coach-api" (FastAPI, asia-south1), runs as a service account
   ├─ guest routes: stored samples and marking, with no model calls
   └─ every other /api/ route needs a valid ID token (no token → 401)
        ├─► Gemini on Google Cloud: reads PDFs and links, spots job postings, researches
        │     their key skills with Google Search, then writes the questions
        │     (no key file: Cloud Run supplies short-lived credentials)
        └─► Firestore: the daily counts of model calls
```

- **Sign-in is checked on the server, for every request that uses the model.** The
  browser sends the ID token Firebase gave it, and the server checks Google's signature
  before reading the request body. New API routes need sign-in unless they are added to
  the guest list. If a token can't be checked, the request is refused.
- **Guests can't spend anything.** Sample quizzes were written by Gemini once, checked,
  and stored as files by `scripts/make_samples.py`. A guest sends nothing to the model.

- **The model's output is checked, not trusted.** Each question needs four different
  options and an answer that is one of them. If too few pass, the app tries again, up
  to three times. Options are then shuffled, because Gemini tends to list the correct
  answer first.
- **A job posting changes the quiz.** Gemini decides whether the material is a job
  posting and names its key skills. It searches the web for each one and writes
  practice questions from those notes, and the sources are listed after the quiz. If
  the search fails, the visitor gets an ordinary quiz on the posting.
- **Pages can read differently each time.** Pages built by scripts can return nothing,
  part of the page or all of it. The app tries twice, keeps the longest result, and
  then asks Google Search for the page.
- **Busy spells are expected.** When Google has no capacity, the app waits and retries,
  then moves from the India region to Google's global endpoint, where processing may
  happen outside India. Other errors aren't retried.
- **Answers stay on the server** until the quiz is submitted, and marking compares text
  in code. The model plays no part in the score.
- **This server never fetches a link.** Gemini's URL context tool reads the page on
  Google's side, so a crafted link can't reach anything private to this service, such
  as the metadata server that issues its credentials.
- **Visitor material is treated as data.** It is placed between tags and the model is
  told not to follow instructions inside it. The model has no tools when it writes
  questions, and its output is shown as plain text, never as HTML.
- **Uploads are checked and not stored.** Files are limited to 5 MB and held in memory.
  PDFs must really be PDFs, and a Word file's unpacked size is checked before it is read.
- **Cost is limited.** Each model call takes one from a daily budget of 200, and a web
  search takes 10. Each user gets 30 calls a day, where a search counts as one. Both are
  counted in a Firestore transaction, by a random Firebase ID rather than a name or email,
  so that a restart can't reset them. If Firestore can't be reached, quizzes are refused.
  One address can make six quizzes an hour.

## What isn't built yet

Chat and saved progress. The six-an-hour limit is held in memory, so it resets on a restart.

## Run it locally

Needs Python 3.12 or later and `gcloud auth application-default login`.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
GOOGLE_CLOUD_PROJECT=your-project GOOGLE_CLOUD_LOCATION=asia-south1 \
  .venv/bin/uvicorn app.main:app --reload
```

The tests use a fake model and fake sign-in, so they need no cloud account. A local run
keeps the daily counts in memory; Firestore is used only on Cloud Run. Sign-in works on
`localhost` because Firebase allows it by default.

## Layout

| Path | What it does |
|---|---|
| `app/coach.py` | Validation, limits and marking. No cloud code, so it can be tested on its own |
| `app/auth.py` | Checks the ID token on each signed-in request |
| `app/budget.py` | The daily limits, in memory or in Firestore |
| `app/sources.py` | Checks links and uploads, and extracts text from files |
| `app/gemini.py` | The only file that calls Gemini |
| `app/main.py` | The web layer: routes and error messages |
| `app/samples/` | The stored sample quizzes guests can take |
| `app/topics/` | Study notes the Google Cloud sample was written from |
| `scripts/make_samples.py` | Rewrites the samples with real Gemini, run by hand |
| `static/index.html` | The page |
