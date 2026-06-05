import asyncio
import html
import json
import logging
import os
import tempfile
import time 
from typing import Any, Awaitable, Callable, Dict
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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


class AdminReject(StatesGroup):
    waiting_for_reason = State()


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, limit: float = 1.0):
        self.limit = limit
        self.users: Dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[types.Message, Dict[str, Any]], Awaitable[Any]],
        event: types.Message,
        data: Dict[str, Any]
    ) -> Any:
        if not event.from_user:
            return await handler(event, data)
            
        user_id = event.from_user.id
        now = time.time()
        
        if now - self.users.get(user_id, 0.0) < self.limit:
            return
            
        self.users[user_id] = now
        return await handler(event, data)

pending_users = set()
announcement_index = {}

storage = {
    "rooms": [],
    "announcements": []
}


def is_main_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_room_admin(user_id: int, room_name: str) -> bool:
    if not room_name:
        return is_main_admin(user_id)
    return is_main_admin(user_id) or user_id in storage.get("room_admins", {}).get(room_name, [])


def is_any_admin(user_id: int) -> bool:
    if is_main_admin(user_id):
        return True
    return any(user_id in admins for admins in storage.get("room_admins", {}).values())


def get_main_menu(user_id: int = None) -> types.ReplyKeyboardMarkup:
    buttons = [
        [types.KeyboardButton(text="🏠 Комнаты")],
        [types.KeyboardButton(text="📬 Мои объявления")],
    ]
    if is_main_admin(user_id):
        buttons.append([types.KeyboardButton(text="🛠 Управление комнатами")])
    if is_any_admin(user_id):
        buttons.append([types.KeyboardButton(text="📖 Инструкция")])
    buttons.append([types.KeyboardButton(text="🔄 Главное меню")])
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
        
    if "room_admins" not in storage:
        storage["room_admins"] = {}
        
    # Восстанавливаем pending_users, чтобы после рестарта бота 
    # злоумышленники не могли заспамить БД незавершенными черновиками
    for item in storage.get("announcements", []):
        if item.get("status") in ("draft", "pending"):
            pending_users.add(item["user_id"])

def update_user_info(user_id: int, username: str):
    """Обновляет имя пользователя во всех его объявлениях, если оно изменилось."""
    new_username = f"@{username}" if username else f"ID: {user_id}"
    changed = False
    for ann in storage.get("announcements", []):
        if ann["user_id"] == user_id and ann.get("username") != new_username:
            ann["username"] = new_username
            changed = True
    if changed:
        save_storage()


def save_storage():
    try:
        # Атомарное сохранение через временный файл предотвращает 
        # повреждение storage.json при одновременной записи
        fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(DATA_FILE), prefix="storage_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(storage, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, DATA_FILE)
    except Exception as e:
        logging.error(f"Ошибка сохранения storage.json: {e}")
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.remove(temp_path)


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


def get_admin_ad_keyboard(announcement_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Удалить", callback_data=f"deletead_{announcement_id}")
    return builder.as_markup()

def get_group_rooms_keyboard(group_idx: int):
    builder = InlineKeyboardBuilder()
    groups = storage.get("publish_groups", [])
    if not (0 <= group_idx < len(groups)):
        return builder.as_markup()
    
    group = groups[group_idx]
    selected_rooms = group.get("rooms", [])
    
    for r_idx, room_name in enumerate(storage["rooms"]):
        marker = "✅ " if room_name in selected_rooms else "🔲 "
        builder.button(
            text=f"{marker}{room_name}", 
            callback_data=f"grproom_{group_idx}_{r_idx}"
        )
    builder.button(text="💾 Сохранить", callback_data=f"savegrprooms_{group_idx}")
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
    owner = html.escape(item.get("username", f"ID:{item.get('user_id')}"))
    status = item.get("status", "unknown")
    room = item.get("room", "—")
    return (
        f"📌 ID: {item['id']}\n"
        f"👤 От: {owner}\n"
        f"🕒 Добавлено: {created}\n"
        f"🏷️ Статус: {status}\n"
        f"📂 Комната: {room}\n"
        f"Описание: {html.escape(item['caption'])}"
    )


def find_announcement_by_id(announcement_id: int):
    for item in storage["announcements"]:
        if item["id"] == announcement_id:
            return item
    return None


def build_admin_caption(message: types.Message) -> str:
    username = html.escape(f"@{message.from_user.username}" if message.from_user.username else f"ID: {message.from_user.id}")
    return f"Новый лот от {username}:\n\n{html.escape(message.caption)}"


@dp.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: types.Message):
    update_user_info(message.from_user.id, message.from_user.username)
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
    if not is_any_admin(message.from_user.id):
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
    if not is_main_admin(message.from_user.id):
        await message.answer("Только администратор может управлять комнатами.", reply_markup=get_main_menu(message.from_user.id))
        return
    await message.answer(
        "<b>Доступные команды:</b>\n"
        "➕ <b>Создать:</b> <code>/newroom Название</code>\n"
        "✏️ <b>Изменить:</b> <code>/editroom Старое_имя | Новое_имя</code>\n"
        "❌ <b>Удалить:</b> <code>/delroom Название</code>\n\n"
        "👤 <b>Админы комнат:</b>\n"
        "➕ <b>Назначить:</b> <code>/assignadmin ID Название_комнаты</code>\n"
        "➖ <b>Разжаловать:</b> <code>/revokeadmin ID Название_комнаты</code>\n"
        "📋 <b>Список админов:</b> <code>/adminlist</code>\n\n"
        "📢 <b>Публикация:</b>\n"
        "📋 <b>Список чатов:</b> <code>/groups</code>\n"
        "➕ <b>Добавить чат:</b> напиши <code>/addgroup</code> в самой группе\n"
        "⚙️ <b>Настроить комнаты:</b> <code>/setrooms</code> (внутри группы)",
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
            
    for grp in storage.get("publish_groups", []):
        if "rooms" in grp and old_name in grp["rooms"]:
            grp["rooms"].remove(old_name)
            grp["rooms"].append(new_name)
            
    if old_name in storage.get("room_admins", {}):
        storage["room_admins"][new_name] = storage["room_admins"].pop(old_name)

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
    for grp in storage.get("publish_groups", []):
        if "rooms" in grp and room_name in grp["rooms"]:
            grp["rooms"].remove(room_name)
            
    storage.get("room_admins", {}).pop(room_name, None)
            
    save_storage()
    await message.answer(f"Комната '{room_name}' успешно удалена.", reply_markup=get_main_menu(message.from_user.id))


@dp.message(F.text.startswith("/assignadmin"), F.chat.type == "private")
async def assign_admin(message: types.Message):
    if not is_main_admin(message.from_user.id):
        return
        
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: /assignadmin ID_пользователя Название_комнаты")
        return
        
    try:
        new_admin_id = int(parts[1])
    except ValueError:
        await message.answer("ID пользователя должен быть числом.")
        return
        
    room_name = normalize_room(parts[2])
    if room_name not in storage["rooms"]:
        await message.answer(f"Комната '{room_name}' не найдена.")
        return
        
    if "room_admins" not in storage:
        storage["room_admins"] = {}
        
    if room_name not in storage["room_admins"]:
        storage["room_admins"][room_name] = []
        
    if new_admin_id in storage["room_admins"][room_name]:
        await message.answer("Этот пользователь уже является администратором данной комнаты.")
        return
        
    storage["room_admins"][room_name].append(new_admin_id)
    save_storage()
    await message.answer(f"✅ Пользователь {new_admin_id} назначен модератором комнаты '{room_name}'.")


@dp.message(F.text.startswith("/revokeadmin"), F.chat.type == "private")
async def revoke_admin(message: types.Message):
    if not is_main_admin(message.from_user.id):
        return
        
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: /revokeadmin ID_пользователя Название_комнаты")
        return
        
    try:
        old_admin_id = int(parts[1])
    except ValueError:
        await message.answer("ID пользователя должен быть числом.")
        return
        
    room_name = normalize_room(parts[2])
    
    if "room_admins" in storage and room_name in storage["room_admins"] and old_admin_id in storage["room_admins"][room_name]:
        storage["room_admins"][room_name].remove(old_admin_id)
        save_storage()
        await message.answer(f"❌ Пользователь {old_admin_id} удалён из модераторов комнаты '{room_name}'.")
    else:
        await message.answer("Этот пользователь не является администратором указанной комнаты.")


@dp.message(F.text == "/adminlist", F.chat.type == "private")
async def admin_list(message: types.Message):
    if not is_main_admin(message.from_user.id):
        return
        
    lines = ["👑 <b>Главные администраторы:</b>"]
    for aid in ADMIN_IDS:
        lines.append(f"• {aid}")
        
    lines.append("\n👤 <b>Администраторы комнат:</b>")
    has_room_admins = False
    for room, admins in storage.get("room_admins", {}).items():
        if admins:
            has_room_admins = True
            lines.append(f"\n📂 <b>{room}:</b>\n" + "\n".join(f"• {aid}" for aid in admins))
            
    if not has_room_admins:
        lines.append("Нет назначенных администраторов комнат.")
        
    await message.answer("\n".join(lines), parse_mode="HTML")

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
        "name": name,
        "rooms": []
    })
    save_storage()
    group_idx = len(storage["publish_groups"]) - 1
    await message.answer(
        f"✅ Чат '{name}' успешно добавлен для публикаций!\n"
        "Выберите комнаты, из которых сюда будут публиковаться объявления:",
        reply_markup=get_group_rooms_keyboard(group_idx)
    )

    admin_name = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    for admin_id in ADMIN_IDS:
        if admin_id != message.from_user.id:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"📢 Администратор {html.escape(admin_name)} добавил новый чат для публикаций: <b>{html.escape(name)}</b>",
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")


@dp.message(F.text.startswith("/setrooms"))
async def set_rooms_command(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id
    
    groups = storage.get("publish_groups", [])
    group_idx = -1
    for i, g in enumerate(groups):
        if g["chat_id"] == chat_id and g.get("thread_id") == thread_id:
            group_idx = i
            break
    
    if group_idx == -1:
        if message.chat.type == "private":
            await message.answer("В личных сообщениях используйте команду /groups для выбора чата.")
        else:
            await message.answer("Этот чат еще не добавлен в список рассылки. Используйте /addgroup.")
        return

    await message.answer(
        f"⚙️ Настройка комнат для чата <b>{html.escape(groups[group_idx].get('name'))}</b>:",
        reply_markup=get_group_rooms_keyboard(group_idx),
        parse_mode="HTML"
    )


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
        builder.button(text=f"⚙️ Настроить {g.get('name')}{thread_info}", callback_data=f"editgrp_{i}")
        builder.button(text=f"❌ Удалить", callback_data=f"delgroup_{i}")
    builder.adjust(2)

    await message.answer(
        "📢 <b>Группы для публикаций:</b>\nВыбери группу для настройки комнат или нажми ❌ для удаления.",
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
                builder.button(text=f"⚙️ Настроить {g.get('name')}{thread_info}", callback_data=f"editgrp_{i}")
                builder.button(text=f"❌ Удалить", callback_data=f"delgroup_{i}")
            builder.adjust(2)
            await callback.message.edit_text(
                "📢 <b>Группы для публикаций:</b>\nВыбери группу для настройки комнат или нажми ❌ для удаления.",
                parse_mode="HTML",
                reply_markup=builder.as_markup()
            )
    else:
        await callback.answer("Группа не найдена или уже удалена.", show_alert=True)


@dp.callback_query(F.data.startswith("grproom_"))
async def toggle_group_room(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Только администратор может настраивать комнаты.", show_alert=True)
        return

    parts = callback.data.split("_")
    if len(parts) != 3:
        return
        
    group_idx = int(parts[1])
    r_idx = int(parts[2])
    
    groups = storage.get("publish_groups", [])
    if not (0 <= group_idx < len(groups)):
        await callback.answer("Группа не найдена.", show_alert=True)
        return
        
    group = groups[group_idx]
    if "rooms" not in group:
        group["rooms"] = []
        
    rooms_list = storage.get("rooms", [])
    if not (0 <= r_idx < len(rooms_list)):
        await callback.answer("Комната не найдена.", show_alert=True)
        return
        
    room_name = rooms_list[r_idx]
    
    if room_name in group["rooms"]:
        group["rooms"].remove(room_name)
    else:
        group["rooms"].append(room_name)
        
    save_storage()
    
    await callback.message.edit_reply_markup(reply_markup=get_group_rooms_keyboard(group_idx))


@dp.callback_query(F.data.startswith("savegrprooms_"))
async def save_group_rooms(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Только администратор может настраивать комнаты.", show_alert=True)
        return

    parts = callback.data.split("_")
    group_idx = int(parts[1])
    
    groups = storage.get("publish_groups", [])
    if not (0 <= group_idx < len(groups)):
        await callback.message.edit_text("Группа не найдена или была удалена.")
        return
        
    group = groups[group_idx]
    selected = group.get("rooms", [])
    
    if not selected:
        rooms_text = "Ни одной комнаты не выбрано. Объявления сюда приходить не будут."
    else:
        rooms_text = "Выбранные комнаты:\n" + "\n".join(f"• {r}" for r in selected)
        
    await callback.message.edit_text(f"✅ Настройка комнат для чата <b>{group.get('name')}</b> сохранена.\n\n{rooms_text}", parse_mode="HTML")


@dp.callback_query(F.data.startswith("editgrp_"))
async def edit_group_rooms(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Только администратор может настраивать комнаты.", show_alert=True)
        return
    idx = int(callback.data.split("_")[1])
    groups = storage.get("publish_groups", [])
    if not (0 <= idx < len(groups)):
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    
    group = groups[idx]
    await callback.message.edit_text(
        f"⚙️ Настройка комнат для чата <b>{group.get('name')}</b>:\n"
        "Выберите комнаты, из которых в этот чат будут приходить объявления:",
        reply_markup=get_group_rooms_keyboard(idx),
        parse_mode="HTML"
    )


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
        reply_markup = get_admin_ad_keyboard(item["id"]) if is_room_admin(message.from_user.id, room_name) else None
        await bot.send_photo(
            chat_id=message.from_user.id,
            photo=item["photo_file_id"],
            caption=format_announcement(item),
            parse_mode="HTML",
            reply_markup=reply_markup
        )


@dp.message((F.text == "/myads") | (F.text == "📬 Мои объявления"), F.chat.type == "private")
async def my_ads(message: types.Message):
    user_id = message.from_user.id
    update_user_info(user_id, message.from_user.username)
    user_items = [item for item in storage["announcements"] if item["user_id"] == user_id]
    if not user_items:
        await message.answer("У тебя еще нет объявлений.", reply_markup=get_main_menu(user_id))
        return

    await message.answer("📬 <b>Твои объявления:</b>", parse_mode="HTML")

    for item in user_items:
        room = item.get("room", "—")
        status_ru = {
            "draft": "📝 Черновик", 
            "pending": "⏳ На модерации", 
            "approved": "✅ Одобрено", 
            "rejected": "❌ Отклонено", 
            "cancelled": "🚫 Отменено", 
            "deleted": "🗑 Удалено"
        }.get(item["status"], item["status"])
        
        caption = (
            f"📌 ID: {item['id']}\n"
            f"🏷️ Статус: {status_ru}\n"
            f"📂 Комната: {room}\n"
            f"🕒 Добавлено: {item.get('created_at', '—')}\n\n"
            f"Описание:\n{html.escape(item['caption'])}"
        )

        builder = InlineKeyboardBuilder()
        if item["status"] in ["pending", "approved"]:
            builder.button(text="🗑 Удалить", callback_data=f"userdeletead_{item['id']}")
            
        reply_markup = builder.as_markup() if item["status"] in ["pending", "approved"] else None
        
        await bot.send_photo(
            chat_id=user_id,
            photo=item["photo_file_id"],
            caption=caption,
            parse_mode="HTML",
            reply_markup=reply_markup
        )


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
        reply_markup = get_admin_ad_keyboard(item["id"]) if is_room_admin(callback.from_user.id, room_name) else None
        await bot.send_photo(
            chat_id=callback.from_user.id,
            photo=item["photo_file_id"],
            caption=format_announcement(item),
            parse_mode="HTML",
            reply_markup=reply_markup
        )


@dp.message(F.photo & F.caption, F.chat.type == "private")
async def handle_lot_submission(message: types.Message):
    user_id = message.from_user.id
    update_user_info(user_id, message.from_user.username)
    if user_id in pending_users:
        await message.answer("Пожалуйста, заверши текущую заявку (выбери комнату) или дождись решения администратора.")
        return

    if not storage["rooms"]:
        await message.answer("В данный момент нет доступных комнат для публикации.")
        return

    announcement_id = max((item["id"] for item in storage["announcements"]), default=0) + 1
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
        "admin_id": None,
        "published_messages": []
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

    admin_caption = f"Новый лот от {html.escape(announcement['username'])} в комнату <b>{html.escape(room_name)}</b>:\n\n{html.escape(announcement['caption'])}"
    notify_admins = set(ADMIN_IDS)
    if "room_admins" in storage and room_name in storage["room_admins"]:
        notify_admins.update(storage["room_admins"][room_name])
        
    for admin_id in notify_admins:
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
    announcement_id = int(callback.data.split("_")[1])
    announcement = find_announcement_by_id(announcement_id)
    if not announcement:
        await callback.answer("Объявление не найдено.", show_alert=True)
        return
        
    room_name = announcement.get("room")
    if not is_room_admin(callback.from_user.id, room_name):
        await callback.answer("У вас нет прав на модерацию этой комнаты.", show_alert=True)
        return

    if announcement["status"] != "pending":
        await callback.answer("Эта заявка уже обработана.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return
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
    announcement["published_messages"] = []
    for grp in groups:
        if "rooms" in grp and room_name not in grp["rooms"]:
            continue
            
        try:
            msg = await bot.send_photo(
                chat_id=grp["chat_id"],
                message_thread_id=grp.get("thread_id"),
                photo=announcement["photo_file_id"],
                caption=announcement["caption"]
            )
            announcement["published_messages"].append({
                "chat_id": grp["chat_id"],
                "message_id": msg.message_id
            })
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
async def reject_lot_start(callback: types.CallbackQuery, state: FSMContext):
    announcement_id = int(callback.data.split("_")[1])
    announcement = find_announcement_by_id(announcement_id)
    if not announcement or announcement["status"] != "pending":
        await callback.answer("Эта заявка уже обработана.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return
        
    if not is_room_admin(callback.from_user.id, announcement.get("room")):
        await callback.answer("У вас нет прав на модерацию этой комнаты.", show_alert=True)
        return

    await state.set_state(AdminReject.waiting_for_reason)

    builder = InlineKeyboardBuilder()
    builder.button(text="Без причины", callback_data="skip_reason")
    builder.button(text="Отмена", callback_data="cancel_reject")
    builder.adjust(2)

    prompt_msg = await callback.message.reply(
        "Напиши причину отказа для пользователя текстом ниже или выбери действие:", 
        reply_markup=builder.as_markup()
    )

    await state.update_data(
        announcement_id=announcement_id,
        admin_msg_id=callback.message.message_id,
        orig_caption=callback.message.caption or "",
        prompt_msg_id=prompt_msg.message_id
    )
    await callback.answer()


async def finalize_rejection(announcement_id: int, admin_chat_id: int, admin_msg_id: int, orig_caption: str, reason: str = None):
    announcement = find_announcement_by_id(announcement_id)
    if not announcement or announcement["status"] != "pending":
        return False

    announcement["status"] = "rejected"
    save_storage()

    user_id = announcement["user_id"]
    pending_users.discard(user_id)
    announcement_index.pop(user_id, None)

    msg_text = "😔 К сожалению, твое объявление не прошло модерацию."
    if reason:
        msg_text += f"\n\n<b>Причина отказа:</b> {html.escape(reason)}"

    try:
        await bot.send_message(user_id, msg_text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    try:
        await bot.edit_message_caption(
            chat_id=admin_chat_id,
            message_id=admin_msg_id,
            caption=f"{orig_caption}\n\n<b>[❌ ОТКЛОНЕНО]</b>" + (f"\nПричина: {html.escape(reason)}" if reason else ""),
            reply_markup=None,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Не удалось обновить сообщение админа {admin_msg_id}: {e}")

    return True


@dp.message(AdminReject.waiting_for_reason, F.text)
async def reject_reason_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    
    await state.clear()
    try:
        await bot.delete_message(message.chat.id, data.get("prompt_msg_id"))
    except Exception:
        pass
        
    success = await finalize_rejection(
        data.get("announcement_id"), message.chat.id, data.get("admin_msg_id"), data.get("orig_caption"), message.text
    )
    if success:
        await message.reply("✅ Объявление отклонено, пользователю отправлена причина.")
    else:
        await message.reply("❌ Ошибка: заявка уже обработана или не найдена.")


@dp.callback_query(AdminReject.waiting_for_reason)
async def reject_reason_callback(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    await state.clear()
    try:
        await bot.delete_message(callback.message.chat.id, data.get("prompt_msg_id"))
    except Exception:
        pass
        
    if callback.data == "cancel_reject":
        await callback.answer("Отклонение отменено.")
        return
        
    if callback.data == "skip_reason":
        success = await finalize_rejection(
            data.get("announcement_id"), callback.message.chat.id, data.get("admin_msg_id"), data.get("orig_caption"), None
        )
        text = "✅ Объявление отклонено без причины." if success else "❌ Ошибка: заявка уже обработана."
        await bot.send_message(callback.message.chat.id, text, reply_to_message_id=data.get("admin_msg_id"))
        await callback.answer()


@dp.callback_query(F.data.startswith("deletead_"))
async def delete_ad(callback: types.CallbackQuery):
    ad_id = int(callback.data.split("_")[1])
    announcement = find_announcement_by_id(ad_id)
    
    if not announcement or announcement["status"] != "approved":
        await callback.answer("Объявление не найдено или уже удалено.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    if not is_room_admin(callback.from_user.id, announcement.get("room")):
        await callback.answer("У вас нет прав на удаление в этой комнате.", show_alert=True)
        return

    if "published_messages" in announcement:
        for pub_msg in announcement["published_messages"]:
            try:
                await bot.delete_message(chat_id=pub_msg["chat_id"], message_id=pub_msg["message_id"])
            except Exception as e:
                logging.error(f"Не удалось удалить сообщение {pub_msg['message_id']} из чата {pub_msg['chat_id']}: {e}")

    announcement["status"] = "deleted"
    save_storage()
    
    await callback.message.edit_caption(
        caption=f"{callback.message.caption}\n\n<b>[❌ УДАЛЕНО АДМИНИСТРАТОРОМ]</b>",
        reply_markup=None,
        parse_mode="HTML"
    )
    await callback.answer("Объявление удалено из базы и групп.")


@dp.callback_query(F.data.startswith("userdeletead_"))
async def user_delete_ad(callback: types.CallbackQuery):
    ad_id = int(callback.data.split("_")[1])
    announcement = find_announcement_by_id(ad_id)
    
    if not announcement or announcement["user_id"] != callback.from_user.id:
        await callback.answer("Объявление не найдено.", show_alert=True)
        return
        
    if announcement["status"] not in ["pending", "approved"]:
        await callback.answer("Это объявление уже нельзя удалить.", show_alert=True)
        return

    if announcement["status"] == "approved" and "published_messages" in announcement:
        for pub_msg in announcement["published_messages"]:
            try:
                await bot.delete_message(chat_id=pub_msg["chat_id"], message_id=pub_msg["message_id"])
            except Exception as e:
                logging.error(f"Не удалось удалить сообщение {pub_msg['message_id']} из чата {pub_msg['chat_id']}: {e}")

    if announcement["status"] == "pending":
        pending_users.discard(announcement["user_id"])
        announcement_index.pop(announcement["user_id"], None)

    announcement["status"] = "deleted"
    save_storage()
    
    await callback.answer("Объявление успешно удалено.", show_alert=True)
    
    await callback.message.edit_caption(
        caption=f"{callback.message.caption}\n\n<b>[🗑 УДАЛЕНО ВЛАДЕЛЬЦЕМ]</b>",
        reply_markup=None,
        parse_mode="HTML"
    )


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
    dp.message.middleware(ThrottlingMiddleware(limit=1.0))
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен вручную.")
