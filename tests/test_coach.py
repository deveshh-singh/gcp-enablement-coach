import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from firebase_admin.auth import ExpiredIdTokenError, InvalidIdTokenError
from google.api_core.exceptions import Aborted
from google.genai import errors

from app.auth import FirebaseTokens, NotSignedIn, bearer_token
from app.budget import DAY_USED, OK, USER_USED, MemoryBudget, retry_when_contended
from app.coach import (
    Coach,
    DailyLimitReached,
    Limits,
    NoValidQuiz,
    QuizNotFound,
    SampleNotFound,
    UserLimitReached,
    VisitorLimitReached,
    WrongAnswerCount,
    load_samples,
    keep_valid,
    problems_with,
)
from app.gemini import Gemini
from app.main import CLOUD_RUN_URL, CONTENT_SECURITY_POLICY, HEALTH_PATH, create_app
from app.sources import ModelBusy, Unreadable

USER = "user-1"
TOKEN = {"Authorization": "Bearer good-token"}
LONG_TEXT = "Service accounts give workloads an identity of their own. " * 10


def question(number=1, **changes):
    base = {
        "question": f"Question {number}?",
        "options": ["A", "B", "C", "D"],
        "answer": "B",
        "explanation": "Because B.",
    }
    return {**base, **changes}


def good_quiz(*_):
    return [question(n) for n in range(1, 6)]


SAMPLES = {
    "sample-one": {"topic": "sample", "questions": good_quiz(), "job": None},
    "sample-job": {
        "topic": "sample",
        "questions": good_quiz(),
        "job": {"role": "AI Engineer", "topics": ["RAG"], "sources": [], "closed": False},
    },
}


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class CountingWriter:
    def __init__(self, reply=good_quiz):
        self.calls = 0
        self.reply = reply
        self.material = None

    def __call__(self, title, material, count):
        self.calls += 1
        self.material = material
        return self.reply(title, material, count)


class FakeReader:
    def __init__(self):
        self.links = []
        self.pdfs = []
        self.searches = []

    def read_link(self, link):
        self.links.append(link)
        return LONG_TEXT * 2

    def find_link(self, link):
        self.searches.append(link)
        return LONG_TEXT

    def read_pdf(self, data):
        self.pdfs.append(data)
        return LONG_TEXT


class FakePreparer:
    def __init__(self, job=True, research_fails=False):
        self.job = job
        self.research_fails = research_fails
        self.researched = None
        self.job_writes = 0

    def analyse(self, material):
        if not self.job:
            return {"is_job_posting": False, "role": "", "topics": []}
        return {"is_job_posting": True, "role": "AI Engineer", "topics": ["RAG", "Agents"]}

    def research(self, role, topics):
        if self.research_fails:
            raise RuntimeError("search down")
        self.researched = (role, topics)
        return {
            "notes": LONG_TEXT,
            "sources": [
                {"title": "example.com", "url": "https://example.com/a"},
                {"title": "bad", "url": "javascript:alert(1)"},
            ],
        }

    def write_job_questions(self, role, notes, count):
        self.job_writes += 1
        return good_quiz()


class FakeTokens:
    """Stands in for Firebase. Only "good-token" and "other-token" are accepted."""

    def user_of(self, token):
        users = {"good-token": USER, "other-token": "user-2"}
        if token not in users:
            raise NotSignedIn()
        return users[token]


def make_coach(writer=None, clock=None, daily_model_calls=200, preparer=None, user_model_calls=30, **limits):
    clock = clock or Clock()
    return Coach(
        writer or CountingWriter(),
        MemoryBudget(daily_model_calls, clock, user_calls=user_model_calls),
        Limits(**limits),
        clock=clock,
        shuffle=lambda options: options.reverse(),
        preparer=preparer,
        samples=SAMPLES,
    )


def guest(coach=None, reader=None):
    app = create_app(coach or make_coach(), reader or FakeReader(), FakeTokens())
    return TestClient(app, raise_server_exceptions=False)


def signed_in(coach=None, reader=None):
    client = guest(coach, reader)
    client.headers.update(TOKEN)
    return client


@pytest.mark.parametrize(
    "changes",
    [
        {"answer": "E"},
        {"options": ["A", "B", "C"]},
        {"options": ["A", "B", "C", "D", "B"]},
        {"options": ["A", "B", "B", "D"]},
        {"options": ["A", "B", " ", "D"]},
        {"options": "ABCD"},
        {"question": "  "},
        {"explanation": ""},
    ],
)
def test_bad_questions_are_rejected(changes):
    assert problems_with(question(**changes))


def test_good_question_passes():
    assert problems_with(question()) == []


def test_repeated_questions_are_dropped():
    assert len(keep_valid([question(1), question(1), question(2)])) == 2


def test_retries_until_enough_questions_pass():
    replies = iter([[question(1)], good_quiz()])
    writer = CountingWriter(lambda *_: next(replies))
    quiz = make_coach(writer).quiz_from_material(LONG_TEXT, USER)
    assert writer.calls == 2
    assert len(quiz["questions"]) == 5


def test_gives_up_after_the_allowed_attempts():
    writer = CountingWriter(lambda *_: [question(1, answer="E")])
    with pytest.raises(NoValidQuiz):
        make_coach(writer, attempts_per_quiz=3).quiz_from_material(LONG_TEXT, USER)
    assert writer.calls == 3


def test_quiz_for_players_contains_no_answers():
    quiz = make_coach().quiz_from_material(LONG_TEXT, USER)
    assert all(set(q) == {"question", "options"} for q in quiz["questions"])


def test_daily_limit_stops_model_calls_then_resets_next_day():
    writer, clock = CountingWriter(), Clock()
    coach = make_coach(writer, clock, daily_model_calls=2)
    coach.quiz_from_material(LONG_TEXT, USER)
    clock.now += 1
    coach.quiz_from_material(LONG_TEXT, USER)
    clock.now += 1
    with pytest.raises(DailyLimitReached):
        coach.quiz_from_material(LONG_TEXT, USER)
    assert writer.calls == 2
    clock.now += 86_400
    coach.quiz_from_material(LONG_TEXT, USER)
    assert writer.calls == 3


def test_failed_attempts_count_against_the_daily_limit():
    writer = CountingWriter(lambda *_: [])
    coach = make_coach(writer, daily_model_calls=2, attempts_per_quiz=5)
    with pytest.raises(DailyLimitReached):
        coach.quiz_from_material(LONG_TEXT, USER)
    assert writer.calls == 2


def test_grading_is_done_by_code():
    coach = make_coach()
    quiz = coach.quiz_from_material(LONG_TEXT, USER)
    result = coach.grade(quiz["quiz_id"], ["B", "B", "A", None, "B"])
    assert (result["score"], result["total"]) == (3, 5)
    assert [r["correct"] for r in result["results"]] == [True, True, False, False, True]
    assert result["results"][2]["correct_answer"] == "B"


def test_shuffling_options_does_not_break_grading():
    coach = make_coach()
    quiz = coach.quiz_from_material(LONG_TEXT, USER)
    assert quiz["questions"][0]["options"] == ["D", "C", "B", "A"]
    assert coach.grade(quiz["quiz_id"], ["B"] * 5)["score"] == 5


def test_wrong_number_of_answers_is_refused():
    coach = make_coach()
    quiz = coach.quiz_from_material(LONG_TEXT, USER)
    with pytest.raises(WrongAnswerCount):
        coach.grade(quiz["quiz_id"], ["B"])


def test_quiz_expires():
    clock = Clock()
    coach = make_coach(clock=clock, quiz_lifetime_seconds=60)
    quiz = coach.quiz_from_material(LONG_TEXT, USER)
    clock.now += 61
    with pytest.raises(QuizNotFound):
        coach.grade(quiz["quiz_id"], ["B"] * 5)


def test_stored_quizzes_are_capped():
    clock = Clock()
    coach = make_coach(clock=clock, max_stored_quizzes=2)
    oldest = coach.quiz_from_material(LONG_TEXT, USER)
    for _ in range(2):
        clock.now += 1
        coach.quiz_from_material(LONG_TEXT, USER)
    with pytest.raises(QuizNotFound):
        coach.grade(oldest["quiz_id"], ["B"] * 5)


def test_visitor_limit_resets_after_an_hour():
    clock = Clock()
    coach = make_coach(clock=clock, visitor_quizzes_per_hour=2)
    coach.check_visitor("a")
    coach.check_visitor("a")
    coach.check_visitor("b")
    with pytest.raises(VisitorLimitReached):
        coach.check_visitor("a")
    clock.now += 3_601
    coach.check_visitor("a")


@pytest.fixture
def reader():
    return FakeReader()


@pytest.fixture
def writer():
    return CountingWriter()


@pytest.fixture
def client(reader, writer):
    return signed_in(make_coach(writer), reader)


@pytest.fixture
def stranger(reader, writer):
    return guest(make_coach(writer), reader)


def test_page_and_health_load(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Enablement Coach" in page.text
    assert page.headers["x-content-type-options"] == "nosniff"
    assert client.get(HEALTH_PATH).json() == {"status": "ok"}
    assert client.get("/favicon.svg").status_code == 200


def test_quiz_and_grade_over_http(client):
    quiz = client.post("/api/quiz/text", json={"text": LONG_TEXT}).json()
    assert '"answer"' not in str(quiz).replace("'", '"')
    graded = client.post("/api/grade", json={"quiz_id": quiz["quiz_id"], "answers": ["B"] * 5})
    assert graded.json()["score"] == 5


def test_http_errors_are_friendly(client):
    assert client.get("/api/samples/nope").status_code == 404
    assert client.post("/api/grade", json={"quiz_id": "nope", "answers": []}).status_code == 404
    assert client.post("/api/quiz/text", json={}).status_code == 422


def test_model_failure_returns_502_without_details(reader):
    def broken(*_):
        raise RuntimeError("secret internal detail")

    client = signed_in(make_coach(broken), reader)
    response = client.post("/api/quiz/text", json={"text": LONG_TEXT})
    assert response.status_code == 502
    assert "secret" not in response.text


def test_budget_failure_refuses_the_quiz(reader):
    class BrokenBudget:
        def spend(self, user, units=1):
            raise RuntimeError("database down")

    writer = CountingWriter()
    coach = Coach(writer, BrokenBudget(), samples=SAMPLES)
    client = signed_in(coach, reader)
    assert client.post("/api/quiz/text", json={"text": LONG_TEXT}).status_code == 502
    assert writer.calls == 0


def test_daily_limit_returns_429(reader):
    coach = make_coach(daily_model_calls=0)
    client = signed_in(coach, reader)
    assert client.post("/api/quiz/text", json={"text": LONG_TEXT}).status_code == 429


def test_quiz_from_pasted_text(client, writer):
    response = client.post("/api/quiz/text", json={"text": LONG_TEXT})
    assert response.status_code == 200
    assert "Service accounts" in writer.material


def test_short_text_is_refused_without_a_model_call(client, writer):
    assert client.post("/api/quiz/text", json={"text": "Too short"}).status_code == 422
    assert writer.calls == 0


def test_quiz_from_link_is_read_by_the_reader(client, reader, writer):
    response = client.post("/api/quiz/link", json={"link": "example.com/page"})
    assert response.status_code == 200
    assert reader.links == ["https://example.com/page"]
    assert writer.calls == 1


@pytest.mark.parametrize(
    "link",
    [
        "http://169.254.169.254/computeMetadata/v1/",
        "http://metadata.google.internal/",
        "https://127.0.0.1/",
        "http://[::1]/",
        "http://localhost/",
        "file:///etc/passwd",
        "ftp://example.com/file",
        "https://user:pw@example.com/",
    ],
)
def test_private_and_odd_links_are_refused(client, reader, link):
    assert client.post("/api/quiz/link", json={"link": link}).status_code == 422
    assert reader.links == []


def test_unreadable_link_gets_a_helpful_message(writer):
    class BlockedReader(FakeReader):
        def read_link(self, link):
            raise Unreadable()

        def find_link(self, link):
            raise Unreadable()

    client = signed_in(make_coach(writer), BlockedReader())
    response = client.post("/api/quiz/link", json={"link": "example.com"})
    assert response.status_code == 422
    assert "pasting" in response.json()["detail"]
    assert writer.calls == 0


def upload(client, name, data):
    return client.post("/api/quiz/file", files={"file": (name, data)})


def test_text_and_markdown_uploads(client, writer):
    assert upload(client, "notes.txt", LONG_TEXT.encode()).status_code == 200
    assert upload(client, "notes.md", LONG_TEXT.encode()).status_code == 200
    assert writer.calls == 2


def test_pdf_upload_goes_to_the_reader(client, reader):
    assert upload(client, "notes.pdf", b"%PDF-1.7 content").status_code == 200
    assert reader.pdfs == [b"%PDF-1.7 content"]


def test_word_upload(client, writer):
    import io
    import zipfile

    body = (
        '<w:document><w:p><w:r><w:t xml:space="preserve">Fish &amp; chips. </w:t></w:r>'
        f"<w:r><w:t>{LONG_TEXT}</w:t></w:r></w:p><w:p><w:r><w:t>Second</w:t></w:r></w:p></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", body)
    assert upload(client, "notes.docx", buffer.getvalue()).status_code == 200
    assert "Fish & chips." in writer.material
    assert "\nSecond" in writer.material


@pytest.mark.parametrize(
    "name,data,status",
    [
        ("notes.exe", b"MZ", 415),
        ("notes.pdf", b"not really a pdf", 415),
        ("notes.docx", b"not a zip", 415),
        ("notes.txt", b"\xff\xfe\xff", 415),
        ("notes.txt", b"short", 422),
        ("notes.txt", b"x" * (5 * 1024 * 1024 + 1), 413),
    ],
)
def test_bad_uploads_are_refused_without_a_model_call(client, reader, writer, name, data, status):
    assert upload(client, name, data).status_code == status
    assert writer.calls == 0
    assert reader.pdfs == []


def test_word_file_that_unpacks_too_large_is_refused(client, writer):
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"a" * (21 * 1024 * 1024))
    assert upload(client, "bomb.docx", buffer.getvalue()).status_code == 415
    assert writer.calls == 0


def test_visitor_limit_returns_429(reader, writer):
    coach = make_coach(writer, visitor_quizzes_per_hour=1)
    client = signed_in(coach, reader)
    headers = {"x-forwarded-for": "6.6.6.6, 1.1.1.1"}
    first = client.post("/api/quiz/text", json={"text": LONG_TEXT}, headers=headers)
    assert first.status_code == 200
    spoofed = {"x-forwarded-for": "7.7.7.7, 1.1.1.1"}
    second = client.post("/api/quiz/text", json={"text": LONG_TEXT}, headers=spoofed)
    assert second.status_code == 429
    other = {"x-forwarded-for": "2.2.2.2"}
    assert client.post("/api/quiz/text", json={"text": LONG_TEXT}, headers=other).status_code == 200


class FlakyReader(FakeReader):
    """Replies in turn, like a page built by scripts."""

    def __init__(self, *replies, found=LONG_TEXT):
        super().__init__()
        self.replies = list(replies)
        self.found = found

    def read_link(self, link):
        self.links.append(link)
        reply = self.replies.pop(0)
        if reply is None:
            raise Unreadable()
        return reply

    def find_link(self, link):
        self.searches.append(link)
        if self.found is None:
            raise Unreadable()
        return self.found


FULL_PAGE = "A full page. " * 60


def test_second_try_can_rescue_a_link():
    reader = FlakyReader(None, FULL_PAGE)
    assert make_coach().read_link(reader, "https://a.example", USER) == FULL_PAGE
    assert reader.searches == []


def test_partial_page_triggers_a_retry():
    reader = FlakyReader("Loading the job...", FULL_PAGE)
    assert make_coach().read_link(reader, "https://a.example", USER) == FULL_PAGE


def test_search_is_the_last_resort():
    reader = FlakyReader(None, None, found=FULL_PAGE)
    assert make_coach().read_link(reader, "https://a.example", USER) == FULL_PAGE
    assert len(reader.searches) == 1


def test_longest_partial_is_kept_when_search_finds_nothing():
    partial = "A partial page. " * 20
    reader = FlakyReader("Short.", partial, found=None)
    assert make_coach().read_link(reader, "https://a.example", USER) == partial


def test_link_that_never_reads_is_unreadable():
    with pytest.raises(Unreadable):
        make_coach().read_link(FlakyReader(None, None, found=None), "https://a.example", USER)


def test_short_page_gets_the_unreadable_message_not_the_short_text_one(writer):
    reader = FlakyReader("Short.", "Short too.", found=None)
    client = signed_in(make_coach(writer), reader)
    response = client.post("/api/quiz/link", json={"link": "example.com"})
    assert response.status_code == 422
    assert "pasting" in response.json()["detail"]
    assert writer.calls == 0


def test_search_is_charged_at_its_higher_cost():
    reader = FlakyReader(None, None, found=FULL_PAGE)
    with pytest.raises(DailyLimitReached):
        make_coach(daily_model_calls=11).read_link(reader, "https://a.example", USER)
    assert reader.searches == []


def test_job_posting_gets_researched_questions():
    writer, preparer = CountingWriter(), FakePreparer()
    quiz = make_coach(writer, preparer=preparer).quiz_from_material(LONG_TEXT, USER)
    assert preparer.researched == ("AI Engineer", ["RAG", "Agents"])
    assert preparer.job_writes == 1
    assert writer.calls == 0
    assert quiz["job"]["role"] == "AI Engineer"


def test_other_material_gets_an_ordinary_quiz_without_a_search():
    writer, preparer = CountingWriter(), FakePreparer(job=False)
    quiz = make_coach(writer, preparer=preparer).quiz_from_material(LONG_TEXT, USER)
    assert preparer.researched is None
    assert writer.calls == 1
    assert quiz["job"] is None


def test_failed_search_falls_back_to_an_ordinary_quiz():
    writer = CountingWriter()
    preparer = FakePreparer(research_fails=True)
    quiz = make_coach(writer, preparer=preparer).quiz_from_material(LONG_TEXT, USER)
    assert writer.calls == 1
    assert quiz["job"] is None


def test_only_safe_source_links_are_kept():
    coach = make_coach(preparer=FakePreparer())
    sources = coach.quiz_from_material(LONG_TEXT, USER)["job"]["sources"]
    assert sources == [{"title": "example.com", "url": "https://example.com/a"}]


def test_research_input_is_cut_down():
    class Noisy(FakePreparer):
        def analyse(self, material):
            return {"is_job_posting": True, "role": "R" * 500, "topics": ["T" * 200] * 9}

    preparer = Noisy()
    make_coach(preparer=preparer).quiz_from_material(LONG_TEXT, USER)
    role, topics = preparer.researched
    assert len(role) == 120
    assert len(topics) == 5
    assert all(len(topic) == 80 for topic in topics)


def test_job_research_is_limited_by_the_budget():
    preparer = FakePreparer()
    coach = make_coach(preparer=preparer, daily_model_calls=5)
    with pytest.raises(DailyLimitReached):
        coach.quiz_from_material(LONG_TEXT, USER)
    assert preparer.researched is None


def test_closed_role_is_reported():
    class Closed(FakePreparer):
        def analyse(self, material):
            return {**super().analyse(material), "is_closed": True}

    quiz = make_coach(preparer=Closed()).quiz_from_material(LONG_TEXT, USER)
    assert quiz["job"]["closed"] is True


def test_grading_reports_the_job_and_sources():
    coach = make_coach(preparer=FakePreparer())
    quiz = coach.quiz_from_material(LONG_TEXT, USER)
    assert coach.grade(quiz["quiz_id"], ["B"] * 5)["job"]["role"] == "AI Engineer"


class BusyThen:
    """Stands in for a Gemini client that is busy a set number of times."""

    def __init__(self, busy_times, code=429):
        self.busy_times = busy_times
        self.code = code
        self.calls = 0
        self.models = self

    def generate_content(self, **_):
        self.calls += 1
        if self.calls <= self.busy_times:
            raise errors.ClientError(self.code, {"error": {"code": self.code, "message": "x", "status": "x"}})
        return "reply"


def gemini_with(home, fallback):
    waits = []
    gemini = Gemini(sleep=waits.append)
    gemini._clients = {"asia-south1": home, "global": fallback}
    return gemini, waits


def test_gemini_retries_when_busy(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    home, fallback = BusyThen(1), BusyThen(0)
    gemini, waits = gemini_with(home, fallback)
    assert gemini._generate("hi") == "reply"
    assert (home.calls, fallback.calls, len(waits)) == (2, 0, 1)


def test_gemini_moves_to_the_global_endpoint_when_the_region_stays_busy(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    home, fallback = BusyThen(9), BusyThen(0)
    gemini, _ = gemini_with(home, fallback)
    assert gemini._generate("hi") == "reply"
    assert (home.calls, fallback.calls) == (2, 1)


def test_gemini_gives_up_after_every_retry(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    gemini, _ = gemini_with(BusyThen(9), BusyThen(9))
    with pytest.raises(ModelBusy):
        gemini._generate("hi")


def test_real_errors_are_not_retried(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    home, fallback = BusyThen(9, code=403), BusyThen(0)
    gemini, _ = gemini_with(home, fallback)
    with pytest.raises(errors.ClientError):
        gemini._generate("hi")
    assert (home.calls, fallback.calls) == (1, 0)


def test_busy_model_gets_a_clear_message(writer):
    class BusyReader(FakeReader):
        def read_link(self, link):
            raise ModelBusy()

    client = signed_in(make_coach(writer), BusyReader())
    response = client.post("/api/quiz/link", json={"link": "example.com"})
    assert response.status_code == 503
    assert "busy" in response.json()["detail"]


def test_a_timeout_counts_as_busy(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)

    class TimesOutOnce(BusyThen):
        def generate_content(self, **_):
            self.calls += 1
            if self.calls == 1:
                raise errors.ServerError(504, {"error": {"code": 504, "message": "x", "status": "x"}})
            return "reply"

    home = TimesOutOnce(0)
    gemini, _ = gemini_with(home, BusyThen(0))
    assert gemini._generate("hi") == "reply"
    assert home.calls == 2


SITE = "https://coach.devesh-singh.com"


def preflight(client, origin):
    return client.options(
        "/api/quiz/link",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )


def test_the_public_site_may_call_the_api(client):
    response = preflight(client, SITE)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == SITE


def test_another_site_may_not_call_the_api(client):
    response = preflight(client, "https://example.com")
    assert "access-control-allow-origin" not in response.headers


def test_the_browser_may_read_answers_sent_to_the_public_site(client):
    response = client.get(HEALTH_PATH, headers={"Origin": SITE})
    assert response.headers["access-control-allow-origin"] == SITE


def test_the_site_page_may_talk_to_the_api():
    static = Path(__file__).parent.parent / "static"
    assert CLOUD_RUN_URL in (static / "_headers").read_text()
    assert CLOUD_RUN_URL in (static / "index.html").read_text()


FIREBASE_ADDRESSES = [
    "https://www.gstatic.com",
    "https://apis.google.com",
    "https://identitytoolkit.googleapis.com",
    "https://securetoken.googleapis.com",
    "https://enablement-coach-ds26.firebaseapp.com",
]


@pytest.mark.parametrize("address", FIREBASE_ADDRESSES)
def test_both_copies_of_the_page_allow_firebase(address):
    static = Path(__file__).parent.parent / "static"
    assert address in (static / "_headers").read_text()
    assert address in CONTENT_SECURITY_POLICY


def test_the_page_can_send_its_sign_in_pass_to_the_api(client):
    response = preflight(client, SITE)
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


@pytest.mark.parametrize(
    "header,token",
    [("Bearer abc", "abc"), ("bearer abc", "abc"), ("Bearer  abc ", "abc")],
)
def test_token_is_read_from_the_header(header, token):
    assert bearer_token(header) == token


@pytest.mark.parametrize("header", ["", "Bearer", "Bearer ", "Basic abc", "abc"])
def test_a_missing_or_odd_header_is_not_signed_in(header):
    with pytest.raises(NotSignedIn):
        bearer_token(header)


@pytest.mark.parametrize("problem", [ValueError("bad"), InvalidIdTokenError("bad"), ExpiredIdTokenError("old", None)])
def test_a_token_firebase_rejects_is_not_signed_in(problem):
    def reject(token):
        raise problem

    with pytest.raises(NotSignedIn):
        FirebaseTokens(verify=reject).user_of("token")


def test_a_token_firebase_accepts_gives_the_user_id():
    assert FirebaseTokens(verify=lambda token: {"uid": "abc"}).user_of("token") == "abc"


def test_a_token_with_no_user_id_is_not_signed_in():
    with pytest.raises(NotSignedIn):
        FirebaseTokens(verify=lambda token: {"uid": ""}).user_of("token")


GATED = [
    ("/api/quiz/text", {"json": {"text": LONG_TEXT}}),
    ("/api/quiz/link", {"json": {"link": "example.com"}}),
    ("/api/quiz/file", {"files": {"file": ("notes.txt", LONG_TEXT.encode())}}),
]


@pytest.mark.parametrize("path,request_body", GATED)
def test_own_material_needs_sign_in(stranger, reader, writer, path, request_body):
    response = stranger.post(path, **request_body)
    assert response.status_code == 401
    assert "sign in" in response.json()["detail"].lower()
    assert response.headers["www-authenticate"] == "Bearer"
    assert writer.calls == 0
    assert reader.links == []


@pytest.mark.parametrize("path,request_body", GATED)
def test_a_forged_token_is_refused(stranger, writer, path, request_body):
    response = stranger.post(path, headers={"Authorization": "Bearer forged"}, **request_body)
    assert response.status_code == 401
    assert writer.calls == 0


def test_the_token_is_checked_before_the_body(stranger):
    assert stranger.post("/api/quiz/text", json={"wrong": "shape"}).status_code == 401


def test_a_new_api_address_needs_sign_in_by_default(stranger):
    assert stranger.post("/api/chat", json={}).status_code == 401
    assert stranger.get("/api/anything").status_code == 401


def test_the_old_model_route_is_gone(client, writer):
    response = client.post("/api/quiz", json={"topic": "google-cloud-basics"})
    assert response.status_code == 404
    assert writer.calls == 0


def test_the_browser_may_read_a_refusal_on_the_public_site(stranger):
    response = stranger.post("/api/quiz/text", json={"text": LONG_TEXT}, headers={"Origin": SITE})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == SITE


def test_a_refusal_keeps_the_security_headers(stranger):
    response = stranger.post("/api/quiz/text", json={"text": LONG_TEXT})
    assert response.headers["x-content-type-options"] == "nosniff"


def test_sign_in_fails_closed_when_it_cannot_be_checked(reader, writer):
    class BrokenTokens:
        def user_of(self, token):
            raise RuntimeError("certificates unreachable")

    app = create_app(make_coach(writer), reader, BrokenTokens())
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/quiz/text", json={"text": LONG_TEXT}, headers=TOKEN)
    assert response.status_code == 503
    assert "certificates" not in response.text
    assert writer.calls == 0


@pytest.mark.parametrize("sample", ["sample-one", "sample-job"])
def test_a_guest_can_take_a_sample_with_no_model_call(stranger, writer, sample):
    quiz = stranger.get(f"/api/samples/{sample}")
    assert quiz.status_code == 200
    assert '"answer"' not in quiz.text
    answers = {"quiz_id": quiz.json()["quiz_id"], "answers": ["B"] * 5}
    graded = stranger.post("/api/grade", json=answers)
    assert graded.json()["score"] == 5
    assert writer.calls == 0


def test_guests_spend_none_of_the_daily_budget(reader):
    coach = make_coach(daily_model_calls=0)
    client = guest(coach, reader)
    assert client.get("/api/samples/sample-one").status_code == 200
    assert client.post("/api/grade", json={"quiz_id": "sample-one", "answers": ["B"] * 5}).status_code == 200


def test_the_job_sample_shows_its_job(stranger):
    assert stranger.get("/api/samples/sample-job").json()["job"]["role"] == "AI Engineer"


def test_an_unknown_sample_is_refused():
    with pytest.raises(SampleNotFound):
        make_coach().sample("nope")


def test_sample_traffic_cannot_push_out_a_quiz_in_progress():
    coach = make_coach(max_stored_quizzes=2)
    mine = coach.quiz_from_material(LONG_TEXT, USER)
    for _ in range(100):
        coach.sample("sample-one")
    assert coach.grade(mine["quiz_id"], ["B"] * 5)["score"] == 5


def test_samples_do_not_expire():
    clock = Clock()
    coach = make_coach(clock=clock, quiz_lifetime_seconds=60)
    clock.now += 86_400
    assert coach.grade("sample-one", ["B"] * 5)["score"] == 5


def test_a_saved_quiz_can_be_exported_with_its_answers():
    coach = make_coach(preparer=FakePreparer())
    quiz = coach.quiz_from_material(LONG_TEXT, USER)
    saved = coach.full_quiz(quiz["quiz_id"])
    assert saved["questions"][0]["answer"] == "B"
    assert saved["job"]["role"] == "AI Engineer"


def test_a_sample_with_a_bad_question_stops_the_app(tmp_path):
    bad = {"questions": [question(n, answer="E") for n in range(1, 6)], "job": None}
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        load_samples(tmp_path)


def test_the_published_samples_are_valid():
    samples = load_samples()
    assert set(samples) == {"google-cloud-basics", "sample-job-posting"}
    assert samples["google-cloud-basics"]["job"] is None
    job = samples["sample-job-posting"]["job"]
    assert job["role"] and job["topics"] and job["sources"]
    assert all(source["url"].startswith("https://") for source in job["sources"])


def spend_quizzes(coach, count, user=USER):
    for _ in range(count):
        coach.quiz_from_material(LONG_TEXT, user)


def test_the_31st_call_from_one_user_is_refused():
    writer = CountingWriter()
    coach = make_coach(writer)
    spend_quizzes(coach, 30)
    with pytest.raises(UserLimitReached):
        coach.quiz_from_material(LONG_TEXT, USER)
    assert writer.calls == 30


def test_one_users_limit_does_not_affect_another():
    coach = make_coach(user_model_calls=1)
    spend_quizzes(coach, 1)
    spend_quizzes(coach, 1, "user-2")


def test_user_limit_resets_the_next_day():
    clock = Clock()
    coach = make_coach(clock=clock, user_model_calls=1)
    spend_quizzes(coach, 1)
    clock.now += 86_400
    spend_quizzes(coach, 1)


def test_a_refused_call_costs_nothing():
    budget = MemoryBudget(10, Clock(), user_calls=1)
    assert budget.spend("a") == OK
    assert budget.spend("a") == USER_USED
    for _ in range(9):
        assert budget.spend("b" + str(_)) == OK
    assert budget.spend("c") == DAY_USED


def test_a_search_costs_a_user_one_call_but_the_day_ten():
    budget = MemoryBudget(10, Clock(), user_calls=1)
    assert budget.spend("a", units=10) == OK
    assert budget.spend("b") == DAY_USED


def test_user_limit_over_http_is_polite(reader):
    coach = make_coach(user_model_calls=1)
    client = signed_in(coach, reader)
    assert client.post("/api/quiz/text", json={"text": LONG_TEXT}).status_code == 200
    refused = client.post("/api/quiz/text", json={"text": LONG_TEXT})
    assert refused.status_code == 429
    assert "tomorrow" in refused.json()["detail"]
    other = {"Authorization": "Bearer other-token"}
    assert client.post("/api/quiz/text", json={"text": LONG_TEXT}, headers=other).status_code == 200


def test_the_signed_in_user_is_the_one_charged(reader):
    class Recording(MemoryBudget):
        users = []

        def spend(self, user, units=1):
            self.users.append(user)
            return super().spend(user, units)

    budget = Recording()
    coach = Coach(CountingWriter(), budget, samples=SAMPLES)
    signed_in(coach, reader).post("/api/quiz/text", json={"text": LONG_TEXT})
    assert budget.users == [USER]


def test_a_contended_transaction_is_tried_again():
    replies = iter([Aborted("busy"), Aborted("busy"), OK])
    waits = []

    def attempt():
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    assert retry_when_contended(attempt, sleep=waits.append) == OK
    assert len(waits) == 2


def test_contention_that_never_clears_is_refused():
    def attempt():
        raise Aborted("busy")

    with pytest.raises(Aborted):
        retry_when_contended(attempt, sleep=lambda _: None)
