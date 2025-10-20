"""OpenAI integration helpers."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from openai import OpenAI

try:  # pragma: no cover - support running without package context
    from .config import get_settings
except ImportError:  # pragma: no cover
    from config import get_settings


@dataclass(slots=True)
class MealAnalysis:
    calories: float
    protein: float
    fat: float
    carbs: float
    notes: str
    items: list[dict[str, Any]]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MealAnalysis":
        raw_notes = payload.get("notes", "")
        if isinstance(raw_notes, dict):
            notes_parts = [
                str(raw_notes.get("description", "")).strip(),
                str(raw_notes.get("conclusions", "")).strip(),
            ]
            notes = "\n".join(part for part in notes_parts if part)
        elif isinstance(raw_notes, list):
            notes = "\n".join(str(part).strip() for part in raw_notes if part)
        else:
            notes = str(raw_notes).strip()

        raw_items = payload.get("items", [])
        if isinstance(raw_items, dict):
            items: list[dict[str, Any]] = [raw_items]
        elif isinstance(raw_items, list):
            items = [item for item in raw_items if isinstance(item, dict)]
        else:
            items = []

        return cls(
            calories=float(payload.get("calories", 0) or 0),
            protein=float(payload.get("protein", 0) or 0),
            fat=float(payload.get("fat", 0) or 0),
            carbs=float(payload.get("carbs", 0) or 0),
            notes=notes,
            items=items,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "calories": self.calories,
            "protein": self.protein,
            "fat": self.fat,
            "carbs": self.carbs,
            "notes": self.notes,
            "items": self.items,
        }


PROMPT_TEMPLATE = """
Ты — внимательный нутрициолог, который ведёт дружелюбный дневник питания клиента.
Проанализируй описание блюда и оцени общую калорийность, белки, жиры и углеводы (в граммах).
Ответь строго в формате JSON с ключами: calories, protein, fat, carbs, notes, items (массив объектов с полями name, calories, protein, fat, carbs).
В поле notes сначала коротко опиши блюдо или набор продуктов, затем добавь рекомендации или важные уточнения.
Если каких-то данных нет, сделай аккуратную оценку и расскажи об этом в notes.
Описание пользователя: {description}
"""


SUMMARY_PROMPT = """
Ты — вдохновляющий нутрициолог и коуч здорового образа жизни.
У клиента дневная цель {target_calories:.0f} ккал и ориентиры по макроэлементам:
- Белки: {target_protein:.0f} г
- Жиры: {target_fat:.0f} г
- Углеводы: {target_carbs:.0f} г

Фактически за день получено:
- Калории: {actual_calories:.0f} ккал
- Белки: {actual_protein:.0f} г
- Жиры: {actual_fat:.0f} г
- Углеводы: {actual_carbs:.0f} г

Сформулируй короткий анализ (до 120 слов) и конкретные рекомендации на завтра.
Верни JSON с ключами summary и recommendations. Пиши только по-русски.
"""


def _client() -> OpenAI:
    settings = get_settings()
    return OpenAI(api_key=settings.openai_api_key)


def _chat_request(messages: list[dict[str, str]], response_format: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    client = _client()
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        response_format=response_format or {"type": "json_object"},
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("LLM returned empty content")
    return json.loads(content)


def _image_to_base64(file_content: bytes) -> str:
    return base64.b64encode(file_content).decode("utf-8")


def analyze_meal_from_text(description: str) -> MealAnalysis:
    payload = _chat_request(
        [
            {
                "role": "system",
                "content": (
                    "Ты — внимательный русскоязычный нутрициолог. Отвечай только строгим JSON без комментариев."
                ),
            },
            {"role": "user", "content": PROMPT_TEMPLATE.format(description=description)},
        ]
    )
    return MealAnalysis.from_dict(payload)


def analyze_meal_from_image(description: str, image_bytes: bytes) -> MealAnalysis:
    settings = get_settings()
    client = OpenAI(api_key=settings.openai_api_key)
    image_b64 = _image_to_base64(image_bytes)
    prompt_text = (
        (description.strip() + "\n\n" if description else "")
        + "Сформируй строгий JSON с полями calories, protein, fat, carbs, notes, items. В notes сначала опиши, что изображено на фото, затем добавь выводы."
    )
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты — русскоязычный нутрициолог, который анализирует фото блюд. Всегда отвечай строгим JSON."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt_text,
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}",
                        },
                    },
                ],
            },
        ],
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("LLM returned empty content")
    payload = json.loads(content)
    return MealAnalysis.from_dict(payload)


def request_day_summary(
    target: dict[str, float], actual: dict[str, float]
) -> dict[str, Any]:
    payload = _chat_request(
        [
            {
                "role": "system",
                "content": (
                    "Ты — поддерживающий русскоязычный нутрициолог. Говори только по-русски и возвращай строгий JSON."
                ),
            },
            {
                "role": "user",
                "content": SUMMARY_PROMPT.format(
                    target_calories=target["calories"],
                    target_protein=target["protein"],
                    target_fat=target["fat"],
                    target_carbs=target["carbs"],
                    actual_calories=actual["calories"],
                    actual_protein=actual["protein"],
                    actual_fat=actual["fat"],
                    actual_carbs=actual["carbs"],
                ),
            },
        ]
    )
    return payload
