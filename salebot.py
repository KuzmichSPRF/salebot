import asyncio
import html
import json
import logging
import os
import time 
import sqlite3
import tempfile
from typing import Any, Awaitable, Callable, Dict
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
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
DB_FILE = os.path.join(os.path.dirname(__file__), "salebot.db")
PROMO_TEXT = "\n\nВсе объявления можно посмотреть в @Raccogram_bot"
# =============================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


class AdminReject(StatesGroup):
    waiting_for_reason = State()

def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        # Комнаты
        conn.execute("CREATE TABLE IF NOT EXISTS rooms (name TEXT PRIMARY KEY)")
        # Объявления (расширенная схема)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                username TEXT,
                photo_file_id TEXT,
                caption TEXT,
                status TEXT,
                room TEXT,
                created_at TEXT,
                approved_at TEXT,
                admin_id INTEGER,
                published_messages TEXT
            )
        """)
        # Группы для публикации
        conn.execute("""
            CREATE TABLE IF NOT EXISTS publish_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                thread_id INTEGER,
                name TEXT
            )
        """)
        # Связь групп и комнат (многие-ко-многим)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS group_rooms (
                group_id INTEGER,
                room_name TEXT,
                PRIMARY KEY (group_id, room_name)
            )
        """)
        # Модераторы комнат
        conn.execute("""
            CREATE TABLE IF NOT EXISTS room_admins (
                user_id INTEGER,
                room_name TEXT,
                PRIMARY KEY (user_id, room_name)
            )
        """)

        # Проверка и добавление отсутствующих колонок (миграция)
        cursor = conn.execute("PRAGMA table_info(announcements)")
        columns = [row[1] for row in cursor.fetchall()]

        # Словарь всех необходимых колонок и их типов
        required_columns = {
            "username": "TEXT",
            "photo_file_id": "TEXT",
            "caption": "TEXT",
            "status": "TEXT",
            "room": "TEXT",
            "created_at": "TEXT",
            "approved_at": "TEXT",
            "admin_id": "INTEGER",
            "published_messages": "TEXT"
        }

        for col_name, col_type in required_columns.items():
            if col_name not in columns:
                logging.info(f"Добавление отсутствующей колонки {col_name} в таблицу announcements")
                conn.execute(f"ALTER TABLE announcements ADD COLUMN {col_name} {col_type}")

        conn.commit()

def migrate_json_to_db():
    """Переносит данные из storage.json в SQLite, если они еще не перенесены."""
    if not os.path.exists(DATA_FILE):
        return

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logging.error(f"Ошибка чтения JSON при миграции: {e}")
        return

    with get_db() as conn:
        # 1. Комнаты
        for room in data.get("rooms", []):
            conn.execute("INSERT OR IGNORE INTO rooms (name) VALUES (?)", (room,))
        
        # 2. Объявления
        for ann in data.get("announcements", []):
            conn.execute("""
                INSERT OR IGNORE INTO announcements 
                (id, user_id, username, photo_file_id, caption, status, room, created_at, approved_at, admin_id, published_messages)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ann.get("id"), ann.get("user_id"), ann.get("username"), 
                ann.get("photo_file_id"), ann.get("caption"), ann.get("status"), 
                ann.get("room"), ann.get("created_at"), ann.get("approved_at"), 
                ann.get("admin_id"), json.dumps(ann.get("published_messages", []))
            ))

        # 3. Админы комнат
        for room, admins in data.get("room_admins", {}).items():
            for admin_id in admins:
                conn.execute("INSERT OR IGNORE INTO room_admins (user_id, room_name) VALUES (?, ?)", (admin_id, room))

        # 4. Группы публикации (упрощенно, проверяем по chat_id)
        for grp in data.get("publish_groups", []):
            # Проверяем существование
            res = conn.execute("SELECT id FROM publish_groups WHERE chat_id = ? AND (thread_id = ? OR (thread_id IS NULL AND ? IS NULL))", 
                             (grp.get("chat_id"), grp.get("thread_id"), grp.get("thread_id"))).fetchone()
            if not res:
                cur = conn.execute("INSERT INTO publish_groups (chat_id, thread_id, name) VALUES (?, ?, ?)", 
                                 (grp.get("chat_id"), grp.get("thread_id"), grp.get("name")))
                group_id = cur.lastrowid
                for r_name in grp.get("rooms", []):
                    conn.execute("INSERT OR IGNORE INTO group_rooms (group_id, room_name) VALUES (?, ?)", (group_id, r_name))

        conn.commit()
    logging.info("Синхронизация данных из JSON в SQLite завершена.")
    # Переименовываем файл, чтобы не проводить миграцию при каждом запуске
    try:
        os.rename(DATA_FILE, DATA_FILE + ".bak")
        logging.info(f"Старый файл данных {DATA_FILE} переименован в .bak")
    except Exception as e:
        logging.error(f"Не удалось переименовать файл {DATA_FILE}: {e}")


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


def is_main_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_room_admin(user_id: int, room_name: str) -> bool:
    if not room_name:
        return is_main_admin(user_id)
    if is_main_admin(user_id):
        return True
    with get_db() as conn:
        res = conn.execute("SELECT 1 FROM room_admins WHERE user_id = ? AND room_name = ?", (user_id, room_name)).fetchone()
        return res is not None


def is_any_admin(user_id: int) -> bool:
    if is_main_admin(user_id):
        return True
    with get_db() as conn:
        res = conn.execute("SELECT 1 FROM room_admins WHERE user_id = ?", (user_id,)).fetchone()
        return res is not None


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
    with get_db() as conn:
        rows = conn.execute("SELECT user_id FROM announcements WHERE status IN ('draft', 'pending')").fetchall()
        for row in rows:
            pending_users.add(row["user_id"])

def get_active_announcement(user_id: int):
    """Возвращает последнюю активную (draft или pending) заявку пользователя."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM announcements WHERE user_id = ? AND status IN ('draft', 'pending') ORDER BY id DESC LIMIT 1",
            (user_id,)
        ).fetchone()
        return dict(row) if row else None

def update_user_info(user_id: int, username: str):
    """Обновляет имя пользователя во всех его объявлениях, если оно изменилось."""
    new_username = f"@{username}" if username else f"ID: {user_id}"
    with get_db() as conn:
        conn.execute("UPDATE announcements SET username = ? WHERE user_id = ?", (new_username, user_id))
        conn.commit()


def normalize_room(name: str) -> str:
    return name.strip()


def get_user_room_keyboard(announcement_id: int):
    builder = InlineKeyboardBuilder()
    with get_db() as conn:
        rooms = conn.execute("SELECT name FROM rooms").fetchall()
        for row in rooms:
            room_name = row["name"]
            callback = f"urs_{announcement_id}_{room_name}"
            builder.button(text=room_name, callback_data=callback)
    builder.button(text="❌ Отменить", callback_data=f"urc_{announcement_id}")
    builder.adjust(1)
    return builder.as_markup()


def get_admin_ad_keyboard(announcement_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Удалить", callback_data=f"deletead_{announcement_id}")
    return builder.as_markup()

def get_group_rooms_keyboard(group_db_id: int):
    builder = InlineKeyboardBuilder()
    with get_db() as conn:
        selected_rooms = [r["room_name"] for r in conn.execute("SELECT room_name FROM group_rooms WHERE group_id = ?", (group_db_id,)).fetchall()]
        all_rooms = conn.execute("SELECT name FROM rooms").fetchall()
        
        for room in all_rooms:
            room_name = room["name"]
            marker = "✅ " if room_name in selected_rooms else "🔲 "
            builder.button(
                text=f"{marker}{room_name}", 
                callback_data=f"grm_{group_db_id}_{room_name}"
            )
    builder.button(text="💾 Сохранить", callback_data=f"sgr_{group_db_id}")
    builder.adjust(1)
    return builder.as_markup()


def format_room_list() -> str:
    with get_db() as conn:
        rooms = conn.execute("SELECT name FROM rooms").fetchall()
        if not rooms:
            return "Пока нет комнат. Админ может создать комнату командой /newroom Название"
        
        lines = ["💬 Доступные комнаты:"]
        for idx, row in enumerate(rooms, 1):
            room_name = row["name"]
            count = conn.execute("SELECT COUNT(*) FROM announcements WHERE status = 'approved' AND room = ?", (room_name,)).fetchone()[0]
            lines.append(f"{idx}. {room_name} — {count} объявлений")
        return "\n".join(lines)


def find_announcement_by_id(announcement_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM announcements WHERE id = ?", (announcement_id,)).fetchone()
        if row:
            data = dict(row)
            data["published_messages"] = json.loads(data["published_messages"]) if data["published_messages"] else []
            return data
    return None

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
        f"{PROMO_TEXT}"
    )


def build_admin_caption(message: types.Message) -> str:
    username = html.escape(f"@{message.from_user.username}" if message.from_user.username else f"ID: {message.from_user.id}")
    return f"Новый лот от {username}:\n\n{html.escape(message.caption)}"


@dp.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: types.Message):
    update_user_info(message.from_user.id, message.from_user.username)
    user_id = message.from_user.id
    
    if user_id in pending_users:
        active = get_active_announcement(user_id)
        if active:
            if active["status"] == "draft":
                await message.answer("У тебя есть незавершенная заявка. Пожалуйста, выбери комнату или отмени её:", reply_markup=get_user_room_keyboard(active["id"]))
                return
            else:
                builder = InlineKeyboardBuilder()
                builder.button(text="❌ Отменить текущую заявку", callback_data=f"urc_{active['id']}")
                await message.answer("Твое объявление уже на модерации. Пожалуйста, дождись решения администратора или отмени её:", 
                                   reply_markup=builder.as_markup())
                return
        else:
            pending_users.discard(user_id)

    await message.answer(
        "Привет! Отправь мне фото и описание своего объявления одним сообщением. "
        "Ты сможешь выбрать комнату для публикации. После одобрения админом оно появится в этой комнате.",
        reply_markup=get_main_menu(user_id)
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
            await message.answer(text, parse_mode="HTML")
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
        "📋 <b>Список админов:</b> <code>/adminlist</code>\n"
        "🧹 <b>Очистить фантомы:</b> <code>/clean_ghosts</code>\n"
        "♻️ <b>Восстановить объявление:</b> <code>/restoread ID</code>\n\n"
        "� <b>Публикация:</b>\n"
        "📋 <b>Список чатов:</b> <code>/groups</code>\n"
        "➕ <b>Добавить чат:</b> напиши <code>/addgroup</code> в самой группе\n"
        "⚙️ <b>Настроить комнаты:</b> <code>/setrooms</code> (внутри группы)",
        parse_mode="HTML",
        reply_markup=get_main_menu(message.from_user.id)
    )


@dp.message(Command("newroom"), F.chat.type == "private", StateFilter("*"))
async def add_room(message: types.Message, command: CommandObject):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только администратор может создавать комнаты.")
        return

    if not command.args:
        await message.answer("Использование: /newroom Название_комнаты", reply_markup=get_main_menu(message.from_user.id))
        return

    room_name = normalize_room(command.args)
    if not room_name:
        await message.answer("Название комнаты не может быть пустым.")
        return

    if len(room_name) > 30:
        await message.answer("Название комнаты слишком длинное (максимум 30 символов).")
        return

    with get_db() as conn:
        try:
            conn.execute("INSERT INTO rooms (name) VALUES (?)", (room_name,))
            conn.commit()
            await message.answer(f"Комната '{room_name}' успешно создана.", reply_markup=get_main_menu(message.from_user.id))
        except sqlite3.IntegrityError:
            await message.answer(f"Комната '{room_name}' уже существует.", reply_markup=get_main_menu(message.from_user.id))


@dp.message(Command("editroom"), F.chat.type == "private", StateFilter("*"))
async def edit_room(message: types.Message, command: CommandObject):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только администратор может редактировать комнаты.")
        return

    if not command.args or "|" not in command.args:
        await message.answer("Использование: /editroom Старое_название | Новое_название\n(например: /editroom Авто | Автомобили)", reply_markup=get_main_menu(message.from_user.id))
        return

    parts = [p.strip() for p in command.args.split("|")]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        await message.answer("Использование: /editroom Старое_название | Новое_название\n(например: /editroom Авто | Автомобили)", reply_markup=get_main_menu(message.from_user.id))
        return

    old_name, new_name = parts[0], parts[1]
    with get_db() as conn:
        room_exists = conn.execute("SELECT 1 FROM rooms WHERE name = ?", (old_name,)).fetchone()
        if not room_exists:
            await message.answer(f"Комната '{old_name}' не найдена.")
            return

        try:
            conn.execute("INSERT INTO rooms (name) VALUES (?)", (new_name,))
            conn.execute("UPDATE announcements SET room = ? WHERE room = ?", (new_name, old_name))
            conn.execute("UPDATE group_rooms SET room_name = ? WHERE room_name = ?", (new_name, old_name))
            conn.execute("UPDATE room_admins SET room_name = ? WHERE room_name = ?", (new_name, old_name))
            conn.execute("DELETE FROM rooms WHERE name = ?", (old_name,))
            conn.commit()
            await message.answer(f"Комната '{old_name}' успешно переименована в '{new_name}'.", reply_markup=get_main_menu(message.from_user.id))
        except sqlite3.Error as e:
            await message.answer(f"Ошибка при переименовании: {e}")


@dp.message(Command("delroom"), F.chat.type == "private", StateFilter("*"))
async def del_room(message: types.Message, command: CommandObject):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только администратор может удалять комнаты.")
        return

    room_name = command.args.strip() if command.args else None
    if not room_name:
        await message.answer("Использование: /delroom Название_комнаты", reply_markup=get_main_menu(message.from_user.id))
        return

    with get_db() as conn:
        conn.execute("DELETE FROM rooms WHERE name = ?", (room_name,))
        conn.execute("DELETE FROM group_rooms WHERE room_name = ?", (room_name,))
        conn.execute("DELETE FROM room_admins WHERE room_name = ?", (room_name,))
        conn.commit()
        await message.answer(f"Комната '{room_name}' успешно удалена.", reply_markup=get_main_menu(message.from_user.id))


@dp.message(Command("assignadmin"), F.chat.type == "private", StateFilter("*"))
async def assign_admin(message: types.Message, command: CommandObject):
    if not is_main_admin(message.from_user.id):
        return
        
    if not command.args or len(command.args.split()) < 2:
        await message.answer("Использование: /assignadmin ID_пользователя Название_комнаты")
        return
        
    parts = command.args.split(maxsplit=1)
    try:
        new_admin_id = int(parts[0])
        room_name = normalize_room(parts[1])
    except (ValueError, IndexError):
        await message.answer("Использование: /assignadmin ID_пользователя Название_комнаты\nID должен быть числом.")
        return

    with get_db() as conn:
        room_exists = conn.execute("SELECT 1 FROM rooms WHERE name = ?", (room_name,)).fetchone()
        if not room_exists:
            await message.answer(f"Комната '{room_name}' не найдена.")
            return
        try:
            conn.execute("INSERT INTO room_admins (user_id, room_name) VALUES (?, ?)", (new_admin_id, room_name))
            conn.commit()
        except sqlite3.IntegrityError:
            await message.answer("Этот пользователь уже является администратором данной комнаты.")
            return
    await message.answer(f"✅ Пользователь {new_admin_id} назначен модератором комнаты '{room_name}'.")


@dp.message(Command("revokeadmin"), F.chat.type == "private", StateFilter("*"))
async def revoke_admin(message: types.Message, command: CommandObject):
    if not is_main_admin(message.from_user.id):
        return
        
    if not command.args or len(command.args.split()) < 2:
        await message.answer("Использование: /revokeadmin ID_пользователя Название_комнаты")
        return
        
    parts = command.args.split(maxsplit=1)
    try:
        old_admin_id = int(parts[0])
        room_name = normalize_room(parts[1])
    except (ValueError, IndexError):
        await message.answer("Использование: /revokeadmin ID_пользователя Название_комнаты\nID должен быть числом.")
        return

    with get_db() as conn:
        conn.execute("DELETE FROM room_admins WHERE user_id = ? AND room_name = ?", (old_admin_id, room_name))
        conn.commit()
        await message.answer(f"❌ Пользователь {old_admin_id} удалён из модераторов комнаты '{room_name}'.")


@dp.message(Command("adminlist"), F.chat.type == "private", StateFilter("*"))
async def admin_list(message: types.Message):
    if not is_main_admin(message.from_user.id):
        return
        
    lines = ["👑 <b>Главные администраторы:</b>"]
    for aid in ADMIN_IDS:
        lines.append(f"• {aid}")
        
    lines.append("\n👤 <b>Администраторы комнат:</b>")
    with get_db() as conn:
        admins = conn.execute("SELECT room_name, user_id FROM room_admins ORDER BY room_name").fetchall()
        has_room_admins = False
        current_room = None
        for row in admins:
            has_room_admins = True
            if row["room_name"] != current_room:
                current_room = row["room_name"]
                lines.append(f"\n📂 <b>{current_room}:</b>")
            lines.append(f"• {row['user_id']}")
            
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

    with get_db() as conn:
        existing = conn.execute("SELECT id FROM publish_groups WHERE chat_id = ? AND (thread_id = ? OR (thread_id IS NULL AND ? IS NULL))", (chat_id, thread_id, thread_id)).fetchone()
        if existing:
            await message.answer("Этот чат (или ветка) уже добавлен в список для публикаций.")
            return
        cursor = conn.execute("INSERT INTO publish_groups (chat_id, thread_id, name) VALUES (?, ?, ?)", (chat_id, thread_id, name))
        conn.commit()
        group_id = cursor.lastrowid

    await message.answer(
        f"✅ Чат '{name}' успешно добавлен для публикаций!\n"
        "Выберите комнаты, из которых сюда будут публиковаться объявления:",
        reply_markup=get_group_rooms_keyboard(group_id)
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
    
    with get_db() as conn:
        group = conn.execute("SELECT id, name FROM publish_groups WHERE chat_id = ? AND (thread_id = ? OR (thread_id IS NULL AND ? IS NULL))", (chat_id, thread_id, thread_id)).fetchone()
    
    if not group:
        if message.chat.type == "private":
            await message.answer("В личных сообщениях используйте команду /groups для выбора чата.")
        else:
            await message.answer("Этот чат еще не добавлен в список рассылки. Используйте /addgroup.")
        return

    await message.answer(
        f"⚙️ Настройка комнат для чата <b>{html.escape(group['name'])}</b>:",
        reply_markup=get_group_rooms_keyboard(group["id"]),
        parse_mode="HTML"
    )


@dp.message(F.text.startswith("/delgroup"))
async def del_publish_group(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id

    if message.chat.type == "private":
        await message.answer("В личных сообщениях используйте команду /groups для управления списком групп.")
        return

    with get_db() as conn:
        res = conn.execute(
            "DELETE FROM publish_groups WHERE chat_id = ? AND (thread_id = ? OR (thread_id IS NULL AND ? IS NULL))",
            (chat_id, thread_id, thread_id)
        )
        if res.rowcount > 0:
            conn.commit()
            await message.answer("✅ Эта группа удалена из списка для публикаций.")
        else:
            await message.answer("Эта группа не найдена в списке для публикаций.")


@dp.message(F.text == "/groups", F.chat.type == "private")
async def list_publish_groups(message: types.Message):
    if not is_main_admin(message.from_user.id):
        return
    with get_db() as conn:
        groups = conn.execute("SELECT id, name, thread_id FROM publish_groups").fetchall()
    
    if not groups:
        await message.answer("Список групп для публикаций пуст.")
        return

    builder = InlineKeyboardBuilder()
    for g in groups:
        thread_info = f" (Ветка: {g['thread_id']})" if g['thread_id'] else ""
        builder.button(text=f"⚙️ Настроить {g['name']}{thread_info}", callback_data=f"editgrp_{g['id']}")
        builder.button(text=f"❌ Удалить", callback_data=f"delgroup_{g['id']}")
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

    group_id = int(callback.data.split("_")[1])
    with get_db() as conn:
        conn.execute("DELETE FROM publish_groups WHERE id = ?", (group_id,))
        conn.execute("DELETE FROM group_rooms WHERE group_id = ?", (group_id,))
        conn.commit()
    await callback.message.edit_text("Группа удалена из списка.")
    await callback.answer()


@dp.callback_query(F.data.startswith("grm_"))
async def toggle_group_room(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Только администратор может настраивать комнаты.", show_alert=True)
        return

    parts = callback.data.split("_")
    group_id = int(parts[1])
    room_name = parts[2]
    
    with get_db() as conn:
        exists = conn.execute("SELECT 1 FROM group_rooms WHERE group_id = ? AND room_name = ?", (group_id, room_name)).fetchone()
        if exists:
            conn.execute("DELETE FROM group_rooms WHERE group_id = ? AND room_name = ?", (group_id, room_name))
        else:
            conn.execute("INSERT INTO group_rooms (group_id, room_name) VALUES (?, ?)", (group_id, room_name))
        conn.commit()
    
    await callback.message.edit_reply_markup(reply_markup=get_group_rooms_keyboard(group_id))
    await callback.answer()


@dp.callback_query(F.data.startswith("sgr_"))
async def save_group_rooms(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Только администратор может настраивать комнаты.", show_alert=True)
        return

    parts = callback.data.split("_")
    group_id = int(parts[1])
    
    with get_db() as conn:
        group = conn.execute("SELECT name FROM publish_groups WHERE id = ?", (group_id,)).fetchone()
        selected = [r["room_name"] for r in conn.execute("SELECT room_name FROM group_rooms WHERE group_id = ?", (group_id,)).fetchall()]
        
    if not selected:
        rooms_text = "Ни одной комнаты не выбрано. Объявления сюда приходить не будут."
    else:
        rooms_text = "Выбранные комнаты:\n" + "\n".join(f"• {r}" for r in selected)
        
    await callback.message.edit_text(f"✅ Настройка комнат для чата <b>{group['name']}</b> сохранена.\n\n{rooms_text}", parse_mode="HTML")


@dp.callback_query(F.data.startswith("editgrp_"))
async def edit_group_rooms(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Только администратор может настраивать комнаты.", show_alert=True)
        return
    group_id = int(callback.data.split("_")[1])
    with get_db() as conn:
        group = conn.execute("SELECT name FROM publish_groups WHERE id = ?", (group_id,)).fetchone()

    await callback.message.edit_text(
        f"⚙️ Настройка комнат для чата <b>{group['name']}</b>:\n"
        "Выберите комнаты, из которых в этот чат будут приходить объявления:",
        reply_markup=get_group_rooms_keyboard(group_id),
        parse_mode="HTML"
    )


@dp.message((F.text == "/rooms") | (F.text == "🏠 Комнаты"), F.chat.type == "private")
async def list_rooms(message: types.Message):
    with get_db() as conn:
        rooms = conn.execute("SELECT name FROM rooms").fetchall()
    
    if rooms:
        builder = InlineKeyboardBuilder()
        for row in rooms:
            room_name = row["name"]
            builder.button(text=room_name, callback_data=f"or:{room_name}")
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
    with get_db() as conn:
        room_exists = conn.execute("SELECT 1 FROM rooms WHERE name = ?", (room_name,)).fetchone()
        if not room_exists:
            await message.answer(f"Комната '{room_name}' не найдена. Используй /rooms для списка комнат.", reply_markup=get_main_menu(message.from_user.id))
            return
        
        rows = conn.execute("SELECT * FROM announcements WHERE status = 'approved' AND room = ?", (room_name,)).fetchall()
        items = [dict(r) for r in rows]

    if not items:
        await message.answer(f"В комнате '{room_name}' пока нет одобренных объявлений.")
        return

    await message.answer(f"Объявления в комнате '{room_name}':")
    for item in items:
        reply_markup = get_admin_ad_keyboard(item["id"]) if is_room_admin(message.from_user.id, room_name) else None
        try:
            await bot.send_photo(
                chat_id=message.from_user.id,
                photo=item["photo_file_id"],
                caption=format_announcement(item),
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        except Exception as e:
            logging.error(f"Ошибка при отправке фото для объявления {item['id']}: {e}")
            await message.answer(
                f"⚠️ Не удалось загрузить фото для объявления ID: {item['id']}.\n\n{format_announcement(item)}",
                parse_mode="HTML",
                reply_markup=reply_markup
            )


@dp.message((F.text == "/myads") | (F.text == "📬 Мои объявления"), F.chat.type == "private")
async def my_ads(message: types.Message):
    user_id = message.from_user.id
    update_user_info(user_id, message.from_user.username)
    with get_db() as conn:
        # Показываем только актуальные объявления: черновики, на модерации, одобренные или отклоненные.
        # Игнорируем статусы 'deleted' и 'cancelled'.
        rows = conn.execute(
            "SELECT * FROM announcements WHERE user_id = ? AND status NOT IN ('deleted', 'cancelled')",
            (user_id,)
        ).fetchall()
        user_items = [dict(r) for r in rows]

    if not user_items:
        await message.answer("У тебя пока нет активных объявлений.", reply_markup=get_main_menu(user_id))
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
            f"👤 От: {html.escape(item.get('username', '—'))}\n"
            f"🏷️ Статус: {status_ru}\n"
            f"📂 Комната: {room}\n"
            f"🕒 Добавлено: {item.get('created_at', '—')}\n\n"
            f"Описание:\n{html.escape(item['caption'])}"
            f"{PROMO_TEXT}"
        )

        builder = InlineKeyboardBuilder()
        if item["status"] in ["draft", "pending"]:
            builder.button(text="❌ Отменить заявку", callback_data=f"urc_{item['id']}")
        elif item["status"] == "approved":
            builder.button(text="🗑 Удалить", callback_data=f"userdeletead_{item['id']}")
            
        reply_markup = builder.as_markup()

        await bot.send_photo(
            chat_id=user_id,
            photo=item["photo_file_id"],
            caption=caption,
            parse_mode="HTML",
            reply_markup=reply_markup
        )


@dp.callback_query(F.data.startswith("or:"))
async def open_room(callback: types.CallbackQuery):
    room_name = callback.data.split(":", 1)[1]
    with get_db() as conn:
        room_exists = conn.execute("SELECT 1 FROM rooms WHERE name = ?", (room_name,)).fetchone()
        if not room_exists:
            await callback.answer("Комната не найдена.", show_alert=True)
            return
        
        rows = conn.execute("SELECT * FROM announcements WHERE status = 'approved' AND room = ?", (room_name,)).fetchall()
        items = [dict(r) for r in rows]

    if not items:
        await callback.answer(f"В комнате '{room_name}' пока нет одобренных объявлений.")
        return

    await callback.answer()
    await bot.send_message(callback.from_user.id, f"Объявления в комнате '{room_name}':")
    for item in items:
        reply_markup = get_admin_ad_keyboard(item["id"]) if is_room_admin(callback.from_user.id, room_name) else None
        try:
            await bot.send_photo(
                chat_id=callback.from_user.id,
                photo=item["photo_file_id"],
                caption=format_announcement(item),
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        except Exception as e:
            logging.error(f"Ошибка при отправке фото в open_room для ID {item['id']}: {e}")
            await bot.send_message(
                callback.from_user.id, 
                f"⚠️ Объявление ID {item['id']} недоступно (ошибка загрузки фото).\n\n{format_announcement(item)}",
                parse_mode="HTML",
                reply_markup=reply_markup
            )


@dp.message(F.photo & F.caption, F.chat.type == "private")
async def handle_lot_submission(message: types.Message):
    user_id = message.from_user.id
    update_user_info(user_id, message.from_user.username)
    if user_id in pending_users:
        active = get_active_announcement(user_id)
        if active:
            if active["status"] == "draft":
                await message.answer("У тебя есть незавершенная заявка. Пожалуйста, выбери комнату или отмени её:", reply_markup=get_user_room_keyboard(active["id"]))
            else:
                builder = InlineKeyboardBuilder()
                builder.button(text="❌ Отменить текущую заявку", callback_data=f"urc_{active['id']}")
                await message.answer(
                    "Твоя предыдущая заявка еще на модерации. Дождись решения администратора или отмени её, чтобы отправить новую.",
                    reply_markup=builder.as_markup())
        else:
            # Если в сете есть, а в базе нет - чистим сет
            pending_users.discard(user_id)
            await message.answer("Произошла ошибка состояния. Попробуй отправить объявление еще раз.")
        return

    # Лимит Telegram для подписи к фото — 1024 символа.
    # Мы вычитаем длину промо-текста и небольшой запас для системных данных (ID, Имя и т.д.)
    # 850 символов — безопасный порог.
    MAX_CAPTION_LEN = 850
    if len(message.caption) > MAX_CAPTION_LEN:
        await message.answer(
            f"⚠️ Твоё описание слишком длинное ({len(message.caption)} симв.).\n"
            f"Максимально допустимая длина — <b>{MAX_CAPTION_LEN}</b> символов, "
            f"чтобы объявление корректно отображалось вместе со ссылками. Пожалуйста, сократи текст.")
        return

    with get_db() as conn:
        rooms_count = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
        if rooms_count == 0:
            await message.answer("В данный момент нет доступных комнат для публикации.")
            return

        cursor = conn.execute("""
            INSERT INTO announcements (user_id, username, photo_file_id, caption, status, created_at, published_messages)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, f"@{message.from_user.username}" if message.from_user.username else f"ID: {user_id}", 
              message.photo[-1].file_id, message.caption.strip(), "draft", 
              datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "[]"))
        announcement_id = cursor.lastrowid
        conn.commit()

    pending_users.add(user_id)

    await message.answer(
        "Выбери комнату, в которую хочешь предложить это объявление:",
        reply_markup=get_user_room_keyboard(announcement_id)
    )


@dp.callback_query(F.data.startswith("urc_"))
async def user_cancel_lot(callback: types.CallbackQuery):
    announcement_id = int(callback.data.split("_")[1])
    announcement = find_announcement_by_id(announcement_id)
    if not announcement or announcement["user_id"] != callback.from_user.id:
        await callback.answer("Ошибка доступа.", show_alert=True)
        return

    if announcement["status"] not in ["draft", "pending"]:
        await callback.answer("Эту заявку уже нельзя отменить.", show_alert=True)
        return

    with get_db() as conn:
        conn.execute("UPDATE announcements SET status = 'cancelled' WHERE id = ?", (announcement_id,))
        conn.commit()
    pending_users.discard(callback.from_user.id)

    await callback.message.edit_text("❌ Заявка успешно отменена. Теперь ты можешь отправить новую.")
    await callback.answer()


@dp.callback_query(F.data.startswith("urs_"))
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

    with get_db() as conn:
        room_exists = conn.execute("SELECT 1 FROM rooms WHERE name = ?", (room_name,)).fetchone()
        if not room_exists:
            await callback.answer("Комната не найдена.", show_alert=True)
            return
        conn.execute("UPDATE announcements SET room = ?, status = 'pending' WHERE id = ?", (room_name, announcement_id))
        conn.commit()
    
    await callback.message.edit_text(f"Твой лот отправлен на модерацию в комнату '{room_name}'. Ожидай решения администратора.")

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Одобрить", callback_data=f"approve_{announcement_id}")
    builder.button(text="❌ Отклонить", callback_data=f"reject_{announcement_id}")
    builder.adjust(2)

    # Ограничиваем длину текста, чтобы не превысить лимит Telegram в 1024 символа для фото
    raw_caption = announcement['caption']
    if len(raw_caption) > 800:
        raw_caption = raw_caption[:800] + "..."

    admin_caption = f"Новый лот от {html.escape(announcement['username'])} в комнату <b>{html.escape(room_name)}</b>:\n\n{html.escape(raw_caption)}"
    notify_admins = set(ADMIN_IDS)
    with get_db() as conn:
        room_admins = conn.execute("SELECT user_id FROM room_admins WHERE room_name = ?", (room_name,)).fetchall()
        for r in room_admins:
            notify_admins.add(r["user_id"])

    for admin_id in notify_admins:
        if not admin_id:
            continue
            
        try:
            await bot.send_photo(
                chat_id=admin_id,
                photo=announcement["photo_file_id"],
                caption=admin_caption,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Ошибка отправки уведомления админу {admin_id} для комнаты {room_name}: {e}")


@dp.callback_query(F.data.startswith("approve_"))
async def approve_lot(callback: types.CallbackQuery):
    announcement_id = int(callback.data.split("_")[1])
    announcement = find_announcement_by_id(announcement_id)
    if not announcement:
        await callback.answer("Объявление не найдено.", show_alert=True)
        return
        
    if not announcement.get("photo_file_id"):
        await callback.answer("Ошибка: у объявления отсутствует фото.", show_alert=True)
        return

    room_name = announcement.get("room")
    if not is_room_admin(callback.from_user.id, room_name):
        await callback.answer("У вас нет прав на модерацию этой комнаты.", show_alert=True)
        return

    if announcement["status"] != "pending":
        await callback.answer("Эта заявка уже обработана.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    with get_db() as conn:
        room_exists = conn.execute("SELECT 1 FROM rooms WHERE name = ?", (room_name,)).fetchone()
        if not room_exists:
            await callback.answer("Выбранная комната была удалена.", show_alert=True)
            return

    approved_at = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    user_id = announcement["user_id"]
    pending_users.discard(user_id)

    try:
        await bot.send_message(user_id, f"🎉 Твое объявление одобрено и сохранено в комнате '{room_name}'.")
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    published_count = 0
    published_messages = []
    
    with get_db() as conn:
        groups = conn.execute("SELECT pg.chat_id, pg.thread_id FROM publish_groups pg JOIN group_rooms gr ON pg.id = gr.group_id WHERE gr.room_name = ?", (room_name,)).fetchall()
    
    if not groups:
        logging.warning(f"Внимание: Комната '{room_name}' не привязана ни к одной группе!")

    for grp in groups:
        try:
            msg = await bot.send_photo(
                chat_id=grp["chat_id"],
                message_thread_id=grp["thread_id"],
                photo=announcement["photo_file_id"],
                caption=f"{announcement['caption']}\n\n👤 Контакт: {announcement['username']}{PROMO_TEXT}"
            )
            published_messages.append({
                "chat_id": grp["chat_id"],
                "message_id": msg.message_id
            })
            published_count += 1
        except Exception as e:
            logging.error(f"Не удалось опубликовать объявление в группе {grp['chat_id']}: {e}")

    with get_db() as conn:
        conn.execute("UPDATE announcements SET status = 'approved', approved_at = ?, admin_id = ?, published_messages = ? WHERE id = ?",
                     (approved_at, callback.from_user.id, json.dumps(published_messages), announcement_id))
        conn.commit()

    escaped_caption = html.escape(callback.message.caption or "")
    await callback.message.edit_caption(
        caption=f"{escaped_caption}\n\n<b>[✅ ОДОБРЕНО В КОМНАТУ {room_name}]</b>",
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

    with get_db() as conn:
        conn.execute("UPDATE announcements SET status = 'rejected' WHERE id = ?", (announcement_id,))
        conn.commit()

    user_id = announcement["user_id"]
    pending_users.discard(user_id)

    msg_text = "😔 К сожалению, твое объявление не прошло модерацию."
    if reason:
        msg_text += f"\n\n<b>Причина отказа:</b> {html.escape(reason)}"

    try:
        await bot.send_message(user_id, msg_text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    try:
        escaped_orig = html.escape(orig_caption or "")
        await bot.edit_message_caption(
            chat_id=admin_chat_id,
            message_id=admin_msg_id,
            caption=f"{escaped_orig}\n\n<b>[❌ ОТКЛОНЕНО]</b>" + (f"\nПричина: {html.escape(reason)}" if reason else ""),
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

    with get_db() as conn:
        conn.execute("UPDATE announcements SET status = 'deleted' WHERE id = ?", (ad_id,))
        conn.commit()

    escaped_caption = html.escape(callback.message.caption or "")
    await callback.message.edit_caption(
        caption=f"{escaped_caption}\n\n<b>[❌ УДАЛЕНО АДМИНИСТРАТОРОМ]</b>",
        reply_markup=None,
        parse_mode="HTML"
    )
    await callback.answer("Объявление удалено из базы и групп.")


@dp.message(F.text.startswith("/restoread"), F.chat.type == "private")
async def restore_ad_cmd(message: types.Message):
    if not is_main_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/restoread ID_объявления</code>", parse_mode="HTML")
        return

    ad_id = int(parts[1])
    announcement = find_announcement_by_id(ad_id)

    if not announcement:
        await message.answer(f"Объявление с ID {ad_id} не найдено в базе.")
        return

    if announcement["status"] == "approved":
        await message.answer("Это объявление и так имеет статус 'Одобрено'.")
        return

    # Возвращаем статус approved
    room = announcement.get("room")
    with get_db() as conn:
        conn.execute("UPDATE announcements SET status = 'approved' WHERE id = ?", (ad_id,))
        conn.commit()

    await message.answer(
        f"✅ Объявление ID {ad_id} успешно восстановлено!\n"
        f"Теперь оно снова отображается в комнате <b>{html.escape(str(room))}</b>.",
        parse_mode="HTML"
    )

@dp.message(F.text == "/clean_ghosts", F.chat.type == "private")
async def clean_ghosts_cmd(message: types.Message):
    if not is_main_admin(message.from_user.id):
        return

    count = 0
    with get_db() as conn:
        rows = conn.execute("SELECT id, room FROM announcements WHERE status = 'approved'").fetchall()
        for row in rows:
            room_exists = conn.execute("SELECT 1 FROM rooms WHERE name = ?", (row["room"],)).fetchone()
            if not room_exists:
                conn.execute("UPDATE announcements SET status = 'deleted' WHERE id = ?", (row["id"],))
                count += 1
        conn.commit()

    if count > 0:
        await message.answer(f"✅ База очищена! Удалено {count} фантомных объявлений, у которых не было комнат.")
    else:
        await message.answer("База чиста, фантомных объявлений не обнаружено.")

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

    with get_db() as conn:
        conn.execute("UPDATE announcements SET status = 'deleted' WHERE id = ?", (ad_id,))
        conn.commit()

    await callback.answer("Объявление успешно удалено.", show_alert=True)
    
    escaped_caption = html.escape(callback.message.caption or "")
    await callback.message.edit_caption(
        caption=f"{escaped_caption}\n\n<b>[🗑 УДАЛЕНО ВЛАДЕЛЬЦЕМ]</b>",
        reply_markup=None,
        parse_mode="HTML"
    )


@dp.message(~F.photo | ~F.caption, F.chat.type == "private")
async def handle_invalid_submission(message: types.Message):
    user_id = message.from_user.id
    if message.text in ["🏠 Комнаты", "📬 Мои объявления", "🛠 Управление комнатами", "📖 Инструкция", "🔄 Главное меню"]:
        return # Эти команды обрабатываются своими хендлерами

    if user_id in pending_users:
        active = get_active_announcement(user_id)
        if active and active["status"] == "draft":
            await message.answer("Выбери комнату для текущего объявления или отмени его:", reply_markup=get_user_room_keyboard(active["id"]))
            return
        elif active and active["status"] == "pending":
            builder = InlineKeyboardBuilder()
            builder.button(text="❌ Отменить текущую заявку", callback_data=f"urc_{active['id']}")
            await message.answer(
                "Твоё объявление находится на модерации. Ты можешь отменить его, если хочешь отправить новую заявку:",
                reply_markup=builder.as_markup()
            )
            return

    await message.answer(
        "Пожалуйста, отправь картинку и описание лота ОДНИМ сообщением (прикрепи фото и добавь к нему текст).",
        reply_markup=get_main_menu(user_id)
    )


async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    migrate_json_to_db()
    load_storage()
    dp.message.middleware(ThrottlingMiddleware(limit=1.0))
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен вручную.")
