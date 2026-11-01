import pytest
from fastapi.testclient import TestClient

from app.budget import MemoryBudget
from app.coach import (
    Coach,
    DailyLimitReached,
    Limits,
    NoValidQuiz,
    QuizNotFound,
    TopicNotFound,
    VisitorLimitReached,
    WrongAnswerCount,
    keep_valid,
    problems_with,
)
from app.main import HEALTH_PATH, create_app

TOPIC = "google-cloud-basics"
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

    def read_link(self, link):
        self.links.append(link)
        return LONG_TEXT

    def read_pdf(self, data):
        self.pdfs.append(data)
        return LONG_TEXT


def make_coach(writer=None, clock=None, daily_model_calls=200, **limits):
    clock = clock or Clock()
    return Coach(
        writer or CountingWriter(),
        MemoryBudget(daily_model_calls, clock),
        Limits(**limits),
        clock=clock,
        shuffle=lambda options: options.reverse(),
    )


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
    quiz = make_coach(writer).new_quiz(TOPIC)
    assert writer.calls == 2
    assert len(quiz["questions"]) == 5


def test_gives_up_after_the_allowed_attempts():
    writer = CountingWriter(lambda *_: [question(1, answer="E")])
    with pytest.raises(NoValidQuiz):
        make_coach(writer, attempts_per_quiz=3).new_quiz(TOPIC)
    assert writer.calls == 3


def test_unknown_topic_is_refused_without_a_model_call():
    writer = CountingWriter()
    with pytest.raises(TopicNotFound):
        make_coach(writer).new_quiz("anything else")
    assert writer.calls == 0


def test_quiz_for_players_contains_no_answers():
    quiz = make_coach().new_quiz(TOPIC)
    assert all(set(q) == {"question", "options"} for q in quiz["questions"])


def test_requests_inside_the_cooldown_share_one_model_call():
    writer, clock = CountingWriter(), Clock()
    coach = make_coach(writer, clock, cooldown_seconds=30)
    first = coach.new_quiz(TOPIC)
    clock.now += 29
    assert coach.new_quiz(TOPIC)["quiz_id"] == first["quiz_id"]
    assert writer.calls == 1
    clock.now += 2
    assert coach.new_quiz(TOPIC)["quiz_id"] != first["quiz_id"]
    assert writer.calls == 2


def test_daily_limit_stops_model_calls_then_resets_next_day():
    writer, clock = CountingWriter(), Clock()
    coach = make_coach(writer, clock, cooldown_seconds=0, daily_model_calls=2)
    coach.new_quiz(TOPIC)
    clock.now += 1
    coach.new_quiz(TOPIC)
    clock.now += 1
    with pytest.raises(DailyLimitReached):
        coach.new_quiz(TOPIC)
    assert writer.calls == 2
    clock.now += 86_400
    coach.new_quiz(TOPIC)
    assert writer.calls == 3


def test_failed_attempts_count_against_the_daily_limit():
    writer = CountingWriter(lambda *_: [])
    coach = make_coach(writer, daily_model_calls=2, attempts_per_quiz=5)
    with pytest.raises(DailyLimitReached):
        coach.new_quiz(TOPIC)
    assert writer.calls == 2


def test_grading_is_done_by_code():
    coach = make_coach()
    quiz = coach.new_quiz(TOPIC)
    result = coach.grade(quiz["quiz_id"], ["B", "B", "A", None, "B"])
    assert (result["score"], result["total"]) == (3, 5)
    assert [r["correct"] for r in result["results"]] == [True, True, False, False, True]
    assert result["results"][2]["correct_answer"] == "B"


def test_shuffling_options_does_not_break_grading():
    coach = make_coach()
    quiz = coach.new_quiz(TOPIC)
    assert quiz["questions"][0]["options"] == ["D", "C", "B", "A"]
    assert coach.grade(quiz["quiz_id"], ["B"] * 5)["score"] == 5


def test_wrong_number_of_answers_is_refused():
    coach = make_coach()
    quiz = coach.new_quiz(TOPIC)
    with pytest.raises(WrongAnswerCount):
        coach.grade(quiz["quiz_id"], ["B"])


def test_quiz_expires():
    clock = Clock()
    coach = make_coach(clock=clock, quiz_lifetime_seconds=60)
    quiz = coach.new_quiz(TOPIC)
    clock.now += 61
    with pytest.raises(QuizNotFound):
        coach.grade(quiz["quiz_id"], ["B"] * 5)


def test_stored_quizzes_are_capped():
    clock = Clock()
    coach = make_coach(clock=clock, cooldown_seconds=0, max_stored_quizzes=2)
    oldest = coach.new_quiz(TOPIC)
    for _ in range(2):
        clock.now += 1
        coach.new_quiz(TOPIC)
    with pytest.raises(QuizNotFound):
        coach.grade(oldest["quiz_id"], ["B"] * 5)


def test_custom_quizzes_never_share_a_cooldown():
    writer = CountingWriter()
    coach = make_coach(writer, cooldown_seconds=30)
    assert coach.quiz_from_material("one")["quiz_id"] != coach.quiz_from_material("two")["quiz_id"]
    assert writer.calls == 2


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
    return TestClient(create_app(make_coach(writer), reader), raise_server_exceptions=False)


def test_page_and_health_load(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Enablement Coach" in page.text
    assert page.headers["x-content-type-options"] == "nosniff"
    assert client.get(HEALTH_PATH).json() == {"status": "ok"}
    assert client.get("/favicon.svg").status_code == 200


def test_quiz_and_grade_over_http(client):
    quiz = client.post("/api/quiz", json={"topic": TOPIC}).json()
    assert '"answer"' not in str(quiz).replace("'", '"')
    graded = client.post("/api/grade", json={"quiz_id": quiz["quiz_id"], "answers": ["B"] * 5})
    assert graded.json()["score"] == 5


def test_http_errors_are_friendly(client):
    assert client.post("/api/quiz", json={"topic": "nope"}).status_code == 404
    assert client.post("/api/grade", json={"quiz_id": "nope", "answers": []}).status_code == 404
    assert client.post("/api/quiz", json={}).status_code == 422


def test_model_failure_returns_502_without_details(reader):
    def broken(*_):
        raise RuntimeError("secret internal detail")

    client = TestClient(create_app(make_coach(broken), reader), raise_server_exceptions=False)
    response = client.post("/api/quiz", json={"topic": TOPIC})
    assert response.status_code == 502
    assert "secret" not in response.text


def test_budget_failure_refuses_the_quiz(reader):
    class BrokenBudget:
        def spend(self):
            raise RuntimeError("database down")

    writer = CountingWriter()
    coach = Coach(writer, BrokenBudget())
    client = TestClient(create_app(coach, reader), raise_server_exceptions=False)
    assert client.post("/api/quiz", json={"topic": TOPIC}).status_code == 502
    assert writer.calls == 0


def test_daily_limit_returns_429(reader):
    coach = make_coach(daily_model_calls=0)
    client = TestClient(create_app(coach, reader), raise_server_exceptions=False)
    assert client.post("/api/quiz", json={"topic": TOPIC}).status_code == 429


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
    from app.gemini import Unreadable

    class BlockedReader(FakeReader):
        def read_link(self, link):
            raise Unreadable()

    client = TestClient(create_app(make_coach(writer), BlockedReader()))
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
    client = TestClient(create_app(coach, reader), raise_server_exceptions=False)
    headers = {"x-forwarded-for": "6.6.6.6, 1.1.1.1"}
    first = client.post("/api/quiz/text", json={"text": LONG_TEXT}, headers=headers)
    assert first.status_code == 200
    spoofed = {"x-forwarded-for": "7.7.7.7, 1.1.1.1"}
    second = client.post("/api/quiz/text", json={"text": LONG_TEXT}, headers=spoofed)
    assert second.status_code == 429
    other = {"x-forwarded-for": "2.2.2.2"}
    assert client.post("/api/quiz/text", json={"text": LONG_TEXT}, headers=other).status_code == 200
