"""Calorie and macro nutrient calculations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Sex(str, Enum):
    MALE = "male"
    FEMALE = "female"


class Goal(str, Enum):
    LOSE = "lose"
    MAINTAIN = "maintain"
    GAIN = "gain"


class ActivityLevel(str, Enum):
    SEDENTARY = "sedentary"
    LIGHT = "light"
    MODERATE = "moderate"
    HIGH = "high"
    VERY_HIGH = "very_high"


ACTIVITY_FACTORS = {
    ActivityLevel.SEDENTARY: 1.2,
    ActivityLevel.LIGHT: 1.375,
    ActivityLevel.MODERATE: 1.55,
    ActivityLevel.HIGH: 1.725,
    ActivityLevel.VERY_HIGH: 1.9,
}

PROTEIN_TARGETS = {
    Goal.LOSE: 2.2,
    Goal.MAINTAIN: 2.0,
    Goal.GAIN: 2.2,
}

FAT_TARGETS = {
    Goal.LOSE: 1.0,
    Goal.MAINTAIN: 1.2,
    Goal.GAIN: 1.3,
}


@dataclass(slots=True)
class UserMetrics:
    """Aggregated calculated metrics."""

    bmr: float
    tdee: float
    calorie_target: float
    protein_target_g: float
    fat_target_g: float
    carb_target_g: float


def calculate_bmr(weight: float, height: float, age: int, sex: Sex) -> float:
    """Calculate BMR using Mifflin-St Jeor equations."""

    if sex is Sex.MALE:
        return 10 * weight + 6.25 * height - 5 * age + 5
    if sex is Sex.FEMALE:
        return 10 * weight + 6.25 * height - 5 * age - 161
    raise ValueError(f"Unsupported sex: {sex}")


def calculate_tdee(bmr: float, activity_level: ActivityLevel) -> float:
    """Multiply BMR by the activity factor."""

    return bmr * ACTIVITY_FACTORS[activity_level]


def calculate_calorie_target(tdee: float, goal: Goal) -> float:
    """Calculate target calories based on goal."""

    if goal is Goal.LOSE:
        return tdee * 0.8
    if goal is Goal.MAINTAIN:
        return tdee
    if goal is Goal.GAIN:
        return tdee * 1.2
    raise ValueError(f"Unsupported goal: {goal}")


def calculate_macros(weight: float, calorie_target: float, goal: Goal) -> tuple[float, float, float]:
    """Return (protein_g, fat_g, carb_g)."""

    protein_per_kg = PROTEIN_TARGETS[goal]
    fat_per_kg = FAT_TARGETS[goal]

    protein_g = protein_per_kg * weight
    fat_g = fat_per_kg * weight

    calories_from_protein = protein_g * 4
    calories_from_fat = fat_g * 9

    carb_calories = max(calorie_target - (calories_from_protein + calories_from_fat), 0)
    carb_g = carb_calories / 4

    return protein_g, fat_g, carb_g


def build_metrics(weight: float, height: float, age: int, sex: Sex, activity: ActivityLevel, goal: Goal) -> UserMetrics:
    """Compute full metrics bundle."""

    bmr = calculate_bmr(weight, height, age, sex)
    tdee = calculate_tdee(bmr, activity)
    calorie_target = calculate_calorie_target(tdee, goal)
    protein_g, fat_g, carb_g = calculate_macros(weight, calorie_target, goal)
    return UserMetrics(
        bmr=bmr,
        tdee=tdee,
        calorie_target=calorie_target,
        protein_target_g=protein_g,
        fat_target_g=fat_g,
        carb_target_g=carb_g,
    )
