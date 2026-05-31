import asyncio
import json
import logging
import os
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
_group_id_env = os.getenv("GROUP_ID", "")
GROUP_ID = int(_group_id_env) if _group_id_env.lstrip('-').isdigit() else _group_id_env
THREAD_ID = int(os.getenv("THREAD_ID")) if os.getenv("THREAD_ID") else None
DATA_FILE = os.path.join(os.path.dirname(__file__), "storage.json")
# =============================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

pending_users = set()
announcement_index = {}

storage = {
    "rooms": [],
    "announcements": []
}


def get_main_menu(user_id: int = None) -> types.ReplyKeyboardMarkup:
    buttons = [
        [types.KeyboardButton(text="🏠 Комнаты")],
        [types.KeyboardButton(text="📬 Мои объявления")],
    ]
    if user_id in ADMIN_IDS:
        buttons.append([types.KeyboardButton(text="🛠 Управление комнатами"), types.KeyboardButton(text="📖 Инструкция")])
    buttons.append([types.KeyboardButton(text="� Главное меню")])
    return types.ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=False)


def load_storage():
    global storage
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                storage = json.load(f)
        except Exception as e:
            logging.error(f"Ошибка загрузки storage.json: {e}")
            storage = {"rooms": [], "announcements": []}
    else:
        save_storage()
        
    # Инициализация списка групп для рассылки (сохраняем старую группу из .env для совместимости)
    if "publish_groups" not in storage:
        storage["publish_groups"] = []
        if GROUP_ID:
            storage["publish_groups"].append({
                "chat_id": GROUP_ID,
                "thread_id": THREAD_ID,
                "name": "Основная группа (из .env)"
            })
        save_storage()


def save_storage():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(storage, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Ошибка сохранения storage.json: {e}")


def normalize_room(name: str) -> str:
    return name.strip()


def get_user_room_keyboard(announcement_id: int):
    builder = InlineKeyboardBuilder()
    for room_name in storage["rooms"]:
        callback = f"userselectroom_{announcement_id}_{room_name}"
        builder.button(text=room_name, callback_data=callback)
    builder.button(text="❌ Отменить", callback_data=f"usercancel_{announcement_id}")
    builder.adjust(1)
    return builder.as_markup()


def format_room_list() -> str:
    if not storage["rooms"]:
        return "Пока нет комнат. Админ может создать комнату командой /newroom Название"
    lines = ["💬 Доступные комнаты:"]
    for idx, room_name in enumerate(storage["rooms"], 1):
        count = sum(1 for item in storage["announcements"] if item["status"] == "approved" and item["room"] == room_name)
        lines.append(f"{idx}. {room_name} — {count} объявлений")
    return "\n".join(lines)


def format_announcement(item: dict) -> str:
    created = item.get("created_at", "—")
    owner = item.get("username", f"ID:{item.get('user_id')}")
    status = item.get("status", "unknown")
    room = item.get("room", "—")
    return (
        f"📌 ID: {item['id']}\n"
        f"👤 От: {owner}\n"
        f"🕒 Добавлено: {created}\n"
        f"🏷️ Статус: {status}\n"
        f"📂 Комната: {room}\n"
        f"Описание: {item['caption']}"
    )


def find_announcement_by_id(announcement_id: int):
    for item in storage["announcements"]:
        if item["id"] == announcement_id:
            return item
    return None


def build_admin_caption(message: types.Message) -> str:
    username = f"@{message.from_user.username}" if message.from_user.username else f"ID: {message.from_user.id}"
    return f"Новый лот от {username}:\n\n{message.caption}"


@dp.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: types.Message):
    if message.from_user.id in pending_users:
        await message.answer("Пожалуйста, заверши текущую заявку (выбери комнату) или дождись решения администратора.", reply_markup=get_main_menu(message.from_user.id))
    else:
        await message.answer(
            "Привет! Отправь мне фото и описание своего объявления одним сообщением. "
            "Ты сможешь выбрать комнату для публикации. После одобрения админом оно появится в этой комнате.",
            reply_markup=get_main_menu(message.from_user.id)
        )


@dp.message(F.text == "🔄 Главное меню", F.chat.type == "private")
async def main_menu(message: types.Message):
    await start_cmd(message)


@dp.message(F.text == "📖 Инструкция", F.chat.type == "private")
async def admin_instruction(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
        
    file_path = os.path.join(os.path.dirname(__file__), "admin_guide.txt")
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()
            await message.answer(text)
        except Exception as e:
            logging.error(f"Ошибка при чтении инструкции: {e}")
            await message.answer("Произошла ошибка при загрузке инструкции.")
    else:
        await message.answer("Файл инструкции (admin_guide.txt) не найден.")


@dp.message(F.text == "🛠 Управление комнатами", F.chat.type == "private")
async def prompt_room_management(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Только администратор может управлять комнатами.", reply_markup=get_main_menu(message.from_user.id))
        return
    await message.answer(
        "<b>Доступные команды:</b>\n"
        "➕ <b>Создать:</b> <code>/newroom Название</code>\n"
        "✏️ <b>Изменить:</b> <code>/editroom Старое_имя | Новое_имя</code>\n"
        "❌ <b>Удалить:</b> <code>/delroom Название</code>\n\n"
        "📢 <b>Публикация:</b>\n"
        "📋 <b>Список чатов:</b> <code>/groups</code>\n"
        "➕ <b>Добавить чат:</b> напиши <code>/addgroup</code> в самой группе ИЛИ отправь боту <code>/addgroup ID_чата Название</code>",
        parse_mode="HTML",
        reply_markup=get_main_menu(message.from_user.id)
    )


@dp.message(F.text.startswith("/newroom"), F.chat.type == "private")
async def add_room(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Только администратор может создавать комнаты.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: /newroom Название_комнаты", reply_markup=get_main_menu(message.from_user.id))
        return

    room_name = normalize_room(parts[1])
    if not room_name:
        await message.answer("Название комнаты не может быть пустым.")
        return

    if room_name in storage["rooms"]:
        await message.answer(f"Комната '{room_name}' уже существует.", reply_markup=get_main_menu(message.from_user.id))
        return

    storage["rooms"].append(room_name)
    save_storage()
    await message.answer(f"Комната '{room_name}' успешно создана.", reply_markup=get_main_menu(message.from_user.id))


@dp.message(F.text.startswith("/editroom"), F.chat.type == "private")
async def edit_room(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Только администратор может редактировать комнаты.")
        return

    text_without_cmd = message.text[len("/editroom"):].strip()
    parts = [p.strip() for p in text_without_cmd.split("|")]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        await message.answer("Использование: /editroom Старое_название | Новое_название\n(например: /editroom Авто | Автомобили)", reply_markup=get_main_menu(message.from_user.id))
        return

    old_name, new_name = parts[0], parts[1]

    if old_name not in storage["rooms"]:
        await message.answer(f"Комната '{old_name}' не найдена.", reply_markup=get_main_menu(message.from_user.id))
        return

    if new_name in storage["rooms"]:
        await message.answer(f"Комната '{new_name}' уже существует.", reply_markup=get_main_menu(message.from_user.id))
        return

    idx = storage["rooms"].index(old_name)
    storage["rooms"][idx] = new_name

    for ann in storage["announcements"]:
        if ann.get("room") == old_name:
            ann["room"] = new_name

    save_storage()
    await message.answer(f"Комната '{old_name}' успешно переименована в '{new_name}'.", reply_markup=get_main_menu(message.from_user.id))


@dp.message(F.text.startswith("/delroom"), F.chat.type == "private")
async def del_room(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Только администратор может удалять комнаты.")
        return

    room_name = message.text[len("/delroom"):].strip()
    if not room_name:
        await message.answer("Использование: /delroom Название_комнаты", reply_markup=get_main_menu(message.from_user.id))
        return

    if room_name not in storage["rooms"]:
        await message.answer(f"Комната '{room_name}' не найдена.", reply_markup=get_main_menu(message.from_user.id))
        return

    storage["rooms"].remove(room_name)
    save_storage()
    await message.answer(f"Комната '{room_name}' успешно удалена.", reply_markup=get_main_menu(message.from_user.id))


@dp.message(F.text.startswith("/addgroup"))
async def add_publish_group(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    parts = message.text.split(maxsplit=2)
    
    if message.chat.type == "private":
        if len(parts) < 2:
            await message.answer(
                "Чтобы добавить канал или группу через личные сообщения, используй формат:\n"
                "<code>/addgroup -1001234567890 Название</code>\n\n"
                "Либо просто добавь бота в саму группу и напиши там <code>/addgroup</code>",
                parse_mode="HTML"
            )
            return
        try:
            chat_id = int(parts[1])
        except ValueError:
            await message.answer("ID чата/канала должен быть числом (обычно начинается с -100).")
            return
        thread_id = None
        name = parts[2] if len(parts) > 2 else f"Чат {chat_id}"
    else:
        chat_id = message.chat.id
        thread_id = message.message_thread_id
        name = parts[1] if len(parts) > 1 else message.chat.title or "Группа без названия"

    storage.setdefault("publish_groups", [])
    for g in storage["publish_groups"]:
        if g["chat_id"] == chat_id and g.get("thread_id") == thread_id:
            await message.answer("Этот чат (или ветка) уже добавлен в список для публикаций.")
            return

    storage["publish_groups"].append({
        "chat_id": chat_id,
        "thread_id": thread_id,
        "name": name
    })
    save_storage()
    await message.answer(f"✅ Чат '{name}' успешно добавлен для публикаций!")


@dp.message(F.text.startswith("/delgroup"))
async def del_publish_group(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id

    if message.chat.type == "private":
        parts = message.text.split()
        if len(parts) == 2 and parts[1].isdigit():
            idx = int(parts[1]) - 1
            groups = storage.get("publish_groups", [])
            if 0 <= idx < len(groups):
                removed = groups.pop(idx)
                save_storage()
                await message.answer(f"✅ Группа '{removed['name']}' удалена из рассылки.")
            else:
                await message.answer("Неверный номер группы.")
        else:
            await message.answer("В личных сообщениях используй: /delgroup <номер_из_списка>\nИли напиши /delgroup прямо в группе.")
        return

    groups = storage.get("publish_groups", [])
    for g in groups:
        if g["chat_id"] == chat_id and g.get("thread_id") == thread_id:
            groups.remove(g)
            save_storage()
            await message.answer("✅ Эта группа удалена из списка для публикаций.")
            return

    await message.answer("Эта группа не найдена в списке для публикаций.")


@dp.message(F.text == "/groups", F.chat.type == "private")
async def list_publish_groups(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    groups = storage.get("publish_groups", [])
    if not groups:
        await message.answer("Список групп для публикаций пуст.")
        return

    builder = InlineKeyboardBuilder()
    for i, g in enumerate(groups):
        thread_info = f" (Ветка: {g.get('thread_id')})" if g.get("thread_id") else ""
        builder.button(text=f"❌ Удалить {g.get('name')}{thread_info}", callback_data=f"delgroup_{i}")
    builder.adjust(1)

    await message.answer(
        "📢 <b>Группы для публикаций:</b>\nНажми на кнопку, чтобы удалить группу из рассылки.",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data.startswith("delgroup_"))
async def callback_del_group(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Только администратор может удалять группы.", show_alert=True)
        return

    idx = int(callback.data.split("_")[1])
    groups = storage.get("publish_groups", [])
    
    if 0 <= idx < len(groups):
        removed = groups.pop(idx)
        save_storage()
        await callback.answer(f"Группа '{removed['name']}' удалена.", show_alert=True)
        
        # Обновляем сообщение, если удалили не последнюю группу, иначе пишем, что список пуст
        if not groups:
            await callback.message.edit_text("Список групп для публикаций пуст.")
        else:
            builder = InlineKeyboardBuilder()
            for i, g in enumerate(groups):
                thread_info = f" (Ветка: {g.get('thread_id')})" if g.get("thread_id") else ""
                builder.button(text=f"❌ Удалить {g.get('name')}{thread_info}", callback_data=f"delgroup_{i}")
            builder.adjust(1)
            await callback.message.edit_text(
                "📢 <b>Группы для публикаций:</b>\nНажми на кнопку, чтобы удалить группу из рассылки.",
                parse_mode="HTML",
                reply_markup=builder.as_markup()
            )
    else:
        await callback.answer("Группа не найдена или уже удалена.", show_alert=True)


@dp.message((F.text == "/rooms") | (F.text == "🏠 Комнаты"), F.chat.type == "private")
async def list_rooms(message: types.Message):
    builder = InlineKeyboardBuilder()
    if storage["rooms"]:
        for room_name in storage["rooms"]:
            builder.button(text=room_name, callback_data=f"openroom:{room_name}")
        builder.adjust(1)
        await message.answer(format_room_list(), reply_markup=builder.as_markup())
    else:
        await message.answer(format_room_list(), reply_markup=get_main_menu(message.from_user.id))


@dp.message(F.text.startswith("/room"), F.chat.type == "private")
async def show_room(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: /room Название_комнаты", reply_markup=get_main_menu(message.from_user.id))
        return

    room_name = normalize_room(parts[1])
    if room_name not in storage["rooms"]:
        await message.answer(f"Комната '{room_name}' не найдена. Используй /rooms для списка комнат.", reply_markup=get_main_menu(message.from_user.id))
        return

    items = [item for item in storage["announcements"] if item["status"] == "approved" and item["room"] == room_name]
    if not items:
        await message.answer(f"В комнате '{room_name}' пока нет одобренных объявлений.")
        return

    await message.answer(f"Объявления в комнате '{room_name}':")
    for item in items:
        await bot.send_photo(
            chat_id=message.from_user.id,
            photo=item["photo_file_id"],
            caption=format_announcement(item),
            parse_mode="HTML"
        )


@dp.message((F.text == "/myads") | (F.text == "📬 Мои объявления"), F.chat.type == "private")
async def my_ads(message: types.Message):
    user_id = message.from_user.id
    user_items = [item for item in storage["announcements"] if item["user_id"] == user_id]
    if not user_items:
        await message.answer("У тебя еще нет объявлений.", reply_markup=get_main_menu(user_id))
        return

    lines = ["Твои объявления:"]
    for item in user_items:
        room = item.get("room", "—")
        lines.append(f"ID {item['id']}: {item['status']} в комнате {room}")
    await message.answer("\n".join(lines), reply_markup=get_main_menu(user_id))


@dp.callback_query(F.data.startswith("openroom:"))
async def open_room(callback: types.CallbackQuery):
    room_name = callback.data.split(":", 1)[1]
    if room_name not in storage["rooms"]:
        await callback.answer("Комната не найдена.", show_alert=True)
        return

    items = [item for item in storage["announcements"] if item["status"] == "approved" and item["room"] == room_name]
    if not items:
        await callback.answer(f"В комнате '{room_name}' пока нет одобренных объявлений.")
        return

    await callback.answer()
    await bot.send_message(callback.from_user.id, f"Объявления в комнате '{room_name}':")
    for item in items:
        await bot.send_photo(
            chat_id=callback.from_user.id,
            photo=item["photo_file_id"],
            caption=format_announcement(item),
            parse_mode="HTML"
        )


@dp.message(F.photo & F.caption, F.chat.type == "private")
async def handle_lot_submission(message: types.Message):
    user_id = message.from_user.id
    if user_id in pending_users:
        await message.answer("Пожалуйста, заверши текущую заявку (выбери комнату) или дождись решения администратора.")
        return

    if not storage["rooms"]:
        await message.answer("В данный момент нет доступных комнат для публикации.")
        return

    announcement_id = len(storage["announcements"]) + 1
    caption = message.caption.strip()
    username = f"@{message.from_user.username}" if message.from_user.username else f"ID: {user_id}"

    announcement = {
        "id": announcement_id,
        "user_id": user_id,
        "username": username,
        "photo_file_id": message.photo[-1].file_id,
        "caption": caption,
        "status": "draft",
        "room": None,
        "created_at": datetime.utcnow().isoformat(sep=" ", timespec="seconds"),
        "approved_at": None,
        "admin_id": None
    }

    storage["announcements"].append(announcement)
    save_storage()
    pending_users.add(user_id)
    announcement_index[user_id] = announcement_id

    await message.answer(
        "Выбери комнату, в которую хочешь предложить это объявление:",
        reply_markup=get_user_room_keyboard(announcement_id)
    )


@dp.callback_query(F.data.startswith("usercancel_"))
async def user_cancel_lot(callback: types.CallbackQuery):
    announcement_id = int(callback.data.split("_")[1])
    announcement = find_announcement_by_id(announcement_id)
    if not announcement or announcement["user_id"] != callback.from_user.id:
        await callback.answer("Ошибка доступа.", show_alert=True)
        return

    if announcement["status"] != "draft":
        await callback.answer("Заявка уже отправлена.", show_alert=True)
        return

    announcement["status"] = "cancelled"
    save_storage()
    pending_users.discard(callback.from_user.id)
    announcement_index.pop(callback.from_user.id, None)

    await callback.message.edit_text("❌ Заявка отменена.")


@dp.callback_query(F.data.startswith("userselectroom_"))
async def user_select_room(callback: types.CallbackQuery):
    data = callback.data.split("_", 2)
    if len(data) != 3:
        return

    announcement_id = int(data[1])
    room_name = data[2]

    announcement = find_announcement_by_id(announcement_id)
    if not announcement or announcement["user_id"] != callback.from_user.id:
        await callback.answer("Ошибка доступа.", show_alert=True)
        return

    if announcement["status"] != "draft":
        await callback.answer("Комната уже выбрана.", show_alert=True)
        return

    if room_name not in storage["rooms"]:
        await callback.answer("Комната не найдена.", show_alert=True)
        return

    announcement["room"] = room_name
    announcement["status"] = "pending"
    save_storage()

    await callback.message.edit_text(f"Твой лот отправлен на модерацию в комнату '{room_name}'. Ожидай решения администратора.")

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Одобрить", callback_data=f"approve_{announcement_id}")
    builder.button(text="❌ Отклонить", callback_data=f"reject_{announcement_id}")
    builder.adjust(2)

    admin_caption = f"Новый лот от {announcement['username']} в комнату <b>{room_name}</b>:\n\n{announcement['caption']}"
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(
                chat_id=admin_id,
                photo=announcement["photo_file_id"],
                caption=admin_caption,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Не удалось отправить админу {admin_id}: {e}")


@dp.callback_query(F.data.startswith("approve_"))
async def approve_lot(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Только администратор может одобрять объявления.", show_alert=True)
        return

    announcement_id = int(callback.data.split("_")[1])
    announcement = find_announcement_by_id(announcement_id)
    if not announcement or announcement["status"] != "pending":
        await callback.answer("Эта заявка уже обработана.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    room_name = announcement.get("room")
    if room_name not in storage["rooms"]:
        await callback.answer("Выбранная комната была удалена.", show_alert=True)
        return

    announcement["status"] = "approved"
    announcement["approved_at"] = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    announcement["admin_id"] = callback.from_user.id
    save_storage()

    user_id = announcement["user_id"]
    pending_users.discard(user_id)
    announcement_index.pop(user_id, None)

    try:
        await bot.send_message(user_id, f"🎉 Твое объявление одобрено и сохранено в комнате '{room_name}'.")
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    groups = storage.get("publish_groups", [])
    if not groups and GROUP_ID:
        groups = [{"chat_id": GROUP_ID, "thread_id": THREAD_ID}]

    published_count = 0
    for grp in groups:
        try:
            await bot.send_photo(
                chat_id=grp["chat_id"],
                message_thread_id=grp.get("thread_id"),
                photo=announcement["photo_file_id"],
                caption=announcement["caption"]
            )
            published_count += 1
        except Exception as e:
            logging.error(f"Не удалось опубликовать объявление в группе {grp.get('chat_id')}: {e}")

    await callback.message.edit_caption(
        caption=f"{callback.message.caption}\n\n<b>[✅ ОДОБРЕНО В КОМНАТУ {room_name}]</b>",
        reply_markup=None,
        parse_mode="HTML"
    )
    await callback.answer(f"Сохранено и отправлено в {published_count} групп(ы).")


@dp.callback_query(F.data.startswith("reject_"))
async def reject_lot(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Только администратор может отклонять объявления.", show_alert=True)
        return

    announcement_id = int(callback.data.split("_")[1])
    announcement = find_announcement_by_id(announcement_id)
    if not announcement or announcement["status"] != "pending":
        await callback.answer("Эта заявка уже обработана.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    announcement["status"] = "rejected"
    save_storage()

    user_id = announcement["user_id"]
    pending_users.discard(user_id)
    announcement_index.pop(user_id, None)

    try:
        await bot.send_message(user_id, "😔 К сожалению, твое объявление не прошло модерацию.")
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    await callback.message.edit_caption(
        caption=f"{callback.message.caption}\n\n<b>[❌ ОТКЛОНЕНО]</b>",
        reply_markup=None,
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(~F.photo | ~F.caption, F.chat.type == "private")
async def handle_invalid_submission(message: types.Message):
    user_id = message.from_user.id
    if user_id in pending_users:
        await message.answer("Пожалуйста, заверши текущую заявку (выбери комнату) или дождись решения администратора.", reply_markup=get_main_menu(user_id))
    elif message.text != "/start":
        await message.answer(
            "Пожалуйста, отправь картинку и описание лота одним сообщением (прикрепи фото и добавь к нему текст).",
            reply_markup=get_main_menu(user_id)
        )


async def main():
    load_storage()
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен вручную.")
