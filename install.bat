@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo Создаю virtualenv...
python -m venv venv
venv\Scripts\python -m pip install --upgrade pip -q
venv\Scripts\python -m pip install -r requirements.txt -q

echo Устанавливаю Chromium для Playwright...
venv\Scripts\python -m playwright install chromium

if not exist config.json (
  copy config.example.json config.json
  echo Создал config.json из шаблона - ВСТАВЬ СВОИ ТОКЕНЫ перед запуском!
) else (
  echo config.json уже есть.
)

echo.
echo Готово. Запуск: venv\Scripts\python bot.py
