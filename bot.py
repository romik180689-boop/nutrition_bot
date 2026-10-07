import asyncio
import os
import re
import sqlite3
from io import BytesIO
import uuid
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole
from aiohttp import web

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise Exception("BOT_TOKEN не задан!")

GIGACHAT_KEY = os.environ.get("GIGACHAT_KEY")
if not GIGACHAT_KEY:
    raise Exception("GIGACHAT_KEY не задан!")

DB_PATH = "nutrition.db"
# ===================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

giga = GigaChat(
    credentials=GIGACHAT_KEY,
    scope="GIGACHAT_API_PERS",
    model="GigaChat-2-Max",
    verify_ssl_certs=False,
)


# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            age INTEGER,
            gender TEXT,
            height REAL,
            weight REAL,
            target_weight REAL,
            goal TEXT,
            activity REAL DEFAULT 1.375,
            daily_calories REAL,
            daily_protein REAL,
            daily_fat REAL,
            daily_carbs REAL,
            water INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            food_name TEXT,
            calories REAL,
            protein REAL,
            fat REAL,
            carbs REAL,
            date TEXT DEFAULT (date('now')),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def get_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def create_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


def save_user(user_id, **kwargs):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [user_id]
    cur.execute(f"UPDATE users SET {fields} WHERE user_id = ?", values)
    conn.commit()
    conn.close()


def save_meal(user_id, food_name, cal, prot, fat, carbs):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO meals (user_id, food_name, calories, protein, fat, carbs) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, food_name, cal, prot, fat, carbs)
    )
    conn.commit()
    conn.close()


def get_today_totals(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(calories), 0), COALESCE(SUM(protein), 0), "
        "COALESCE(SUM(fat), 0), COALESCE(SUM(carbs), 0) "
        "FROM meals WHERE user_id = ? AND date = date('now')",
        (user_id,)
    )
    row = cur.fetchone()
    conn.close()
    return row


def get_today_meals(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT food_name, calories FROM meals "
        "WHERE user_id = ? AND date = date('now') "
        "ORDER BY created_at DESC LIMIT 20",
        (user_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ==================== РАСЧЁТ КАЛОРИЙ ====================
def calculate_calories(age, gender, height, weight, target_weight, activity):
    if gender == "male":
        bmr = 88.362 + (13.397 * weight) + (4.799 * height) - (5.677 * age)
    else:
        bmr = 447.593 + (9.247 * weight) + (3.098 * height) - (4.330 * age)

    tdee = bmr * activity

    if target_weight < weight:
        calories = tdee * 0.8
        protein_pct, fat_pct, carbs_pct = 0.35, 0.25, 0.40
    elif target_weight > weight:
        calories = tdee * 1.15
        protein_pct, fat_pct, carbs_pct = 0.25, 0.25, 0.50
    else:
        calories = tdee
        protein_pct, fat_pct, carbs_pct = 0.30, 0.30, 0.40

    if calories < 1200:
        calories = 1200

    protein = (calories * protein_pct) / 4
    fat = (calories * fat_pct) / 9
    carbs = (calories * carbs_pct) / 4

    return round(calories), round(protein), round(fat), round(carbs)


# ==================== УМНЫЙ АНАЛИЗ ЕДЫ ЧЕРЕЗ GIGACHAT ====================
def analyze_food_text(food_text):
    """Возвращает (cal, prot, fat, carbs) или None."""
    try:
        prompt = (
            f"Продукт/блюдо: {food_text}\n\n"
            "Посчитай КБЖУ. Ответь СТРОГО в формате (только числа):\n"
            "КАЛОРИИ: число\n"
            "БЕЛКИ: число\n"
            "ЖИРЫ: число\n"
            "УГЛЕВОДЫ: число"
        )
        payload = Chat(messages=[
            Messages(
                role=MessagesRole.SYSTEM,
                content=(
                    "Ты — нутрициолог. Считаешь КБЖУ продуктов. "
                    "Отвечай строго в указанном формате. Только числа."
                )
            ),
            Messages(role=MessagesRole.USER, content=prompt),
        ])
        resp = giga.chat(payload)
        answer = resp.choices[0].message.content

        cal = re.search(r'КАЛОРИИ:\s*(\d+(?:[.,]\d+)?)', answer, re.IGNORECASE)
        prot = re.search(r'БЕЛКИ:\s*(\d+(?:[.,]\d+)?)', answer, re.IGNORECASE)
        fat = re.search(r'ЖИРЫ:\s*(\d+(?:[.,]\d+)?)', answer, re.IGNORECASE)
        carbs = re.search(r'УГЛЕВОДЫ:\s*(\d+(?:[.,]\d+)?)', answer, re.IGNORECASE)

        if not cal:
            return None

        return (
            float(cal.group(1).replace(",", ".")),
            float(prot.group(1).replace(",", ".")) if prot else 0,
            float(fat.group(1).replace(",", ".")) if fat else 0,
            float(carbs.group(1).replace(",", ".")) if carbs else 0,
        )
    except Exception as e:
        print(f"Ошибка анализа еды: {e}")
        return None


# ==================== КЛАВИАТУРЫ ====================
def gender_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨 Мужской", callback_data="gender_male")],
        [InlineKeyboardButton(text="👩 Женский", callback_data="gender_female")],
    ])


def activity_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪑 Сидячий", callback_data="act_1.2")],
        [InlineKeyboardButton(text="🚶 1-3 тренировки", callback_data="act_1.375")],
        [InlineKeyboardButton(text="🏃 3-5 тренировок", callback_data="act_1.55")],
        [InlineKeyboardButton(text="💪 6-7 тренировок", callback_data="act_1.725")],
        [InlineKeyboardButton(text="🔥 Очень активный", callback_data="act_1.9")],
    ])


def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍽 Добавить еду текстом", callback_data="add_food")],
        [InlineKeyboardButton(text="📸 Добавить еду по фото", callback_data="add_food_photo")],
        [InlineKeyboardButton(text="📊 Мой день", callback_data="my_day")],
        [InlineKeyboardButton(text="💧 Вода", callback_data="water")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🗑 Сбросить профиль", callback_data="reset")],
    ])


# ==================== FSM ====================
class RegStates(StatesGroup):
    name = State()
    age = State()
    gender = State()
    height = State()
    weight = State()
    target_weight = State()
    activity = State()


class FoodStates(StatesGroup):
    food_name = State()
    food_photo = State()


# ==================== СТАРТ ====================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    create_user(message.from_user.id)
    user = get_user(message.from_user.id)

    if user and user[3]:
        await message.answer(
            "🌸 <b>С возвращением!</b>\n\n"
            "💕 Рада тебя видеть. Выбери действие:",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
    else:
        await message.answer(
            "🌸 <b>Привет! Я — твой трекер питания!</b>\n\n"
            "💕 Я помогу тебе питаться правильно, считать КБЖУ "
            "и достичь твоей цели — мягко и с заботой.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "❓ <b>Как тебя зовут?</b>",
            parse_mode="HTML"
        )
        await state.set_state(RegStates.name)


# ==================== РЕГИСТРАЦИЯ ====================
@dp.message(RegStates.name)
async def reg_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("❓ <b>Сколько тебе лет?</b>\n\nНапиши цифрой:", parse_mode="HTML")
    await state.set_state(RegStates.age)


@dp.message(RegStates.age)
async def reg_age(message: Message, state: FSMContext):
    try:
        age = int(message.text.strip())
        if age < 10 or age > 100:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введи возраст числом (10-100):")
        return
    await state.update_data(age=age)
    await message.answer("❓ <b>Укажи пол:</b>", parse_mode="HTML", reply_markup=gender_kb())
    await state.set_state(RegStates.gender)


@dp.callback_query(F.data.startswith("gender_"), RegStates.gender)
async def reg_gender(call: CallbackQuery, state: FSMContext):
    gender = call.data.replace("gender_", "")
    await state.update_data(gender=gender)
    await call.message.edit_text("❓ <b>Какой у тебя рост (см)?</b>", parse_mode="HTML")
    await state.set_state(RegStates.height)
    await call.answer()


@dp.message(RegStates.height)
async def reg_height(message: Message, state: FSMContext):
    try:
        height = float(message.text.strip().replace(",", "."))
        if height < 100 or height > 250:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введи рост числом (100-250 см):")
        return
    await state.update_data(height=height)
    await message.answer("❓ <b>Какой у тебя текущий вес (кг)?</b>", parse_mode="HTML")
    await state.set_state(RegStates.weight)


@dp.message(RegStates.weight)
async def reg_weight(message: Message, state: FSMContext):
    try:
        weight = float(message.text.strip().replace(",", "."))
        if weight < 30 or weight > 300:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введи вес числом (30-300 кг):")
        return
    await state.update_data(weight=weight)
    await message.answer("❓ <b>Какой вес хочешь достичь (кг)?</b>", parse_mode="HTML")
    await state.set_state(RegStates.target_weight)


@dp.message(RegStates.target_weight)
async def reg_target(message: Message, state: FSMContext):
    try:
        target = float(message.text.strip().replace(",", "."))
        if target < 30 or target > 300:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введи вес числом (30-300 кг):")
        return
    await state.update_data(target_weight=target)
    await message.answer(
        "❓ <b>Уровень активности:</b>",
        parse_mode="HTML",
        reply_markup=activity_kb()
    )
    await state.set_state(RegStates.activity)


@dp.callback_query(F.data.startswith("act_"), RegStates.activity)
async def reg_activity(call: CallbackQuery, state: FSMContext):
    activity = float(call.data.replace("act_", ""))
    await state.update_data(activity=activity)
    data = await state.get_data()

    cal, prot, fat, carbs = calculate_calories(
        data["age"], data["gender"], data["height"],
        data["weight"], data["target_weight"], activity
    )

    goal = "loss" if data["target_weight"] < data["weight"] else "gain" if data["target_weight"] > data["weight"] else "maintain"
    goal_ru = "похудение 💕" if goal == "loss" else "набор 💪" if goal == "gain" else "поддержание 🌸"

    save_user(
        call.from_user.id,
        name=data["name"], age=data["age"], gender=data["gender"],
        height=data["height"], weight=data["weight"],
        target_weight=data["target_weight"],
        goal=goal, activity=activity,
        daily_calories=cal, daily_protein=prot,
        daily_fat=fat, daily_carbs=carbs
    )

    await call.message.edit_text(
        f"🌸 <b>Профиль готов, {data['name']}!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 Цель: <b>{goal_ru}</b>\n"
        f"⚖️ Сейчас: <b>{data['weight']} кг</b>\n"
        f"🌷 Цель: <b>{data['target_weight']} кг</b>\n\n"
        f"🍽 <b>Твоя норма на день:</b>\n"
        f"🔥 Калории: <b>{cal}</b> ккал\n"
        f"🥩 Белки: <b>{prot}</b> г\n"
        f"🧈 Жиры: <b>{fat}</b> г\n"
        f"🍞 Углеводы: <b>{carbs}</b> г\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💕 Я рядом, чтобы поддержать. Начнём!",
        parse_mode="HTML",
        reply_markup=main_menu()
    )
    await state.clear()
    await call.answer()


# ==================== ПРОФИЛЬ ====================
@dp.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    user = get_user(call.from_user.id)
    if not user or not user[3]:
        await call.answer("Профиль не заполнен", show_alert=True)
        return

    text = (
        f"👤 <b>Твой профиль</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📛 Имя: {user[1]}\n"
        f"🎂 Возраст: {user[2]}\n"
        f"📏 Рост: {user[4]} см\n"
        f"⚖️ Вес: {user[5]} кг\n"
        f"🌷 Цель: {user[6]} кг\n\n"
        f"🍽 <b>Норма на день:</b>\n"
        f"🔥 {user[9]} ккал\n"
        f"🥩 {user[10]} г белка\n"
        f"🧈 {user[11]} г жиров\n"
        f"🍞 {user[12]} г углеводов"
    )
    await call.message.answer(text, parse_mode="HTML", reply_markup=main_menu())
    await call.answer()


# ==================== СБРОС ====================
@dp.callback_query(F.data == "reset")
async def cb_reset(call: CallbackQuery, state: FSMContext):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE user_id = ?", (call.from_user.id,))
    cur.execute("DELETE FROM meals WHERE user_id = ?", (call.from_user.id,))
    conn.commit()
    conn.close()

    await call.message.edit_text(
        "🗑 <b>Профиль сброшен.</b>\n\nНапиши /start чтобы начать заново.",
        parse_mode="HTML"
    )
    await call.answer()


# ==================== ДОБАВЛЕНИЕ ЕДЫ (ТЕКСТ) ====================
@dp.callback_query(F.data == "add_food")
async def cb_add_food(call: CallbackQuery, state: FSMContext):
    await call.message.answer(
        "🍽 <b>Что ты сегодня съела?</b>\n\n"
        "💡 Напиши название и вес, например:\n"
        "  • «Овсянка 200 г»\n"
        "  • «Куриная грудка 150 г»\n"
        "  • «Салат с авокадо 250 г»\n\n"
        "❌ Отмена — /cancel",
        parse_mode="HTML"
    )
    await state.set_state(FoodStates.food_name)
    await call.answer()


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено.", reply_markup=main_menu())


@dp.message(FoodStates.food_name)
async def process_food(message: Message, state: FSMContext):
    food_text = message.text.strip()
    if len(food_text) < 2:
        await message.answer("❌ Слишком коротко. Напиши ещё раз:")
        return

    await state.clear()

    status = await message.answer(
        "🤔 <b>Считаю калории...</b>\n\n⏳ Секундочку...",
        parse_mode="HTML"
    )

    result = analyze_food_text(food_text)

    if not result:
        await status.delete()
        await message.answer(
            "😔 Не смогла распознать КБЖУ. Попробуй переписать подробнее:\n"
            "«Овсянка на молоке 200 г»",
            reply_markup=main_menu()
        )
        return

    cal, prot, fat, carbs = result
    save_meal(message.from_user.id, food_text, cal, prot, fat, carbs)

    await status.delete()
    await message.answer(
        f"✅ <b>Записано!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🍽 {food_text}\n\n"
        f"🔥 Калории: <b>{round(cal)}</b> ккал\n"
        f"🥩 Белки: <b>{round(prot)}</b> г\n"
        f"🧈 Жиры: <b>{round(fat)}</b> г\n"
        f"🍞 Углеводы: <b>{round(carbs)}</b> г\n\n"
        f"🌸 Продолжай в том же духе!",
        parse_mode="HTML",
        reply_markup=main_menu()
    )

    await check_daily_limit(message.from_user.id, message)


# ==================== ДОБАВЛЕНИЕ ЕДЫ (ФОТО) ====================
@dp.callback_query(F.data == "add_food_photo")
async def cb_add_food_photo(call: CallbackQuery, state: FSMContext):
    await call.message.answer(
        "📸 <b>Отправь фото еды</b>\n\n"
        "💡 Я распознаю, что на фото, и посчитаю КБЖУ.\n\n"
        "❌ Отмена — /cancel",
        parse_mode="HTML"
    )
    await state.set_state(FoodStates.food_photo)
    await call.answer()


@dp.message(FoodStates.food_photo, F.photo)
async def process_food_photo(message: Message, state: FSMContext):
    await state.clear()

    status = await message.answer(
        "📸 <b>Смотрю на фото...</b>\n\n"
        "🧠 Распознаю блюдо\n"
        "⏳ Секундочку...",
        parse_mode="HTML"
    )

    try:
        # Скачиваем фото
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        file_bytes = BytesIO()
        await bot.download_file(file_info.file_path, file_bytes)
        file_bytes.seek(0)

        # Сохраняем во временный файл
        img_filename = f"/tmp/{uuid.uuid4()}.jpg"
        with open(img_filename, "wb") as f:
            f.write(file_bytes.read())

        # Загружаем в GigaChat
        with open(img_filename, "rb") as f:
            uploaded = giga.upload_file(f)

        # Просим GigaChat распознать еду и посчитать КБЖУ
        user_text = (
            "Внимательно посмотри на фото. Что это за еда/блюдо? "
            "Определи примерный вес порции и посчитай КБЖУ.\n\n"
            "Ответь СТРОГО в формате:\n"
            "БЛЮДО: название\n"
            "ВЕС: число г\n"
            "КАЛОРИИ: число\n"
            "БЕЛКИ: число\n"
            "ЖИРЫ: число\n"
            "УГЛЕВОДЫ: число\n\n"
            "Только указанные строки, без пояснений."
        )

        payload = Chat(messages=[
            Messages(
                role=MessagesRole.SYSTEM,
                content=(
                    "Ты — нутрициолог. Распознаёшь еду по фото и считаешь КБЖУ. "
                    "Отвечай строго в указанном формате."
                )
            ),
            Messages(
                role=MessagesRole.USER,
                content=user_text,
                attachments=[uploaded.id_]
            ),
        ])

        response = giga.chat(payload)
        answer = response.choices[0].message.content

        # Парсим
        food_match = re.search(r'БЛЮДО:\s*(.+)', answer, re.IGNORECASE)
        weight_match = re.search(r'ВЕС:\s*(\d+(?:[.,]\d+)?)', answer, re.IGNORECASE)
        cal_match = re.search(r'КАЛОРИИ:\s*(\d+(?:[.,]\d+)?)', answer, re.IGNORECASE)
        prot_match = re.search(r'БЕЛКИ:\s*(\d+(?:[.,]\d+)?)', answer, re.IGNORECASE)
        fat_match = re.search(r'ЖИРЫ:\s*(\d+(?:[.,]\d+)?)', answer, re.IGNORECASE)
        carbs_match = re.search(r'УГЛЕВОДЫ:\s*(\d+(?:[.,]\d+)?)', answer, re.IGNORECASE)

        if not cal_match:
            await status.delete()
            await message.answer(
                "😔 Не смогла распознать еду на фото.\n\n"
                "💡 Попробуй сфотографировать ближе и лучше осветить.",
                reply_markup=main_menu()
            )
            # Удаляем временный файл
            try:
                os.remove(img_filename)
            except Exception:
                pass
            return

        food_name = food_match.group(1).strip() if food_match else "Блюдо с фото"
        weight = weight_match.group(1) if weight_match else "?"
        cal = float(cal_match.group(1).replace(",", "."))
        prot = float(prot_match.group(1).replace(",", ".")) if prot_match else 0
        fat = float(fat_match.group(1).replace(",", ".")) if fat_match else 0
        carbs = float(carbs_match.group(1).replace(",", ".")) if carbs_match else 0

        # Сохраняем
        food_text = f"{food_name} ({weight} г)"
        save_meal(message.from_user.id, food_text, cal, prot, fat, carbs)

        await status.delete()
        await message.answer(
            f"✅ <b>Распознала блюдо!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🍽 <b>{food_name}</b>\n"
            f"⚖️ Вес: ~{weight} г\n\n"
            f"🔥 Калории: <b>{round(cal)}</b> ккал\n"
            f"🥩 Белки: <b>{round(prot)}</b> г\n"
            f"🧈 Жиры: <b>{round(fat)}</b> г\n"
            f"🍞 Углеводы: <b>{round(carbs)}</b> г\n\n"
            f"🌸 Записала! Продолжай в том же духе!",
            parse_mode="HTML",
            reply_markup=main_menu()
        )

        await check_daily_limit(message.from_user.id, message)

        # Удаляем временный файл
        try:
            os.remove(img_filename)
        except Exception:
            pass

    except Exception as e:
        await status.delete()
        await message.answer(
            f"😔 <b>Ошибка</b>\n\n<code>{e}</code>",
            parse_mode="HTML",
            reply_markup=main_menu()
        )


# ==================== ПРОВЕРКА НОРМЫ ====================
async def check_daily_limit(user_id, message):
    total_cal, total_prot, total_fat, total_carbs = get_today_totals(user_id)

    user = get_user(user_id)
    if not user or not user[9]:
        return

    daily_cal = user[9]
    percent = (total_cal / daily_cal) * 100

    if percent >= 100:
        await message.answer(
            f"⚠️ <b>Ты достигла дневной нормы!</b>\n\n"
            f"🔥 Съедено: {round(total_cal)} / {daily_cal} ккал\n\n"
            f"🌸 Не переживай — завтра новый день! "
            f"Вечером выбери что-то лёгкое.",
            parse_mode="HTML"
        )
    elif percent >= 90:
        await message.answer(
            f"💡 <b>Ты близка к норме</b> ({round(percent)}%)\n\n"
            f"🔥 Съедено: {round(total_cal)} / {daily_cal} ккал\n\n"
            f"🌸 Осталось немного!",
            parse_mode="HTML"
        )


# ==================== МОЙ ДЕНЬ ====================
@dp.callback_query(F.data == "my_day")
async def cb_my_day(call: CallbackQuery):
    user = get_user(call.from_user.id)
    if not user or not user[9]:
        await call.answer("Сначала заполни профиль: /start", show_alert=True)
        return

    total_cal, total_prot, total_fat, total_carbs = get_today_totals(call.from_user.id)
    meals_list = get_today_meals(call.from_user.id)

    daily_cal = user[9]
    percent = min((total_cal / daily_cal) * 100, 100)
    filled = int(percent / 10)
    bar = "🟩" * filled + "⬜" * (10 - filled)

    text = (
        f"📊 <b>Твой день</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔥 <b>Калории:</b>\n"
        f"{bar} {round(percent)}%\n"
        f"<b>{round(total_cal)}</b> / {daily_cal} ккал\n\n"
        f"🥩 Белки: <b>{round(total_prot)}</b> / {user[10]} г\n"
        f"🧈 Жиры: <b>{round(total_fat)}</b> / {user[11]} г\n"
        f"🍞 Углеводы: <b>{round(total_carbs)}</b> / {user[12]} г\n\n"
    )

    if meals_list:
        text += "━━━━━━━━━━━━━━━━━━━━━━\n🍽 <b>Что съедено сегодня:</b>\n\n"
        for food_name, cal in meals_list[:10]:
            text += f"  • {food_name} — {round(cal)} ккал\n"
    else:
        text += "🍽 <i>Сегодня ещё ничего не записано.</i>\n"

    if percent < 50:
        text += "\n🌸 Ты молодчина, продолжай!"
    elif percent < 80:
        text += "\n🌸 Хороший темп, держись!"
    elif percent < 100:
        text += "\n🌸 Почти у цели на сегодня!"
    else:
        text += "\n🌸 Дневная норма достигнута. Отдыхай!"

    await call.message.answer(text, parse_mode="HTML", reply_markup=main_menu())
    await call.answer()


# ==================== ВОДА ====================
@dp.callback_query(F.data == "water")
async def cb_water(call: CallbackQuery):
    user = get_user(call.from_user.id)
    if not user or not user[9]:
        await call.answer("Сначала заполни профиль: /start", show_alert=True)
        return

    water = (user[13] or 0) + 1
    save_user(call.from_user.id, water=water)

    glasses = "💧" * min(water, 8)
    text = (
        f"💧 <b>Вода за сегодня</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{glasses}\n\n"
        f"Выпито: <b>{water}</b> стаканов\n"
        f"Цель: <b>8</b> стаканов\n\n"
        f"🌸 Продолжай пить водичку!"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Ещё стакан", callback_data="water")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu")],
    ])
    await call.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "back_menu")
async def cb_back_menu(call: CallbackQuery):
    await call.message.answer("🌸 Главное меню:", reply_markup=main_menu())
    await call.answer()


# ==================== ВЕБ-СЕРВЕР ДЛЯ RENDER ====================
async def handle(request):
    return web.Response(text="Nutrition Bot is alive!")


async def start_web():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
    await site.start()


# ==================== ЗАПУСК ====================
async def main():
    init_db()
    print("🌸 Nutrition Bot запущен...")
    asyncio.create_task(start_web())
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
