"""Telegram bot entrypoint."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from enum import Enum, auto
from typing import Any, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .calculations import ActivityLevel, Goal, Sex, build_metrics
from .config import get_settings
from .llm import MealAnalysis, analyze_meal_from_image, analyze_meal_from_text, request_day_summary
from .storage import Storage, User


LOGGER = logging.getLogger(__name__)


class RegistrationState(Enum):
    AGE = auto()
    SEX = auto()
    HEIGHT = auto()
    WEIGHT = auto()
    ACTIVITY = auto()
    GOAL = auto()


class LogState(Enum):
    CHOOSE_DAY = auto()
    CHOOSE_MEAL = auto()
    CHOOSE_ENTRY_TYPE = auto()
    ENTER_TEXT = auto()
    ENTER_PHOTO = auto()
    CONFIRM = auto()
    CORRECTION_TEXT = auto()


MEAL_TYPES = {
    "breakfast": "Завтрак",
    "lunch": "Обед",
    "dinner": "Ужин",
    "snack": "Перекус",
}

ENTRY_TYPES = {
    "text": "Текст",
    "image": "Фото",
}

ACTIVITY_OPTIONS = {
    "sedentary": "Минимальная (сидячая работа)",
    "light": "Легкая (1-3 тренировки в неделю)",
    "moderate": "Средняя (3-5 тренировок в неделю)",
    "high": "Высокая (6-7 тренировок в неделю)",
    "very_high": "Очень высокая (физический труд + спорт)",
}

GOAL_OPTIONS = {
    "lose": "Похудение",
    "maintain": "Поддержание",
    "gain": "Набор массы",
}


class CalorieBot:
    def __init__(self) -> None:
        settings = get_settings()
        self.storage = Storage(settings.database_path)
        self.application = (
            Application.builder()
            .token(settings.telegram_token)
            .rate_limiter(AIORateLimiter())
            .build()
        )
        self._register_handlers()

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------
    def _get_user(self, telegram_id: int) -> Optional[User]:
        return self.storage.get_user(telegram_id)

    def _ensure_user(self, update: Update) -> Optional[User]:
        telegram_id = update.effective_user.id if update.effective_user else None
        if not telegram_id:
            return None
        user = self._get_user(telegram_id)
        if not user:
            update.message.reply_text(
                "Сначала зарегистрируйтесь с помощью команды /start", reply_markup=ReplyKeyboardRemove()
            )
            return None
        return user

    # ------------------------------------------------------------------
    # Registration flow
    # ------------------------------------------------------------------
    async def start(self, update: Update, context: CallbackContext) -> int:
        telegram_id = update.effective_user.id
        user = self._get_user(telegram_id)
        if user:
            await update.message.reply_text(
                "С возвращением! Используйте команды:\n"
                "• /log_day — внести прием пищи\n"
                "• /finish_day — завершить день\n"
                "• /stats — статистика\n"
                "• /profile — профиль и цели"
            )
            return ConversationHandler.END

        await update.message.reply_text("Добро пожаловать! Для расчета калоража ответьте на несколько вопросов.\nСколько вам лет?")
        return RegistrationState.AGE

    async def registration_age(self, update: Update, context: CallbackContext) -> RegistrationState:
        try:
            age = int(update.message.text)
        except ValueError:
            await update.message.reply_text("Пожалуйста, введите число.")
            return RegistrationState.AGE

        if not 0 < age <= 120:
            await update.message.reply_text("Возраст должен быть от 1 до 120.")
            return RegistrationState.AGE

        context.user_data["registration"] = {"age": age}
        keyboard = [["М"], ["Ж"]]
        await update.message.reply_text("Выберите пол:", reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
        return RegistrationState.SEX

    async def registration_sex(self, update: Update, context: CallbackContext) -> RegistrationState:
        sex_value = update.message.text.strip().lower()
        if sex_value not in {"м", "ж"}:
            await update.message.reply_text("Выберите М или Ж.")
            return RegistrationState.SEX

        context.user_data["registration"]["sex"] = Sex.MALE if sex_value == "м" else Sex.FEMALE
        await update.message.reply_text("Введите ваш рост в см:", reply_markup=ReplyKeyboardRemove())
        return RegistrationState.HEIGHT

    async def registration_height(self, update: Update, context: CallbackContext) -> RegistrationState:
        try:
            height = float(update.message.text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("Введите число, например 175.")
            return RegistrationState.HEIGHT

        if not 50 <= height <= 250:
            await update.message.reply_text("Рост должен быть в пределах 50-250 см.")
            return RegistrationState.HEIGHT

        context.user_data["registration"]["height"] = height
        await update.message.reply_text("Введите ваш вес в кг:")
        return RegistrationState.WEIGHT

    async def registration_weight(self, update: Update, context: CallbackContext) -> RegistrationState:
        try:
            weight = float(update.message.text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("Введите число, например 70.5")
            return RegistrationState.WEIGHT

        if not 30 <= weight <= 400:
            await update.message.reply_text("Вес должен быть в пределах 30-400 кг.")
            return RegistrationState.WEIGHT

        context.user_data["registration"]["weight"] = weight
        keyboard = [[label] for label in ACTIVITY_OPTIONS.values()]
        await update.message.reply_text(
            "Выберите уровень активности:",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
        )
        return RegistrationState.ACTIVITY

    async def registration_activity(self, update: Update, context: CallbackContext) -> RegistrationState:
        selected = update.message.text.strip()
        for key, label in ACTIVITY_OPTIONS.items():
            if selected == label:
                context.user_data["registration"]["activity"] = ActivityLevel(key)
                break
        else:
            await update.message.reply_text("Пожалуйста, выберите один из вариантов из клавиатуры.")
            return RegistrationState.ACTIVITY

        keyboard = [[label] for label in GOAL_OPTIONS.values()]
        await update.message.reply_text(
            "Какая ваша цель?",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
        )
        return RegistrationState.GOAL

    async def registration_goal(self, update: Update, context: CallbackContext) -> int:
        selected = update.message.text.strip()
        for key, label in GOAL_OPTIONS.items():
            if selected == label:
                context.user_data["registration"]["goal"] = Goal(key)
                break
        else:
            await update.message.reply_text("Пожалуйста, выберите один из вариантов из клавиатуры.")
            return RegistrationState.GOAL

        data = context.user_data.pop("registration")
        telegram_id = update.effective_user.id
        metrics = build_metrics(
            weight=data["weight"],
            height=data["height"],
            age=data["age"],
            sex=data["sex"],
            activity=data["activity"],
            goal=data["goal"],
        )
        user = User(
            telegram_id=telegram_id,
            age=data["age"],
            sex=data["sex"],
            height=data["height"],
            weight=data["weight"],
            activity=data["activity"],
            goal=data["goal"],
            metrics=metrics,
        )
        self.storage.upsert_user(user)

        await update.message.reply_text(
            self._format_user_profile(user), reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN
        )
        await update.message.reply_text(
            "Используйте /log_day, чтобы внести прием пищи, или /finish_day, чтобы завершить день."
        )
        return ConversationHandler.END

    # ------------------------------------------------------------------
    # Logging meals
    # ------------------------------------------------------------------
    async def log_day_start(self, update: Update, context: CallbackContext) -> LogState:
        user = self._ensure_user(update)
        if not user:
            return ConversationHandler.END

        context.user_data["current_user"] = user
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Сегодня", callback_data="day_today"),
                    InlineKeyboardButton("Другой день", callback_data="day_other"),
                ]
            ]
        )
        await update.message.reply_text("Какой день хотите заполнить?", reply_markup=keyboard)
        return LogState.CHOOSE_DAY

    async def log_day_choose_day(self, update: Update, context: CallbackContext) -> LogState:
        query = update.callback_query
        await query.answer()
        user = context.user_data.get("current_user")
        if not user:
            await query.edit_message_text("Сессия устарела. Попробуйте снова командой /log_day.")
            return ConversationHandler.END

        if query.data == "day_today":
            selected_date = date.today()
            context.user_data["log_date"] = selected_date
            await query.edit_message_text(f"Выбран день: {selected_date.isoformat()}")
            return await self._prompt_meal_type(query.message, context)

        await query.edit_message_text("Введите дату в формате ГГГГ-ММ-ДД:")
        return LogState.CHOOSE_DAY

    async def log_day_receive_date(self, update: Update, context: CallbackContext) -> LogState:
        text = update.message.text.strip()
        try:
            selected_date = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            await update.message.reply_text("Неверный формат. Попробуйте снова (ГГГГ-ММ-ДД).")
            return LogState.CHOOSE_DAY

        context.user_data["log_date"] = selected_date
        await update.message.reply_text(f"Выбран день: {selected_date.isoformat()}")
        return await self._prompt_meal_type(update.message, context)

    async def _prompt_meal_type(self, message, context: CallbackContext) -> LogState:
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(label, callback_data=f"meal_{key}")]
                for key, label in MEAL_TYPES.items()
            ]
        )
        await message.reply_text("Выберите прием пищи:", reply_markup=keyboard)
        return LogState.CHOOSE_MEAL

    async def log_day_choose_meal(self, update: Update, context: CallbackContext) -> LogState:
        query = update.callback_query
        await query.answer()
        data = query.data.replace("meal_", "")
        if data not in MEAL_TYPES:
            await query.edit_message_text("Неизвестный тип приема пищи.")
            return ConversationHandler.END

        context.user_data["meal_type"] = data
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Текст", callback_data="entry_text"),
                    InlineKeyboardButton("Фото", callback_data="entry_image"),
                ]
            ]
        )
        await query.edit_message_text("Как хотите внести информацию?", reply_markup=keyboard)
        return LogState.CHOOSE_ENTRY_TYPE

    async def log_day_entry_type(self, update: Update, context: CallbackContext) -> LogState:
        query = update.callback_query
        await query.answer()
        entry_type = query.data.replace("entry_", "")
        if entry_type not in ENTRY_TYPES:
            await query.edit_message_text("Неизвестный тип записи.")
            return ConversationHandler.END

        context.user_data["entry_type"] = entry_type
        if entry_type == "text":
            await query.edit_message_text("Опишите, что вы съели. Можно указать количество и вес продуктов.")
            return LogState.ENTER_TEXT

        await query.edit_message_text("Пришлите фото блюда. Можно добавить подпись с описанием.")
        return LogState.ENTER_PHOTO

    async def log_day_receive_text(self, update: Update, context: CallbackContext) -> LogState:
        description = update.message.text
        return await self._handle_meal_input(update, context, description=description, photo_bytes=None)

    async def log_day_receive_photo(self, update: Update, context: CallbackContext) -> LogState:
        if not update.message.photo:
            await update.message.reply_text("Не удалось получить фото. Попробуйте еще раз.")
            return LogState.ENTER_PHOTO

        photo = update.message.photo[-1]
        file = await photo.get_file()
        image_bytes = await file.download_as_bytearray()
        description = update.message.caption or ""
        return await self._handle_meal_input(update, context, description=description, photo_bytes=bytes(image_bytes))

    async def _handle_meal_input(
        self,
        update: Update,
        context: CallbackContext,
        *,
        description: str,
        photo_bytes: Optional[bytes],
    ) -> LogState:
        message = update.message
        try:
            if photo_bytes:
                analysis = analyze_meal_from_image(description, photo_bytes)
            else:
                analysis = analyze_meal_from_text(description)
        except Exception as exc:  # pragma: no cover - network errors
            LOGGER.exception("LLM request failed")
            await message.reply_text(
                "Не удалось получить ответ от LLM. Пожалуйста, отправьте информацию еще раз."
            )
            return LogState.ENTER_TEXT if not photo_bytes else LogState.ENTER_PHOTO

        context.user_data["analysis"] = analysis
        context.user_data["user_input"] = description
        await message.reply_text(self._format_analysis(analysis), parse_mode=ParseMode.MARKDOWN)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Подтвердить", callback_data="confirm_yes"),
                    InlineKeyboardButton("Исправить", callback_data="confirm_edit"),
                ]
            ]
        )
        await message.reply_text("Все верно?", reply_markup=keyboard)
        return LogState.CONFIRM

    async def log_day_confirm(self, update: Update, context: CallbackContext) -> LogState:
        query = update.callback_query
        await query.answer()
        choice = query.data
        if choice == "confirm_yes":
            await query.edit_message_text("Сохраняю запись...")
            await self._persist_meal(context)
            await query.message.reply_text("Запись сохранена. Хотите добавить еще один прием пищи?", reply_markup=ReplyKeyboardRemove())
            return await self._prompt_meal_type(query.message, context)

        await query.edit_message_text("Опишите корректную информацию о блюде текстом.")
        return LogState.CORRECTION_TEXT

    async def log_day_correction(self, update: Update, context: CallbackContext) -> LogState:
        text = update.message.text
        try:
            analysis = analyze_meal_from_text(text)
        except Exception:
            await update.message.reply_text(
                "Не удалось получить ответ от LLM. Попробуйте отправить исправленный текст еще раз."
            )
            return LogState.CORRECTION_TEXT

        context.user_data["analysis"] = analysis
        context.user_data["user_input"] = text
        await update.message.reply_text(self._format_analysis(analysis), parse_mode=ParseMode.MARKDOWN)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Подтвердить", callback_data="confirm_yes"),
                    InlineKeyboardButton("Исправить", callback_data="confirm_edit"),
                ]
            ]
        )
        await update.message.reply_text("Все верно?", reply_markup=keyboard)
        return LogState.CONFIRM

    async def _persist_meal(self, context: CallbackContext) -> None:
        user: User = context.user_data["current_user"]
        log_date: date = context.user_data["log_date"]
        meal_type: str = context.user_data["meal_type"]
        entry_type: str = context.user_data["entry_type"]
        analysis: MealAnalysis = context.user_data["analysis"]
        user_input: str = context.user_data.get("user_input", "")

        day_log_id = self.storage.ensure_day_log(user.telegram_id, log_date)
        self.storage.add_meal_entry(
            day_log_id=day_log_id,
            meal_type=meal_type,
            entry_type=entry_type,
            user_input=user_input,
            llm_payload=analysis.to_dict(),
            corrected_payload=None,
        )

    # ------------------------------------------------------------------
    # Finish day
    # ------------------------------------------------------------------
    async def finish_day(self, update: Update, context: CallbackContext) -> None:
        user = self._ensure_user(update)
        if not user:
            return

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Сегодня", callback_data="finish_today"),
                    InlineKeyboardButton("Выбрать дату", callback_data="finish_other"),
                ]
            ]
        )
        await update.message.reply_text("Какой день завершить?", reply_markup=keyboard)

    async def finish_day_callback(self, update: Update, context: CallbackContext) -> None:
        query = update.callback_query
        await query.answer()
        user = self._get_user(query.from_user.id)
        if not user:
            await query.edit_message_text("Пользователь не найден. Используйте /start.")
            return

        if query.data == "finish_today":
            selected_date = date.today()
            await query.edit_message_text(f"Подводим итоги за {selected_date.isoformat()}...")
            await self._summarize_day(query.message, user, selected_date)
            return

        await query.edit_message_text("Введите дату в формате ГГГГ-ММ-ДД:")
        context.user_data["finish_pending"] = True

    async def finish_day_text(self, update: Update, context: CallbackContext) -> None:
        if not context.user_data.get("finish_pending"):
            return

        try:
            selected_date = datetime.strptime(update.message.text.strip(), "%Y-%m-%d").date()
        except ValueError:
            await update.message.reply_text("Неверный формат даты. Попробуйте снова.")
            return

        user = self._ensure_user(update)
        if not user:
            return

        context.user_data.pop("finish_pending", None)
        await update.message.reply_text(f"Подводим итоги за {selected_date.isoformat()}...")
        await self._summarize_day(update.message, user, selected_date)

    async def _summarize_day(self, message, user: User, selected_date: date) -> None:
        summary = self.storage.get_day_summary(user.telegram_id, selected_date)
        if not summary:
            await message.reply_text("За выбранный день нет данных.")
            return

        totals = summary["totals"]
        target = {
            "calories": user.metrics.calorie_target,
            "protein": user.metrics.protein_target_g,
            "fat": user.metrics.fat_target_g,
            "carbs": user.metrics.carb_target_g,
        }
        try:
            recommendations = request_day_summary(target, totals)
        except Exception:
            await message.reply_text(
                "Не удалось получить рекомендации от LLM. Попробуйте завершить день еще раз."
            )
            return

        await message.reply_text(
            self._format_day_summary(summary, target, recommendations),
            parse_mode=ParseMode.MARKDOWN,
        )

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------
    async def stats(self, update: Update, context: CallbackContext) -> None:
        user = self._ensure_user(update)
        if not user:
            return

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Неделя", callback_data="stats_week"),
                    InlineKeyboardButton("Месяц", callback_data="stats_month"),
                ]
            ]
        )
        await update.message.reply_text("Какой период показать?", reply_markup=keyboard)

    async def stats_callback(self, update: Update, context: CallbackContext) -> None:
        query = update.callback_query
        await query.answer()
        user = self._get_user(query.from_user.id)
        if not user:
            await query.edit_message_text("Пользователь не найден. Используйте /start.")
            return

        if query.data == "stats_week":
            end = date.today()
            start = end - timedelta(days=6)
            label = "неделю"
        else:
            end = date.today()
            start = end - timedelta(days=29)
            label = "месяц"

        rows = list(self.storage.iter_period_totals(user.telegram_id, start, end))
        if not rows:
            await query.edit_message_text("Нет данных за выбранный период.")
            return

        text_lines = [f"Статистика за {label} ({start.isoformat()} — {end.isoformat()}):"]
        total_calories = sum(row["total_calories"] for row in rows)
        total_protein = sum(row["total_protein"] for row in rows)
        total_fat = sum(row["total_fat"] for row in rows)
        total_carbs = sum(row["total_carbs"] for row in rows)
        text_lines.append(
            f"Всего: {total_calories:.0f} ккал, белки {total_protein:.0f} г, жиры {total_fat:.0f} г, углеводы {total_carbs:.0f} г"
        )
        for row in rows:
            text_lines.append(
                f"{row['day']}: {row['total_calories']:.0f} ккал (Б {row['total_protein']:.0f} г / Ж {row['total_fat']:.0f} г / У {row['total_carbs']:.0f} г)"
            )

        await query.edit_message_text("\n".join(text_lines))

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------
    async def profile(self, update: Update, context: CallbackContext) -> None:
        user = self._ensure_user(update)
        if not user:
            return
        await update.message.reply_text(
            self._format_user_profile(user), parse_mode=ParseMode.MARKDOWN
        )

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------
    def _format_user_profile(self, user: User) -> str:
        metrics = user.metrics
        return (
            "*Ваш профиль*\n"
            f"Возраст: {user.age}\n"
            f"Пол: {'М' if user.sex is Sex.MALE else 'Ж'}\n"
            f"Рост: {user.height:.0f} см\n"
            f"Вес: {user.weight:.1f} кг\n"
            f"Активность: {ACTIVITY_OPTIONS[user.activity.value]}\n"
            f"Цель: {GOAL_OPTIONS[user.goal.value]}\n\n"
            "*Целевые показатели*\n"
            f"Калории: {metrics.calorie_target:.0f} ккал\n"
            f"Белки: {metrics.protein_target_g:.0f} г\n"
            f"Жиры: {metrics.fat_target_g:.0f} г\n"
            f"Углеводы: {metrics.carb_target_g:.0f} г"
        )

    def _format_analysis(self, analysis: MealAnalysis) -> str:
        items_text = "\n".join(
            [
                f"• {item.get('name', 'Продукт')}: {item.get('calories', 0):.0f} ккал"
                for item in analysis.items
            ]
        )
        if not items_text:
            items_text = "• нет деталей"
        return (
            "*Оценка приема пищи*\n"
            f"Калории: {analysis.calories:.0f} ккал\n"
            f"Белки: {analysis.protein:.0f} г\n"
            f"Жиры: {analysis.fat:.0f} г\n"
            f"Углеводы: {analysis.carbs:.0f} г\n"
            f"Заметки: {analysis.notes or '—'}\n\n"
            f"Состав:\n{items_text}"
        )

    def _format_day_summary(self, summary: dict[str, Any], target: dict[str, float], recommendations: dict[str, Any]) -> str:
        totals = summary["totals"]
        meals = summary["meals"]
        lines = [
            f"*Итоги за {summary['day']}*",
            f"Цель: {target['calories']:.0f} ккал (Б {target['protein']:.0f} / Ж {target['fat']:.0f} / У {target['carbs']:.0f})",
            f"Факт: {totals['calories']:.0f} ккал (Б {totals['protein']:.0f} / Ж {totals['fat']:.0f} / У {totals['carbs']:.0f})",
            "\n*Приемы пищи:*",
        ]
        for meal in meals:
            label = MEAL_TYPES.get(meal["meal_type"], meal["meal_type"])
            lines.append(
                f"— {label}: {meal['calories']:.0f} ккал (Б {meal['protein']:.0f} / Ж {meal['fat']:.0f} / У {meal['carbs']:.0f})"
            )
        lines.append("\n*Рекомендации:*")
        lines.append(recommendations.get("summary", "Нет данных"))
        lines.append(recommendations.get("recommendations", ""))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------
    def _register_handlers(self) -> None:
        registration_handler = ConversationHandler(
            entry_points=[CommandHandler("start", self.start)],
            states={
                RegistrationState.AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.registration_age)],
                RegistrationState.SEX: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.registration_sex)],
                RegistrationState.HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.registration_height)],
                RegistrationState.WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.registration_weight)],
                RegistrationState.ACTIVITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.registration_activity)],
                RegistrationState.GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.registration_goal)],
            },
            fallbacks=[CommandHandler("start", self.start)],
            allow_reentry=True,
        )

        log_handler = ConversationHandler(
            entry_points=[CommandHandler("log_day", self.log_day_start)],
            states={
                LogState.CHOOSE_DAY: [
                    CallbackQueryHandler(self.log_day_choose_day, pattern="^day_"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.log_day_receive_date),
                ],
                LogState.CHOOSE_MEAL: [CallbackQueryHandler(self.log_day_choose_meal, pattern="^meal_")],
                LogState.CHOOSE_ENTRY_TYPE: [CallbackQueryHandler(self.log_day_entry_type, pattern="^entry_")],
                LogState.ENTER_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.log_day_receive_text)],
                LogState.ENTER_PHOTO: [MessageHandler(filters.PHOTO, self.log_day_receive_photo)],
                LogState.CONFIRM: [CallbackQueryHandler(self.log_day_confirm, pattern="^confirm_")],
                LogState.CORRECTION_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.log_day_correction)],
            },
            fallbacks=[CommandHandler("cancel", self._cancel_log)],
        )

        self.application.add_handler(registration_handler)
        self.application.add_handler(log_handler)
        self.application.add_handler(CommandHandler("finish_day", self.finish_day))
        self.application.add_handler(CallbackQueryHandler(self.finish_day_callback, pattern="^finish_"))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.finish_day_text))
        self.application.add_handler(CommandHandler("stats", self.stats))
        self.application.add_handler(CallbackQueryHandler(self.stats_callback, pattern="^stats_"))
        self.application.add_handler(CommandHandler("profile", self.profile))
        self.application.add_error_handler(self._error_handler)

    async def _cancel_log(self, update: Update, context: CallbackContext) -> int:
        await update.message.reply_text("Ввод прерван.", reply_markup=ReplyKeyboardRemove())
        context.user_data.clear()
        return ConversationHandler.END

    async def _error_handler(self, update: object, context: CallbackContext) -> None:
        LOGGER.exception("Ошибка при обработке обновления: %s", context.error)
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Произошла ошибка. Попробуйте позже.")

    # ------------------------------------------------------------------
    # Entrypoint
    # ------------------------------------------------------------------
    def run(self) -> None:
        self.application.run_polling()


async def _shutdown(application: Application) -> None:  # pragma: no cover - used in __main__
    await application.shutdown()
    await application.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = CalorieBot()
    bot.run()


if __name__ == "__main__":
    main()
