import pytest
from fastapi.testclient import TestClient

from app.coach import (
    Coach,
    DailyLimitReached,
    Limits,
    NoValidQuiz,
    QuizNotFound,
    TopicNotFound,
    WrongAnswerCount,
    keep_valid,
    problems_with,
)
from app.main import HEALTH_PATH, create_app

TOPIC = "google-cloud-basics"


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

    def __call__(self, *args):
        self.calls += 1
        return self.reply(*args)


def make_coach(writer=None, clock=None, **limits):
    return Coach(
        writer or CountingWriter(),
        Limits(**limits),
        clock=clock or Clock(),
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


@pytest.fixture
def client():
    return TestClient(create_app(make_coach()), raise_server_exceptions=False)


def test_page_and_health_load(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Enablement Coach" in page.text
    assert page.headers["x-content-type-options"] == "nosniff"
    assert client.get(HEALTH_PATH).json() == {"status": "ok"}


def test_quiz_and_grade_over_http(client):
    quiz = client.post("/api/quiz", json={"topic": TOPIC}).json()
    assert '"answer"' not in str(quiz).replace("'", '"')
    graded = client.post("/api/grade", json={"quiz_id": quiz["quiz_id"], "answers": ["B"] * 5})
    assert graded.json()["score"] == 5


def test_http_errors_are_friendly(client):
    assert client.post("/api/quiz", json={"topic": "nope"}).status_code == 404
    assert client.post("/api/grade", json={"quiz_id": "nope", "answers": []}).status_code == 404
    assert client.post("/api/quiz", json={}).status_code == 422


def test_model_failure_returns_502_without_details():
    def broken(*_):
        raise RuntimeError("secret internal detail")

    client = TestClient(create_app(make_coach(broken)), raise_server_exceptions=False)
    response = client.post("/api/quiz", json={"topic": TOPIC})
    assert response.status_code == 502
    assert "secret" not in response.text


def test_daily_limit_returns_429():
    coach = make_coach(daily_model_calls=0)
    client = TestClient(create_app(coach), raise_server_exceptions=False)
    assert client.post("/api/quiz", json={"topic": TOPIC}).status_code == 429
