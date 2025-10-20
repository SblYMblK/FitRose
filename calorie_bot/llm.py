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
        return cls(
            calories=float(payload.get("calories", 0)),
            protein=float(payload.get("protein", 0)),
            fat=float(payload.get("fat", 0)),
            carbs=float(payload.get("carbs", 0)),
            notes=payload.get("notes", ""),
            items=list(payload.get("items", [])),
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
You are a nutrition expert helping people log their calorie intake.
Based on the user's description, estimate total calories, protein, fat and carbs (in grams).
Respond with valid JSON containing keys: calories, protein, fat, carbs, notes, items (list of ingredient dicts with name, calories, protein, fat, carbs).
If some values are unknown, provide best estimate and mention assumptions in notes.
Description: {description}
"""


SUMMARY_PROMPT = """
You are a nutrition coach.
The user has a daily calorie target of {target_calories:.0f} kcal and macro targets:
- Protein: {target_protein:.0f} g
- Fat: {target_fat:.0f} g
- Carbs: {target_carbs:.0f} g

The user actually consumed:
- Calories: {actual_calories:.0f} kcal
- Protein: {actual_protein:.0f} g
- Fat: {actual_fat:.0f} g
- Carbs: {actual_carbs:.0f} g

Provide a short analysis (<= 120 words) and specific recommendations for tomorrow.
Return a short JSON object with keys: summary, recommendations.
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
            {"role": "system", "content": "You are a helpful nutrition assistant."},
            {"role": "user", "content": PROMPT_TEMPLATE.format(description=description)},
        ]
    )
    return MealAnalysis.from_dict(payload)


def analyze_meal_from_image(description: str, image_bytes: bytes) -> MealAnalysis:
    settings = get_settings()
    client = OpenAI(api_key=settings.openai_api_key)
    image_b64 = _image_to_base64(image_bytes)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "You are a helpful nutrition assistant interpreting meal photos.",
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": description or "Please estimate calories and macros for this meal.",
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
            {"role": "system", "content": "You are a nutrition coach."},
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
