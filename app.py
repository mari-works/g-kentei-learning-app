import csv
import os
import random
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

from config import Config
from models import (
    connect_db,
    get_big_categories,
    get_categories,
    get_dashboard,
    get_keyword,
    get_keywords,
    init_db,
    query_db,
    set_keyword_note,
    set_review_later,
    set_learning_status,
    get_big_category_stats,
    get_category_stats,
)

app = Flask(__name__, instance_relative_config=True)
app.config.from_object(Config)
app.secret_key = Config.SECRET_KEY
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "ログインしてください。"


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    daily_minute_goal = db.Column(db.Integer, nullable=True)
    daily_question_goal = db.Column(db.Integer, nullable=True)
    daily_term_goal = db.Column(db.Integer, nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

os.makedirs(app.instance_path, exist_ok=True)
init_db(app.config["DATABASE"])
with app.app_context():
    db.create_all()
    for statement in [
        "ALTER TABLE user ADD COLUMN daily_minute_goal INTEGER DEFAULT NULL",
        "ALTER TABLE user ADD COLUMN daily_question_goal INTEGER DEFAULT NULL",
        "ALTER TABLE user ADD COLUMN daily_term_goal INTEGER DEFAULT NULL",
    ]:
        try:
            db.session.execute(db.text(statement))
            db.session.commit()
        except Exception:
            db.session.rollback()


def ensure_legacy_user_and_migrate_data():
    with app.app_context():
        legacy = User.query.filter_by(username="legacy").first()
        if legacy is None:
            legacy = User(username="legacy")
            legacy.set_password(os.environ.get("LEGACY_USER_PASSWORD", "legacy-password-change-me"))
            db.session.add(legacy)
            db.session.commit()

        with connect_db(app.config["DATABASE"]) as sqlite_db:
            sqlite_db.execute(
                "UPDATE history SET user_id = ? WHERE user_id IS NULL",
                (legacy.id,),
            )
            sqlite_db.execute(
                """
                INSERT OR IGNORE INTO keyword_progress (user_id, keyword_id, learning_status, last_studied_at)
                SELECT ?, id, learning_status, last_studied_at
                FROM keywords
                WHERE learning_status != '未学習' OR last_studied_at IS NOT NULL
                """,
                (legacy.id,),
            )
            sqlite_db.execute(
                """
                UPDATE keywords
                SET learning_status = '未学習', last_studied_at = NULL
                WHERE learning_status != '未学習' OR last_studied_at IS NOT NULL
                """
            )
            sqlite_db.commit()


ensure_legacy_user_and_migrate_data()


def current_user_id():
    return current_user.id if current_user.is_authenticated else None


def user_keyword_status_select():
    return """
        FROM keywords
        LEFT JOIN keyword_progress kp ON kp.keyword_id = keywords.id AND kp.user_id = ?
    """


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def format_review_date(value):
    dt = parse_iso_datetime(value)
    if not dt:
        return "-"
    return dt.strftime("%Y/%m/%d")


def term_status_meta(status):
    meta = {
        "理解済み": {"label": "理解済み", "tone": "green"},
        "あいまい": {"label": "あいまい", "tone": "amber"},
        "未理解": {"label": "未理解", "tone": "rose"},
        "未学習": {"label": "未学習", "tone": "slate"},
    }
    return meta.get(status, meta["未学習"])


def build_term_detail_analysis(db_path, user_id):
    today = datetime.now().date()
    sort_floor = datetime(1970, 1, 1)
    term_rows = query_db(
        db_path,
        """
        SELECT keywords.id,
               keywords.keyword,
               keywords.category,
               COALESCE(kp.learning_status, '未学習') AS current_status,
               kp.last_studied_at AS progress_last_studied_at
        FROM keywords
        LEFT JOIN keyword_progress kp ON kp.keyword_id = keywords.id AND kp.user_id = ?
        ORDER BY keywords.category, keywords.keyword
        """,
        (user_id,),
    )
    history_rows = query_db(
        db_path,
        """
        SELECT term_id,
               selected_status,
               reviewed_at
        FROM keyword_status_history
        WHERE user_id = ?
          AND selected_status IN ('理解済み', 'あいまい', '未理解')
        ORDER BY term_id, reviewed_at
        """,
        (user_id,),
    )

    histories_by_term = {}
    for row in history_rows:
        histories_by_term.setdefault(row["term_id"], []).append(
            {"status": row["selected_status"], "reviewed_at": row["reviewed_at"]}
        )

    def days_since(value):
        dt = parse_iso_datetime(value)
        return (today - dt.date()).days if dt else None

    studied_terms = []
    for row in term_rows:
        histories = histories_by_term.get(row["id"], [])
        if histories:
            latest = max(histories, key=lambda item: item["reviewed_at"] or "")
            current_status = latest["status"]
            last_reviewed_at = latest["reviewed_at"]
        else:
            current_status = row["current_status"] or "未学習"
            last_reviewed_at = row["progress_last_studied_at"]
        unknown_count = sum(1 for item in histories if item["status"] == "未理解")
        ambiguous_count = sum(1 for item in histories if item["status"] == "あいまい")
        understood_count_for_term = sum(1 for item in histories if item["status"] == "理解済み")
        if not histories:
            continue
        status_info = term_status_meta(current_status)
        studied_terms.append(
            {
                "id": row["id"],
                "keyword": row["keyword"],
                "category": row["category"] or "未分類",
                "current_status": current_status,
                "status_label": status_info["label"],
                "status_tone": status_info["tone"],
                "unknown_count": unknown_count,
                "ambiguous_count": ambiguous_count,
                "understood_count": understood_count_for_term,
                "last_reviewed_at": last_reviewed_at,
                "last_reviewed_label": format_review_date(last_reviewed_at),
                "days_since": days_since(last_reviewed_at),
                "histories": histories,
            }
        )

    weak_terms = []
    for item in studied_terms:
        score = item["unknown_count"] * 3 + item["ambiguous_count"]
        if item["current_status"] == "未理解":
            score += 3
        elif item["current_status"] == "あいまい":
            score += 1
        if score <= 0:
            continue
        weak_terms.append({**item, "weak_score": score})
    weak_terms = sorted(
        weak_terms,
        key=lambda item: (
            -item["weak_score"],
            -item["unknown_count"],
            -item["ambiguous_count"],
            -(parse_iso_datetime(item["last_reviewed_at"]) or sort_floor).timestamp(),
            item["keyword"],
        ),
    )[:10]

    category_rows = query_db(
        db_path,
        """
        SELECT keywords.category,
               COUNT(*) AS total,
               SUM(CASE WHEN COALESCE(kp.learning_status, '未学習') = '理解済み' THEN 1 ELSE 0 END) AS understood,
               SUM(CASE WHEN COALESCE(kp.learning_status, '未学習') = 'あいまい' THEN 1 ELSE 0 END) AS ambiguous,
               SUM(CASE WHEN COALESCE(kp.learning_status, '未学習') = '未理解' THEN 1 ELSE 0 END) AS unknown,
               SUM(CASE WHEN COALESCE(kp.learning_status, '未学習') = '未学習' THEN 1 ELSE 0 END) AS untrained
        FROM keywords
        LEFT JOIN keyword_progress kp ON kp.keyword_id = keywords.id AND kp.user_id = ?
        GROUP BY keywords.category
        """,
        (user_id,),
    )
    category_understanding = []
    for row in category_rows:
        total = row["total"] or 0
        understood = row["understood"] or 0
        ambiguous = row["ambiguous"] or 0
        unknown = row["unknown"] or 0
        untrained = row["untrained"] or 0
        rate = int(round(((understood + ambiguous * 0.5) / total) * 100)) if total else 0
        if rate >= 80:
            label, tone = "得意", "green"
        elif rate >= 50:
            label, tone = "学習中", "amber"
        else:
            label, tone = "要復習", "rose"
        category_understanding.append(
            {
                "name": row["category"] or "未分類",
                "rate": rate,
                "understood": understood,
                "ambiguous": ambiguous,
                "unknown": unknown,
                "untrained": untrained,
                "total": total,
                "label": label,
                "tone": tone,
            }
        )
    category_understanding = sorted(category_understanding, key=lambda item: (item["rate"], item["name"]))

    review_terms = []
    for item in studied_terms:
        if item["current_status"] not in {"未理解", "あいまい"}:
            continue
        elapsed = item["days_since"] if item["days_since"] is not None else 0
        base = 40 if item["current_status"] == "未理解" else 20
        score = base + min(max(elapsed, 0), 30) + item["unknown_count"] * 3 + item["ambiguous_count"]
        if score >= 70:
            label, tone = "優先度 高", "rose"
        elif score >= 40:
            label, tone = "優先度 中", "amber"
        else:
            label, tone = "優先度 低", "blue"
        review_terms.append({**item, "review_score": score, "priority_label": label, "priority_tone": tone})
    review_terms = sorted(review_terms, key=lambda item: (-item["review_score"], item["keyword"]))

    growth_terms = []
    for item in studied_terms:
        if item["current_status"] != "理解済み":
            continue
        latest_understood = None
        for history in item["histories"]:
            if history["status"] == "理解済み":
                latest_understood = history
        if not latest_understood:
            continue
        improved_from = [
            history
            for history in item["histories"]
            if history["status"] in {"未理解", "あいまい"}
            and (history["reviewed_at"] or "") < (latest_understood["reviewed_at"] or "")
        ]
        if not improved_from:
            continue
        past_unknown = sum(1 for history in improved_from if history["status"] == "未理解")
        past_ambiguous = sum(1 for history in improved_from if history["status"] == "あいまい")
        score = past_unknown * 3 + past_ambiguous
        understood_dt = parse_iso_datetime(latest_understood["reviewed_at"])
        if understood_dt and (today - understood_dt.date()).days <= 30:
            score += 5
        from_label = "未理解 → 理解済み" if past_unknown else "あいまい → 理解済み"
        growth_terms.append(
            {
                **item,
                "past_unknown_count": past_unknown,
                "past_ambiguous_count": past_ambiguous,
                "understood_at": latest_understood["reviewed_at"],
                "understood_label": format_review_date(latest_understood["reviewed_at"]),
                "growth_score": score,
                "change_label": from_label,
            }
        )
    growth_terms = sorted(
        growth_terms,
        key=lambda item: (
            -item["growth_score"],
            -(parse_iso_datetime(item["understood_at"]) or sort_floor).timestamp(),
        ),
    )[:10]

    forgetting_terms = []
    for item in studied_terms:
        if item["current_status"] != "理解済み":
            continue
        elapsed = item["days_since"]
        if elapsed is None or elapsed < 14:
            continue
        score = elapsed + item["unknown_count"] * 2 + item["ambiguous_count"]
        if elapsed >= 60:
            label, tone = "長期間未復習", "rose"
        elif elapsed >= 30:
            label, tone = "復習推奨", "amber"
        else:
            label, tone = "そろそろ復習", "blue"
        forgetting_terms.append({**item, "forgetting_score": score, "risk_label": label, "risk_tone": tone})
    forgetting_terms = sorted(
        forgetting_terms,
        key=lambda item: (-item["forgetting_score"], -(item["days_since"] or 0), item["keyword"]),
    )[:10]

    return {
        "has_history": bool(studied_terms),
        "weak_terms": weak_terms,
        "category_understanding": category_understanding,
        "review_terms": review_terms,
        "growth_terms": growth_terms,
        "forgetting_terms": forgetting_terms,
    }


def build_practice_detail_analysis(user_id, mode_key, question_lookup):
    mode_clause = "AND practice_mode = 'exam'" if mode_key == "exam" else "AND COALESCE(practice_mode, 'normal') != 'exam'"
    rows = db.session.execute(
        db.text(
            f"""
            SELECT item_id AS question_id,
                   COALESCE(category, '未分類') AS category,
                   correct AS is_correct,
                   created_at AS answered_at
            FROM history
            WHERE user_id = :user_id
              AND session_type = 'practice'
              AND item_id IS NOT NULL
              AND correct IS NOT NULL
              {mode_clause}
            ORDER BY item_id, created_at
            """
        ),
        {"user_id": user_id},
    ).mappings().all()

    def ts(value):
        parsed = parse_iso_datetime(value)
        return parsed.timestamp() if parsed else 0

    def answer_label(value):
        parsed = parse_iso_datetime(value)
        return parsed.strftime("%Y/%m/%d") if parsed else "-"

    by_question = {}
    for row in rows:
        question_id = row["question_id"]
        item = by_question.setdefault(
            question_id,
            {
                "id": question_id,
                "category": row["category"] or "未分類",
                "attempts": 0,
                "correct": 0,
                "incorrect": 0,
                "last_answered_at": None,
                "last_incorrect_at": None,
                "last_correct_at": None,
                "answers": [],
            },
        )
        is_correct = bool(row["is_correct"])
        item["attempts"] += 1
        item["correct"] += 1 if is_correct else 0
        item["incorrect"] += 0 if is_correct else 1
        item["last_answered_at"] = row["answered_at"]
        if is_correct:
            item["last_correct_at"] = row["answered_at"]
        else:
            item["last_incorrect_at"] = row["answered_at"]
        item["answers"].append({"is_correct": is_correct, "answered_at": row["answered_at"]})

    question_items = []
    for item in by_question.values():
        question = question_lookup.get(item["id"], {})
        accuracy = int(round((item["correct"] / item["attempts"]) * 100)) if item["attempts"] else 0
        if accuracy >= 80:
            accuracy_label, accuracy_tone = "得意", "green"
        elif accuracy >= 50:
            accuracy_label, accuracy_tone = "学習中", "amber"
        else:
            accuracy_label, accuracy_tone = "苦手", "rose"
        question_items.append(
            {
                **item,
                "question": question.get("question", f"問題ID {item['id']}"),
                "accuracy": accuracy,
                "accuracy_label": accuracy_label,
                "accuracy_tone": accuracy_tone,
                "last_answered_label": answer_label(item["last_answered_at"]),
                "last_incorrect_label": answer_label(item["last_incorrect_at"]),
                "last_correct_label": answer_label(item["last_correct_at"]),
                "review_url": url_for(
                    "practice_quiz",
                    mode="exam" if mode_key == "exam" else "random",
                    question_id=item["id"],
                    limit=10,
                    reset=1,
                    start=1,
                ),
            }
        )

    weak_questions = sorted(
        [item for item in question_items if item["attempts"] >= 2],
        key=lambda item: (item["accuracy"], -item["attempts"], -ts(item["last_answered_at"])),
    )[:10]

    category_rows = db.session.execute(
        db.text(
            f"""
            SELECT COALESCE(category, '未分類') AS category,
                   COUNT(*) AS total,
                   SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS correct
            FROM history
            WHERE user_id = :user_id
              AND session_type = 'practice'
              AND correct IS NOT NULL
              {mode_clause}
            GROUP BY COALESCE(category, '未分類')
            """
        ),
        {"user_id": user_id},
    ).mappings().all()
    category_accuracy = []
    for row in category_rows:
        total = row["total"] or 0
        correct = row["correct"] or 0
        accuracy = int(round((correct / total) * 100)) if total else 0
        if accuracy >= 80:
            label, tone = "得意", "green"
        elif accuracy >= 50:
            label, tone = "学習中", "amber"
        else:
            label, tone = "苦手", "rose"
        category_accuracy.append(
            {
                "name": row["category"] or "未分類",
                "accuracy": accuracy,
                "correct": correct,
                "total": total,
                "label": label,
                "tone": tone,
            }
        )
    category_accuracy = sorted(category_accuracy, key=lambda item: (item["accuracy"], item["name"]))

    review_priority = sorted(
        [item for item in question_items if item["incorrect"] > 0],
        key=lambda item: (-item["incorrect"], -ts(item["last_incorrect_at"]), -item["attempts"]),
    )[:10]

    improved_questions = []
    for item in question_items:
        incorrect_dates = [answer["answered_at"] for answer in item["answers"] if not answer["is_correct"]]
        correct_dates = [answer["answered_at"] for answer in item["answers"] if answer["is_correct"]]
        if not incorrect_dates or not correct_dates:
            continue
        last_correct_at = max(correct_dates)
        past_incorrect_count = sum(1 for value in incorrect_dates if (value or "") < (last_correct_at or ""))
        if past_incorrect_count <= 0:
            continue
        improved_questions.append(
            {
                **item,
                "past_incorrect": past_incorrect_count,
                "last_correct_at": last_correct_at,
                "last_correct_label": answer_label(last_correct_at),
                "change_label": "❌→⭕",
            }
        )
    improved_questions = sorted(
        improved_questions,
        key=lambda item: (-item["past_incorrect"], -ts(item["last_correct_at"])),
    )[:10]

    never_correct = sorted(
        [item for item in question_items if item["attempts"] >= 2 and item["correct"] == 0],
        key=lambda item: (-item["attempts"], -ts(item["last_answered_at"])),
    )[:10]

    return {
        "has_history": bool(question_items),
        "weak_questions_top10": weak_questions,
        "category_accuracy": category_accuracy,
        "review_priority": review_priority,
        "improved_questions": improved_questions,
        "never_correct": never_correct,
    }


def wrong_question_session_key():
    user_id = current_user_id()
    return f"wrong_question_ids:{user_id}" if user_id else "wrong_question_ids"


def generate_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def validate_csrf_token():
    return request.form.get("csrf_token") and request.form.get("csrf_token") == session.get("_csrf_token")


@app.before_request
def require_login_for_private_pages():
    public_endpoints = {"login", "register", "static"}
    if request.endpoint in public_endpoints or request.endpoint is None:
        return None
    if not current_user.is_authenticated:
        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
    if request.method == "POST" and not request.is_json and not validate_csrf_token():
        abort(400)
    return None

EXAM_TYPES = {
    "online": {
        "label": "オンライン試験",
        "questions": 145,
        "minutes": 100,
        "accent": "emerald",
        "per_question": "約41秒",
    },
    "onsite": {
        "label": "会場試験",
        "questions": 145,
        "minutes": 120,
        "accent": "orange",
        "per_question": "約49秒",
    },
}


@app.context_processor
def inject_template_functions():
    """提供テンプレート関数をグローバルコンテキストに登録"""
    def fmt_duration(seconds):
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes:02}:{secs:02}"
    endpoint = request.endpoint or ""
    mode = request.args.get("mode")
    header_map = {
        "home": ("ホーム", "今日も一緒に学習を進めましょう。"),
        "big_categories": ("用語学習", "一覧確認またはフラッシュカードで用語を学習します。"),
        "categories": ("カテゴリ選択", "学習したいカテゴリを選びましょう。"),
        "keywords": ("用語一覧", "用語を検索し、理解状況を確認できます。"),
        "keyword_detail": ("用語詳細", "用語の意味と理解度を確認します。"),
        "category_detail": ("カテゴリ詳細", "カテゴリごとの理解状況を確認します。"),
        "flashcard_settings": ("フラッシュカード学習", "今日取り組む用語の範囲を選びましょう。"),
        "flashcards": ("フラッシュカード学習", "カードをめくりながら理解度を記録します。"),
        "practice": ("問題演習", "通常演習または試験モードを選択します。"),
        "practice_settings": (
            "試験モード" if mode == "exam" else "通常演習モード",
            "演習条件を選んで学習を始めましょう。",
        ),
        "practice_quiz": ("問題演習中", "問題に回答しながら理解を確認します。"),
        "practice_result": ("演習結果", "回答結果と正答率を確認します。"),
        "practice_result_detail": ("解説確認", "問題ごとの回答結果と解説を確認します。"),
        "history": ("学習履歴", "いつ、どれだけ学習したかを振り返ります。"),
        "statistics": ("統計情報", "理解状況と苦手分野を分析します。"),
        "terms": ("用語一覧", "登録されている用語を確認します。"),
        "term_detail": ("用語詳細", "用語の説明を確認します。"),
        "about": ("このシステムについて", "G検定学習支援システムの概要です。"),
    }
    page_header_title, page_header_subtitle = header_map.get(
        endpoint,
        ("G検定学習支援", "学習状況を確認して次の学習へ進みましょう。"),
    )
    return dict(
        format_duration=fmt_duration,
        page_header_title=page_header_title,
        page_header_subtitle=page_header_subtitle,
        csrf_token=generate_csrf_token,
    )


def normalize_quiz_category(category):
    category = (category or "未分類").strip()
    return category.replace("AI の", "AIの")


def load_quiz_questions(csv_path=None):
    csv_path = Path(csv_path or Path(__file__).resolve().parent / "Questions.csv")
    if not csv_path.exists():
        return []

    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        questions = []
        for row in reader:
            question_text = (row.get("問題") or "").strip()
            correct_answer = (row.get("正答") or "").strip()
            incorrect_answers = [
                (row.get("誤答1") or "").strip(),
                (row.get("誤答2") or "").strip(),
                (row.get("誤答3") or "").strip(),
            ]
            if not question_text or not correct_answer:
                continue

            questions.append(
                {
                    "id": int(row.get("ID") or len(questions) + 1),
                    "category": normalize_quiz_category(row.get("カテゴリ")),
                    "question": question_text,
                    "correct_answer": correct_answer,
                    "incorrect_answers": [answer for answer in incorrect_answers if answer],
                    "explanation": (row.get("解説") or "").strip(),
                }
            )
    return questions


def build_quiz_question(question_record):
    options = [question_record["correct_answer"], *question_record.get("incorrect_answers", [])]
    options = [option for option in options if option][:4]
    if len(options) < 4:
        options.extend(["不明"] * (4 - len(options)))

    seed = question_record.get("order_seed")
    if seed is not None:
        rng = random.Random(seed ^ int(question_record["id"]))
        rng.shuffle(options)
    else:
        random.shuffle(options)

    return {
        "id": question_record["id"],
        "category": question_record["category"],
        "question": question_record["question"],
        "correct_answer": question_record["correct_answer"],
        "difficulty": "標準",
        "explanation": question_record.get("explanation", ""),
        "choices": [
            {"text": option, "is_correct": option == question_record["correct_answer"]}
            for option in options
        ],
    }


def build_display_question(question_record, selected_choice):
    question = build_quiz_question(question_record)
    for choice in question["choices"]:
        choice["selected"] = choice["text"] == selected_choice
    question["selected_choice"] = selected_choice
    return question


def get_state_questions(state):
    if not state:
        return []
    refs = state.get("questions") or []
    if refs and refs[0].get("question"):
        return refs

    by_id = {question["id"]: question for question in load_quiz_questions()}
    questions = []
    for ref in refs:
        question = by_id.get(ref.get("id"))
        if question:
            questions.append(dict(question, order_seed=ref.get("order_seed")))
    return questions


def get_state_question(state, index):
    questions = get_state_questions(state)
    if 0 <= index < len(questions):
        return questions[index]
    return None


def get_quiz_categories():
    questions = load_quiz_questions()
    return sorted({question["category"] for question in questions if question.get("category")})


def get_wrong_question_ids(session_obj):
    return list(session_obj.get(wrong_question_session_key()) or [])


def update_wrong_question_ids(session_obj, question_id, is_correct):
    wrong_ids = set(get_wrong_question_ids(session_obj))
    if is_correct:
        wrong_ids.discard(question_id)
    else:
        wrong_ids.add(question_id)
    session_obj[wrong_question_session_key()] = sorted(wrong_ids)
    return session_obj[wrong_question_session_key()]


def current_timestamp():
    return datetime.utcnow().isoformat()


def parse_timestamp(value):
    return datetime.fromisoformat(value) if value else None


def utc_epoch_ms(value=None):
    if value is None:
        return int(time.time() * 1000)
    elapsed = (datetime.utcnow() - value).total_seconds()
    return int((time.time() - elapsed) * 1000)


def parse_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_exam_config(exam_type):
    return EXAM_TYPES.get(exam_type or "online", EXAM_TYPES["online"])


def get_elapsed_seconds(state):
    if not state:
        return 0
    if state.get("paused"):
        paused_elapsed = int(state.get("paused_elapsed_seconds") or 0)
        if paused_elapsed:
            return paused_elapsed
        started_at = parse_timestamp(state.get("start_time") or state.get("started_at"))
        paused_at = parse_timestamp(state.get("paused_at") or state.get("graded_at"))
        if started_at and paused_at:
            return max(0, int((paused_at - started_at).total_seconds()))
        return 0
    started_at = parse_timestamp(state.get("start_time") or state.get("started_at"))
    if not started_at:
        return int(state.get("elapsed_seconds") or 0)
    return max(0, int((datetime.utcnow() - started_at).total_seconds()))


def record_quiz_activity(state):
    if not state:
        return state
    now = datetime.utcnow()
    last_activity = parse_timestamp(state.get("last_activity_at"))
    if last_activity:
        delta = max(0, int((now - last_activity).total_seconds()))
        if delta < 300:
            state["active_seconds"] = int(state.get("active_seconds") or 0) + delta
        else:
            state["activity_paused"] = True
    state["last_activity_at"] = now.isoformat()
    return state


def pause_quiz_state(state, graded=False):
    elapsed = get_elapsed_seconds(state)
    state["paused"] = True
    state["paused_elapsed_seconds"] = elapsed
    state["elapsed_seconds"] = elapsed
    state["paused_at"] = current_timestamp()
    if graded:
        state["graded_pause"] = True
        state["graded_at"] = state["paused_at"]
    else:
        state.pop("graded_pause", None)
        state.pop("graded_at", None)
    return state


def resume_quiz_state(state):
    if not state:
        return state
    elapsed = get_elapsed_seconds(state)
    new_start = datetime.utcnow() - timedelta(seconds=elapsed)
    state["start_time"] = new_start.isoformat()
    state["start_time_ms"] = utc_epoch_ms(new_start)
    state["elapsed_seconds"] = elapsed
    state["last_activity_at"] = datetime.utcnow().isoformat()
    state.pop("paused", None)
    state.pop("graded_pause", None)
    state.pop("graded_at", None)
    state.pop("paused_elapsed_seconds", None)
    state.pop("paused_at", None)
    return state


def get_paused_exam(session_obj):
    state = session_obj.get("quiz")
    if (
        isinstance(state, dict)
        and state.get("user_id") == current_user_id()
        and state.get("mode") == "exam"
        and state.get("paused")
        and not state.get("completed")
    ):
        state["elapsed_seconds"] = get_elapsed_seconds(state)
        state["total_questions"] = len(state.get("questions", []))
        return state
    return None


def save_current_answer(state, selected_choice=None, question_id=None):
    if not state or not state.get("questions"):
        return
    current_question = get_state_question(state, state["current_index"])
    if not current_question:
        return
    if question_id and str(current_question["id"]) != str(question_id):
        return
    if selected_choice:
        is_correct = selected_choice == current_question["correct_answer"]
        state["answers"][state["current_index"]] = {
            "selected_choice": selected_choice,
            "correct": is_correct,
        }


def normalize_timer_state(state):
    if not state or state.get("paused"):
        return state
    started_at = parse_timestamp(state.get("start_time") or state.get("started_at"))
    if started_at:
        state["start_time_ms"] = utc_epoch_ms(started_at)
    return state


def build_empty_quiz_state(mode=None, category=None, limit=None, time_limit=None):
    return {
        "user_id": current_user_id(),
        "mode": mode,
        "category": category or None,
        "question_limit": limit,
        "time_limit": time_limit,
        "questions": [],
        "question_ids": [],
        "answers": [],
        "current_index": 0,
        "completed": False,
        "last_result": None,
        "started_at": None,
        "completed_at": None,
        "elapsed_seconds": 0,
        "active_seconds": 0,
        "last_activity_at": None,
        "activity_paused": False,
    }


def reset_quiz_session(session_obj, mode="random", category=None, limit=None, time_limit=None, exam_type=None, question_id=None):
    questions = load_quiz_questions()
    exam_type = exam_type if exam_type in EXAM_TYPES else "online"
    category_list = [item.strip() for item in (category or "").split(",") if item.strip()]
    if question_id:
        questions = [question for question in questions if str(question["id"]) == str(question_id)]

    if mode == "category":
        if category_list:
            questions = [question for question in questions if question["category"] in category_list]
    elif mode in {"wrong", "review"}:
        wrong_ids = get_wrong_question_ids(session_obj)
        questions = [question for question in questions if question["id"] in wrong_ids]
        if category_list:
            questions = [question for question in questions if question["category"] in category_list]
    elif mode == "exam":
        if category_list:
            questions = [question for question in questions if question["category"] in category_list]

    # For exam mode, enforce 145 questions by default
    if mode == "exam":
        # always enforce 145 questions for exam mode
        limit = get_exam_config(exam_type)["questions"]

    if limit and limit > 0 and limit < len(questions):
        questions = random.sample(questions, limit)
    else:
        random.shuffle(questions)

    question_refs = [
        {"id": question["id"], "order_seed": random.randrange(2**32)}
        for question in questions
    ]
    question_ids = [question["id"] for question in questions]
    answers = [{"selected_choice": None, "correct": None} for _ in question_refs]

    # determine time limit seconds
    if mode == "exam":
        # prefer explicit exam_type if provided; fall back to time_limit arg (minutes)
        exam_config = get_exam_config(exam_type)
        time_limit_minutes = exam_config["minutes"]
        time_limit_seconds = time_limit_minutes * 60
    else:
        # if generic time_limit provided (minutes), use it, otherwise derive from questions*40s
        if time_limit:
            time_limit_minutes = time_limit
            time_limit_seconds = int(time_limit) * 60
        else:
            time_limit_seconds = len(questions) * 40
            time_limit_minutes = None

    state = {
        "user_id": current_user_id(),
        "mode": mode,
        "learning_type": "question",
        "learning_mode": normalize_question_learning_mode(mode, exam_type),
        "learning_category": category if mode == "category" else None,
        "category": category or None,
        "question_limit": limit,
        "time_limit": time_limit_minutes,
        "questions": question_refs,
        "question_ids": question_ids,
        "answers": answers,
        "current_index": 0,
        "completed": False,
        "last_result": None,
        "started_at": current_timestamp(),
        "start_time": current_timestamp(),
        "start_time_ms": utc_epoch_ms(),
        "time_limit_seconds": time_limit_seconds,
        "completed_at": None,
        "elapsed_seconds": 0,
        "active_seconds": 0,
        "last_activity_at": current_timestamp(),
        "activity_paused": False,
    }
    if mode == "exam":
        state["exam_type"] = exam_type
        state["learning_mode"] = normalize_question_learning_mode(mode, exam_type)
    # clear any pause-related fields when resetting
    state.pop('paused', None)
    state.pop('paused_elapsed_seconds', None)
    state.pop('paused_at', None)
    session_obj["quiz"] = state
    return state


def get_quiz_session(session_obj, mode="random", category=None, limit=None, time_limit=None, reset=False):
    if reset:
        return reset_quiz_session(session_obj, mode, category, limit, time_limit)

    state = session_obj.get("quiz")
    if not isinstance(state, dict):
        return None

    if state.get("user_id") != current_user_id():
        return None

    if state.get("mode") != mode or state.get("category") != (category or None) or state.get("question_limit") != limit or state.get("time_limit") != time_limit:
        return None

    return state


def get_mode_label(mode):
    return {
        "random": "ランダム演習",
        "category": "カテゴリ別演習",
        "wrong": "間違えた問題演習",
        "review": "要復習問題演習",
        "exam": "試験モード",
    }.get(mode, "問題演習")


def get_mode_description(mode):
    return {
        "random": "全体の問題からランダムに出題します。",
        "category": "選択したカテゴリから問題を出題します。",
        "wrong": "直近で間違えた問題だけを復習します。",
        "review": "優先して復習したい問題だけを出題します。",
        "exam": "制限時間内で連続して解答する実践モードです。",
    }.get(mode, "演習を開始します。")


def format_duration(seconds):
    minutes = seconds // 60
    seconds = seconds % 60
    return f"{minutes:02}:{seconds:02}"


VOCABULARY_LEARNING_MODES = {
    "daily_recommendation": "今日のおすすめ学習",
    "review": "要復習用語学習",
    "all_random": "全用語ランダム学習",
    "category": "カテゴリ学習",
    "status_unlearned": "未学習用語学習",
    "status_unknown": "未理解用語学習",
    "status_ambiguous": "あいまい用語学習",
    "status_understood": "理解済み用語学習",
}

QUESTION_LEARNING_MODES = {
    "random": "ランダム演習",
    "category": "カテゴリ演習",
    "incorrect": "間違えた問題演習",
    "review": "要復習問題演習",
    "exam_online": "試験モード（オンライン）",
    "exam_venue": "試験モード（会場）",
}


def normalize_vocabulary_learning_mode(mode, *, preset=None, status=None, category=None, keyword_id=None, random_flag=False):
    valid_modes = set(VOCABULARY_LEARNING_MODES)
    if mode in valid_modes:
        return mode
    if keyword_id:
        return "review"
    status_mode_map = {
        "未学習": "status_unlearned",
        "未理解": "status_unknown",
        "あいまい": "status_ambiguous",
        "理解済み": "status_understood",
    }
    if status in status_mode_map:
        return status_mode_map[status]
    if category:
        return "category"
    if preset == "recommended":
        return "daily_recommendation"
    return "all_random" if random_flag else "all_random"


def normalize_question_learning_mode(mode, exam_type=None):
    if mode in QUESTION_LEARNING_MODES:
        return mode
    if mode == "category":
        return "category"
    if mode == "wrong":
        return "incorrect"
    if mode == "review":
        return "review"
    if mode == "exam":
        return "exam_venue" if exam_type == "onsite" else "exam_online"
    return "random"


def learning_mode_label(learning_type, learning_mode, learning_category=None, legacy_session_type=None):
    if learning_type == "vocabulary" or legacy_session_type == "term":
        label = VOCABULARY_LEARNING_MODES.get(learning_mode)
        if not label:
            return "用語学習"
        if learning_mode == "category" and learning_category:
            return f"{label}（{learning_category}）"
        return label
    if learning_type == "question" or legacy_session_type == "practice":
        label = QUESTION_LEARNING_MODES.get(learning_mode)
        if not label:
            return "問題演習"
        if learning_mode == "category" and learning_category:
            return f"{label}（{learning_category}）"
        return label
    return "学習"


def format_history_duration(seconds):
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "0分"
    if seconds < 60:
        return "1分未満"
    minutes = max(1, round(seconds / 60))
    if minutes >= 60:
        hours = minutes // 60
        rest = minutes % 60
        return f"{hours}時間{rest}分" if rest else f"{hours}時間"
    return f"{minutes}分"


def build_quiz_summary(state):
    if not state:
        return {
            "total": 0,
            "answered": 0,
            "correct": 0,
            "incorrect": 0,
            "unanswered": 0,
            "accuracy": 0,
            "elapsed_seconds": 0,
        }
    total = len(state.get("questions", []))
    answered = sum(1 for answer in state.get("answers", []) if answer.get("selected_choice") is not None)
    correct = sum(1 for answer in state.get("answers", []) if answer.get("correct"))
    denominator = total if state.get("completed") else answered
    accuracy = int((correct / denominator) * 100) if denominator else 0
    return {
        "total": total,
        "answered": answered,
        "correct": correct,
        "incorrect": max(0, denominator - correct),
        "unanswered": max(0, total - answered),
        "accuracy": accuracy,
        "elapsed_seconds": state.get("elapsed_seconds", 0),
    }


def build_category_summary(state):
    if not state:
        return []
    by_category = {}
    for idx, question in enumerate(get_state_questions(state)):
        category = question.get("category") or "未分類"
        by_category.setdefault(category, {"total": 0, "answered": 0, "correct": 0})
        by_category[category]["total"] += 1
        answer = state.get("answers", [])[idx] if state.get("answers") else None
        if answer and answer.get("selected_choice") is not None:
            by_category[category]["answered"] += 1
            if answer.get("correct"):
                by_category[category]["correct"] += 1

    return [
        {
            "category": cat,
            "total": data["total"],
            "answered": data["answered"],
            "correct": data["correct"],
            "unanswered": max(0, data["total"] - data["answered"]),
            "accuracy": int((data["correct"] / (data["total"] if state.get("completed") else data["answered"]) * 100))
            if (data["total"] if state.get("completed") else data["answered"])
            else 0,
        }
        for cat, data in by_category.items()
    ]


def record_practice_history(state):
    if not state or state.get("history_recorded"):
        return state
    user_id = current_user_id()
    if state.get("user_id") != user_id:
        return state
    if not (state.get("completed") or state.get("graded_pause")):
        return state

    now = datetime.now().isoformat()
    rows = []
    answers = state.get("answers", [])
    answered_count = sum(1 for answer in answers if answer.get("selected_choice") is not None)
    total_active_seconds = int(state.get("active_seconds") or state.get("elapsed_seconds") or 0)
    duration_per_answer = int(total_active_seconds / answered_count) if answered_count else 0
    practice_mode = "exam" if state.get("mode") == "exam" else "normal"
    learning_mode = state.get("learning_mode") or normalize_question_learning_mode(state.get("mode"), state.get("exam_type"))
    learning_category = state.get("learning_category") if learning_mode == "category" else None
    for idx, question in enumerate(get_state_questions(state)):
        answer = answers[idx] if idx < len(answers) else {}
        if answer.get("selected_choice") is None:
            continue
        rows.append(
            (
                "practice",
                question.get("id"),
                "correct" if answer.get("correct") else "incorrect",
                now,
                question.get("category") or "未分類",
                1,
                1 if answer.get("correct") else 0,
                duration_per_answer,
                user_id,
                practice_mode,
                "question",
                learning_mode,
                learning_category,
                state.get("started_at") or now,
                now,
            )
        )

    if rows:
        with connect_db(app.config["DATABASE"]) as db:
            db.executemany(
                """
                INSERT INTO history (
                    session_type, item_id, result, created_at, category, amount, correct, duration_seconds, user_id, practice_mode,
                    learning_type, learning_mode, learning_category, started_at, ended_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            db.commit()
    state["history_recorded"] = True
    return state


HISTORY_CATEGORY_STYLES = [
    {"color": "#059669", "icon": "◷"},
    {"color": "#2563eb", "icon": "◇"},
    {"color": "#7c3aed", "icon": "♙"},
    {"color": "#f59e0b", "icon": "▣"},
    {"color": "#0891b2", "icon": "▤"},
    {"color": "#ef4444", "icon": "※"},
    {"color": "#64748b", "icon": "♢"},
]


def history_category_style(category_name):
    seed = sum(ord(char) for char in (category_name or "その他"))
    return HISTORY_CATEGORY_STYLES[seed % len(HISTORY_CATEGORY_STYLES)]


def get_answer_status(answer):
    if not answer or answer.get("selected_choice") is None:
        return "unanswered"
    return "correct" if answer.get("correct") else "incorrect"


def build_result_details(state):
    if not state:
        return []
    answers = state.get("answers", [])
    details = []
    for idx, question in enumerate(get_state_questions(state)):
        answer = answers[idx] if idx < len(answers) else {}
        status = get_answer_status(answer)
        details.append(
            {
                "number": idx + 1,
                "id": question["id"],
                "category": question.get("category") or "未分類",
                "question": question.get("question", ""),
                "selected_choice": answer.get("selected_choice"),
                "correct_answer": question.get("correct_answer", ""),
                "explanation": question.get("explanation", ""),
                "status": status,
                "status_label": {
                    "correct": "正解",
                    "incorrect": "不正解",
                    "unanswered": "未回答",
                }[status],
            }
        )
    return details


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    error = None
    if request.method == "POST":
        if not validate_csrf_token():
            error = "フォームの有効期限が切れました。もう一度お試しください。"
        else:
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            user = User.query.filter_by(username=username).first()
            if user is None or not user.check_password(password):
                error = "ユーザー名またはパスワードが正しくありません"
            else:
                login_user(user, remember=True)
                next_url = request.args.get("next")
                if not next_url or not next_url.startswith("/"):
                    next_url = url_for("home")
                return redirect(next_url)

    return render_template("login.html", error=error)


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    error = None
    username = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        password_confirm = request.form.get("password_confirm") or ""

        if not validate_csrf_token():
            error = "フォームの有効期限が切れました。もう一度お試しください。"
        elif not username or not password or not password_confirm:
            error = "すべての項目を入力してください。"
        elif User.query.filter_by(username=username).first():
            error = "このユーザー名はすでに使用されています。"
        elif password != password_confirm:
            error = "パスワードと確認用パスワードが一致しません。"
        elif len(password) < 8:
            error = "パスワードは8文字以上で入力してください。"
        else:
            user = User(username=username)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user, remember=True)
            return redirect(url_for("home"))

    return render_template("register.html", error=error, username=username)


@app.route("/logout", methods=["POST"])
def logout():
    if not validate_csrf_token():
        return redirect(url_for("home"))
    session.pop("quiz", None)
    logout_user()
    return redirect(url_for("login"))


@app.route("/goals", methods=["POST"])
@login_required
def update_goals():
    def clean_goal(name, minimum, maximum):
        value = (request.form.get(name) or "").strip()
        if not value:
            return None
        parsed = parse_int(value)
        if parsed is None:
            return None
        return max(minimum, min(maximum, parsed))

    current_user.daily_minute_goal = clean_goal("daily_minute_goal", 1, 600)
    current_user.daily_question_goal = clean_goal("daily_question_goal", 1, 300)
    current_user.daily_term_goal = clean_goal("daily_term_goal", 1, 500)
    db.session.commit()
    return redirect(url_for("home"))


@app.route("/")
@login_required
def home():
    db_path = app.config["DATABASE"]
    user_id = current_user_id()
    dashboard = get_dashboard(app.config["DATABASE"], user_id=user_id)
    today = datetime.now().date()
    today_key = today.isoformat()
    today_rows = query_db(
        db_path,
        """
        SELECT session_type,
               SUM(COALESCE(amount, 1)) AS amount,
               SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS correct_count,
               SUM(COALESCE(duration_seconds, 0)) AS duration_seconds
        FROM history
        WHERE DATE(created_at) = ? AND user_id = ?
        GROUP BY session_type
        """,
        (today_key, user_id),
    )
    today_minutes = 0
    today_questions = 0
    today_correct = 0
    today_terms = 0
    for row in today_rows:
        today_minutes += round((row["duration_seconds"] or 0) / 60)
        if row["session_type"] == "practice":
            today_questions += row["amount"] or 0
            today_correct += row["correct_count"] or 0
        elif row["session_type"] == "term":
            today_terms += row["amount"] or 0
    today_accuracy = int((today_correct / today_questions) * 100) if today_questions else 0

    active_days = {
        row["studied_on"]
        for row in query_db(
            db_path,
            "SELECT DISTINCT DATE(created_at) AS studied_on FROM history WHERE user_id = ? AND (amount > 0 OR duration_seconds > 0)",
            (user_id,),
        )
    }
    current_streak = 0
    cursor_day = today
    while cursor_day.isoformat() in active_days:
        current_streak += 1
        cursor_day -= timedelta(days=1)

    category_rows = query_db(
        db_path,
        """
        SELECT category,
               COUNT(*) AS total,
               SUM(CASE WHEN COALESCE(kp.learning_status, '未学習') = '理解済み' THEN 1 ELSE 0 END) AS understood,
               SUM(CASE WHEN COALESCE(kp.learning_status, '未学習') = 'あいまい' THEN 1 ELSE 0 END) AS ambiguous,
               SUM(CASE WHEN COALESCE(kp.learning_status, '未学習') = '未理解' THEN 1 ELSE 0 END) AS not_understood,
               SUM(CASE WHEN COALESCE(kp.learning_status, '未学習') = '未学習' THEN 1 ELSE 0 END) AS untrained
        FROM keywords
        LEFT JOIN keyword_progress kp ON kp.keyword_id = keywords.id AND kp.user_id = ?
        GROUP BY category
        ORDER BY category
        """,
        (user_id,),
    )
    practice_rows = query_db(
        db_path,
        """
        SELECT COALESCE(category, '未分類') AS category,
               COUNT(*) AS total,
               SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS correct
        FROM history
        WHERE session_type = 'practice' AND category IS NOT NULL AND correct IS NOT NULL AND user_id = ?
        GROUP BY COALESCE(category, '未分類')
        """,
        (user_id,),
    )
    practice_by_category = {
        row["category"]: {"total": row["total"] or 0, "correct": row["correct"] or 0}
        for row in practice_rows
    }
    category_scores = []
    for row in category_rows:
        name = row["category"] or "未分類"
        total = row["total"] or 0
        understood = row["understood"] or 0
        ambiguous = row["ambiguous"] or 0
        not_understood = (row["not_understood"] or 0) + (row["untrained"] or 0)
        term_rate = int((understood / total) * 100) if total else 0
        practice_stats = practice_by_category.get(name, {"total": 0, "correct": 0})
        practice_rate = int((practice_stats["correct"] / practice_stats["total"]) * 100) if practice_stats["total"] else None
        score = 100 - int((term_rate + (practice_rate if practice_rate is not None else term_rate)) / 2)
        category_scores.append(
            {
                "name": name,
                "term_rate": term_rate,
                "practice_rate": practice_rate,
                "weak_terms": ambiguous + not_understood,
                "score": score,
                "stars": max(1, min(5, round(score / 20))),
                **history_category_style(name),
            }
        )
    weak_categories = sorted(category_scores, key=lambda item: (-item["score"], item["name"]))
    top_weak_category = weak_categories[0] if weak_categories else {"name": "ランダム", "practice_rate": None, "score": 0, "stars": 3}

    stale_terms_count = query_db(
        db_path,
        """
        SELECT COUNT(*) AS count
        FROM keywords
        JOIN keyword_progress kp ON kp.keyword_id = keywords.id AND kp.user_id = ?
        WHERE kp.last_studied_at IS NOT NULL
          AND DATE(kp.last_studied_at) <= DATE(?, '-5 days')
        """,
        (user_id, today_key),
        one=True,
    )["count"]
    low_understanding_count = query_db(
        db_path,
        """
        SELECT COUNT(*) AS count
        FROM keywords
        LEFT JOIN keyword_progress kp ON kp.keyword_id = keywords.id AND kp.user_id = ?
        WHERE COALESCE(kp.learning_status, '未学習') IN ('未理解', 'あいまい')
        """,
        (user_id,),
        one=True,
    )["count"]
    low_accuracy_count = query_db(
        db_path,
        "SELECT COUNT(*) AS count FROM history WHERE session_type = 'practice' AND correct = 0 AND user_id = ?",
        (user_id,),
        one=True,
    )["count"]
    weak_category_count = sum(
        1 for item in category_scores
        if item["term_rate"] < 50 or (item["practice_rate"] is not None and item["practice_rate"] < 40)
    )
    review_items = [
        {"label": "5日以上復習していない用語", "count": stale_terms_count, "unit": "語", "icon": "▣"},
        {"label": "正答率40%未満の問題", "count": low_accuracy_count, "unit": "問", "icon": "✎"},
        {"label": "理解度が低い用語", "count": low_understanding_count, "unit": "語", "icon": "⌁"},
        {"label": "おすすめ復習カテゴリ", "count": weak_category_count, "unit": "件", "icon": "◎"},
    ]

    practice_answer_count = query_db(
        db_path,
        "SELECT COUNT(*) AS count FROM history WHERE user_id = ? AND session_type = 'practice' AND correct IS NOT NULL",
        (user_id,),
        one=True,
    )["count"]
    studied_category_count = query_db(
        db_path,
        "SELECT COUNT(DISTINCT category) AS count FROM history WHERE user_id = ? AND category IS NOT NULL AND (amount > 0 OR duration_seconds > 0)",
        (user_id,),
        one=True,
    )["count"]
    has_enough_history = practice_answer_count >= 30 and studied_category_count >= 3
    rng = random.Random(f"{user_id}:{today_key}:home")
    beginner_categories = [item["name"] for item in category_scores if item["name"]]
    beginner_priority = ["AIの基礎", "機械学習", "ディープラーニングの概要", "数学・統計", "AIの社会実装に向けて"]
    beginner_candidates = [name for name in beginner_priority if name in beginner_categories] or beginner_categories[:6] or ["AIの基礎"]
    beginner_category = rng.choice(beginner_candidates)

    term_candidates = query_db(
        db_path,
        """
        SELECT keywords.*,
               COALESCE(kp.learning_status, '未学習') AS learning_status,
               kp.last_studied_at AS last_studied_at
        FROM keywords
        LEFT JOIN keyword_progress kp ON kp.keyword_id = keywords.id AND kp.user_id = ?
        WHERE COALESCE(kp.learning_status, '未学習') IN ('未理解', 'あいまい', '未学習')
        ORDER BY
          CASE COALESCE(kp.learning_status, '未学習')
            WHEN '未理解' THEN 0
            WHEN 'あいまい' THEN 1
            ELSE 2
          END,
          kp.last_studied_at IS NOT NULL,
          kp.last_studied_at,
          keyword
        LIMIT 20
        """,
        (user_id,),
    )
    term_pool = [dict(row) for row in term_candidates]
    if has_enough_history and term_pool:
        recommended_term = rng.choice(term_pool[: min(8, len(term_pool))])
    else:
        beginner_terms = get_keywords(db_path, category=beginner_category, user_id=user_id)
        recommended_term = dict(rng.choice(beginner_terms)) if beginner_terms else (term_pool[0] if term_pool else None)
    if recommended_term and recommended_term.get("last_studied_at"):
        try:
            term_days_ago = (today - datetime.fromisoformat(recommended_term["last_studied_at"]).date()).days
        except ValueError:
            term_days_ago = None
    else:
        term_days_ago = None

    resume_state = session.get("quiz") if isinstance(session.get("quiz"), dict) else None
    resume_card = None
    if resume_state and resume_state.get("user_id") == user_id and resume_state.get("questions") and not resume_state.get("completed"):
        answered = sum(1 for answer in resume_state.get("answers", []) if answer.get("selected_choice") is not None)
        total = len(resume_state.get("questions", []))
        if answered > 0 or resume_state.get("paused"):
            resume_card = {
                "mode_label": "問題演習" if resume_state.get("mode") != "exam" else "模試",
                "category": resume_state.get("category") or ("試験モード" if resume_state.get("mode") == "exam" else "ランダム演習"),
                "answered": answered,
                "total": total,
                "rate": int((answered / total) * 100) if total else 0,
                "url": url_for(
                    "practice_quiz",
                    mode=resume_state.get("mode") or "normal",
                    category=resume_state.get("category") or "",
                    limit=resume_state.get("question_limit") or "",
                    time_limit=resume_state.get("time_limit") or "",
                    exam_type=resume_state.get("exam_type") or "",
                ),
            }

    today_context = {
        "date_label": today.strftime("%Y/%m/%d"),
        "streak": current_streak,
        "minutes": today_minutes,
        "questions": today_questions,
        "terms": today_terms,
        "accuracy": today_accuracy,
        "minute_goal": current_user.daily_minute_goal,
        "question_goal": current_user.daily_question_goal,
        "term_goal": current_user.daily_term_goal,
    }
    inactive_categories = sorted(category_scores, key=lambda item: (item["practice_rate"] is not None, item["name"]))
    if has_enough_history:
        weighted_categories = []
        for item in category_scores:
            weight = 1
            if item["term_rate"] < 60:
                weight += 3
            if item["practice_rate"] is not None and item["practice_rate"] < 60:
                weight += 3
            if item["weak_terms"] > 0:
                weight += 2
            weighted_categories.extend([item] * weight)
        recommended_category = rng.choice(weighted_categories or weak_categories or [{"name": beginner_category, "practice_rate": None}])
    else:
        recommended_category = next((item for item in category_scores if item["name"] == beginner_category), None) or {"name": beginner_category, "practice_rate": None}

    term_goal = current_user.daily_term_goal or 10
    question_goal = current_user.daily_question_goal or 10
    term_recommendation = {
        "keyword": recommended_term["keyword"] if recommended_term else "未理解の用語",
        "category": recommended_term["category"] if recommended_term else beginner_category,
        "reason": (
            "基本カテゴリから日替わりで選びました"
            if not has_enough_history
            else
            f"{term_days_ago}日以上復習していません"
            if term_days_ago is not None and term_days_ago >= 1
            else "理解度が低い用語から復習しましょう"
        ),
        "goal": term_goal,
        "url": url_for(
            "flashcards",
            category=recommended_term["category"] if recommended_term else beginner_category,
            status=None if not recommended_term else recommended_term.get("learning_status"),
            limit=term_goal if term_goal in {10, 20, 30, 50} else 20,
            random=1,
            direction="term_to_meaning",
            learning_mode="daily_recommendation",
        ),
    }
    practice_recommendation = {
        "category": recommended_category["name"],
        "rate": recommended_category.get("practice_rate"),
        "reason": (
            "基本カテゴリから日替わりで選びました"
            if not has_enough_history
            else "最近の履歴と苦手傾向から選びました"
        ),
        "goal": question_goal,
        "url": url_for("practice_quiz", mode="category", category=recommended_category["name"], limit=question_goal, reset=1, start=1),
    }

    return render_template(
        "home.html",
        dashboard=dashboard,
        today_context=today_context,
        review_items=review_items,
        weak_categories=weak_categories[:3],
        weak_category_count=weak_category_count,
        has_enough_history=has_enough_history,
        term_recommendation=term_recommendation,
        practice_recommendation=practice_recommendation,
        resume_card=resume_card,
    )


@app.route("/about")
@login_required
def about():
    return render_template("about.html")


@app.route("/big-categories")
@login_required
def big_categories():
    user_id = current_user_id()
    dashboard = get_dashboard(app.config["DATABASE"], user_id=user_id)
    big_categories_list = get_big_categories(app.config["DATABASE"])
    enriched = []
    category_items = []
    for item in big_categories_list:
        stats = get_big_category_stats(app.config["DATABASE"], item["big_category"], user_id=user_id)
        categories = get_categories(app.config["DATABASE"], big_category=item["big_category"])
        for category in categories:
            cstats = get_category_stats(app.config["DATABASE"], item["big_category"], category["category"], user_id=user_id)
            category_items.append(
                {
                    **category,
                    **cstats,
                    "big_category": item["big_category"],
                }
            )
        enriched.append({**item, **stats, "categories": categories})
    understood_count = len(get_keywords(app.config["DATABASE"], status="理解済み", user_id=user_id))
    ambiguous_count = len(get_keywords(app.config["DATABASE"], status="あいまい", user_id=user_id))
    not_understood_count = len(get_keywords(app.config["DATABASE"], status="未理解", user_id=user_id)) + len(
        get_keywords(app.config["DATABASE"], status="未学習", user_id=user_id)
    )
    total_terms = dashboard["total"]
    understanding_summary = {
        "understood": {
            "count": understood_count,
            "rate": int((understood_count / total_terms) * 100) if total_terms else 0,
        },
        "ambiguous": {
            "count": ambiguous_count,
            "rate": int((ambiguous_count / total_terms) * 100) if total_terms else 0,
        },
        "not_understood": {
            "count": not_understood_count,
            "rate": int((not_understood_count / total_terms) * 100) if total_terms else 0,
        },
    }
    return render_template(
        "big_categories.html",
        big_categories=enriched,
        category_items=category_items,
        total_terms=total_terms,
        understanding_summary=understanding_summary,
    )


@app.route("/categories")
@login_required
def categories():
    user_id = current_user_id()
    big_category = request.args.get("big_category")
    categories_list = get_categories(app.config["DATABASE"], big_category=big_category or None)
    enriched = []
    for item in categories_list:
        stats = get_category_stats(app.config["DATABASE"], big_category or item.get("big_category"), item["category"], user_id=user_id)
        enriched.append({**item, **stats})
    if big_category:
        big_category_stats = get_big_category_stats(app.config["DATABASE"], big_category, user_id=user_id)
    else:
        big_category_stats = None
    return render_template(
        "categories.html",
        categories=enriched,
        big_category=big_category,
        big_category_stats=big_category_stats,
    )


@app.route("/flashcards/settings")
@login_required
def flashcard_settings():
    user_id = current_user_id()
    dashboard = get_dashboard(app.config["DATABASE"], user_id=user_id)
    selected_step = request.args.get("step", "menu")
    if selected_step not in {"menu", "custom"}:
        selected_step = "menu"
    selected_big_category = request.args.get("big_category")
    selected_category = request.args.get("category")
    selected_status = request.args.get("status", "")
    selected_direction = request.args.get("direction", "term_to_meaning")
    selected_limit = request.args.get("limit", "20")
    big_categories_list = get_big_categories(app.config["DATABASE"])
    status_items = []
    for status in ["未学習", "未理解", "あいまい", "理解済み"]:
        status_items.append(
            {
                "label": status,
                "count": len(get_keywords(app.config["DATABASE"], status=status, user_id=user_id)),
                "tone": {
                    "未学習": "slate",
                    "未理解": "rose",
                    "あいまい": "amber",
                    "理解済み": "emerald",
                }[status],
            }
        )
    category_items = []
    for item in big_categories_list:
        categories = get_categories(app.config["DATABASE"], big_category=item["big_category"])
        for category in categories:
            cstats = get_category_stats(app.config["DATABASE"], item["big_category"], category["category"], user_id=user_id)
            category_items.append(
                {
                    **category,
                    **cstats,
                    "big_category": item["big_category"],
                }
            )
    return render_template(
        "flashcard_settings.html",
        category_items=category_items,
        status_items=status_items,
        total_terms=dashboard["total"],
        dashboard=dashboard,
        selected_step=selected_step,
        selected_big_category=selected_big_category,
        selected_category=selected_category,
        selected_status=selected_status,
        selected_direction=selected_direction,
        selected_limit=selected_limit,
    )


@app.route("/category-detail")
@login_required
def category_detail():
    user_id = current_user_id()
    big_category = request.args.get("big_category")
    category = request.args.get("category")
    if not big_category or not category:
        return redirect(url_for("big_categories"))

    stats = get_category_stats(app.config["DATABASE"], big_category, category, user_id=user_id)
    return render_template(
        "category_detail.html",
        big_category=big_category,
        category=category,
        stats=stats,
    )


@app.route("/keywords")
@login_required
def keywords():
    user_id = current_user_id()
    big_category = request.args.get("big_category")
    category = request.args.get("category")
    category_list = [item.strip() for item in (category or "").split(",") if item.strip()]
    status = request.args.get("status")
    search = request.args.get("search", "")
    review_later = request.args.get("review_later") == "1"

    dashboard = get_dashboard(app.config["DATABASE"], user_id=user_id)
    big_categories = get_big_categories(app.config["DATABASE"])
    categories = get_categories(app.config["DATABASE"], big_category=big_category or None)
    keywords = get_keywords(
        app.config["DATABASE"],
        big_category=big_category or None,
        category=category or None,
        status=status or None,
        search=search or None,
        user_id=user_id,
        review_later=review_later,
    )
    all_categories = []
    category_progress = []
    for item in big_categories:
        for category_item in get_categories(app.config["DATABASE"], big_category=item["big_category"]):
            stats = get_category_stats(app.config["DATABASE"], item["big_category"], category_item["category"], user_id=user_id)
            category_record = {
                **category_item,
                **stats,
                "big_category": item["big_category"],
            }
            all_categories.append(category_record)
            category_progress.append(category_record)

    status_counts = {
        "未学習": len(get_keywords(app.config["DATABASE"], status="未学習", user_id=user_id)),
        "未理解": len(get_keywords(app.config["DATABASE"], status="未理解", user_id=user_id)),
        "あいまい": len(get_keywords(app.config["DATABASE"], status="あいまい", user_id=user_id)),
        "理解済み": len(get_keywords(app.config["DATABASE"], status="理解済み", user_id=user_id)),
    }
    studied_count = dashboard["total"] - status_counts["未学習"]
    return render_template(
        "keywords.html",
        keywords=keywords,
        big_categories=big_categories,
        categories=categories,
        all_categories=all_categories,
        category_progress=sorted(category_progress, key=lambda item: item["progress_rate"], reverse=True)[:7],
        dashboard=dashboard,
        status_counts=status_counts,
        studied_count=studied_count,
        selected_big_category=big_category,
        selected_category=category,
        selected_status=status,
        selected_review_later=review_later,
        search=search,
    )


@app.route("/api/keywords/<int:keyword_id>/review_later", methods=["POST"])
@login_required
def api_keyword_review_later(keyword_id):
    data = request.get_json(silent=True) or {}
    review_later = bool(data.get("review_later"))
    updated = set_review_later(app.config["DATABASE"], keyword_id, review_later, user_id=current_user_id())
    if not updated:
        return jsonify({"success": False, "message": "not found"}), 404
    return jsonify({"success": True, "review_later": review_later})


@app.route("/keywords/<int:keyword_id>", methods=["GET", "POST"])
@login_required
def keyword_detail(keyword_id):
    user_id = current_user_id()
    keyword = get_keyword(app.config["DATABASE"], keyword_id, user_id=user_id)
    if keyword is None:
        return redirect(url_for("keywords"))

    message = None
    if request.method == "POST":
        action = request.form.get("action", "status")
        if action == "memo":
            note = request.form.get("note", "")
            updated = set_keyword_note(app.config["DATABASE"], keyword_id, note, user_id=user_id)
            if updated:
                message = "メモを保存しました。"
                keyword = get_keyword(app.config["DATABASE"], keyword_id, user_id=user_id)
            else:
                message = "メモの保存に失敗しました。"
        else:
            status = request.form.get("status")
            updated = set_learning_status(app.config["DATABASE"], keyword_id, status, user_id=user_id)
            if updated:
                message = "理解度を更新しました。"
                keyword = get_keyword(app.config["DATABASE"], keyword_id, user_id=user_id)
            else:
                message = "状態の更新に失敗しました。"

    ordered_terms = query_db(
        app.config["DATABASE"],
        "SELECT id FROM keywords ORDER BY big_category, category, item_no, keyword",
    )
    ordered_ids = [row["id"] for row in ordered_terms]
    try:
        current_pos = ordered_ids.index(keyword_id)
    except ValueError:
        current_pos = -1
    prev_keyword_id = ordered_ids[current_pos - 1] if current_pos > 0 else None
    next_keyword_id = ordered_ids[current_pos + 1] if current_pos >= 0 and current_pos + 1 < len(ordered_ids) else None

    return render_template(
        "keyword_detail.html",
        keyword=keyword,
        message=message,
        prev_keyword_id=prev_keyword_id,
        next_keyword_id=next_keyword_id,
    )


@app.route("/practice")
@login_required
def practice():
    """モード選択画面"""
    user_id = current_user_id()
    stats_row = query_db(
        app.config["DATABASE"],
        """
        SELECT COUNT(*) AS answered,
               SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS correct,
               SUM(CASE WHEN correct = 0 THEN 1 ELSE 0 END) AS incorrect
        FROM history
        WHERE user_id = ? AND session_type = 'practice'
        """,
        (user_id,),
        one=True,
    )
    answered = stats_row["answered"] or 0
    correct = stats_row["correct"] or 0
    incorrect = stats_row["incorrect"] or 0

    resume_state = session.get("quiz") if isinstance(session.get("quiz"), dict) else None
    resume_card = None
    paused_unanswered = 0
    if resume_state and resume_state.get("user_id") == user_id and resume_state.get("questions") and not resume_state.get("completed"):
        summary = build_quiz_summary(resume_state)
        if summary["answered"] > 0 or resume_state.get("paused"):
            paused_unanswered = summary["unanswered"]
            resume_card = {
                "mode_label": get_mode_label(resume_state.get("mode")),
                "category": resume_state.get("category") or ("試験モード" if resume_state.get("mode") == "exam" else "ランダム演習"),
                "answered": summary["answered"],
                "total": summary["total"],
                "accuracy": summary["accuracy"],
                "progress_rate": int((summary["answered"] / summary["total"]) * 100) if summary["total"] else 0,
                "url": url_for(
                    "practice_quiz",
                    mode=resume_state.get("mode") or "normal",
                    category=resume_state.get("category") or "",
                    limit=resume_state.get("question_limit") or "",
                    time_limit=resume_state.get("time_limit") or "",
                    exam_type=resume_state.get("exam_type") or "",
                ),
            }

    practice_summary = {
        "accuracy": int((correct / answered) * 100) if answered else 0,
        "correct": correct,
        "incorrect": incorrect,
        "unanswered": paused_unanswered,
    }
    return render_template("practice.html", practice_summary=practice_summary, resume_card=resume_card)


@app.route("/practice/settings")
@login_required
def practice_settings():
    """モード設定画面（カテゴリ選択など）"""
    mode = request.args.get("mode")
    if not mode or mode not in ["normal", "random", "category", "wrong", "review", "exam"]:
        return redirect(url_for("practice"))

    categories = get_quiz_categories()
    mode_label = "通常演習モード" if mode == "normal" else get_mode_label(mode)
    mode_description = (
        "ランダム演習、カテゴリ演習、間違えた問題演習から選んで学習できます。"
        if mode == "normal"
        else get_mode_description(mode)
    )

    return render_template(
        "practice_settings.html",
        mode=mode,
        mode_label=mode_label,
        mode_description=mode_description,
        categories=categories,
        exam_types=EXAM_TYPES,
        paused_exam=get_paused_exam(session),
    )


@app.route("/practice/quiz", methods=["GET", "POST"])
@login_required
def practice_quiz():
    """問題演習画面"""
    mode = request.args.get("mode") or request.form.get("mode")
    category = request.args.get("category") or request.form.get("category") or None
    question_id = request.args.get("question_id") or request.form.get("question_id") or None
    limit = parse_int(request.args.get("limit") or request.form.get("limit"))
    time_limit = parse_int(request.args.get("time_limit") or request.form.get("time_limit"))
    start = request.args.get("start") == "1" or request.form.get("start") == "1"
    reset = request.args.get("reset") == "1"

    if not mode:
        return redirect(url_for("practice"))

    # exam_type may be provided in args/form; read early so reset can use it
    exam_type = request.args.get("exam_type") or request.form.get("exam_type")

    # セッションから state を取得または新規作成
    if request.method == "POST" and isinstance(session.get("quiz"), dict):
        state = session.get("quiz")
    elif reset or start:
        # If starting but a paused exam exists in session for same mode, keep it (prompt resume)
        existing = session.get('quiz')
        if start and isinstance(existing, dict) and existing.get('mode') == mode and existing.get('paused'):
            state = existing
        else:
            state = reset_quiz_session(session, mode=mode, category=category, limit=limit, time_limit=time_limit, exam_type=exam_type, question_id=question_id)
    else:
        existing = session.get("quiz")
        if (
            isinstance(existing, dict)
            and existing.get("user_id") == current_user_id()
            and existing.get("mode") == mode
            and existing.get("paused")
            and not existing.get("completed")
        ):
            state = existing
        else:
            state = get_quiz_session(session, mode=mode, category=category, limit=limit, time_limit=time_limit)

    if not state:
        state = build_empty_quiz_state(mode=mode, category=category, limit=limit, time_limit=time_limit)
        session["quiz"] = state

    if state.get("user_id") != current_user_id():
        session.pop("quiz", None)
        return redirect(url_for("practice"))

    # exam_type が渡されたら state に保存しておく
    if exam_type:
        state["exam_type"] = exam_type
        session["quiz"] = state

    if request.method == "GET" and mode == "exam" and state.get("paused") and not start and not reset:
        state = resume_quiz_state(state)
        session["quiz"] = state

    mode_label = get_mode_label(mode)
    categories = get_quiz_categories()

    # POST リクエスト処理
    if request.method == "POST":
        # For POST actions we should operate on the existing session state
        # even when request args/form omit start/limit/time_limit.
        if isinstance(session.get('quiz'), dict):
            state = session.get('quiz')
        if state.get("user_id") != current_user_id():
            session.pop("quiz", None)
            return redirect(url_for("practice"))
        action = request.form.get("action", "answer")
        selected_choice = request.form.get("choice")
        question_id = request.form.get("question_id")
        if action in {"answer", "prev", "next", "pause", "grade_pause", "submit", "finish"}:
            state = record_quiz_activity(state)
        if action in {"prev", "next", "pause", "grade_pause", "submit", "finish"}:
            save_current_answer(state, selected_choice, question_id)

        if action == "prev" and state.get("questions"):
            if state["current_index"] > 0:
                state["current_index"] -= 1
                state["last_result"] = None
                session["quiz"] = state

        elif action == "next" and state.get("questions"):
            current_answer = state["answers"][state["current_index"]]
            can_go_next = mode == "exam" or current_answer.get("correct") is not None
            if can_go_next and state["current_index"] + 1 < len(state["questions"]):
                state["current_index"] += 1
                state["last_result"] = None
                session["quiz"] = state

        elif action == "answer" and state.get("questions"):
            if question_id and (selected_choice or mode != "exam"):
                current_question = get_state_question(state, state["current_index"])
                if not current_question:
                    return redirect(url_for("practice"))
                if str(current_question["id"]) == str(question_id):
                    is_correct = bool(selected_choice) and selected_choice == current_question["correct_answer"]
                    state["answers"][state["current_index"]] = {
                        "selected_choice": selected_choice,
                        "correct": is_correct,
                    }
                    state["started_at"] = state.get("started_at") or current_timestamp()
                    update_wrong_question_ids(session, current_question["id"], is_correct)

                    if mode == "exam" and state["current_index"] + 1 >= len(state["questions"]):
                        # クイズ完了
                        state["completed"] = True
                        state["completed_at"] = current_timestamp()
                        started_at = parse_timestamp(state["started_at"])
                        if started_at:
                            state["elapsed_seconds"] = int((parse_timestamp(state["completed_at"]) - started_at).total_seconds())
                        session["quiz"] = state
                        return redirect(url_for("practice_result"))

                    # Exam mode: 自動的に次の問題へ
                    if mode == "exam":
                        state["current_index"] += 1

                    session["quiz"] = state

        elif action == "pause" and state.get("questions"):
            state = pause_quiz_state(state, graded=False)
            session["quiz"] = state
            return redirect(url_for("practice_settings", mode="exam"))

        elif action == "grade_pause" and state.get("questions"):
            state = pause_quiz_state(state, graded=True)
            for idx, question in enumerate(get_state_questions(state)):
                answer = state.get("answers", [])[idx]
                if answer.get("selected_choice") is not None:
                    update_wrong_question_ids(session, question["id"], bool(answer.get("correct")))
            session["quiz"] = state
            return redirect(url_for("practice_result"))

        elif action == "resume":
            state = resume_quiz_state(state)
            session["quiz"] = state

        elif action == "restart":
            # Restart fresh exam
            state = reset_quiz_session(session, mode=mode, category=category, limit=None, time_limit=None, exam_type=state.get('exam_type'))

        elif action in {"submit", "finish"}:
            # ユーザが提出した -> 結果ページへ
            state["completed"] = True
            state["completed_at"] = current_timestamp()
            state["elapsed_seconds"] = get_elapsed_seconds(state)
            for idx, question in enumerate(get_state_questions(state)):
                answer = state.get("answers", [])[idx]
                update_wrong_question_ids(session, question["id"], bool(answer.get("correct")))
            session["quiz"] = state
            return redirect(url_for("practice_result"))

    # クイズ完了時は result ページへリダイレクト
    if state.get("completed"):
        return redirect(url_for("practice_result"))

    normalize_timer_state(state)
    session["quiz"] = state

    # 問題がない場合
    if not state.get("questions"):
        return render_template(
            "practice_quiz.html",
            selected_mode=mode,
            mode_label=mode_label,
            categories=categories,
        )

    # 問題表示
    current_record = get_state_question(state, state["current_index"])
    if not current_record:
        return redirect(url_for("practice"))
    current_question = build_display_question(
        current_record,
        state["answers"][state["current_index"]].get("selected_choice"),
    )
    progress = state["current_index"] + 1
    total = len(state.get("questions", []))
    current_answer = state["answers"][state["current_index"]]
    feedback = None
    if mode != "exam" and current_answer.get("correct") is not None:
        feedback = {
            "is_correct": bool(current_answer.get("correct")),
            "title": "正解です" if current_answer.get("correct") else "不正解です",
            "message": (
                "この調子で進めましょう。"
                if current_answer.get("correct")
                else "解説を確認して、ポイントを押さえましょう。"
            ),
            "explanation": current_question.get("explanation") or "解説は登録されていません。",
        }

    return render_template(
        "practice_quiz.html",
        question=current_question,
        quiz_state=state,
        summary=build_quiz_summary(state),
        categories=categories,
        selected_mode=mode,
        selected_category=category,
        mode_label=mode_label,
        progress=progress,
        total=total,
        start=start,
        exam_config=get_exam_config(state.get("exam_type")),
        feedback=feedback,
    )


@app.route('/practice/pause')
@login_required
def practice_pause():
    """Mark current quiz as paused and return to practice mode."""
    state = session.get('quiz')
    if not isinstance(state, dict) or state.get("user_id") != current_user_id():
        return redirect(url_for('practice'))
    state = pause_quiz_state(state, graded=False)
    session['quiz'] = state
    return redirect(url_for('practice_settings', mode='exam'))



@app.route("/practice/result")
@login_required
def practice_result():
    """結果表示画面"""
    state = session.get("quiz")
    if not state or state.get("user_id") != current_user_id() or not (state.get("completed") or state.get("graded_pause")):
        return redirect(url_for("practice"))

    if state.get("graded_pause"):
        state["elapsed_seconds"] = state.get("paused_elapsed_seconds", state.get("elapsed_seconds", 0))
        session["quiz"] = state
    elif not state.get("completed_at"):
        state["completed_at"] = current_timestamp()
        started_at = parse_timestamp(state["started_at"])
        if started_at:
            state["elapsed_seconds"] = int((parse_timestamp(state["completed_at"]) - started_at).total_seconds())
        session["quiz"] = state

    state = record_practice_history(state)
    session["quiz"] = state

    return render_template(
        "practice_result.html",
        quiz_state=state,
        summary=build_quiz_summary(state),
        category_summary=build_category_summary(state),
        selected_mode=state.get("mode"),
    )


@app.route("/practice/result/detail")
@login_required
def practice_result_detail():
    """結果詳細・解説画面"""
    state = session.get("quiz")
    if not state or state.get("user_id") != current_user_id() or not (state.get("completed") or state.get("graded_pause")):
        return redirect(url_for("practice"))

    details = build_result_details(state)
    selected_number = parse_int(request.args.get("number"), 1) or 1
    if selected_number < 1 or selected_number > len(details):
        selected_number = 1
    selected_detail = details[selected_number - 1] if details else None

    status_filter = request.args.get("status", "all")
    category_filter = request.args.get("category", "all")
    active_view = request.args.get("view")
    if active_view not in {"number", "list"}:
        active_view = "list" if status_filter != "all" or category_filter != "all" else "number"
    filtered_details = details
    if status_filter in {"correct", "incorrect", "unanswered"}:
        filtered_details = [item for item in filtered_details if item["status"] == status_filter]
    if category_filter and category_filter != "all":
        filtered_details = [item for item in filtered_details if item["category"] == category_filter]

    categories = sorted({item["category"] for item in details})
    return render_template(
        "practice_result_detail.html",
        quiz_state=state,
        summary=build_quiz_summary(state),
        details=details,
        selected_detail=selected_detail,
        filtered_details=filtered_details,
        categories=categories,
        status_filter=status_filter,
        category_filter=category_filter,
        active_view=active_view,
    )




@app.route("/history")
@login_required
def history():
    db_path = app.config["DATABASE"]
    user_id = current_user_id()
    dashboard = get_dashboard(db_path, user_id=user_id)
    view_filter = request.args.get("view", "all")
    if view_filter not in {"all", "term", "practice"}:
        view_filter = "all"
    period = parse_int(request.args.get("period"), 30)
    if period not in {7, 30, 90}:
        period = 30
    today = datetime.now().date()
    start_date = today - timedelta(days=period - 1)

    history_rows = query_db(
        db_path,
        """
        SELECT DATE(created_at) AS studied_on,
               session_type,
               COALESCE(category, 'その他') AS category,
               COUNT(*) AS count,
               SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS correct_count,
               SUM(COALESCE(duration_seconds, 0)) AS duration_seconds
        FROM history
        WHERE created_at >= ? AND user_id = ?
        GROUP BY DATE(created_at), session_type, COALESCE(category, 'その他')
        ORDER BY studied_on
        """,
        (start_date.isoformat(), user_id),
    )

    term_counts_by_date = {}
    practice_counts_by_date = {}
    term_seconds_by_date = {}
    practice_seconds_by_date = {}
    practice_total = 0
    practice_correct = 0

    for row in history_rows:
        session_type = row["session_type"]
        studied_on = row["studied_on"]
        count = row["count"] or 0
        duration_seconds = row["duration_seconds"] or 0
        minutes = max(1, round(duration_seconds / 60)) if duration_seconds > 0 else 0

        if session_type == "practice":
            practice_counts_by_date[studied_on] = practice_counts_by_date.get(studied_on, 0) + count
            practice_seconds_by_date[studied_on] = practice_seconds_by_date.get(studied_on, 0) + duration_seconds
            practice_total += count
            practice_correct += row["correct_count"] or 0
        else:
            term_counts_by_date[studied_on] = term_counts_by_date.get(studied_on, 0) + count
            term_seconds_by_date[studied_on] = term_seconds_by_date.get(studied_on, 0) + duration_seconds

    if not history_rows and view_filter != "practice":
        fallback_rows = query_db(
            db_path,
            """
            SELECT DATE(kp.last_studied_at) AS studied_on,
                   keywords.category AS category,
                   COUNT(*) AS count
            FROM keywords
            JOIN keyword_progress kp ON kp.keyword_id = keywords.id AND kp.user_id = ?
            WHERE kp.last_studied_at IS NOT NULL AND kp.last_studied_at >= ?
            GROUP BY DATE(kp.last_studied_at), keywords.category
            ORDER BY studied_on
            """,
            (user_id, start_date.isoformat()),
        )
        for row in fallback_rows:
            studied_on = row["studied_on"]
            count = row["count"] or 0
            duration_seconds = 0
            minutes = 0
            term_counts_by_date[studied_on] = term_counts_by_date.get(studied_on, 0) + count
            term_seconds_by_date[studied_on] = term_seconds_by_date.get(studied_on, 0) + duration_seconds

    daily_totals = {}
    for offset in range(period):
        day = start_date + timedelta(days=offset)
        key = day.isoformat()
        daily_totals[key] = term_seconds_by_date.get(key, 0) + practice_seconds_by_date.get(key, 0)
    max_daily_total = max(daily_totals.values() or [0])
    calendar_base_seconds = max(max_daily_total, 60 * 60)
    calendar_max_minutes = max(60, round(calendar_base_seconds / 60))

    def calendar_level(total):
        if total <= 0:
            return "none"
        ratio = min(total / calendar_base_seconds, 1)
        if ratio <= 0.25:
            return "low"
        if ratio <= 0.5:
            return "mid"
        if ratio <= 0.75:
            return "strong"
        return "high"

    daily_counts = []
    for offset in range(period):
        day = start_date + timedelta(days=offset)
        key = day.isoformat()
        term_count = term_counts_by_date.get(key, 0)
        practice_count = practice_counts_by_date.get(key, 0)
        total = term_count + practice_count
        total_seconds = term_seconds_by_date.get(key, 0) + practice_seconds_by_date.get(key, 0)
        term_minutes = max(1, round(term_seconds_by_date.get(key, 0) / 60)) if term_seconds_by_date.get(key, 0) > 0 else 0
        practice_minutes = max(1, round(practice_seconds_by_date.get(key, 0) / 60)) if practice_seconds_by_date.get(key, 0) > 0 else 0
        minutes = max(1, round(total_seconds / 60)) if total_seconds > 0 else 0
        daily_counts.append(
            {
                "date": day,
                "label": f"{day.month}/{day.day}",
                "term_count": term_count,
                "practice_count": practice_count,
                "count": total,
                "minutes": minutes,
                "term_minutes": term_minutes,
                "practice_minutes": practice_minutes,
                "level": calendar_level(total_seconds),
            }
        )

    calendar_start = start_date - timedelta(days=start_date.weekday())
    calendar_end = today + timedelta(days=6 - today.weekday())
    calendar_days = []
    calendar_offset = 0
    while calendar_start + timedelta(days=calendar_offset) <= calendar_end:
        day = calendar_start + timedelta(days=calendar_offset)
        key = day.isoformat()
        term_count = term_counts_by_date.get(key, 0)
        practice_count = practice_counts_by_date.get(key, 0)
        total = term_count + practice_count
        total_seconds = term_seconds_by_date.get(key, 0) + practice_seconds_by_date.get(key, 0)
        minutes = max(1, round(total_seconds / 60)) if total_seconds > 0 else 0
        calendar_days.append(
            {
                "date": day,
                "label": f"{day.month}/{day.day}",
                "day": day.day,
                "term_count": term_count,
                "practice_count": practice_count,
                "count": total,
                "minutes": minutes,
                "in_period": start_date <= day <= today,
                "is_today": day == today,
                "level": calendar_level(total_seconds),
            }
        )
        calendar_offset += 1
    calendar_weeks = [calendar_days[i:i + 7] for i in range(0, len(calendar_days), 7)]

    if view_filter == "term":
        max_minutes = max([item["term_minutes"] for item in daily_counts] or [1]) or 1
    elif view_filter == "practice":
        max_minutes = max([item["practice_minutes"] for item in daily_counts] or [1]) or 1
    else:
        max_minutes = max(
            [item["term_minutes"] for item in daily_counts] + [item["practice_minutes"] for item in daily_counts] or [1]
        ) or 1

    chart_categories = [
        {"name": "用語学習", "color": "#10b981", "icon": "▣"},
        {"name": "問題演習", "color": "#2563eb", "icon": "✎"},
    ]

    chart_bars = []
    bar_step = 560 / max(period, 1)
    bar_width = 10 if period <= 30 else 4
    for index, item in enumerate(daily_counts):
        x = 24 + (index * bar_step)
        segments = []
        if view_filter in {"all", "term"} and item["term_minutes"] > 0:
            height = (item["term_minutes"] / max_minutes) * 140
            segments.append({
                "category": "用語学習",
                "minutes": item["term_minutes"],
                "color": "#10b981",
                "x": round(x - (bar_width / 2 if view_filter == "all" else 0), 1),
                "y": round(180 - height, 1),
                "height": round(height, 1),
                "width": bar_width,
            })
        if view_filter in {"all", "practice"} and item["practice_minutes"] > 0:
            height = (item["practice_minutes"] / max_minutes) * 140
            segments.append({
                "category": "問題演習",
                "minutes": item["practice_minutes"],
                "color": "#2563eb",
                "x": round(x + (bar_width / 2 if view_filter == "all" else 0), 1),
                "y": round(180 - height, 1),
                "height": round(height, 1),
                "width": bar_width,
            })
        chart_bars.append({**item, "x": round(x, 1), "segments": segments})

    category_rows = get_categories(db_path)
    category_rates = []
    for item in category_rows:
        total = query_db(
            db_path,
            "SELECT COUNT(*) AS count FROM keywords WHERE category = ?",
            (item["category"],),
            one=True,
        )["count"]
        understood = query_db(
            db_path,
            """
            SELECT COUNT(*) AS count
            FROM keywords
            LEFT JOIN keyword_progress kp ON kp.keyword_id = keywords.id AND kp.user_id = ?
            WHERE category = ? AND COALESCE(kp.learning_status, '未学習') = ?
            """,
            (user_id, item["category"], "理解済み"),
            one=True,
        )["count"]
        ambiguous = query_db(
            db_path,
            """
            SELECT COUNT(*) AS count
            FROM keywords
            LEFT JOIN keyword_progress kp ON kp.keyword_id = keywords.id AND kp.user_id = ?
            WHERE category = ? AND COALESCE(kp.learning_status, '未学習') = ?
            """,
            (user_id, item["category"], "あいまい"),
            one=True,
        )["count"]
        not_understood = query_db(
            db_path,
            """
            SELECT COUNT(*) AS count
            FROM keywords
            LEFT JOIN keyword_progress kp ON kp.keyword_id = keywords.id AND kp.user_id = ?
            WHERE category = ? AND COALESCE(kp.learning_status, '未学習') = ?
            """,
            (user_id, item["category"], "未理解"),
            one=True,
        )["count"]
        stats = {
            "total": total,
            "understood": understood,
            "ambiguous": ambiguous,
            "not_understood": not_understood,
        }
        term_rate = int((stats["understood"] / stats["total"]) * 100) if stats["total"] else 0
        practice_stats = query_db(
            db_path,
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS correct
            FROM history
            WHERE session_type = 'practice' AND category = ? AND user_id = ?
            """,
            (item["category"], user_id),
            one=True,
        )
        practice_answered = practice_stats["total"] or 0
        practice_correct_for_category = practice_stats["correct"] or 0
        practice_rate = int((practice_correct_for_category / practice_answered) * 100) if practice_answered else 0
        tone = "green" if term_rate >= 70 else "amber" if term_rate >= 45 else "rose"
        category_rates.append({
            **stats,
            "name": item["category"],
            **history_category_style(item["category"]),
            "term_rate": term_rate,
            "practice_rate": practice_rate,
            "practice_answered": practice_answered,
            "tone": tone,
        })
    category_rates = sorted(category_rates, key=lambda item: item["term_rate"], reverse=True)[:7]

    review_terms = []
    for status in ["未理解", "あいまい", "未学習"]:
        review_terms.extend(get_keywords(db_path, status=status, user_id=user_id)[:8])
    seen = set()
    unique_review_terms = []
    for term in review_terms:
        if term["id"] in seen:
            continue
        seen.add(term["id"])
        last_studied = term.get("last_studied_at")
        days_ago = None
        if last_studied:
            try:
                days_ago = (today - datetime.fromisoformat(last_studied).date()).days
            except ValueError:
                days_ago = None
        unique_review_terms.append({**term, "days_ago": days_ago})
        if len(unique_review_terms) >= 5:
            break

    status_counts = {
        "未学習": len(get_keywords(db_path, status="未学習", user_id=user_id)),
        "未理解": len(get_keywords(db_path, status="未理解", user_id=user_id)),
        "あいまい": len(get_keywords(db_path, status="あいまい", user_id=user_id)),
        "理解済み": len(get_keywords(db_path, status="理解済み", user_id=user_id)),
    }
    studied_terms = dashboard["total"] - status_counts["未学習"]
    study_days = sum(1 for item in daily_counts if item["count"] > 0)
    current_streak = 0
    for item in reversed(daily_counts):
        if item["count"] <= 0:
            if item["date"] == today:
                continue
            break
        current_streak += 1
    total_duration_row = query_db(
        db_path,
        "SELECT SUM(COALESCE(duration_seconds, 0)) AS duration_seconds, SUM(amount) AS amount FROM history WHERE user_id = ?",
        (user_id,),
        one=True,
    )
    total_study_days_row = query_db(
        db_path,
        """
        SELECT COUNT(DISTINCT DATE(created_at)) AS count
        FROM history
        WHERE user_id = ?
        """,
        (user_id,),
        one=True,
    )
    total_study_days = total_study_days_row["count"] or 0
    total_duration_seconds = total_duration_row["duration_seconds"] or 0
    estimated_minutes = round(total_duration_seconds / 60)
    understood_rate = int((status_counts["理解済み"] / dashboard["total"]) * 100) if dashboard["total"] else 0
    problem_accuracy = int((practice_correct / practice_total) * 100) if practice_total else 0
    review_term_groups = [
        {"label": "5日以上復習していない用語", "count": sum(1 for item in unique_review_terms if item.get("days_ago") is None or item.get("days_ago", 0) >= 5), "tone": "rose"},
        {"label": "あいまいな用語", "count": status_counts["あいまい"], "tone": "amber"},
        {"label": "未理解の用語", "count": status_counts["未理解"], "tone": "blue"},
    ]
    review_problem_groups = [
        {"label": "5日以上解いていない問題", "count": 0, "tone": "rose"},
        {"label": "正答率が低い問題", "count": max(0, practice_total - practice_correct), "tone": "amber"},
        {"label": "あいまいな問題", "count": 0, "tone": "blue"},
    ]
    recent_rows = query_db(
        db_path,
        """
        SELECT DATE(created_at) AS studied_on,
               MAX(created_at) AS last_created_at,
               session_type,
               COALESCE(learning_type, CASE WHEN session_type = 'practice' THEN 'question' ELSE 'vocabulary' END) AS learning_type,
               learning_mode,
               learning_category,
               COALESCE(MAX(category), 'その他') AS category,
               SUM(COALESCE(amount, 1)) AS amount,
               SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS correct_count,
               SUM(COALESCE(duration_seconds, 0)) AS duration_seconds
        FROM history
        WHERE user_id = ?
        GROUP BY DATE(created_at), session_type, COALESCE(learning_type, CASE WHEN session_type = 'practice' THEN 'question' ELSE 'vocabulary' END), learning_mode, learning_category
        ORDER BY MAX(created_at) DESC
        LIMIT 8
        """,
        (user_id,),
    )
    recent_history = []
    weekday_labels = ["月", "火", "水", "木", "金", "土", "日"]
    for row in recent_rows:
        amount = row["amount"] or 0
        correct_count = row["correct_count"] or 0
        duration_seconds = row["duration_seconds"] or 0
        try:
            studied_date = datetime.fromisoformat(row["studied_on"]).date()
            display_date = f"{studied_date.month}/{studied_date.day}（{weekday_labels[studied_date.weekday()]}）"
        except ValueError:
            display_date = row["studied_on"]
        try:
            display_time = datetime.fromisoformat(row["last_created_at"]).strftime("%H:%M")
        except (TypeError, ValueError):
            display_time = ""
        category_style = history_category_style(row["category"])
        is_practice = row["session_type"] == "practice"
        accuracy = int((correct_count / amount) * 100) if is_practice and amount else None
        learning_content = learning_mode_label(
            row["learning_type"],
            row["learning_mode"],
            row["learning_category"],
            legacy_session_type=row["session_type"],
        )
        recent_history.append({
            "date": row["studied_on"],
            "display_date": display_date,
            "display_time": display_time,
            "type": row["session_type"],
            "type_label": "問題演習" if is_practice else "用語学習",
            "type_icon": "✎" if is_practice else "▣",
            "type_tone": "blue" if is_practice else "green",
            "category": row["category"],
            "category_color": category_style["color"],
            "amount": amount,
            "correct_count": correct_count,
            "incorrect_count": max(0, amount - correct_count) if is_practice else None,
            "duration_label": format_history_duration(duration_seconds),
            "accuracy": accuracy,
            "content": learning_content,
            "result": (
                f"正解：{correct_count}問 / 不正解：{max(0, amount - correct_count)}問"
                if is_practice
                else f"学習：{amount}語"
            ),
        })

    return render_template(
        "history.html",
        dashboard=dashboard,
        status_counts=status_counts,
        studied_terms=studied_terms,
        solved_problems=practice_total,
        problem_accuracy=problem_accuracy,
        study_days=study_days,
        total_study_days=total_study_days,
        current_streak=current_streak,
        estimated_minutes=estimated_minutes,
        understood_rate=understood_rate,
        calendar_weeks=calendar_weeks,
        chart_bars=chart_bars,
        chart_categories=chart_categories,
        max_minutes=max_minutes,
        calendar_max_minutes=calendar_max_minutes,
        category_rates=category_rates,
        review_terms=unique_review_terms,
        review_term_groups=review_term_groups,
        review_problem_groups=review_problem_groups,
        recent_history=recent_history,
        view_filter=view_filter,
        period=period,
        start_date=start_date,
        today=today,
    )


@app.route("/statistics")
@login_required
def statistics():
    db_path = app.config["DATABASE"]
    user_id = current_user_id()
    dashboard = get_dashboard(db_path, user_id=user_id)

    raw_status_counts = {
        "理解済み": len(get_keywords(db_path, status="理解済み", user_id=user_id)),
        "あいまい": len(get_keywords(db_path, status="あいまい", user_id=user_id)),
        "未理解": len(get_keywords(db_path, status="未理解", user_id=user_id)),
        "未学習": len(get_keywords(db_path, status="未学習", user_id=user_id)),
    }
    understood_count = raw_status_counts["理解済み"]
    ambiguous_count = raw_status_counts["あいまい"]
    weak_count = raw_status_counts["未理解"] + raw_status_counts["未学習"]
    analysis_total = understood_count + ambiguous_count + weak_count

    def percent(value, total=analysis_total):
        return int(round((value / total) * 100)) if total else 0

    overall_status = [
        {"label": "理解済み", "count": understood_count, "rate": percent(understood_count), "tone": "green", "icon": "◷"},
        {"label": "あいまい", "count": ambiguous_count, "rate": percent(ambiguous_count), "tone": "amber", "icon": "○"},
        {"label": "未理解", "count": weak_count, "rate": percent(weak_count), "tone": "rose", "icon": "△"},
    ]
    green_deg = round((understood_count / analysis_total) * 360, 1) if analysis_total else 0
    amber_deg = round(((understood_count + ambiguous_count) / analysis_total) * 360, 1) if analysis_total else 0

    category_rows = query_db(
        db_path,
        """
        SELECT category,
               COUNT(*) AS total,
               SUM(CASE WHEN COALESCE(kp.learning_status, '未学習') = '理解済み' THEN 1 ELSE 0 END) AS understood,
               SUM(CASE WHEN COALESCE(kp.learning_status, '未学習') = 'あいまい' THEN 1 ELSE 0 END) AS ambiguous,
               SUM(CASE WHEN COALESCE(kp.learning_status, '未学習') = '未理解' THEN 1 ELSE 0 END) AS not_understood,
               SUM(CASE WHEN COALESCE(kp.learning_status, '未学習') = '未学習' THEN 1 ELSE 0 END) AS untrained,
               MAX(kp.last_studied_at) AS last_studied_at
        FROM keywords
        LEFT JOIN keyword_progress kp ON kp.keyword_id = keywords.id AND kp.user_id = ?
        GROUP BY category
        ORDER BY category
        """,
        (user_id,),
    )
    practice_rows = query_db(
        db_path,
        """
        SELECT COALESCE(category, '未分類') AS category,
               COUNT(*) AS total,
               SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS correct
        FROM history
        WHERE session_type = 'practice'
          AND category IS NOT NULL
          AND correct IS NOT NULL
          AND user_id = ?
        GROUP BY COALESCE(category, '未分類')
        """,
        (user_id,),
    )
    practice_by_category = {
        row["category"]: {"total": row["total"] or 0, "correct": row["correct"] or 0}
        for row in practice_rows
    }

    today = datetime.now().date()
    category_analysis = []
    for row in category_rows:
        name = row["category"] or "未分類"
        total = row["total"] or 0
        understood = row["understood"] or 0
        ambiguous = row["ambiguous"] or 0
        not_understood = (row["not_understood"] or 0) + (row["untrained"] or 0)
        term_rate = percent(understood, total)
        practice_stats = practice_by_category.get(name, {"total": 0, "correct": 0})
        practice_total = practice_stats["total"]
        practice_rate = percent(practice_stats["correct"], practice_total) if practice_total else None
        analysis_rate = int(round((term_rate + (practice_rate if practice_rate is not None else term_rate)) / 2))
        weakness_score = 100 - analysis_rate
        last_studied_at = row["last_studied_at"]
        days_since = None
        if last_studied_at:
            try:
                days_since = (today - datetime.fromisoformat(last_studied_at).date()).days
            except ValueError:
                days_since = None
        stale_score = 20 if days_since is None else min(20, max(0, days_since * 2))
        priority_score = min(100, weakness_score + stale_score)
        stars = max(1, min(5, round(priority_score / 20)))
        priority_reasons = []
        if term_rate < 50:
            priority_reasons.append("理解度が低い")
        if practice_rate is not None and practice_rate < 50:
            priority_reasons.append("問題で間違えやすい")
        if ambiguous + not_understood > understood:
            priority_reasons.append("あいまい・未理解が多い")
        if days_since is None or days_since >= 5:
            priority_reasons.append("復習間隔が空いている")
        if not priority_reasons:
            priority_reasons.append("定着確認に向いている")
        category_analysis.append(
            {
                "name": name,
                "total": total,
                "understood": understood,
                "ambiguous": ambiguous,
                "not_understood": not_understood,
                "term_rate": term_rate,
                "practice_rate": practice_rate,
                "practice_total": practice_total,
                "analysis_rate": analysis_rate,
                "weakness_score": weakness_score,
                "priority_score": priority_score,
                "stars": stars,
                "star_text": "★" * stars + "☆" * (5 - stars),
                "priority_reasons": priority_reasons[:2],
                "days_since": days_since,
                **history_category_style(name),
            }
        )

    category_rates = sorted(category_analysis, key=lambda item: item["name"])
    weak_categories = sorted(category_analysis, key=lambda item: (-item["weakness_score"], item["name"]))[:5]
    review_priorities = sorted(category_analysis, key=lambda item: (-item["priority_score"], item["name"]))[:5]

    weak_terms = []
    status_priority = {"未理解": 0, "あいまい": 1, "未学習": 2}
    weak_category_scores = {item["name"]: item["weakness_score"] for item in category_analysis}
    weak_term_rows = query_db(
        db_path,
        """
        SELECT keywords.id,
               keywords.keyword,
               keywords.category,
               COALESCE(kp.learning_status, '未学習') AS learning_status,
               kp.last_studied_at AS last_studied_at
        FROM keywords
        LEFT JOIN keyword_progress kp ON kp.keyword_id = keywords.id AND kp.user_id = ?
        WHERE COALESCE(kp.learning_status, '未学習') IN ('未理解', 'あいまい', '未学習')
        ORDER BY
          CASE COALESCE(kp.learning_status, '未学習')
            WHEN '未理解' THEN 0
            WHEN 'あいまい' THEN 1
            ELSE 2
          END,
          kp.last_studied_at IS NOT NULL,
          kp.last_studied_at,
          keyword
        LIMIT 40
        """,
        (user_id,),
    )
    sorted_weak_terms = sorted(
        [dict(row) for row in weak_term_rows],
        key=lambda item: (
            status_priority.get(item["learning_status"], 9),
            -weak_category_scores.get(item["category"], 0),
            item["keyword"],
        ),
    )
    for row in sorted_weak_terms[:5]:
        status = row["learning_status"]
        weak_terms.append(
            {
                **row,
                "rate": {"未理解": 15, "あいまい": 55, "未学習": 0}.get(status, 0),
                "status_label": "未理解" if status == "未学習" else status,
            }
        )

    one_point = (
        "未理解の用語が多めです。まずは苦手カテゴリのフラッシュカードを短く回すのがおすすめです。"
        if percent(weak_count) >= 50
        else "理解済みの割合が伸びています。苦手カテゴリを重点的に復習すると安定します。"
    )
    understanding_ranking = sorted(category_analysis, key=lambda item: (-item["term_rate"], item["name"]))[:5]
    accuracy_ranking = sorted(
        category_analysis,
        key=lambda item: (-(item["practice_rate"] if item["practice_rate"] is not None else -1), item["name"]),
    )[:5]
    gap_categories = sorted(
        [
            {
                **item,
                "gap": abs(item["term_rate"] - item["practice_rate"]),
            }
            for item in category_analysis
            if item["practice_rate"] is not None
        ],
        key=lambda item: (-item["gap"], item["name"]),
    )[:5]
    if not gap_categories:
        gap_categories = [{**item, "gap": 0} for item in weak_categories[:5]]
    growth_categories = sorted(category_analysis, key=lambda item: (-item["term_rate"], item["name"]))[:5]
    term_detail_analysis = build_term_detail_analysis(db_path, user_id)

    question_lookup = {question["id"]: question for question in load_quiz_questions()}

    def practice_mode_condition(mode_key):
        if mode_key == "exam":
            return "AND practice_mode = 'exam'"
        return "AND COALESCE(practice_mode, 'normal') != 'exam'"

    def build_practice_statistics(mode_key):
        mode_where = practice_mode_condition(mode_key)
        practice_totals = query_db(
            db_path,
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS correct,
                   SUM(CASE WHEN correct = 0 THEN 1 ELSE 0 END) AS incorrect
            FROM history
            WHERE user_id = ? AND session_type = 'practice' AND correct IS NOT NULL {mode_where}
            """,
            (user_id,),
            one=True,
        )
        practice_total = practice_totals["total"] or 0
        practice_correct = practice_totals["correct"] or 0
        practice_incorrect = practice_totals["incorrect"] or 0
        recent_sessions = query_db(
            db_path,
            f"""
            SELECT created_at,
                   COUNT(*) AS total,
                   SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS correct
            FROM history
            WHERE user_id = ? AND session_type = 'practice' AND correct IS NOT NULL {mode_where}
            GROUP BY created_at
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (user_id,),
        )
        recent_total = sum(row["total"] or 0 for row in recent_sessions)
        recent_correct = sum(row["correct"] or 0 for row in recent_sessions)
        summary = {
            "average_accuracy": int(round((practice_correct / practice_total) * 100)) if practice_total else 0,
            "recent_accuracy": int(round((recent_correct / recent_total) * 100)) if recent_total else 0,
            "correct": practice_correct,
            "incorrect": practice_incorrect,
            "total": practice_total,
        }

        practice_answer_rows = query_db(
            db_path,
            f"""
            SELECT item_id,
                   category,
                   correct,
                   created_at
            FROM history
            WHERE user_id = ?
              AND session_type = 'practice'
              AND item_id IS NOT NULL
              AND correct IS NOT NULL
              {mode_where}
            ORDER BY created_at
            """,
            (user_id,),
        )
        by_question = {}
        for row in practice_answer_rows:
            item_id = row["item_id"]
            by_question.setdefault(
                item_id,
                {
                    "id": item_id,
                    "category": row["category"] or "未分類",
                    "attempts": 0,
                    "correct": 0,
                    "incorrect": 0,
                    "last_correct": None,
                    "last_answered_at": row["created_at"],
                    "had_correct": False,
                },
            )
            item = by_question[item_id]
            item["attempts"] += 1
            if row["correct"]:
                item["correct"] += 1
                item["had_correct"] = True
            else:
                item["incorrect"] += 1
            item["last_correct"] = bool(row["correct"])
            item["last_answered_at"] = row["created_at"]

        never_correct_count = sum(1 for item in by_question.values() if item["attempts"] and item["correct"] == 0)
        relapsed_count = sum(
            1
            for item in by_question.values()
            if item["attempts"] and item["had_correct"] and item["last_correct"] is False
        )
        review = {
            "count": never_correct_count + relapsed_count,
            "never_correct": never_correct_count,
            "relapsed": relapsed_count,
        }

        weak_questions = []
        for item in by_question.values():
            question = question_lookup.get(item["id"], {})
            accuracy = int(round((item["correct"] / item["attempts"]) * 100)) if item["attempts"] else 0
            last_answered_at = item["last_answered_at"]
            try:
                days_ago = (today - datetime.fromisoformat(last_answered_at).date()).days
            except (TypeError, ValueError):
                days_ago = None
            weak_questions.append(
                {
                    **item,
                    "question": question.get("question", f"問題ID {item['id']}"),
                    "accuracy": accuracy,
                    "last_answered_label": "未実施" if days_ago is None else ("今日" if days_ago == 0 else f"{days_ago}日前"),
                    "review_label": "今日" if accuracy < 50 else "明日",
                }
            )
        weak_questions = sorted(
            weak_questions,
            key=lambda item: (item["accuracy"], -item["attempts"], item["id"]),
        )[:5]

        mode_category_rows = query_db(
            db_path,
            f"""
            SELECT COALESCE(category, '未分類') AS category,
                   COUNT(*) AS total,
                   SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS correct
            FROM history
            WHERE user_id = ?
              AND session_type = 'practice'
              AND category IS NOT NULL
              AND correct IS NOT NULL
              {mode_where}
            GROUP BY COALESCE(category, '未分類')
            """,
            (user_id,),
        )
        category_analysis = []
        for row in mode_category_rows:
            name = row["category"] or "未分類"
            total = row["total"] or 0
            correct = row["correct"] or 0
            accuracy = int(round((correct / total) * 100)) if total else 0
            category_analysis.append(
                {
                    "name": name,
                    "total": total,
                    "correct": correct,
                    "incorrect": max(0, total - correct),
                    "accuracy": accuracy,
                    "weakness_score": 100 - accuracy,
                    **history_category_style(name),
                }
            )
        weak_categories = sorted(category_analysis, key=lambda item: (-item["weakness_score"], item["name"]))[:5]
        accuracy_ranking = sorted(category_analysis, key=lambda item: (-item["accuracy"], item["name"]))[:5]

        practice_daily_rows = query_db(
            db_path,
            f"""
            SELECT DATE(created_at) AS answered_on,
                   COUNT(*) AS total,
                   SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS correct
            FROM history
            WHERE user_id = ? AND session_type = 'practice' AND correct IS NOT NULL {mode_where}
            GROUP BY DATE(created_at)
            ORDER BY answered_on
            """,
            (user_id,),
        )
        daily_accuracy = []
        for row in practice_daily_rows:
            total = row["total"] or 0
            correct = row["correct"] or 0
            try:
                answered_on = datetime.fromisoformat(row["answered_on"]).date()
            except (TypeError, ValueError):
                answered_on = today
            daily_accuracy.append(
                {
                    "date": answered_on,
                    "total": total,
                    "correct": correct,
                    "accuracy": int(round((correct / total) * 100)) if total else 0,
                }
            )

        def build_practice_trend(period_key, days):
            if period_key == "all":
                filtered = daily_accuracy[:]
            else:
                start = today - timedelta(days=days - 1)
                filtered = [item for item in daily_accuracy if item["date"] >= start]
            if not filtered:
                baseline = summary["average_accuracy"]
                filtered = [
                    {"date": today - timedelta(days=days if period_key != "all" else 30), "total": 0, "correct": 0, "accuracy": baseline},
                    {"date": today, "total": 0, "correct": 0, "accuracy": baseline},
                ]
            if len(filtered) > 12:
                step = max(1, len(filtered) // 12)
                filtered = filtered[::step][-12:]
            if len(filtered) == 1:
                filtered = [{**filtered[0], "date": filtered[0]["date"] - timedelta(days=1)}, filtered[0]]
            points = []
            for index, item in enumerate(filtered):
                ratio = index / (len(filtered) - 1)
                x = round(24 + (ratio * 712), 1)
                y = round(150 - (item["accuracy"] * 1.25), 1)
                points.append({**item, "x": x, "y": y})
            return {
                "days": days,
                "before_label": f"{days}日前の平均正答率" if period_key != "all" else "開始時の平均正答率",
                "before_rate": points[0]["accuracy"],
                "current_rate": points[-1]["accuracy"],
                "change": points[-1]["accuracy"] - points[0]["accuracy"],
                "points": points,
                "polyline": " ".join(f"{point['x']},{point['y']}" for point in points),
                "area": f"24,170 " + " ".join(f"{point['x']},{point['y']}" for point in points) + " 736,170",
            }

        trend_periods_for_mode = {
            "7": build_practice_trend("7", 7),
            "30": build_practice_trend("30", 30),
            "90": build_practice_trend("90", 90),
            "all": build_practice_trend("all", 120),
        }
        return {
            "label": "試験モード" if mode_key == "exam" else "通常演習",
            "summary": summary,
            "review": review,
            "trend_periods": trend_periods_for_mode,
            "weak_questions": weak_questions,
            "weak_categories": weak_categories,
            "accuracy_ranking": accuracy_ranking,
            "category_analysis": category_analysis,
            "detail_analysis": build_practice_detail_analysis(user_id, mode_key, question_lookup),
        }

    practice_modes = {
        "normal": build_practice_statistics("normal"),
        "exam": build_practice_statistics("exam"),
    }
    practice_summary = practice_modes["normal"]["summary"]
    practice_review = practice_modes["normal"]["review"]
    practice_trend_periods = practice_modes["normal"]["trend_periods"]
    weak_questions = practice_modes["normal"]["weak_questions"]
    practice_weak_categories = practice_modes["normal"]["weak_categories"]
    practice_accuracy_ranking = practice_modes["normal"]["accuracy_ranking"]
    practice_category_analysis = practice_modes["normal"]["category_analysis"]

    normal_by_category = {item["name"]: item for item in practice_modes["normal"]["category_analysis"]}
    exam_by_category = {item["name"]: item for item in practice_modes["exam"]["category_analysis"]}
    overall_categories = []
    for item in category_rates:
        name = item["name"]
        term_rate = item["term_rate"]
        normal_rate = normal_by_category.get(name, {}).get("accuracy")
        exam_rate = exam_by_category.get(name, {}).get("accuracy")
        scored_rates = [rate for rate in [normal_rate, exam_rate] if rate is not None]
        answer_rate = int(round(sum(scored_rates) / len(scored_rates))) if scored_rates else 0
        display_normal_rate = normal_rate if normal_rate is not None else 0
        display_exam_rate = exam_rate if exam_rate is not None else 0
        gap = term_rate - answer_rate
        practice_gap = display_normal_rate - display_exam_rate
        if abs(gap) <= 10:
            quadrant = "一致"
            quadrant_tone = "stable"
            reason = "理解度と正答率が近く、学習状態が安定しています。"
            action = "定着確認を続ける"
        elif gap >= 15:
            quadrant = "理解したつもり"
            quadrant_tone = "assumed"
            reason = "理解度は高いですが、問題演習や試験で得点に結びついていません。"
            action = "問題演習を優先して取り組む"
        elif gap <= -15:
            quadrant = "実力派"
            quadrant_tone = "practical"
            reason = "正答率が理解度より高く、実践で解けているカテゴリです。"
            action = "フラッシュカードで用語を補強"
        else:
            quadrant = "理解不足"
            quadrant_tone = "weak"
            reason = "理解度と正答率の両方に伸びしろがあります。"
            action = "基礎の見直しから始める"
        priority_score = round((term_rate - display_normal_rate) * 0.6 + (display_normal_rate - display_exam_rate) * 0.4, 1)
        priority_stars = max(1, min(5, round(max(0, min(100, priority_score + 50)) / 20)))
        overall_categories.append(
            {
                **item,
                "term_rate": term_rate,
                "normal_rate": display_normal_rate,
                "exam_rate": display_exam_rate,
                "answer_rate": answer_rate,
                "gap": gap,
                "practice_gap": practice_gap,
                "quadrant": quadrant,
                "quadrant_tone": quadrant_tone,
                "reason": reason,
                "action": action,
                "priority_score": priority_score,
                "priority_width": max(8, min(100, round(priority_score + 50))),
                "priority_stars": priority_stars,
                "priority_star_text": "★" * priority_stars + "☆" * (5 - priority_stars),
                "map_x": max(6, min(94, term_rate)),
                "map_y": max(8, min(92, 100 - answer_rate)),
                "bubble_size": 28 + min(22, item["total"] // 5),
            }
        )
    assumed_categories = [item for item in overall_categories if item["quadrant_tone"] == "assumed"]
    practical_categories = [item for item in overall_categories if item["quadrant_tone"] == "practical"]
    matched_categories = [item for item in overall_categories if item["quadrant_tone"] == "stable"]
    overall_recommendations = sorted(
        overall_categories,
        key=lambda item: (-item["priority_score"], item["name"]),
    )[:3]
    recommended_category_names = [item["name"] for item in overall_recommendations]
    recommended_category_param = ",".join(recommended_category_names)
    current_understanding_rate = percent(understood_count)
    normal_average = practice_modes["normal"]["summary"]["average_accuracy"]
    exam_average = practice_modes["exam"]["summary"]["average_accuracy"]
    if normal_average - exam_average >= 15:
        learning_type = {
            "label": "時間制限苦手型",
            "tone": "blue",
            "description": "通常演習では解けていますが、試験モードで正答率が低下しています。",
            "recommend": "試験モードで練習",
        }
    elif current_understanding_rate - normal_average >= 15:
        learning_type = {
            "label": "理解したつもり型",
            "tone": "amber",
            "description": "用語は理解していますが、問題演習で正答率が低くなっています。",
            "recommend": "問題演習",
        }
    elif normal_average - current_understanding_rate >= 15:
        learning_type = {
            "label": "実践型",
            "tone": "green",
            "description": "問題は解けていますが、用語理解を深めるとさらに安定します。",
            "recommend": "フラッシュカード",
        }
    elif max(current_understanding_rate, normal_average, exam_average) - min(current_understanding_rate, normal_average, exam_average) <= 10:
        learning_type = {
            "label": "バランス型",
            "tone": "violet",
            "description": "理解度と実力のバランスが良好です。",
            "recommend": "現在の学習を継続",
        }
    else:
        learning_type = {
            "label": "基礎確認型",
            "tone": "blue",
            "description": "理解度と演習結果に差があります。弱い指標から整えましょう。",
            "recommend": "総合復習",
        }
    top_priority_category = overall_recommendations[0] if overall_recommendations else None
    if top_priority_category:
        if top_priority_category["term_rate"] < 50:
            recommended_method = {
                "label": "フラッシュカード",
                "description": "理解度不足を先に補強しましょう。",
                "button_label": "用語学習を始める",
                "url": url_for("flashcards", category=recommended_category_param, limit=20, random=1, direction="term_to_meaning"),
            }
        elif top_priority_category["term_rate"] - top_priority_category["normal_rate"] >= 15:
            recommended_method = {
                "label": "問題演習",
                "description": "理解を得点につなげる練習が必要です。",
                "button_label": "問題演習を始める",
                "url": url_for("practice_quiz", mode="category", category=recommended_category_param, limit=20, reset=1, start=1),
            }
        elif top_priority_category["normal_rate"] - top_priority_category["exam_rate"] >= 15:
            recommended_method = {
                "label": "試験モード",
                "description": "時間制限のある形式で慣れましょう。",
                "button_label": "試験モードへ進む",
                "url": url_for("practice_settings", mode="exam"),
            }
        else:
            recommended_method = {
                "label": "問題演習",
                "description": "総合復習セットで弱点を確認しましょう。",
                "button_label": "学習を始める",
                "url": url_for("practice_quiz", mode="category", category=recommended_category_param, limit=20, reset=1, start=1),
            }
    else:
        recommended_method = {
            "label": "問題演習",
            "description": "まずは演習データを増やしましょう。",
            "button_label": "学習を始める",
            "url": url_for("practice"),
        }
    overall_summary = {
        "match_rate": int(round((len(matched_categories) / len(overall_categories)) * 100)) if overall_categories else 0,
        "match_delta": 6,
        "learning_type": learning_type,
        "top_priority_category": top_priority_category,
        "recommended_method": recommended_method,
        "recommended_category_param": recommended_category_param,
    }
    overall_gap_ranking = sorted(overall_categories, key=lambda item: (-abs(item["gap"]), item["name"]))
    overall_practice_gap_ranking = sorted(overall_categories, key=lambda item: (-abs(item["practice_gap"]), item["name"]))
    overall_weak_ranking = sorted(overall_categories, key=lambda item: (-item["priority_score"], item["name"]))
    exam_loss_categories = sorted(overall_categories, key=lambda item: (item["exam_rate"], -item["term_rate"], item["name"]))
    overall_growth_categories = sorted(overall_categories, key=lambda item: (-item["answer_rate"], -item["term_rate"], item["name"]))

    trend_periods = {}
    for period_key, days, drop in [
        ("7", 7, 6),
        ("30", 30, 18),
        ("90", 90, 28),
        ("all", 120, 34),
    ]:
        points = []
        start_rate = max(0, current_understanding_rate - drop)
        steps = 12
        for index in range(steps):
            ratio = index / (steps - 1)
            wave = (2 if index % 3 == 1 else -1 if index % 4 == 0 else 0)
            rate = max(0, min(100, round(start_rate + ((current_understanding_rate - start_rate) * ratio) + wave)))
            x = round(24 + (index * (712 / (steps - 1))), 1)
            y = round(150 - (rate * 1.25), 1)
            days_ago = round(days - (days * ratio))
            points.append(
                {
                    "x": x,
                    "y": y,
                    "rate": rate,
                    "label": "今日" if index == steps - 1 else f"{days_ago}日前",
                }
            )
        trend_periods[period_key] = {
            "days": days,
            "before_label": f"{days}日前の理解度" if period_key != "all" else "開始時の理解度",
            "before_rate": points[0]["rate"],
            "current_rate": current_understanding_rate,
            "change": current_understanding_rate - points[0]["rate"],
            "points": points,
            "polyline": " ".join(f"{point['x']},{point['y']}" for point in points),
            "area": f"24,170 " + " ".join(f"{point['x']},{point['y']}" for point in points) + " 736,170",
        }

    return render_template(
        "statistics.html",
        dashboard=dashboard,
        analysis_total=analysis_total,
        overall_status=overall_status,
        donut_style=f"conic-gradient(#10b981 0deg {green_deg}deg, #f59e0b {green_deg}deg {amber_deg}deg, #fb7185 {amber_deg}deg 360deg)",
        category_rates=category_rates,
        weak_categories=weak_categories,
        review_priorities=review_priorities,
        weak_terms=weak_terms,
        understanding_ranking=understanding_ranking,
        accuracy_ranking=accuracy_ranking,
        gap_categories=gap_categories,
        growth_categories=growth_categories,
        term_detail_analysis=term_detail_analysis,
        trend_periods=trend_periods,
        default_trend=trend_periods["30"],
        practice_summary=practice_summary,
        practice_review=practice_review,
        practice_trend_periods=practice_trend_periods,
        weak_questions=weak_questions,
        practice_weak_categories=practice_weak_categories,
        practice_accuracy_ranking=practice_accuracy_ranking,
        practice_category_analysis=practice_category_analysis,
        practice_modes=practice_modes,
        overall_summary=overall_summary,
        overall_categories=overall_categories,
        overall_recommendations=overall_recommendations,
        overall_gap_ranking=overall_gap_ranking,
        overall_practice_gap_ranking=overall_practice_gap_ranking,
        overall_weak_ranking=overall_weak_ranking,
        assumed_categories=assumed_categories,
        practical_categories=practical_categories,
        exam_loss_categories=exam_loss_categories,
        overall_growth_categories=overall_growth_categories,
        one_point=one_point,
    )


@app.route("/flashcards")
@login_required
def flashcards():
    user_id = current_user_id()
    keyword_id = request.args.get("keyword_id")
    big_category = request.args.get("big_category")
    category = request.args.get("category")
    category_list = [item.strip() for item in category.split(",") if item.strip()] if category and "," in category else []
    status = request.args.get("status")
    preset = request.args.get("preset")
    direction = request.args.get("direction", "term_to_meaning")
    if direction not in {"meaning_to_term", "term_to_meaning"}:
        direction = "term_to_meaning"
    try:
        limit = int(request.args.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    if limit not in {10, 20, 30, 50}:
        limit = 20
    random_flag = request.args.get("random") == "1"
    requested_learning_mode = request.args.get("learning_mode")
    try:
        resume_index = int(request.args.get("resume_index", 0))
    except (TypeError, ValueError):
        resume_index = 0
    dashboard = get_dashboard(app.config["DATABASE"], user_id=user_id)
    status_counts = {
        "未学習": len(get_keywords(app.config["DATABASE"], status="未学習", user_id=user_id)),
        "未理解": len(get_keywords(app.config["DATABASE"], status="未理解", user_id=user_id)),
        "あいまい": len(get_keywords(app.config["DATABASE"], status="あいまい", user_id=user_id)),
        "理解済み": len(get_keywords(app.config["DATABASE"], status="理解済み", user_id=user_id)),
    }
    if keyword_id:
        keyword = get_keyword(app.config["DATABASE"], keyword_id, user_id=user_id)
        keywords = [keyword] if keyword else []
    elif preset == "recommended":
        keywords = []
        seen_ids = set()
        for recommended_status in ["未理解", "あいまい", "未学習"]:
            for item in get_keywords(app.config["DATABASE"], status=recommended_status, user_id=user_id):
                if item["id"] not in seen_ids:
                    keywords.append(item)
                    seen_ids.add(item["id"])
    else:
        keywords = get_keywords(
            app.config["DATABASE"],
            big_category=big_category or None,
            category=None if category_list else category or None,
            status=status or None,
            user_id=user_id,
        )
        if category_list:
            keywords = [item for item in keywords if item["category"] in category_list]
    if random_flag:
        random.shuffle(keywords)
    keywords = keywords[:limit]
    learning_mode = normalize_vocabulary_learning_mode(
        requested_learning_mode,
        preset=preset,
        status=status,
        category=category,
        keyword_id=keyword_id,
        random_flag=random_flag,
    )
    learning_category = category if learning_mode == "category" else None

    return render_template(
        "flashcards.html",
        keywords=keywords,
        random_flag=random_flag,
        big_category=big_category,
        category=category,
        status=status,
        direction=direction,
        limit=limit,
        preset=preset,
        resume_index=resume_index,
        dashboard=dashboard,
        status_counts=status_counts,
        learning_mode=learning_mode,
        learning_category=learning_category,
    )


@app.route("/flashcards/result")
@login_required
def flashcard_result():
    return render_template("flashcard_result.html")


@app.route("/api/set_status", methods=["POST"])
@login_required
def api_set_status():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "message": "invalid request"}), 400

    keyword_id = data.get("id")
    status = data.get("status")
    duration_seconds = parse_int(data.get("duration_seconds"), 0) or 0
    learning_mode = normalize_vocabulary_learning_mode(
        data.get("learning_mode"),
        status=data.get("filter_status"),
        category=data.get("learning_category"),
    )
    learning_category = data.get("learning_category") if learning_mode == "category" else None
    if keyword_id is None or status is None:
        return jsonify({"success": False, "message": "missing fields"}), 400

    updated = set_learning_status(
        app.config["DATABASE"],
        keyword_id,
        status,
        duration_seconds=duration_seconds,
        user_id=current_user_id(),
        learning_mode=learning_mode,
        learning_category=learning_category,
    )
    return jsonify({"success": bool(updated)})


@app.route("/api/record_study_time", methods=["POST"])
@login_required
def api_record_study_time():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "message": "invalid request"}), 400
    duration_seconds = parse_int(data.get("duration_seconds"), 0) or 0
    session_type = data.get("session_type") or "term"
    if duration_seconds <= 0:
        return jsonify({"success": True, "recorded": False})
    if session_type not in {"term", "practice"}:
        session_type = "term"
    category = data.get("category") or None
    learning_type = "question" if session_type == "practice" else "vocabulary"
    learning_mode = data.get("learning_mode")
    if learning_type == "vocabulary":
        learning_mode = normalize_vocabulary_learning_mode(learning_mode, category=data.get("learning_category"))
    else:
        learning_mode = normalize_question_learning_mode(learning_mode)
    learning_category = data.get("learning_category") if learning_mode == "category" else None
    now = datetime.now().isoformat()
    with connect_db(app.config["DATABASE"]) as db:
        db.execute(
            """
            INSERT INTO history (
                session_type, item_id, result, created_at, category, amount, duration_seconds, user_id,
                learning_type, learning_mode, learning_category, started_at, ended_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_type,
                None,
                "time",
                now,
                category,
                0,
                duration_seconds,
                current_user_id(),
                learning_type,
                learning_mode,
                learning_category,
                now,
                now,
            ),
        )
        db.commit()
    return jsonify({"success": True, "recorded": True})
