# Progress

## First slice
- One topic: Gemini writes five questions, code validates them, and code marks the answers.
- `gemini-3.5-flash` in `asia-south1`. `gemini-2.5-flash` returned 404 for this project.
- 27 tests pass with a fake model. A live local run produced valid quizzes in about 14 seconds.
- Gemini listed the correct answer first in most questions, so options are shuffled in code.
- Deployed to Cloud Run with `gcloud run deploy --source`. The app runs as `coach-sa` and
  a separate `coach-build` account builds it, because default service accounts have no
  roles in this organisation.
- Live checks passed: a full quiz in a browser at phone width, no answers sent before
  submission, repeat requests inside the cooldown sharing one model call.

## Your own material
- Quizzes from an uploaded file (PDF, Word, TXT, Markdown), a link or pasted text.
- Links use Gemini's URL context tool, so this server never fetches them.
- The daily budget moved to Firestore, so a restart can't reset it.
- A new page: one question at a time, drag-and-drop upload and a score screen.

## Job postings
- A job posting now gets practice questions on its key skills, researched with Google
  Search, with sources shown after the quiz.
- Link reading is more reliable: one script-built page gave 3 unreadable, 1 partial and
  1 full result from 5 tries of the same address, so the app now retries and falls back
  to search.

## Next
- Sign-in and per-user limits.
- Chat grounded in the material.
- A custom domain.
