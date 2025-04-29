#!/usr/bin/env bash
# setup_stars_bot.sh — автоматическая установка зависимостей и настройка сервиса для звёздного бота на Ubuntu
# Предполагается, что все файлы скрипта уже находятся в каталоге APP_DIR
# Использование:
#   sudo dos2unix setup_stars_bot.sh && sudo bash setup_stars_bot.sh <VPS_USER> <APP_DIR>
# Пример:
#   sudo bash setup_stars_bot.sh ubuntu /home/ubuntu/automatic-output-accounts

set -euo pipefail
IFS=$'\n\t'

if [[ $# -ne 2 ]]; then
  echo "Usage: sudo $0 <VPS_USER> <APP_DIR>"
  exit 1
fi

VPS_USER="$1"
APP_DIR="$2"
VENV_DIR="$APP_DIR/venv"
SERVICE_FILE="/etc/systemd/system/stars_bot.service"

# 1. Установка системных пакетов
apt update
apt install -y python3 python3-venv python3-pip curl unzip dos2unix

# 2. Приведение файла к UNIX-формату (удаление CRLF)
dos2unix "$APP_DIR/setup_stars_bot.sh"

# 3. Создание виртуального окружения и установка зависимостей
runuser -l "$VPS_USER" -c "python3 -m venv '$VENV_DIR'"
runuser -l "$VPS_USER" -c "source '$VENV_DIR/bin/activate' && pip install --upgrade pip"
if [[ -f "$APP_DIR/requirements.txt" ]]; then
  runuser -l "$VPS_USER" -c "source '$VENV_DIR/bin/activate' && pip install -r '$APP_DIR/requirements.txt'"
else
  runuser -l "$VPS_USER" -c "source '$VENV_DIR/bin/activate' && pip install playwright aiohttp playwright-stealth colorama"
fi
# Установка браузерных движков для Playwright
runuser -l "$VPS_USER" -c "source '$VENV_DIR/bin/activate' && playwright install"

# 4. Создание systemd-сервиса
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=PlayerOK Stars Bot
After=network.target

[Service]
Type=simple
User=$VPS_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV_DIR/bin/python $APP_DIR/script.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$SERVICE_FILE"

# 5. Настройка автозапуска (служба не запускается сразу)
systemctl daemon-reload
systemctl enable stars_bot.service

echo -e "\nСервис настроен на автозапуск после перезагрузки, но не запущен автоматически."
echo "Запустите вручную: sudo systemctl start stars_bot.service"
echo "Проверьте статус: sudo systemctl status stars_bot.service"
echo "Чтобы очистить CRLF для других файлов: dos2unix <имя_файла>"
