import os
from datetime import date, datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for
import sqlite3
import openai
from dotenv import load_dotenv

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")

app = Flask(__name__)
DATABASE = "meals.db"


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE,
                meal TEXT,
                rating TEXT,
                generated_by_ai INTEGER
            )
            """
        )
    db.close()


init_db()


def week_dates(reference: date | None = None) -> list[date]:
    """Return list of dates for current week starting Monday."""
    today = reference or date.today()
    start = today - timedelta(days=today.weekday())
    return [start + timedelta(days=i) for i in range(7)]


@app.route("/")
def index():
    db = get_db()
    days = week_dates()
    start, end = days[0].isoformat(), days[-1].isoformat()
    rows = db.execute(
        "SELECT * FROM meals WHERE date BETWEEN ? AND ?",
        (start, end),
    ).fetchall()
    meal_map = {row["date"]: row for row in rows}
    week = []
    for d in days:
        key = d.isoformat()
        item = meal_map.get(key)
        week.append(
            {
                "date": key,
                "meal": item["meal"] if item else "",
                "rating": item["rating"] if item else None,
            }
        )
    history = db.execute(
        "SELECT * FROM meals WHERE date < ? ORDER BY date DESC LIMIT 30",
        (start,),
    ).fetchall()
    return render_template("index.html", week=week, history=history)


def call_openai(prompt: str) -> str:
    if not openai.api_key:
        return "OpenAI key missing"
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}],
    )
    return response["choices"][0]["message"]["content"].strip()


def parse_week_response(text: str) -> list[str]:
    meals = []
    for line in text.splitlines():
        if ":" in line:
            meal = line.split(":", 1)[1].strip()
            if meal:
                meals.append(meal)
    return meals[:7]


def parse_single_response(text: str) -> str:
    return text.strip().splitlines()[0]


@app.route("/generate", methods=["POST"])
def generate():
    db = get_db()
    since = (date.today() - timedelta(days=30)).isoformat()
    rows = db.execute(
        "SELECT date, meal, rating FROM meals WHERE date >= ? ORDER BY date",
        (since,),
    ).fetchall()
    history_lines = "\n".join(
        f"{r['date']} : {r['meal']} ({r['rating']})" for r in rows if r["meal"]
    )
    prompt = (
        "Voici ce qu’on a mangé récemment:\n\n"
        f"{history_lines}\n\nPropose 7 repas pour cette semaine. Varie les repas, "
        "évite ceux marqués \"moins\", favorise ceux appréciés, ne répète pas.\n"
        "Donne simplement :\nLundi : ...\nMardi : ...\n..."
    )
    response = call_openai(prompt)
    suggestions = parse_week_response(response)
    week = week_dates()
    with db:
        for d, meal in zip(week, suggestions):
            db.execute(
                """
                INSERT INTO meals(date, meal, rating, generated_by_ai)
                VALUES(?, ?, NULL, 1)
                ON CONFLICT(date) DO UPDATE SET
                    meal=excluded.meal,
                    rating=NULL,
                    generated_by_ai=1
                """,
                (d.isoformat(), meal),
            )
    return redirect(url_for("index"))


@app.route("/update_meal", methods=["POST"])
def update_meal():
    db = get_db()
    week = week_dates()
    with db:
        for d in week:
            meal = request.form.get(d.isoformat(), "").strip()
            if meal:
                db.execute(
                    """
                    INSERT INTO meals(date, meal, generated_by_ai)
                    VALUES(?, ?, 0)
                    ON CONFLICT(date) DO UPDATE SET meal=excluded.meal, generated_by_ai=0
                    """,
                    (d.isoformat(), meal),
                )
    return redirect(url_for("index"))


def regenerate_for_date(db: sqlite3.Connection, meal_date: str):
    rejected = db.execute(
        "SELECT meal FROM meals WHERE date=?", (meal_date,)
    ).fetchone()
    rejected_meal = rejected["meal"] if rejected else ""
    since = (date.today() - timedelta(days=30)).isoformat()
    liked = db.execute(
        "SELECT meal FROM meals WHERE rating='plus' AND date>=?", (since,)
    ).fetchall()
    week = week_dates()
    week_rows = db.execute(
        "SELECT date, meal FROM meals WHERE date BETWEEN ? AND ?",
        (week[0].isoformat(), week[-1].isoformat()),
    ).fetchall()
    week_meals = "\n".join(r["meal"] for r in week_rows if r["date"] != meal_date)
    liked_lines = "\n".join(r["meal"] for r in liked)
    prompt = (
        f"Le repas suivant a été rejeté : \"{rejected_meal}\".\n\n"
        "Voici des repas appréciés récemment:\n\n"
        f"{liked_lines}\n\nRepas déjà prévus cette semaine:\n{week_meals}\n\n"
        "Propose une nouvelle idée pour ce jour, différente du repas rejeté, en évitant les plats non appréciés et ceux déjà proposés cette semaine."
    )
    response = call_openai(prompt)
    new_meal = parse_single_response(response)
    db.execute(
        "UPDATE meals SET meal=?, rating=NULL, generated_by_ai=1 WHERE date=?",
        (new_meal, meal_date),
    )


@app.route("/regenerate_meal/<meal_date>", methods=["POST"])
def regenerate_meal_route(meal_date):
    db = get_db()
    with db:
        regenerate_for_date(db, meal_date)
    return redirect(url_for("index"))


@app.route("/rate_meal", methods=["POST"])
def rate_meal():
    meal_date = request.form.get("date")
    rating = request.form.get("rating")
    db = get_db()
    with db:
        db.execute("UPDATE meals SET rating=? WHERE date=?", (rating, meal_date))
        db.commit()
        meal_day = datetime.fromisoformat(meal_date).date()
        if rating == "moins" and meal_day > date.today():
            regenerate_for_date(db, meal_date)
            db.commit()
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True)
