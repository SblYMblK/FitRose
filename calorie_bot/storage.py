"""SQLite storage layer."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .calculations import ActivityLevel, Goal, Sex, UserMetrics


CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    age INTEGER NOT NULL,
    sex TEXT NOT NULL,
    height REAL NOT NULL,
    weight REAL NOT NULL,
    activity TEXT NOT NULL,
    goal TEXT NOT NULL,
    bmr REAL NOT NULL,
    tdee REAL NOT NULL,
    calorie_target REAL NOT NULL,
    protein_target REAL NOT NULL,
    fat_target REAL NOT NULL,
    carb_target REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_DAY_LOGS = """
CREATE TABLE IF NOT EXISTS day_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    total_calories REAL NOT NULL DEFAULT 0,
    total_protein REAL NOT NULL DEFAULT 0,
    total_fat REAL NOT NULL DEFAULT 0,
    total_carbs REAL NOT NULL DEFAULT 0,
    UNIQUE(telegram_id, day),
    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
);
"""

CREATE_MEALS = """
CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_log_id INTEGER NOT NULL,
    meal_type TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    user_input TEXT,
    llm_payload TEXT,
    corrected_payload TEXT,
    calories REAL DEFAULT 0,
    protein REAL DEFAULT 0,
    fat REAL DEFAULT 0,
    carbs REAL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(day_log_id) REFERENCES day_logs(id)
);
"""


@dataclass(slots=True)
class User:
    telegram_id: int
    age: int
    sex: Sex
    height: float
    weight: float
    activity: ActivityLevel
    goal: Goal
    metrics: UserMetrics


class Storage:
    """Simple SQLite backed repository."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("\n".join([CREATE_USERS, CREATE_DAY_LOGS, CREATE_MEALS]))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get_user(self, telegram_id: int) -> Optional[User]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
            if not row:
                return None
            metrics = UserMetrics(
                bmr=row["bmr"],
                tdee=row["tdee"],
                calorie_target=row["calorie_target"],
                protein_target_g=row["protein_target"],
                fat_target_g=row["fat_target"],
                carb_target_g=row["carb_target"],
            )
            return User(
                telegram_id=row["telegram_id"],
                age=row["age"],
                sex=Sex(row["sex"]),
                height=row["height"],
                weight=row["weight"],
                activity=ActivityLevel(row["activity"]),
                goal=Goal(row["goal"]),
                metrics=metrics,
            )

    def upsert_user(self, user: User) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    telegram_id, age, sex, height, weight, activity, goal,
                    bmr, tdee, calorie_target, protein_target, fat_target, carb_target
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    age=excluded.age,
                    sex=excluded.sex,
                    height=excluded.height,
                    weight=excluded.weight,
                    activity=excluded.activity,
                    goal=excluded.goal,
                    bmr=excluded.bmr,
                    tdee=excluded.tdee,
                    calorie_target=excluded.calorie_target,
                    protein_target=excluded.protein_target,
                    fat_target=excluded.fat_target,
                    carb_target=excluded.carb_target
                """,
                (
                    user.telegram_id,
                    user.age,
                    user.sex.value,
                    user.height,
                    user.weight,
                    user.activity.value,
                    user.goal.value,
                    user.metrics.bmr,
                    user.metrics.tdee,
                    user.metrics.calorie_target,
                    user.metrics.protein_target_g,
                    user.metrics.fat_target_g,
                    user.metrics.carb_target_g,
                ),
            )

    def ensure_day_log(self, telegram_id: int, log_date: date) -> int:
        day_str = log_date.isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM day_logs WHERE telegram_id=? AND day=?",
                (telegram_id, day_str),
            ).fetchone()
            if row:
                return int(row["id"])
            cursor = conn.execute(
                "INSERT INTO day_logs (telegram_id, day) VALUES (?, ?)",
                (telegram_id, day_str),
            )
            return int(cursor.lastrowid)

    def add_meal_entry(
        self,
        day_log_id: int,
        meal_type: str,
        entry_type: str,
        user_input: Optional[str],
        llm_payload: Optional[dict[str, Any]],
        corrected_payload: Optional[dict[str, Any]],
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO meals (
                    day_log_id, meal_type, entry_type, user_input, llm_payload, corrected_payload,
                    calories, protein, fat, carbs
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    day_log_id,
                    meal_type,
                    entry_type,
                    user_input,
                    json.dumps(llm_payload) if llm_payload else None,
                    json.dumps(corrected_payload) if corrected_payload else None,
                    corrected_payload.get("calories") if corrected_payload else (llm_payload or {}).get("calories", 0),
                    corrected_payload.get("protein") if corrected_payload else (llm_payload or {}).get("protein", 0),
                    corrected_payload.get("fat") if corrected_payload else (llm_payload or {}).get("fat", 0),
                    corrected_payload.get("carbs") if corrected_payload else (llm_payload or {}).get("carbs", 0),
                ),
            )
            meal_id = int(cursor.lastrowid)

            conn.execute(
                """
                UPDATE day_logs
                SET total_calories = (
                    SELECT COALESCE(SUM(calories), 0) FROM meals WHERE day_log_id=?
                ),
                    total_protein = (
                    SELECT COALESCE(SUM(protein), 0) FROM meals WHERE day_log_id=?
                ),
                    total_fat = (
                    SELECT COALESCE(SUM(fat), 0) FROM meals WHERE day_log_id=?
                ),
                    total_carbs = (
                    SELECT COALESCE(SUM(carbs), 0) FROM meals WHERE day_log_id=?
                )
                WHERE id=?
                """,
                (day_log_id, day_log_id, day_log_id, day_log_id, day_log_id),
            )
            return meal_id

    def update_meal_corrections(self, meal_id: int, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE meals
                SET corrected_payload = ?,
                    calories = ?,
                    protein = ?,
                    fat = ?,
                    carbs = ?
                WHERE id=?
                """,
                (
                    json.dumps(payload),
                    payload.get("calories", 0),
                    payload.get("protein", 0),
                    payload.get("fat", 0),
                    payload.get("carbs", 0),
                    meal_id,
                ),
            )

    def get_day_summary(self, telegram_id: int, log_date: date) -> Optional[dict[str, Any]]:
        day_str = log_date.isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM day_logs WHERE telegram_id=? AND day=?",
                (telegram_id, day_str),
            ).fetchone()
            if not row:
                return None
            meals = conn.execute(
                "SELECT * FROM meals WHERE day_log_id=? ORDER BY created_at",
                (row["id"],),
            ).fetchall()
            return {
                "day": day_str,
                "totals": {
                    "calories": row["total_calories"],
                    "protein": row["total_protein"],
                    "fat": row["total_fat"],
                    "carbs": row["total_carbs"],
                },
                "meals": [dict(m) for m in meals],
            }

    def iter_period_totals(self, telegram_id: int, start: date, end: date) -> Iterable[dict[str, Any]]:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT day, total_calories, total_protein, total_fat, total_carbs
                FROM day_logs
                WHERE telegram_id=? AND day BETWEEN ? AND ?
                ORDER BY day
                """,
                (telegram_id, start.isoformat(), end.isoformat()),
            )
            for row in cursor.fetchall():
                yield dict(row)

    def list_meals(self, day_log_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM meals WHERE day_log_id=? ORDER BY created_at",
                (day_log_id,),
            ).fetchall()
            return [dict(r) for r in rows]
