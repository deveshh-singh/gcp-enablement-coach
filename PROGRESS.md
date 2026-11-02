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

## Busy spells
- A live link failed with 429 RESOURCE_EXHAUSTED while later calls succeeded, so Gemini
  calls now retry and then fall back to the global endpoint.

## Own address
- The page is on Cloudflare at coach.devesh-singh.com, as static files on Workers, because
  Wrangler now sends Pages publishing there. The API stays on Cloud Run.
- The browser calls Cloud Run directly. A Cloudflare proxy would give up after 125 seconds
  and would hide each visitor's address from the per-visitor limits.
- CORS lets only that site call the API from a browser. It doesn't stop other programs.
- Google's domain mapping isn't listed for `asia-south1`.
- Page changes need both deploys: `npx wrangler@4 deploy` and `gcloud run deploy`.

## Sign-in
- Firebase Authentication with Google sign-in. The page uses a pop-up, because redirect
  sign-in is unreliable when the page isn't hosted on Firebase.
- Guests take two stored quizzes with no model calls. Everything else under `/api/` needs a
  valid ID token, checked before the body is read, so new routes are safe by default.
- Each user gets 30 calls a day, stored in Firestore under a random ID.
- With the public key and no sign-in, Firestore reads and writes were refused, and Gemini
  refuses API keys altogether.
- Eight requests at once made Firestore abort transactions. The limits held, but requests
  failed. The library retries a failed save but not an aborted read, so the code now retries
  the whole transaction after a short random wait.

## Next
- Chat grounded in the material.
