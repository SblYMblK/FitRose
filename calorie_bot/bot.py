"""Telegram bot entrypoint."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from enum import Enum, auto
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
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

try:  # pragma: no cover - import fallback for running as a script
    from .calculations import ActivityLevel, Goal, Sex, build_metrics
    from .config import get_settings
    from .llm import (
        MealAnalysis,
        analyze_meal_from_image,
        analyze_meal_from_text,
        request_day_summary,
    )
    from .storage import Storage, User
except ImportError:  # pragma: no cover - allows "python bot.py" execution
    from calculations import ActivityLevel, Goal, Sex, build_metrics
    from config import get_settings
    from llm import (
        MealAnalysis,
        analyze_meal_from_image,
        analyze_meal_from_text,
        request_day_summary,
    )
    from storage import Storage, User


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
        self.main_menu = ReplyKeyboardMarkup(
            [
                ["/log_day 🍽 Заполнить день"],
                ["/finish_day ✅ Завершить день"],
                ["/stats 📊 Мой прогресс", "/profile 👤 Мой профиль"],
            ],
            resize_keyboard=True,
        )
        builder = Application.builder().token(settings.telegram_token)
        try:
            builder = builder.rate_limiter(AIORateLimiter())
        except RuntimeError as exc:  # pragma: no cover - depends on optional dependency
            LOGGER.warning("Rate limiter disabled: %s", exc)
        self.application = builder.build()
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
                "Чтобы мы подобрали персональные рекомендации, сначала нажмите /start и заполните профиль."
            )
            return None
        return user

    def _set_active_log(self, user: User, context: CallbackContext, log_day: date) -> int:
        day_log_id = self.storage.set_active_day(user.telegram_id, log_day)
        context.user_data["log_date"] = log_day
        context.user_data["day_log_id"] = day_log_id
        context.user_data["active_day_info"] = {"id": day_log_id, "day": log_day}
        return day_log_id

    # ------------------------------------------------------------------
    # Registration flow
    # ------------------------------------------------------------------
    async def start(self, update: Update, context: CallbackContext) -> int:
        telegram_id = update.effective_user.id
        user = self._get_user(telegram_id)
        if user:
            await update.message.reply_text(
                "Рады снова видеть вас! Ваш персональный фитнес-дневник готов к новым записям.\n"
                "Воспользуйтесь меню ниже или командами:\n"
                "• /log_day — добавить прием пищи\n"
                "• /finish_day — подвести итоги дня\n"
                "• /stats — посмотреть прогресс\n"
                "• /profile — обновить профиль",
                reply_markup=self.main_menu,
            )
            return ConversationHandler.END

        await update.message.reply_text(
            "Добро пожаловать в FitRose! Давайте настроим рекомендации под вас — ответьте на несколько вопросов.\n"
            "Сколько вам полных лет?"
        )
        return RegistrationState.AGE

    async def registration_age(self, update: Update, context: CallbackContext) -> RegistrationState:
        try:
            age = int(update.message.text)
        except ValueError:
            await update.message.reply_text("Введите, пожалуйста, целое число — например, 29.")
            return RegistrationState.AGE

        if not 0 < age <= 120:
            await update.message.reply_text("Возраст должен быть от 1 до 120 лет. Попробуйте снова.")
            return RegistrationState.AGE

        context.user_data["registration"] = {"age": age}
        keyboard = [["М"], ["Ж"]]
        await update.message.reply_text(
            "Выберите пол — это поможет точнее рассчитать норму:",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
        )
        return RegistrationState.SEX

    async def registration_sex(self, update: Update, context: CallbackContext) -> RegistrationState:
        sex_value = update.message.text.strip().lower()
        if sex_value not in {"м", "ж"}:
            await update.message.reply_text("Пожалуйста, воспользуйтесь кнопками М или Ж ниже.")
            return RegistrationState.SEX

        context.user_data["registration"]["sex"] = Sex.MALE if sex_value == "м" else Sex.FEMALE
        await update.message.reply_text("Отлично! Укажите ваш рост в сантиметрах (например, 172).", reply_markup=ReplyKeyboardRemove())
        return RegistrationState.HEIGHT

    async def registration_height(self, update: Update, context: CallbackContext) -> RegistrationState:
        try:
            height = float(update.message.text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("Введите, пожалуйста, число — например, 175.")
            return RegistrationState.HEIGHT

        if not 50 <= height <= 250:
            await update.message.reply_text("Рост должен быть в диапазоне 50–250 см. Попробуем ещё раз?")
            return RegistrationState.HEIGHT

        context.user_data["registration"]["height"] = height
        await update.message.reply_text("Спасибо! Теперь укажите вес в килограммах (например, 68.5).")
        return RegistrationState.WEIGHT

    async def registration_weight(self, update: Update, context: CallbackContext) -> RegistrationState:
        try:
            weight = float(update.message.text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("Введите, пожалуйста, число — например, 70.5.")
            return RegistrationState.WEIGHT

        if not 30 <= weight <= 400:
            await update.message.reply_text("Вес должен быть в пределах 30–400 кг. Попробуйте снова.")
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
            await update.message.reply_text("Пожалуйста, воспользуйтесь вариантами на клавиатуре ниже.")
            return RegistrationState.ACTIVITY

        keyboard = [[label] for label in GOAL_OPTIONS.values()]
        await update.message.reply_text(
            "Какая цель сейчас ближе всего?",
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
            await update.message.reply_text("Пожалуйста, выберите цель из предложенных кнопок.")
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
            self._format_user_profile(user), parse_mode=ParseMode.MARKDOWN
        )
        await update.message.reply_text(
            "Профиль готов! Внизу — клавиатура с быстрыми действиями: добавляйте приёмы пищи, завершайте день и следите за прогрессом.",
            reply_markup=self.main_menu,
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
        active_day = self.storage.get_active_day(user.telegram_id)
        if active_day:
            context.user_data["active_day_info"] = active_day
            intro = (
                f"Продолжаем день {self._format_day_label(active_day['day'])}! "
                "Можно сразу добавить новый приём пищи или выбрать другую дату."
            )
        else:
            context.user_data.pop("active_day_info", None)
            intro = "Начинаем вести дневник питания! Выберите день, который хотите заполнить."

        await update.message.reply_text(intro, reply_markup=self.main_menu)

        buttons = []
        if active_day:
            buttons.append([InlineKeyboardButton("Продолжить текущий день", callback_data="day_current")])
        buttons.append(
            [
                InlineKeyboardButton("Сегодня", callback_data="day_today"),
                InlineKeyboardButton("Выбрать дату", callback_data="day_other"),
            ]
        )
        keyboard = InlineKeyboardMarkup(buttons)
        await update.message.reply_text("Какой день фиксируем?", reply_markup=keyboard)
        return LogState.CHOOSE_DAY

    async def log_day_choose_day(self, update: Update, context: CallbackContext) -> LogState:
        query = update.callback_query
        await query.answer()
        user = context.user_data.get("current_user")
        if not user:
            await query.edit_message_text("Сессия устарела. Попробуйте снова командой /log_day.")
            return ConversationHandler.END

        if query.data == "day_current":
            active_info = context.user_data.get("active_day_info")
            if not active_info:
                await query.edit_message_text("Текущий день не найден. Давайте выберем дату заново через /log_day.")
                return ConversationHandler.END
            selected_date = active_info["day"]
            self._set_active_log(user, context, selected_date)
            await query.edit_message_text(
                f"Продолжаем день {self._format_day_label(selected_date)}."
            )
            return await self._prompt_meal_type(query.message, context)

        if query.data == "day_today":
            selected_date = date.today()
            self._set_active_log(user, context, selected_date)
            await query.edit_message_text(
                f"Выбран день: {selected_date.strftime('%d.%m.%Y')}"
            )
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

        user = context.user_data.get("current_user")
        if not user:
            await update.message.reply_text("Сессия устарела. Запустите команду /log_day заново.")
            return ConversationHandler.END

        self._set_active_log(user, context, selected_date)
        await update.message.reply_text(
            f"Выбран день: {selected_date.strftime('%d.%m.%Y')}"
        )
        return await self._prompt_meal_type(update.message, context)

    async def _prompt_meal_type(self, message, context: CallbackContext) -> LogState:
        log_date: Optional[date] = context.user_data.get("log_date")
        day_label = self._format_day_label(log_date) if log_date else "выбранный день"
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(label, callback_data=f"meal_{key}")]
                for key, label in MEAL_TYPES.items()
            ]
        )
        await message.reply_text(
            f"Какой приём пищи добавим для {day_label}?",
            reply_markup=keyboard,
        )
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
        await query.edit_message_text(
            "Как удобнее зафиксировать приём: текстом или фото?",
            reply_markup=keyboard,
        )
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
            await query.edit_message_text(
                "Расскажите, что было в приёме пищи. Чем подробнее, тем точнее подсчёт!"
            )
            return LogState.ENTER_TEXT

        await query.edit_message_text(
            "Пришлите фото блюда — можно добавить подпись с деталями, это поможет точности."
        )
        return LogState.ENTER_PHOTO

    async def log_day_receive_text(self, update: Update, context: CallbackContext) -> LogState:
        description = update.message.text
        return await self._handle_meal_input(update, context, description=description, photo_bytes=None)

    async def log_day_receive_photo(self, update: Update, context: CallbackContext) -> LogState:
        if not update.message.photo:
            await update.message.reply_text("Похоже, фото не загрузилось. Пришлите его ещё раз, пожалуйста.")
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
                "Сервис анализа временно недоступен. Отправьте информацию ещё раз — и мы обязательно всё посчитаем!"
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
        await message.reply_text("Подтвердите расчёт или скорректируйте данные:", reply_markup=keyboard)
        return LogState.CONFIRM

    async def log_day_confirm(self, update: Update, context: CallbackContext) -> LogState:
        query = update.callback_query
        await query.answer()
        choice = query.data
        if choice == "confirm_yes":
            await query.edit_message_text("Сохраняю запись... ✅")
            await self._persist_meal(context)
            await query.message.reply_text(
                "Готово! Можно добавить следующий приём или завершить день кнопкой внизу.",
                reply_markup=self.main_menu,
            )
            return await self._prompt_meal_type(query.message, context)

        await query.edit_message_text("Опишите корректные данные о блюде текстом.")
        return LogState.CORRECTION_TEXT

    async def log_day_correction(self, update: Update, context: CallbackContext) -> LogState:
        text = update.message.text
        try:
            analysis = analyze_meal_from_text(text)
        except Exception:
            await update.message.reply_text(
                "Пока не удалось получить ответ. Отправьте, пожалуйста, исправленный текст ещё раз."
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
        await update.message.reply_text("Подтвердите расчёт или внесите правки:", reply_markup=keyboard)
        return LogState.CONFIRM

    async def _persist_meal(self, context: CallbackContext) -> None:
        user: User = context.user_data["current_user"]
        log_date: date = context.user_data["log_date"]
        meal_type: str = context.user_data["meal_type"]
        entry_type: str = context.user_data["entry_type"]
        analysis: MealAnalysis = context.user_data["analysis"]
        user_input: str = context.user_data.get("user_input", "")

        day_log_id = context.user_data.get("day_log_id")
        if not day_log_id:
            day_log_id = self._set_active_log(user, context, log_date)
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

        active_day = self.storage.get_active_day(user.telegram_id)
        if not active_day:
            await update.message.reply_text(
                "Сейчас нет открытого дня. Нажмите «/log_day 🍽 Заполнить день», чтобы начать новый.",
                reply_markup=self.main_menu,
            )
            return

        selected_date: date = active_day["day"]
        await update.message.reply_text(
            f"Подводим итоги за {selected_date.strftime('%d.%m.%Y')}...",
            reply_markup=self.main_menu,
        )
        success = await self._summarize_day(update.message, user, selected_date)
        if success:
            self.storage.close_day(user.telegram_id, selected_date)
            context.user_data.pop("log_date", None)
            context.user_data.pop("day_log_id", None)
            context.user_data.pop("active_day_info", None)
            await update.message.reply_text(
                "День завершён! Отдыхайте и возвращайтесь завтра за новым прогрессом.",
                reply_markup=self.main_menu,
            )

    async def _summarize_day(self, message, user: User, selected_date: date) -> bool:
        summary = self.storage.get_day_summary(user.telegram_id, selected_date)
        if not summary:
            await message.reply_text("За этот день пока нет записей. Добавьте хотя бы один приём пищи, чтобы подвести итоги.")
            return False

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
                "Не удалось получить рекомендации от сервиса. Попробуйте завершить день чуть позже."
            )
            return False

        await message.reply_text(
            self._format_day_summary(summary, target, recommendations),
            parse_mode=ParseMode.MARKDOWN,
        )
        return True

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
        await update.message.reply_text(
            "За какой период показать динамику?",
            reply_markup=keyboard,
        )

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
            await query.edit_message_text("Пока что за этот период нет записей. Загляните позже, чтобы увидеть прогресс!")
            return

        text_lines = [f"📊 Статистика за {label} ({start.isoformat()} — {end.isoformat()}):"]
        total_calories = sum(row["total_calories"] for row in rows)
        total_protein = sum(row["total_protein"] for row in rows)
        total_fat = sum(row["total_fat"] for row in rows)
        total_carbs = sum(row["total_carbs"] for row in rows)
        text_lines.append(
            f"Всего: {total_calories:.0f} ккал • Белки {total_protein:.0f} г • Жиры {total_fat:.0f} г • Углеводы {total_carbs:.0f} г"
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
        await update.message.reply_text(
            "Меню ниже поможет быстро добавить приём пищи или завершить день.",
            reply_markup=self.main_menu,
        )

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------
    def _format_user_profile(self, user: User) -> str:
        metrics = user.metrics
        return (
            "✨ *Ваш персональный профиль*\n"
            f"Возраст: {user.age} лет\n"
            f"Пол: {'М' if user.sex is Sex.MALE else 'Ж'}\n"
            f"Рост: {user.height:.0f} см\n"
            f"Вес: {user.weight:.1f} кг\n"
            f"Активность: {ACTIVITY_OPTIONS[user.activity.value]}\n"
            f"Цель: {GOAL_OPTIONS[user.goal.value]}\n\n"
            "🎯 *Ежедневные ориентиры*\n"
            f"Калории: {metrics.calorie_target:.0f} ккал\n"
            f"Белки: {metrics.protein_target_g:.0f} г\n"
            f"Жиры: {metrics.fat_target_g:.0f} г\n"
            f"Углеводы: {metrics.carb_target_g:.0f} г"
        )

    def _format_day_label(self, value: date) -> str:
        today = date.today()
        if value == today:
            return "сегодня"
        if value == today - timedelta(days=1):
            return "вчера"
        return value.strftime("%d.%m.%Y")

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
            "🍽 *Разбор приёма пищи*\n"
            f"Энергия: {analysis.calories:.0f} ккал\n"
            f"Белки: {analysis.protein:.0f} г\n"
            f"Жиры: {analysis.fat:.0f} г\n"
            f"Углеводы: {analysis.carbs:.0f} г\n"
            f"Комментарий эксперта: {analysis.notes or '—'}\n\n"
            f"Состав блюда:\n{items_text}"
        )

    def _format_day_summary(self, summary: dict[str, Any], target: dict[str, float], recommendations: dict[str, Any]) -> str:
        totals = summary["totals"]
        meals = summary["meals"]
        day_date = date.fromisoformat(summary["day"])
        day_label = self._format_day_label(day_date)
        lines = [
            f"✨ *Итоги за {day_label}* ({day_date.strftime('%d.%m.%Y')})",
            f"Цель: {target['calories']:.0f} ккал (Б {target['protein']:.0f} / Ж {target['fat']:.0f} / У {target['carbs']:.0f})",
            f"Факт: {totals['calories']:.0f} ккал (Б {totals['protein']:.0f} / Ж {totals['fat']:.0f} / У {totals['carbs']:.0f})",
            "\n🍴 *Приёмы пищи:*",
        ]
        for meal in meals:
            label = MEAL_TYPES.get(meal["meal_type"], meal["meal_type"])
            lines.append(
                f"— {label}: {meal['calories']:.0f} ккал (Б {meal['protein']:.0f} / Ж {meal['fat']:.0f} / У {meal['carbs']:.0f})"
            )
        lines.append("\n💡 *Рекомендации коуча:*")

        summary_text = recommendations.get("summary", "Нет данных")
        if isinstance(summary_text, list):
            summary_text = "\n".join(str(item).strip() for item in summary_text if item)
        else:
            summary_text = str(summary_text).strip()
        lines.append(summary_text or "Нет данных")

        extra = recommendations.get("recommendations", "")
        if isinstance(extra, list):
            extra_text = "\n".join(str(item).strip() for item in extra if item)
        else:
            extra_text = str(extra).strip()
        if extra_text:
            lines.append(extra_text)
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
        self.application.add_handler(CommandHandler("stats", self.stats))
        self.application.add_handler(CallbackQueryHandler(self.stats_callback, pattern="^stats_"))
        self.application.add_handler(CommandHandler("profile", self.profile))
        self.application.add_error_handler(self._error_handler)

    async def _cancel_log(self, update: Update, context: CallbackContext) -> int:
        await update.message.reply_text(
            "Ввод приостановлен. Меню действий всегда доступно ниже.",
            reply_markup=self.main_menu,
        )
        context.user_data.clear()
        return ConversationHandler.END

    async def _error_handler(self, update: object, context: CallbackContext) -> None:
        LOGGER.exception("Ошибка при обработке обновления: %s", context.error)
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "Что-то пошло не так. Попробуйте ещё раз немного позже — мы уже разбираемся."
            )

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
