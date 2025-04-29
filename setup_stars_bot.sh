#!/usr/bin/env bash
# setup_stars_bot.sh — установка Python3.9+, зависимостей и настройки сервиса для PlayerOK Stars Bot на Ubuntu (<20.04)

set -euo pipefail
IFS=$'\n\t'

if [[ $# -ne 2 ]]; then
  echo "Usage: sudo $0 <VPS_USER> <APP_DIR>"
  echo "Example: sudo bash setup_stars_bot.sh ubuntu /home/ubuntu/automatic-output-accounts"
  exit 1
fi

VPS_USER="$1"
APP_DIR="$2"
VENV_DIR="$APP_DIR/venv"
SERVICE_FILE="/etc/systemd/system/stars_bot.service"

# 1. Установка системных пакетов и PPA для Python
apt update
apt install -y software-properties-common curl unzip dos2unix
add-apt-repository -y ppa:deadsnakes/ppa
apt update

# 2. Установка Python 3.9 и модулей
apt install -y python3.9 python3.9-venv python3.9-distutils python3.9-dev

# 3. Приведение этого скрипта к UNIX-формату (удаление CRLF)
dos2unix "$APP_DIR/setup_stars_bot.sh"

# 4. Создание виртуального окружения на Python 3.9
runuser -l "$VPS_USER" -c "python3.9 -m venv '$VENV_DIR'"

# 5. Установка pip и зависимостей в venv
runuser -l "$VPS_USER" -c "source '$VENV_DIR/bin/activate' && pip install --upgrade pip"
runuser -l "$VPS_USER" -c "source '$VENV_DIR/bin/activate' && $VENV_DIR/bin/python3.9 -m pip install playwright aiohttp playwright-stealth colorama"

# 6. Установка браузерных движков для Playwright
runuser -l "$VPS_USER" -c "source '$VENV_DIR/bin/activate' && $VENV_DIR/bin/python3.9 -m playwright install"

# 7. Создание systemd-сервиса
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=PlayerOK Stars Bot
After=network.target

[Service]
Type=simple
User=$VPS_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV_DIR/bin/python3.9 $APP_DIR/script.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$SERVICE_FILE"

# 8. Настройка автозапуска службы (без немедленного старта)
systemctl daemon-reload
systemctl enable stars_bot.service

echo -e "\nУстановка завершена. Сервис настроен на автозапуск при старте системы, но не запущен." 
echo "Чтобы запустить его сейчас: sudo systemctl start stars_bot.service" 
echo "Проверить статус: sudo systemctl status stars_bot.service" 
echo "Для перезапуска после изменений: sudo systemctl restart stars_bot.service"
