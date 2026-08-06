import pytest
from uuid import uuid4
from app import app as flask_app, load_quiz_questions, build_quiz_question


@pytest.fixture
def client():
    with flask_app.test_client() as client:
        yield client


def extract_csrf(response):
    text = response.get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    start = text.index(marker) + len(marker)
    end = text.index('"', start)
    return text[start:end]


def register(client, username=None, password="password123"):
    username = username or f"pytest_user_{uuid4().hex}"
    response = client.get("/register")
    token = extract_csrf(response)
    return client.post(
        "/register",
        data={
            "csrf_token": token,
            "username": username,
            "password": password,
            "password_confirm": password,
        },
        follow_redirects=True,
    )


def test_home(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]

    register_response = register(client)
    assert register_response.status_code == 200

    response = client.get("/")
    assert response.status_code == 200


def test_about(client):
    register(client, username="pytest_about_user")
    response = client.get("/about")
    assert response.status_code == 200


def test_load_quiz_questions_reads_csv():
    questions = load_quiz_questions()
    assert questions
    assert questions[0]["question"]


def test_build_quiz_question_shuffles_choices_and_keeps_answer():
    question = build_quiz_question(load_quiz_questions()[0])
    assert len(question["choices"]) == 4
    assert question["correct_answer"] in [choice["text"] for choice in question["choices"]]
