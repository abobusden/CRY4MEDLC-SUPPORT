import json
import os
import time
import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from config import (
    BOT_TOKEN, OWNER_IDS, TICKET_TTL_DAYS,
    MODS_FILE, USERS_FILE, TICKETS_FILE,
    PUNISH_FILE, STATS_FILE,
)

# ============================================================
#  ХРАНИЛИЩЕ (JSON-файлы)
# ============================================================

def load_json(path, default):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


mods = load_json(MODS_FILE, {})            # {"111222333": "@vasya"}
users = load_json(USERS_FILE, {})          # {"111222333": {"username": "vasya", "first_name": "Вася"}}
tickets = load_json(TICKETS_FILE, {})      # {"12": {...}}
punishments = load_json(PUNISH_FILE, {})   # {"111222333": {"type": "mute", "until": 1730000000}}
stats = load_json(STATS_FILE, {})          # {"111222333": {"answered": 0, "likes": 0, "dislikes": 0}}

next_ticket_id = max([int(k) for k in tickets.keys()], default=0) + 1

# ============================================================
#  ХЕЛПЕРЫ
# ============================================================

def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


def is_mod(user_id: int) -> bool:
    return str(user_id) in mods


def is_staff(user_id: int) -> bool:
    return is_owner(user_id) or is_mod(user_id)


def remember_user(message: Message):
    """Сохраняет @username ↔ ID для поиска по нику."""
    u = message.from_user
    users[str(u.id)] = {
        "username": u.username or "",
        "first_name": u.first_name or "",
    }
    save_json(USERS_FILE, users)


def find_user_by_username(username: str):
    """Возвращает ID по @username или None."""
    username = username.lstrip("@").lower()
    for uid, info in users.items():
        if info.get("username", "").lower() == username:
            return int(uid)
    return None


def resolve_target(arg: str):
    """
    Принимает '@ник' или '123456789'.
    Возвращает (user_id, error_text).
    """
    if arg.startswith("@"):
        uid = find_user_by_username(arg)
        if uid is None:
            return None, f"❌ Пользователь {arg} не найден. Пусть напишет боту /start, либо укажите ID."
        return uid, None
    if arg.isdigit():
        return int(arg), None
    return None, "❌ Укажите @ник или числовой ID."


def parse_duration(s: str):
    """'10m' / '1h' / '7d' → секунды. None если формат неверный."""
    s = s.strip().lower()
    if not s or s[-1] not in "mhd":
        return None
    try:
        n = int(s[:-1])
    except ValueError:
        return None
    if n <= 0:
        return None
    return n * {"m": 60, "h": 3600, "d": 86400}[s[-1]]


def fmt_duration(seconds: int) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d: parts.append(f"{d} дн.")
    if h: parts.append(f"{h} ч.")
    if m: parts.append(f"{m} мин.")
    return " ".join(parts) or "меньше минуты"


def get_punishment(user_id: int):
    """Возвращает актуальное наказание или None."""
    p = punishments.get(str(user_id))
    if not p:
        return None
    if p["type"] == "ban":
        return p
    if p["type"] == "mute":
        if p["until"] <= time.time():
            punishments.pop(str(user_id), None)
            save_json(PUNISH_FILE, punishments)
            return None
        return p
    return None


def ensure_stats(user_id: int):
    key = str(user_id)
    if key not in stats:
        stats[key] = {"answered": 0, "likes": 0, "dislikes": 0}
    return stats[key]


def display_name(user_id: int) -> str:
    key = str(user_id)
    if is_owner(user_id):
        name = users.get(key, {}).get("username")
        return f"👑 @{name}" if name else f"👑 ID {user_id}"
    info = users.get(key, {})
    if info.get("username"):
        return f"@{info['username']}"
    return info.get("first_name") or f"ID {user_id}"


def cleanup_old_tickets():
    """Удаляет закрытые тикеты старше TICKET_TTL_DAYS."""
    now = time.time()
    ttl = TICKET_TTL_DAYS * 86400
    removed = 0
    for tid in list(tickets.keys()):
        t = tickets[tid]
        if t["status"] in ("rated", "rejected") and now - t.get("closed", t["created"]) > ttl:
            tickets.pop(tid)
            removed += 1
    if removed:
        save_json(TICKETS_FILE, tickets)


# ============================================================
#  БОТ И ДИСПЕТЧЕР
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class States(StatesGroup):
    waiting_question = State()
    waiting_answer = State()


# ---------- Клавиатуры ----------

def user_menu():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❓ Задать вопрос")]],
        resize_keyboard=True,
    )


def staff_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Активные тикеты")],
            [KeyboardButton(text="❓ Задать вопрос")],
        ],
        resize_keyboard=True,
    )


def ticket_kb(ticket_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Ответить", callback_data=f"ans:{ticket_id}"),
        InlineKeyboardButton(text="❌ Отказ", callback_data=f"rej:{ticket_id}"),
    ]])


def rating_kb(ticket_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍 Хорошо", callback_data=f"rate:like:{ticket_id}"),
        InlineKeyboardButton(text="👎 Плохо", callback_data=f"rate:dislike:{ticket_id}"),
    ]])


# ============================================================
#  ПОЛЬЗОВАТЕЛЬСКИЕ ХЕНДЛЕРЫ
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    remember_user(message)
    await state.clear()
    uid = message.from_user.id

    if is_owner(uid):
        mods_count = len(mods)
        open_count = sum(1 for t in tickets.values() if t["status"] == "open")
        await message.answer(
            f"👑 Панель владельца\n\n"
            f"👥 Модеров: {mods_count}\n"
            f"📩 Открытых тикетов: {open_count}\n\n"
            f"Команды:\n"
            f"/addmod @ник — назначить модера\n"
            f"/delmod @ник — снять модера\n"
            f"/mods — список модеров\n"
            f"/mute @ник 1h — мут\n"
            f"/unmute @ник — снять мут\n"
            f"/ban @ник — бан\n"
            f"/unban @ник — разбан\n"
            f"/stats — статистика",
            reply_markup=staff_menu(),
        )
    elif is_mod(uid):
        await message.answer(
            "🛡️ Меню модера\n\n"
            "Команды:\n"
            "/mute @ник 1h — замутить\n"
            "/unmute @ник — снять мут",
            reply_markup=staff_menu(),
        )
    else:
        await message.answer(
            "👋 Здравствуйте! Это бот поддержки.\n"
            "Нажмите кнопку ниже, чтобы задать вопрос.",
            reply_markup=user_menu(),
        )


@dp.message(F.text == "❓ Задать вопрос")
async def ask_question(message: Message, state: FSMContext):
    remember_user(message)
    uid = message.from_user.id

    p = get_punishment(uid)
    if p:
        if p["type"] == "ban":
            await message.answer("🚫 Вы заблокированы в боте поддержки.")
        else:
            left = fmt_duration(p["until"] - time.time())
            await message.answer(f"⛔ Вы временно не можете задавать вопросы.\nОсталось: {left}.")
        return

    # есть ли уже открытый вопрос от этого юзера?
    for t in tickets.values():
        if t["user_id"] == uid and t["status"] == "open":
            await message.answer(f"⏳ У вас уже есть открытый вопрос #{t['id']}.\nДождитесь ответа.")
            return

    await state.set_state(States.waiting_question)
    await message.answer("✍️ Напишите ваш вопрос одним сообщением.")


@dp.message(States.waiting_question)
async def receive_question(message: Message, state: FSMContext):
    global next_ticket_id
    remember_user(message)
    uid = message.from_user.id
    text = (message.text or "").strip()

    if not text:
        await message.answer("✍️ Пожалуйста, напишите вопрос текстом.")
        return

    tid = next_ticket_id
    next_ticket_id += 1

    tickets[str(tid)] = {
        "id": tid,
        "user_id": uid,
        "username": message.from_user.username or "",
        "question": text,
        "status": "open",
        "created": time.time(),
        "answered_by": None,
        "answer": None,
        "rating": None,
        "closed": None,
    }
    save_json(TICKETS_FILE, tickets)
    await state.clear()

    await message.answer("✅ Ваш вопрос отправлен в поддержку.\nОтвет придёт сюда, ожидайте.")

    # Рассылка стаффу
    preview = text if len(text) <= 500 else text[:500] + "…"
    staff_text = (
        f"📩 Новый вопрос #{tid}\n\n"
        f"👤 От: {display_name(uid)}\n"
        f"🆔 ID: {uid}\n\n"
        f"💬 Вопрос:\n«{preview}»"
    )
    targets = set(OWNER_IDS) | {int(x) for x in mods.keys()}
    for target in targets:
        try:
            await bot.send_message(target, staff_text, reply_markup=ticket_kb(tid))
        except Exception:
            pass


# ---------- Оценка ----------

@dp.callback_query(F.data.startswith("rate:"))
async def rate_handler(callback: CallbackQuery):
    _, kind, tid = callback.data.split(":")
    t = tickets.get(tid)
    if not t or t["status"] != "answered":
        await callback.answer("Оценка недоступна.", show_alert=True)
        return
    if callback.from_user.id != t["user_id"]:
        await callback.answer("Это не ваш тикет.", show_alert=True)
        return

    t["rating"] = "like" if kind == "like" else "dislike"
    t["status"] = "rated"
    t["closed"] = time.time()
    save_json(TICKETS_FILE, tickets)

    if t["answered_by"]:
        st = ensure_stats(t["answered_by"])
        if kind == "like":
            st["likes"] += 1
        else:
            st["dislikes"] += 1
        save_json(STATS_FILE, stats)

    await callback.message.edit_reply_markup(reply_markup=None)
    if kind == "like":
        await callback.message.answer("🙏 Спасибо за оценку! Рады помочь.")
    else:
        await callback.message.answer("😔 Спасибо за отзыв. Постараемся стать лучше.")
    await callback.answer()


# ============================================================
#  МОДЕР: ответ / отказ
# ============================================================

@dp.callback_query(F.data.startswith("ans:"))
async def answer_start(callback: CallbackQuery, state: FSMContext):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return

    tid = callback.data.split(":")[1]
    t = tickets.get(tid)
    if not t:
        await callback.answer("Тикет не найден.", show_alert=True)
        return
    if t["status"] != "open":
        await callback.answer(f"⛔ Вопрос #{tid} уже закрыт.", show_alert=True)
        return

    await state.update_data(answer_ticket=tid)
    await state.set_state(States.waiting_answer)
    await callback.message.answer(f"✍️ Напишите ответ для {display_name(t['user_id'])}:")
    await callback.answer()


@dp.message(States.waiting_answer)
async def answer_send(message: Message, state: FSMContext):
    if not is_staff(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    tid = data.get("answer_ticket")
    t = tickets.get(tid) if tid else None

    if not t or t["status"] != "open":
        await state.clear()
        await message.answer("⛔ Этот тикет уже закрыт.")
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("✍️ Пожалуйста, напишите ответ текстом.")
        return

    t["status"] = "answered"
    t["answered_by"] = message.from_user.id
    t["answer"] = text
    t["closed"] = time.time()
    save_json(TICKETS_FILE, tickets)

    st = ensure_stats(message.from_user.id)
    st["answered"] += 1
    save_json(STATS_FILE, stats)

    await state.clear()

    try:
        await bot.send_message(
            t["user_id"],
            f"💬 Ответ поддержки:\n\n«{text}»\n\nОцените работу поддержки:",
            reply_markup=rating_kb(int(tid)),
        )
        await message.answer(f"✅ Ответ отправлен {display_name(t['user_id'])}.\nВопрос #{tid} закрыт.")
    except Exception:
        await message.answer(f"⚠️ Не удалось отправить ответ пользователю #{tid}.")


@dp.callback_query(F.data.startswith("rej:"))
async def reject_handler(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return

    tid = callback.data.split(":")[1]
    t = tickets.get(tid)
    if not t:
        await callback.answer("Тикет не найден.", show_alert=True)
        return
    if t["status"] != "open":
        await callback.answer(f"⛔ Вопрос #{tid} уже закрыт.", show_alert=True)
        return

    t["status"] = "rejected"
    t["answered_by"] = callback.from_user.id
    t["closed"] = time.time()
    save_json(TICKETS_FILE, tickets)

    try:
        await bot.send_message(t["user_id"], "❌ К сожалению, ваш вопрос отклонён.")
    except Exception:
        pass

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ Пользователю {display_name(t['user_id'])} отправлен отказ.\nВопрос #{tid} закрыт.")
    await callback.answer()


# ============================================================
#  СПИСОК ТИКЕТОВ
# ============================================================

@dp.message(F.text == "📋 Активные тикеты")
async def show_tickets(message: Message):
    if not is_staff(message.from_user.id):
        return

    open_t = [t for t in tickets.values() if t["status"] == "open"]
    open_t.sort(key=lambda x: x["created"])

    if not open_t:
        await message.answer("📋 Открытых тикетов нет.")
        return

    lines = [f"📋 Открытые тикеты ({len(open_t)}):\n"]
    buttons = []
    for t in open_t[:15]:
        ago = fmt_duration(time.time() - t["created"])
        q = t["question"]
        q_short = q if len(q) <= 40 else q[:40] + "…"
        lines.append(f"🟡 #{t['id']} — {display_name(t['user_id'])}\n   «{q_short}»\n   ⏳ {ago} назад\n")
        buttons.append([InlineKeyboardButton(text=f"#{t['id']}", callback_data=f"view:{t['id']}")])

    if len(open_t) > 15:
        lines.append(f"…и ещё {len(open_t) - 15}. Показаны первые 15.")

    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("view:"))
async def view_ticket(callback: CallbackQuery):
    if not is_staff(callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return

    tid = callback.data.split(":")[1]
    t = tickets.get(tid)
    if not t:
        await callback.answer("Тикет не найден.", show_alert=True)
        return

    status_map = {"open": "🟡 Открыт", "answered": "🟢 Отвечен",
                  "rated": "⭐ Оценён", "rejected": "❌ Отклонён"}
    status = status_map.get(t["status"], t["status"])
    ago = fmt_duration(time.time() - t["created"])

    text = (
        f"📩 Тикет #{tid}\n\n"
        f"👤 {display_name(t['user_id'])}\n"
        f"🕐 Создан {ago} назад\n"
        f"📌 Статус: {status}\n\n"
        f"💬 «{t['question']}»"
    )
    if t.get("answered_by"):
        text += f"\n\n✍️ Ответил: {display_name(t['answered_by'])}"
    if t.get("answer"):
        text += f"\n\n💬 Ответ: «{t['answer']}»"
    if t.get("rating"):
        text += f"\n\n⭐ Оценка: {'👍' if t['rating'] == 'like' else '👎'}"

    kb = None
    if t["status"] == "open":
        kb = ticket_kb(int(tid))

    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


# ============================================================
#  ВЛАДЕЛЕЦ: команды
# ============================================================

def owner_only(func):
    async def wrapper(message: Message, *args, **kwargs):
        if not is_owner(message.from_user.id):
            await message.answer("⛔ Только владелец.")
            return
        return await func(message, *args, **kwargs)
    return wrapper


def staff_only(func):
    async def wrapper(message: Message, *args, **kwargs):
        if not is_staff(message.from_user.id):
            await message.answer("⛔ Нет доступа.")
            return
        return await func(message, *args, **kwargs)
    return wrapper


@dp.message(Command("addmod"))
@owner_only
async def cmd_addmod(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /addmod @ник или /addmod 123456789")
        return
    uid, err = resolve_target(parts[1].strip())
    if err:
        await message.answer(err)
        return
    if is_owner(uid):
        await message.answer("⚠️ Это владелец, он уже имеет полные права.")
        return
    if str(uid) in mods:
        await message.answer("⚠️ Уже модер.")
        return
    mods[str(uid)] = display_name(uid)
    save_json(MODS_FILE, mods)
    await message.answer(f"✅ {display_name(uid)} назначен модером.")


@dp.message(Command("delmod"))
@owner_only
async def cmd_delmod(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /delmod @ник или /delmod 123456789")
        return
    uid, err = resolve_target(parts[1].strip())
    if err:
        await message.answer(err)
        return
    if str(uid) not in mods:
        await message.answer("⚠️ Это не модер.")
        return
    mods.pop(str(uid))
    save_json(MODS_FILE, mods)
    await message.answer(f"✅ {display_name(uid)} снят с должности модера.")


@dp.message(Command("mods"))
@owner_only
async def cmd_mods(message: Message):
    if not mods:
        await message.answer("👥 Модеров нет.")
        return
    lines = [f"👥 Модеры ({len(mods)}):\n"]
    for uid_str, _ in mods.items():
        uid = int(uid_str)
        st = stats.get(uid_str, {"answered": 0, "likes": 0, "dislikes": 0})
        lines.append(f"• {display_name(uid)} — ответов: {st['answered']}, 👍 {st['likes']} / 👎 {st['dislikes']}")
    await message.answer("\n".join(lines))


@dp.message(Command("mute"))
@staff_only
async def cmd_mute(message: Message):
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Использование: /mute @ник 1h\nФорматы: 10m, 1h, 6h, 1d, 7d, 30d")
        return
    uid, err = resolve_target(parts[1])
    if err:
        await message.answer(err)
        return
    secs = parse_duration(parts[2])
    if secs is None:
        await message.answer("❌ Неверный формат времени. Примеры: 10m, 1h, 1d.")
        return
    if is_staff(uid) and not is_owner(message.from_user.id):
        await message.answer("⛔ Нельзя замутить модера или владельца.")
        return
    if is_owner(uid):
        await message.answer("⛔ Нельзя замутить владельца.")
        return
    if is_staff(uid) and is_owner(message.from_user.id):
        await message.answer("⛔ Нельзя замутить модера или владельца.")
        return

    punishments[str(uid)] = {"type": "mute", "until": time.time() + secs}
    save_json(PUNISH_FILE, punishments)
    await message.answer(f"✅ {display_name(uid)} замучен на {fmt_duration(secs)}.")


@dp.message(Command("unmute"))
@staff_only
async def cmd_unmute(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /unmute @ник")
        return
    uid, err = resolve_target(parts[1].strip())
    if err:
        await message.answer(err)
        return
    p = punishments.get(str(uid))
    if not p or p["type"] != "mute":
        await message.answer("⚠️ У этого пользователя нет мута.")
        return
    punishments.pop(str(uid))
    save_json(PUNISH_FILE, punishments)
    await message.answer(f"✅ Мут с {display_name(uid)} снят.")


@dp.message(Command("ban"))
@owner_only
async def cmd_ban(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /ban @ник")
        return
    uid, err = resolve_target(parts[1].strip())
    if err:
        await message.answer(err)
        return
    if is_owner(uid) or is_mod(uid):
        await message.answer("⛔ Нельзя забанить модера или владельца.")
        return
    punishments[str(uid)] = {"type": "ban"}
    save_json(PUNISH_FILE, punishments)
    await message.answer(f"✅ {display_name(uid)} забанен.")


@dp.message(Command("unban"))
@owner_only
async def cmd_unban(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /unban @ник")
        return
    uid, err = resolve_target(parts[1].strip())
    if err:
        await message.answer(err)
        return
    p = punishments.get(str(uid))
    if not p or p["type"] != "ban":
        await message.answer("⚠️ Этот пользователь не забанен.")
        return
    punishments.pop(str(uid))
    save_json(PUNISH_FILE, punishments)
    await message.answer(f"✅ {display_name(uid)} разбанен.")


@dp.message(Command("stats"))
@owner_only
async def cmd_stats(message: Message):
    lines = ["📊 Статистика:\n"]

    lines.append("👑 Владельцы:")
    for oid in OWNER_IDS:
        st = stats.get(str(oid), {"answered": 0, "likes": 0, "dislikes": 0})
        lines.append(f"  • {display_name(oid)} — ответов: {st['answered']}, 👍 {st['likes']} / 👎 {st['dislikes']}")

    if mods:
        lines.append("\n🛡️ Модеры:")
        for uid_str in mods:
            uid = int(uid_str)
            st = stats.get(uid_str, {"answered": 0, "likes": 0, "dislikes": 0})
            lines.append(f"  • {display_name(uid)} — ответов: {st['answered']}, 👍 {st['likes']} / 👎 {st['dislikes']}")

    total = len(tickets)
    answered = sum(1 for t in tickets.values() if t["status"] in ("answered", "rated"))
    rejected = sum(1 for t in tickets.values() if t["status"] == "rejected")
    rated = [t for t in tickets.values() if t["rating"]]
    likes = sum(1 for t in rated if t["rating"] == "like")

    lines.append(f"\n📩 Всего тикетов: {total}")
    lines.append(f"📬 Закрыто ответом: {answered}")
    lines.append(f"❌ Отклонено: {rejected}")
    if rated:
        lines.append(f"⭐ Положительных оценок: {likes}/{len(rated)} ({round(likes/len(rated)*100)}%)")

    await message.answer("\n".join(lines))


# ============================================================
#  ЗАПУСК
# ============================================================

async def periodic_cleanup():
    while True:
        await asyncio.sleep(3600)
        cleanup_old_tickets()


async def main():
    cleanup_old_tickets()
    asyncio.create_task(periodic_cleanup())
    print("🤖 Бот запущен. Ожидание сообщений...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
