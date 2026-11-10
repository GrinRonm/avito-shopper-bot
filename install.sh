#!/usr/bin/env bash
# Установка одной командой: git clone ... && cd avito-shopper-bot && bash install.sh
set -e
cd "$(dirname "$0")"

echo "📦 Создаю virtualenv..."
python3 -m venv venv
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q

echo "🌐 Устанавливаю Chromium для Playwright..."
venv/bin/python -m playwright install chromium

if [ ! -f config.json ]; then
  cp config.example.json config.json
  echo "⚙️  Создал config.json из шаблона — ВСТАВЬ СВОИ ТОКЕНЫ перед запуском!"
else
  echo "⚙️  config.json уже есть."
fi

echo
echo "✅ Готово. Запуск:  venv/bin/python bot.py"
echo "   (для фона на сервере: nohup venv/bin/python bot.py > bot.out 2>&1 &)"
