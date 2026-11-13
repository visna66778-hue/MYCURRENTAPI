"""Telegram миникошелёк путешественника."""
import os
import re
import logging
import traceback
import threading
from functools import wraps
from decimal import Decimal, InvalidOperation

import telebot
from telebot import types
from dotenv import load_dotenv

import current_api
import database as db
from countries import find_country

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(filename)s:%(lineno)d: %(message)s",
)
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в .env")
bot = telebot.TeleBot(TOKEN)
db.init_db()
sessions = {}
session_lock = threading.RLock()


_original_send_message = bot.send_message


def _log_bot_send(chat_id, text, *args, **kwargs):
    """Единая точка логирования исходящих сообщений без токена бота."""
    markup = kwargs.get("reply_markup")
    try:
        markup_data = markup.to_dict() if markup is not None else None
    except Exception:
        markup_data = type(markup).__name__ if markup is not None else None
    logger.info("OUTGOING message chat_id=%s text=%r inline_markup=%r", chat_id, text, markup_data)
    try:
        sent = _original_send_message(chat_id, text, *args, **kwargs)
        logger.debug("OUTGOING result message_id=%s", getattr(sent, "message_id", None))
        return sent
    except Exception as exc:
        logger.error("send_message failed chat_id=%s error_type=%s", chat_id, type(exc).__name__)
        raise


bot.send_message = _log_bot_send


def set_step(user_id, session, step):
    with session_lock:
        previous = session.get("step")
        session["step"] = step
    logger.info("STATE user_id=%s %s -> %s", user_id, previous, step)


def clear_session(user_id, reason):
    with session_lock:
        previous = sessions.pop(user_id, None)
    logger.info("STATE user_id=%s %s -> none reason=%s", user_id,
                previous.get("step") if previous else None, reason)


def consume_pending_expense(user_id, trip_id, amount, home_amount):
    """Атомарно поглощает подтверждение, не позволяя повторно списать расход."""
    with session_lock:
        pending = sessions.get(user_id)
        if (
            not pending
            or pending.get("step") != "confirm_expense"
            or pending.get("trip_id") != trip_id
            or pending.get("amount") != amount
            or pending.get("home_amount") != home_amount
            or not pending.get("category")
        ):
            return None
        sessions.pop(user_id, None)
    logger.info("STATE user_id=%s confirm_expense -> none reason=confirmed", user_id)
    return pending


def record_confirmed_expense(user_id, chat_id, pending):
    """Записывает подтверждённый расход и показывает обновлённый баланс."""
    saved = db.add_expense(
        user_id,
        pending["trip_id"],
        pending["amount"],
        pending["home_amount"],
        pending["category"],
    )
    if not saved:
        bot.send_message(chat_id, "Путешествие не найдено, расход не записан.")
        main_menu(chat_id)
        return
    bot.send_message(
        chat_id,
        f"Расход учтён!\nСписание: {money(pending['amount'])} {pending['currency']} = "
        f"{money(pending['home_amount'])} {pending['home_currency']}",
    )
    show_balance(chat_id, user_id)


def redact_secrets(value):
    """Скрывает загруженные секреты, даже если пользователь случайно их прислал."""
    if not isinstance(value, str):
        return value
    for secret in (TOKEN, current_api.API_KEY):
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value


def log_incoming_message(message):
    logger.info("INCOMING message user_id=%s chat_id=%s text=%r",
                message.from_user.id, message.chat.id, redact_secrets(message.text))


def log_handler_errors(handler):
    """Ловит исключения обработчика и логирует место без чувствительных URL."""
    @wraps(handler)
    def wrapped(event):
        try:
            return handler(event)
        except Exception as exc:
            frames = traceback.extract_tb(exc.__traceback__)
            origin = frames[-1] if frames else None
            logger.error("Handler exception handler=%s location=%s:%s error_type=%s",
                         handler.__name__, origin.filename if origin else "unknown",
                         origin.lineno if origin else "unknown", type(exc).__name__)
            event_message = getattr(event, "message", event)
            chat_id = getattr(getattr(event_message, "chat", None), "id", None)
            if chat_id:
                try:
                    _original_send_message(chat_id, "Произошла внутренняя ошибка. Попробуйте ещё раз или используйте /cancel.")
                except Exception as send_exc:
                    logger.error("Error notification failed error_type=%s", type(send_exc).__name__)
    return wrapped

MENU = ["Создать новое путешествие", "Мои путешествия", "Баланс", "История расходов", "Изменить курс"]
HISTORY_PERIODS = {
    "day": "за день",
    "week": "за неделю",
    "month": "за месяц",
    "all": "за всё путешествие",
}


def main_menu(chat_id, text="Выберите действие в меню или отправьте сумму расхода:"):
    logger.info("main_menu inputs chat_id=%s text=%r", chat_id, text)
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(name, callback_data=f"menu:{i}") for i, name in enumerate(MENU)]
    kb.add(*buttons)
    bot.send_message(chat_id, text, reply_markup=kb)


def money(value):
    formatted = f"{value:,.2f}".replace(",", " ")
    logger.info("money inputs value=%s returned=%s", value, formatted)
    return formatted


def parse_amount(text):
    logger.info("parse_amount input=%r", redact_secrets(text))
    try:
        value = Decimal(text.strip().replace(" ", "").replace(",", "."))
        result = float(value) if value.is_finite() and value > 0 else None
    except (InvalidOperation, ValueError):
        result = None
    logger.info("parse_amount returned=%s", result)
    return result


def api_conversion(amount, source, target):
    logger.info("api_conversion inputs amount=%s source=%s target=%s", amount, source, target)
    result = current_api.convert_currency(amount, source, target)
    value = result.get("result")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("API вернул некорректную сумму")
    converted = float(value)
    logger.info("api_conversion returned=%s", converted)
    return converted


def rate_from_api(home, local):
    logger.info("rate_from_api inputs home=%s local=%s", home, local)
    converted = api_conversion(1, home, local)
    logger.info("rate_from_api returned=%s", converted)
    return converted


def ask_new_trip(message, user_id=None):
    # У callback message.from_user — бот; состояние должно быть привязано
    # к пользователю, который нажал inline-кнопку.
    user_id = user_id or message.from_user.id
    logger.info("ask_new_trip inputs user_id=%s chat_id=%s", user_id, message.chat.id)
    previous = sessions.get(user_id, {}).get("step")
    sessions[user_id] = {"step": "origin"}
    logger.info("STATE user_id=%s %s -> origin", user_id, previous)
    bot.send_message(message.chat.id, "Введите страну отправления (например, Россия). /cancel — отмена")


@bot.message_handler(commands=["start", "help"])
@log_handler_errors
def start(message):
    log_incoming_message(message)
    main_menu(message.chat.id, "Привет! Создайте путешествие и учитывайте расходы в валюте страны пребывания.")


@bot.message_handler(commands=["cancel"])
@log_handler_errors
def cancel(message):
    log_incoming_message(message)
    clear_session(message.from_user.id, "cancel")
    main_menu(message.chat.id, "Действие отменено.")


@bot.message_handler(commands=["newtrip"])
@log_handler_errors
def newtrip(message):
    log_incoming_message(message)
    ask_new_trip(message)


@bot.message_handler(commands=["switch"])
@log_handler_errors
def switch_command(message):
    log_incoming_message(message)
    show_trips(message.chat.id, message.from_user.id)


@bot.message_handler(commands=["balance"])
@log_handler_errors
def balance_command(message):
    log_incoming_message(message)
    show_balance(message.chat.id, message.from_user.id)


@bot.message_handler(commands=["history"])
@log_handler_errors
def history_command(message):
    log_incoming_message(message)
    show_history(message.chat.id, message.from_user.id)


@bot.message_handler(commands=["setrate"])
@log_handler_errors
def setrate_command(message):
    log_incoming_message(message)
    begin_rate(message.chat.id, message.from_user.id)


def show_trips(chat_id, user_id):
    logger.info("show_trips inputs chat_id=%s user_id=%s", chat_id, user_id)
    user_trips = db.trips(user_id)
    if not user_trips:
        main_menu(chat_id, "Пока нет путешествий. Создайте первое через меню или /newtrip.")
        return
    kb = types.InlineKeyboardMarkup()
    for trip in user_trips:
        mark = " ✓" if (db.active_trip(user_id) and db.active_trip(user_id)["id"] == trip["id"]) else ""
        kb.add(types.InlineKeyboardButton(f"{trip['destination']} ({trip['local_currency']}){mark}", callback_data=f"switch:{trip['id']}"))
    bot.send_message(chat_id, "Выберите активное путешествие:", reply_markup=kb)
    main_menu(chat_id)


def show_balance(chat_id, user_id):
    logger.info("show_balance inputs chat_id=%s user_id=%s", chat_id, user_id)
    trip = db.active_trip(user_id)
    if not trip:
        main_menu(chat_id, "Сначала создайте путешествие.")
        return
    bot.send_message(chat_id, f"{trip['destination']}\nОстаток: {money(trip['balance_local'])} {trip['local_currency']} = {money(trip['balance_home'])} {trip['home_currency']}\nКурс: 1 {trip['home_currency']} = {money(trip['rate'])} {trip['local_currency']}")
    main_menu(chat_id)


def send_long_message(chat_id, text, limit=3500):
    """Разбивает длинную историю на сообщения безопасной длины Telegram."""
    chunk = ""
    for line in text.splitlines() or [text]:
        while len(line) > limit:
            if chunk:
                bot.send_message(chat_id, chunk)
                chunk = ""
            bot.send_message(chat_id, line[:limit])
            line = line[limit:]
        candidate = f"{chunk}\n{line}" if chunk else line
        if len(candidate) > limit:
            bot.send_message(chat_id, chunk)
            chunk = line
        else:
            chunk = candidate
    if chunk:
        bot.send_message(chat_id, chunk)


def show_history(chat_id, user_id):
    logger.info("show_history inputs chat_id=%s user_id=%s", chat_id, user_id)
    trip = db.active_trip(user_id)
    if not trip:
        main_menu(chat_id, "Сначала создайте путешествие.")
        return
    kb = types.InlineKeyboardMarkup(row_width=2)
    for period, label in HISTORY_PERIODS.items():
        kb.add(types.InlineKeyboardButton(label.capitalize(), callback_data=f"history_period:{period}"))
    bot.send_message(chat_id, f"История расходов: {trip['destination']}. Выберите период:", reply_markup=kb)
    main_menu(chat_id)


def show_history_views(chat_id, user_id, period):
    trip = db.active_trip(user_id)
    if not trip:
        main_menu(chat_id, "Сначала создайте путешествие.")
        return
    if period not in HISTORY_PERIODS:
        bot.send_message(chat_id, "Неизвестный период истории.")
        main_menu(chat_id)
        return
    kb = types.InlineKeyboardMarkup(row_width=2)
    options = (
        ("Все расходы", "all"),
        ("Расходы по категориям", "categories"),
        ("Итоги по категориям", "summary"),
        ("Общая сумма", "total"),
    )
    for label, view in options:
        kb.add(types.InlineKeyboardButton(label, callback_data=f"history_view:{view}:{period}"))
    bot.send_message(chat_id, f"Выбран период {HISTORY_PERIODS[period]}. Что показать?", reply_markup=kb)
    main_menu(chat_id)


def show_expense_categories(chat_id, user_id, period):
    trip = db.active_trip(user_id)
    if not trip:
        main_menu(chat_id, "Сначала создайте путешествие.")
        return
    kb = types.InlineKeyboardMarkup(row_width=2)
    for index, category in enumerate(db.EXPENSE_CATEGORIES):
        kb.add(types.InlineKeyboardButton(category, callback_data=f"history_category:{index}:{period}"))
    bot.send_message(chat_id, f"Выберите категорию ({HISTORY_PERIODS[period]}):", reply_markup=kb)
    main_menu(chat_id)


def show_history_result(chat_id, user_id, view, period, category=None):
    trip = db.active_trip(user_id)
    if not trip:
        main_menu(chat_id, "Сначала создайте путешествие.")
        return
    period_label = HISTORY_PERIODS[period]
    home, local = trip["home_currency"], trip["local_currency"]

    if view == "all" or view == "category":
        rows = db.history(
            user_id, trip["id"], category=category, period=period,
        )
        heading = f"Расходы {period_label}"
        if category:
            heading += f" · {category}"
        if not rows:
            bot.send_message(chat_id, f"{heading}: операций нет.")
        else:
            lines = []
            for row in rows:
                lines.append(
                    f"{row['created_at']} · {row['category']}"
                    f" · {money(row['amount'])} {local} = "
                    f"{money(row['home_amount'])} {home}"
                )
            send_long_message(chat_id, heading + ":\n" + "\n".join(lines))
    elif view == "summary":
        rows = db.expense_summary(user_id, trip["id"], period)
        total = db.expense_total(user_id, trip["id"], period)
        if not total["count"]:
            bot.send_message(chat_id, f"{period_label.capitalize()} расходов нет.")
        else:
            lines = [
                f"{row['category']}: {money(row['local_total'])} {local} = "
                f"{money(row['home_total'])} {home} ({row['count']} операций)"
                for row in rows
            ]
            lines.append(
                f"Всего: {money(total['local_total'])} {local} = "
                f"{money(total['home_total'])} {home}"
            )
            send_long_message(chat_id, f"Итоги по категориям {period_label}:\n" + "\n".join(lines))
    elif view == "total":
        total = db.expense_total(user_id, trip["id"], period)
        bot.send_message(
            chat_id,
            f"Общая сумма расходов {period_label}: "
            f"{money(total['local_total'])} {local} = {money(total['home_total'])} {home} "
            f"({total['count']} операций).",
        )
    main_menu(chat_id)


def begin_rate(chat_id, user_id):
    logger.info("begin_rate inputs chat_id=%s user_id=%s", chat_id, user_id)
    trip = db.active_trip(user_id)
    if not trip:
        main_menu(chat_id, "Сначала создайте путешествие.")
        return
    previous = sessions.get(user_id, {}).get("step")
    sessions[user_id] = {"step": "new_rate", "trip_id": trip["id"]}
    logger.info("STATE user_id=%s %s -> new_rate", user_id, previous)
    bot.send_message(chat_id, f"Введите курс: сколько {trip['local_currency']} за 1 {trip['home_currency']}? /cancel — отмена")


@bot.callback_query_handler(func=lambda c: True)
@log_handler_errors
def callbacks(call):
    user_id = call.from_user.id
    logger.info("INCOMING callback user_id=%s callback_data=%r", user_id, call.data)
    bot.answer_callback_query(call.id)
    logger.info("OUTGOING callback acknowledgement callback_id=%s", call.id)
    if call.data.startswith("menu:"):
        action = int(call.data.split(":")[1])
        # Переход из любого ожидающего ввода пункта меню отменяет старый шаг.
        clear_session(user_id, f"menu_navigation_{action}")
        if action == 0:
            ask_new_trip(call.message, user_id)
        elif action == 1:
            show_trips(call.message.chat.id, user_id)
        elif action == 2:
            show_balance(call.message.chat.id, user_id)
        elif action == 3:
            show_history(call.message.chat.id, user_id)
        elif action == 4:
            begin_rate(call.message.chat.id, user_id)
    elif call.data.startswith("history_period:"):
        clear_session(user_id, "history_navigation")
        period = call.data.split(":", 1)[1]
        show_history_views(call.message.chat.id, user_id, period)
    elif call.data.startswith("history_view:"):
        clear_session(user_id, "history_navigation")
        _, view, period = call.data.split(":")
        if view == "categories":
            show_expense_categories(call.message.chat.id, user_id, period)
        else:
            show_history_result(call.message.chat.id, user_id, view, period)
    elif call.data.startswith("history_category:"):
        clear_session(user_id, "history_navigation")
        _, category_index, period = call.data.split(":")
        index = int(category_index)
        if period not in HISTORY_PERIODS or not 0 <= index < len(db.EXPENSE_CATEGORIES):
            bot.send_message(call.message.chat.id, "Не удалось открыть эту категорию или период.")
            main_menu(call.message.chat.id)
        else:
            show_history_result(
                call.message.chat.id, user_id, "category", period,
                db.EXPENSE_CATEGORIES[index],
            )
    elif call.data.startswith("switch:"):
        trip_id = int(call.data.split(":")[1])
        clear_session(user_id, "trip_switched")
        if db.set_active(user_id, trip_id):
            bot.send_message(call.message.chat.id, "Путешествие переключено.")
            show_balance(call.message.chat.id, user_id)
        else:
            bot.send_message(call.message.chat.id, "Не удалось найти это путешествие.")
    elif call.data in ("rate:yes", "rate:no"):
        session = sessions.get(user_id, {})
        if call.data == "rate:no":
            if session.get("step") == "confirm_rate":
                set_step(user_id, session, "manual_rate")
                bot.send_message(call.message.chat.id, "Введите курс: местных единиц за 1 домашнюю. /cancel — отмена")
            else:
                logger.warning("Ignored manual-rate callback without matching state user_id=%s state=%s", user_id, session.get("step"))
                bot.send_message(call.message.chat.id, "Это действие уже неактуально. Начните создание заново через /newtrip.")
            return
        if session.get("step") == "confirm_rate":
            set_step(user_id, session, "initial")
            bot.send_message(call.message.chat.id, f"Введите начальную сумму в {session['home_currency']}. /cancel — отмена")
        else:
            logger.warning("Ignored rate confirmation without matching state user_id=%s state=%s", user_id, session.get("step"))
            bot.send_message(call.message.chat.id, "Это подтверждение курса уже неактуально. Создайте путешествие заново через /newtrip.")
    elif call.data.startswith("expense_category:"):
        _, category_index = call.data.split(":")
        index = int(category_index)
        with session_lock:
            pending = sessions.get(user_id)
            if (
                not pending
                or pending.get("step") != "select_expense_category"
                or not 0 <= index < len(db.EXPENSE_CATEGORIES)
            ):
                pending = None
            else:
                pending["category"] = db.EXPENSE_CATEGORIES[index]
                set_step(user_id, pending, "confirm_expense")
        if pending is None:
            bot.send_message(call.message.chat.id, "Выбор категории уже неактуален. Введите сумму расхода ещё раз.")
            main_menu(call.message.chat.id)
            return
        kb = types.InlineKeyboardMarkup().row(
            types.InlineKeyboardButton("Да", callback_data=f"expense:yes:{pending['amount']}:{pending['home_amount']}:{pending['trip_id']}"),
            types.InlineKeyboardButton("Нет", callback_data=f"expense:no:{pending['amount']}:{pending['home_amount']}:{pending['trip_id']}"),
        )
        bot.send_message(
            call.message.chat.id,
            f"Расход: {money(pending['amount'])} {pending['currency']} = "
            f"{money(pending['home_amount'])} {pending['home_currency']}\n"
            f"Категория: {pending['category']}\nУчесть расход?",
            reply_markup=kb,
        )
        main_menu(call.message.chat.id)
    elif call.data.startswith("expense:"):
        _, decision, amount, home_amount, trip_id = call.data.split(":")
        pending = consume_pending_expense(
            user_id, int(trip_id), float(amount), float(home_amount),
        )
        if pending is None:
            logger.warning("Ignored stale expense callback user_id=%s trip_id=%s", user_id, trip_id)
            bot.send_message(call.message.chat.id, "Это подтверждение расхода уже неактуально.")
            main_menu(call.message.chat.id)
            return
        if decision == "yes":
            record_confirmed_expense(user_id, call.message.chat.id, pending)
        elif decision == "no":
            bot.send_message(call.message.chat.id, "Расход отменён. Остаток не изменился.")
            show_balance(call.message.chat.id, user_id)
        else:
            bot.send_message(call.message.chat.id, "Не удалось учесть расход.")
            main_menu(call.message.chat.id)


@bot.message_handler(func=lambda m: True, content_types=["text"])
@log_handler_errors
def messages(message):
    user_id = message.from_user.id
    text = message.text.strip()
    logger.info("INCOMING message user_id=%s chat_id=%s text=%r", user_id, message.chat.id, redact_secrets(text))
    if text == "Создать новое путешествие":
        clear_session(user_id, "menu_text_create_trip")
        ask_new_trip(message); return
    if text == "Мои путешествия":
        clear_session(user_id, "menu_text_trips")
        show_trips(message.chat.id, user_id); return
    if text == "Баланс":
        clear_session(user_id, "menu_text_balance")
        show_balance(message.chat.id, user_id); return
    if text == "История расходов":
        clear_session(user_id, "menu_text_history")
        show_history(message.chat.id, user_id); return
    if text == "Изменить курс":
        clear_session(user_id, "menu_text_rate")
        begin_rate(message.chat.id, user_id); return

    session = sessions.get(user_id)
    if session:
        step = session["step"]
        if step in ("select_expense_category", "confirm_expense"):
            bot.send_message(message.chat.id, "Завершите выбор категории и подтвердите или отмените расход кнопками ниже.")
            return
        if step in ("origin", "destination"):
            country = find_country(text)
            if not country:
                bot.send_message(message.chat.id, "Не нашла страну в локальном справочнике. Попробуйте другое написание.")
                return
            key = "origin" if step == "origin" else "destination"
            currency_key = "home_currency" if step == "origin" else "destination_currency"
            session[key], session[currency_key] = country
            if step == "origin":
                set_step(user_id, session, "destination")
                bot.send_message(message.chat.id, "Введите страну назначения.")
            else:
                if session["home_currency"] == session["destination_currency"]:
                    session["rate"] = 1.0
                    set_step(user_id, session, "initial")
                    bot.send_message(message.chat.id, "Валюты совпадают, курс 1:1. Введите начальную сумму в домашней валюте.")
                    return
                try:
                    logger.info(
                        "Получаю курс для путешествия user_id=%s: %s -> %s",
                        user_id,
                        session["home_currency"],
                        session["destination_currency"],
                    )
                    session["rate"] = rate_from_api(session["home_currency"], session["destination_currency"])
                except Exception as exc:
                    logger.error("Не удалось получить курс при создании путешествия user_id=%s error_type=%s", user_id, type(exc).__name__)
                    bot.send_message(message.chat.id, f"Не удалось получить курс API ({exc}). Проверьте ключ или попробуйте позже; /cancel отменит создание.")
                    return
                set_step(user_id, session, "confirm_rate")
                kb = types.InlineKeyboardMarkup().row(types.InlineKeyboardButton("Да, подходит", callback_data="rate:yes"), types.InlineKeyboardButton("Ввести свой", callback_data="rate:no"))
                bot.send_message(message.chat.id, f"Текущий курс: 1 {session['home_currency']} = {money(session['rate'])} {session['destination_currency']}. Подходит?", reply_markup=kb)
            return
        if step in ("manual_rate", "new_rate"):
            rate = parse_amount(text)
            if rate is None:
                bot.send_message(message.chat.id, "Введите положительный курс числом, например 12.5."); return
            if step == "new_rate":
                db.set_rate(user_id, session["trip_id"], rate)
                clear_session(user_id, "rate_updated")
                bot.send_message(message.chat.id, "Курс обновлён."); show_balance(message.chat.id, user_id)
            else:
                session["rate"] = rate
                session["manual"] = True
                set_step(user_id, session, "initial")
                bot.send_message(message.chat.id, f"Курс сохранён. Введите начальную сумму в {session['home_currency']}.")
            return
        if step == "initial":
            amount = parse_amount(text)
            if amount is None:
                bot.send_message(message.chat.id, "Введите положительную сумму числом."); return
            try:
                local_amount = api_conversion(amount, session["home_currency"], session["destination_currency"])
                # Ручной курс применяется к кошельку по выбору пользователя.
                if session.get("manual"):
                    local_amount = amount * session["rate"]
            except Exception as exc:
                bot.send_message(message.chat.id, f"Не удалось конвертировать сумму через API: {exc}")
                return
            trip_id = db.create_trip(user_id, session["origin"], session["destination"], session["home_currency"], session["destination_currency"], session["rate"], amount, local_amount)
            clear_session(user_id, "trip_created")
            bot.send_message(message.chat.id, f"Путешествие создано. Стартовый баланс: {money(amount)} {session['home_currency']} = {money(local_amount)} {session['destination_currency']}")
            show_balance(message.chat.id, user_id); return

    amount = parse_amount(text)
    if amount is None:
        if re.fullmatch(r"[-+]?\d+[.,]?\d*", text):
            bot.send_message(message.chat.id, "Сумма должна быть больше нуля.")
        else:
            main_menu(message.chat.id, "Не распознала действие. Используйте меню или отправьте сумму расхода.")
        return
    trip = db.active_trip(user_id)
    if not trip:
        main_menu(message.chat.id, "Сначала создайте путешествие."); return
    try:
        home_amount = api_conversion(amount, trip["local_currency"], trip["home_currency"])
    except Exception as exc:
        bot.send_message(message.chat.id, f"Не удалось пересчитать расход через API. Попробуйте позже. ({exc})"); return
    kb = types.InlineKeyboardMarkup(row_width=2)
    for index, category in enumerate(db.EXPENSE_CATEGORIES):
        kb.add(types.InlineKeyboardButton(category, callback_data=f"expense_category:{index}"))
    with session_lock:
        previous = sessions.get(user_id, {}).get("step")
        sessions[user_id] = {
            "step": "select_expense_category",
            "trip_id": trip["id"],
            "amount": amount,
            "home_amount": home_amount,
            "currency": trip["local_currency"],
            "home_currency": trip["home_currency"],
        }
    logger.info("STATE user_id=%s %s -> select_expense_category", user_id, previous)
    bot.send_message(
        message.chat.id,
        f"{money(amount)} {trip['local_currency']} = {money(home_amount)} {trip['home_currency']}\nВыберите категорию расхода:",
        reply_markup=kb,
    )
    main_menu(message.chat.id)


if __name__ == "__main__":
    logger.info("Starting Telegram wallet bot; database=%s", db.DB_PATH)
    print("Бот запущен. Для остановки нажмите Ctrl+C.")
    bot.infinity_polling(skip_pending=True)
