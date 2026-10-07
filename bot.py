# ==================== ДОБАВЛЕНИЕ ЕДЫ ====================
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
        "🤔 <b>Считаю калории...</b>\n\n⏳ Это займёт пару секунд",
        parse_mode="HTML"
    )

    try:
        # GigaChat считает КБЖУ
        from gigachat.models import Chat, Messages, MessagesRole

        prompt = (
            f"Продукт/блюдо: {food_text}\n\n"
            "Посчитай КБЖУ для указанного количества (или для 100 г, если вес не указан). "
            "Ответь СТРОГО в формате:\n"
            "КАЛОРИИ: число\n"
            "БЕЛКИ: число\n"
            "ЖИРЫ: число\n"
            "УГЛЕВОДЫ: число\n\n"
            "Только числа, без пояснений!"
        )

        payload = Chat(messages=[
            Messages(
                role=MessagesRole.SYSTEM,
                content=(
                    "Ты — нутрициолог. Считаешь КБЖУ продуктов и блюд. "
                    "Отвечай строго в указанном формате. "
                    "Только числа, без букв и пояснений."
                )
            ),
            Messages(role=MessagesRole.USER, content=prompt),
        ])

        response = giga.chat(payload)
        answer = response.choices[0].message.content

        # Парсим ответ
        import re
        cal = re.search(r'КАЛОРИИ:\s*(\d+(?:[.,]\d+)?)', answer, re.IGNORECASE)
        prot = re.search(r'БЕЛКИ:\s*(\d+(?:[.,]\d+)?)', answer, re.IGNORECASE)
        fat = re.search(r'ЖИРЫ:\s*(\d+(?:[.,]\d+)?)', answer, re.IGNORECASE)
        carbs = re.search(r'УГЛЕВОДЫ:\s*(\d+(?:[.,]\d+)?)', answer, re.IGNORECASE)

        if not cal:
            await status.delete()
            await message.answer(
                "😔 Не смогла распознать КБЖУ. Попробуй переписать подробнее:\n"
                "«Овсянка на молоке 200 г»",
                reply_markup=main_menu()
            )
            return

        cal_val = float(cal.group(1).replace(",", "."))
        prot_val = float(prot.group(1).replace(",", ".")) if prot else 0
        fat_val = float(fat.group(1).replace(",", ".")) if fat else 0
        carbs_val = float(carbs.group(1).replace(",", ".")) if carbs else 0

        # Сохраняем в БД
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO meals (user_id, food_name, calories, protein, fat, carbs) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message.from_user.id, food_text, cal_val, prot_val, fat_val, carbs_val)
        )
        conn.commit()
        conn.close()

        await status.delete()

        # Красивый ответ
        await message.answer(
            f"✅ <b>Записано!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🍽 {food_text}\n\n"
            f"🔥 Калории: <b>{round(cal_val)}</b> ккал\n"
            f"🥩 Белки: <b>{round(prot_val)}</b> г\n"
            f"🧈 Жиры: <b>{round(fat_val)}</b> г\n"
            f"🍞 Углеводы: <b>{round(carbs_val)}</b> г\n\n"
            f"🌸 Продолжай в том же духе!",
            parse_mode="HTML",
            reply_markup=main_menu()
        )

        # Проверяем, не превысила ли норму
        await check_daily_limit(message.from_user.id, message)

    except Exception as e:
        await status.delete()
        await message.answer(
            f"😔 <b>Ошибка</b>\n\n<code>{e}</code>",
            parse_mode="HTML",
            reply_markup=main_menu()
        )


# ==================== ПРОВЕРКА НОРМЫ ====================
async def check_daily_limit(user_id, message):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(calories), 0), COALESCE(SUM(protein), 0), "
        "COALESCE(SUM(fat), 0), COALESCE(SUM(carbs), 0) "
        "FROM meals WHERE user_id = ? AND date = date('now')",
        (user_id,)
    )
    total_cal, total_prot, total_fat, total_carbs = cur.fetchone()
    conn.close()

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
            f"Постарайся вечером выбрать что-то лёгкое.",
            parse_mode="HTML"
        )
    elif percent >= 90:
        await message.answer(
            f"💡 <b>Ты близка к норме</b> ({round(percent)}%)\n\n"
            f"🔥 Съедено: {round(total_cal)} / {daily_cal} ккал\n\n"
            f"🌸 Осталось немного — выбирай аккуратно!",
            parse_mode="HTML"
        )


# ==================== МОЙ ДЕНЬ ====================
@dp.callback_query(F.data == "my_day")
async def cb_my_day(call: CallbackQuery):
    user = get_user(call.from_user.id)
    if not user or not user[9]:
        await call.answer("Сначала заполни профиль: /start", show_alert=True)
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(calories), 0), COALESCE(SUM(protein), 0), "
        "COALESCE(SUM(fat), 0), COALESCE(SUM(carbs), 0) "
        "FROM meals WHERE user_id = ? AND date = date('now')",
        (call.from_user.id,)
    )
    total_cal, total_prot, total_fat, total_carbs = cur.fetchone()

    cur.execute(
        "SELECT food_name, calories FROM meals "
        "WHERE user_id = ? AND date = date('now') "
        "ORDER BY created_at DESC LIMIT 10",
        (call.from_user.id,)
    )
    meals_list = cur.fetchall()
    conn.close()

    # Прогресс-бар
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
        for food_name, cal in meals_list:
            text += f"  • {food_name} — {round(cal)} ккал\n"
    else:
        text += "🍽 <i>Сегодня ещё ничего не записано.</i>\n"

    # Мотивация
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