@echo off
title Raccoon Live Launcher
color 0A

echo ===================================================
echo             RACCOON LIVE LAUNCHER
echo ===================================================
echo.
echo Запуск сервисов Raccoon Live в новых окнах...
echo.

:: Запуск Backend сервера (Flask)
start "Raccoon Live - Backend API" cmd /k "cd /d "%~dp0backend" && call venv\Scripts\activate && python init_db.py && python migrate_db.py && python app.py"

:: Запуск Telegram бота (aiogram)
start "Raccoon Live - Telegram Bot" cmd /k "cd /d "%~dp0bot" && call ..\backend\venv\Scripts\activate && python bot.py"

:: Запуск Ngrok
start "Raccoon Live - Ngrok" cmd /k "ngrok http 5000"

echo Готово! Вы можете закрыть это окно.
pause