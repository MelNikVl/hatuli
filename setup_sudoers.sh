#!/bin/bash
# Безопасно добавляет права на запуск/остановку сервисов Hatuli без пароля.
# НЕ открывает интерактивный редактор — создаёт отдельный файл одной строкой
# и проверяет синтаксис через visudo -c перед тем как он вступит в силу.
set -e

SUDOERS_FILE="/etc/sudoers.d/krisha-extra-services"
USER_NAME="${1:-nik}"

sudo tee "$SUDOERS_FILE" > /dev/null <<SUDOEOF
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl start krisha-korter.service, /usr/bin/systemctl stop krisha-korter.service, /usr/bin/systemctl start krisha-homsters.service, /usr/bin/systemctl stop krisha-homsters.service, /usr/bin/systemctl start krisha-market.service, /usr/bin/systemctl stop krisha-market.service
SUDOEOF

sudo chmod 440 "$SUDOERS_FILE"

if sudo visudo -c; then
    echo "OK: синтаксис корректен, файл $SUDOERS_FILE применён."
else
    echo "ОШИБКА: синтаксис sudoers некорректен — откатываю."
    sudo rm -f "$SUDOERS_FILE"
    exit 1
fi
