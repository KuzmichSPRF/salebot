import asyncio
import logging
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))  # Telegram ID администратора (число)
GROUP_ID = int(os.getenv("GROUP_ID"))  # Telegram ID группы/канала (начинается с -100)
# =============================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Обработчик команды /start
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await message.answer(
        "Привет! Отправь мне фотографию с описанием лота (в одном сообщении), "
        "и я передам её на модерацию администратору."
    )

# Обработчик сообщений с фото и текстом (описанием)
@dp.message(F.photo & F.caption)
async def handle_lot_submission(message: types.Message):
    user_id = message.from_user.id
    
    # Создаем inline-клавиатуру для администратора
    builder = InlineKeyboardBuilder()
    # В callback_data зашиваем ID пользователя, чтобы знать, кому отвечать
    builder.button(text="✅ Одобрить", callback_data=f"approve_{user_id}")
    builder.button(text="❌ Отклонить", callback_data=f"reject_{user_id}")
    builder.adjust(2)

    # Формируем сообщение для админа
    username = f"@{message.from_user.username}" if message.from_user.username else f"ID: {user_id}"
    admin_caption = f"Новый лот от {username}:\n\n{message.caption}"

    # Отправляем лот администратору
    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=message.photo[-1].file_id, # Берем фото в лучшем качестве
        caption=admin_caption,
        reply_markup=builder.as_markup()
    )
    
    await message.answer("Твой лот успешно отправлен на модерацию!")

# Обработчик нажатия кнопки "Одобрить"
@dp.callback_query(F.data.startswith("approve_"))
async def approve_lot(callback: types.CallbackQuery):
    # Извлекаем ID пользователя из callback_data
    user_id = int(callback.data.split("_")[1])
    
    # Достаем оригинальное описание, убирая приписку "Новый лот от..."
    original_caption = callback.message.caption.split("\n\n", 1)[-1]

    # Публикуем в группу
    await bot.send_photo(
        chat_id=GROUP_ID,
        photo=callback.message.photo[-1].file_id,
        caption=original_caption
    )
    
    # Уведомляем пользователя
    try:
        await bot.send_message(user_id, "🎉 Твой лот прошел модерацию и опубликован в группе!")
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    # Обновляем сообщение администратора (убираем кнопки)
    await callback.message.edit_caption(
        caption=f"{callback.message.caption}\n\n<b>[✅ ОДОБРЕНО И ОПУБЛИКОВАНО]</b>",
        reply_markup=None,
        parse_mode="HTML"
    )
    await callback.answer()

# Обработчик нажатия кнопки "Отклонить"
@dp.callback_query(F.data.startswith("reject_"))
async def reject_lot(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    
    # Уведомляем пользователя
    try:
        await bot.send_message(user_id, "😔 К сожалению, твой лот не прошел модерацию.")
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    # Обновляем сообщение администратора
    await callback.message.edit_caption(
        caption=f"{callback.message.caption}\n\n<b>[❌ ОТКЛОНЕНО]</b>",
        reply_markup=None,
        parse_mode="HTML"
    )
    await callback.answer()

# Обработчик, если прислали текст без фото или фото без текста
@dp.message(~F.photo | ~F.caption)
async def handle_invalid_submission(message: types.Message):
    if message.text != "/start":
        await message.answer(
            "Пожалуйста, отправь картинку и описание лота *одним сообщением* "
            "(прикрепи фото и добавь к нему текст).",
            parse_mode="Markdown"
        )

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
