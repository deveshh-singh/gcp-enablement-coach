# Progress

## First slice
- One topic: Gemini writes five questions, code validates them, and code marks the answers.
- `gemini-3.5-flash` in `asia-south1`. `gemini-2.5-flash` returned 404 for this project.
- 27 tests pass with a fake model. A live local run produced valid quizzes in about 14 seconds.
- Gemini listed the correct answer first in most questions, so options are shuffled in code.

## Next
- Sign-in, per-user limits and a persistent daily counter.
- Chat grounded in the topic notes.
- A custom domain.
