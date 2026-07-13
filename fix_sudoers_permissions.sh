#!/bin/bash
# Чинит права на файлы в /etc/sudoers.d/ — sudo ИГНОРИРУЕТ (молча!) любой
# файл с правами не 0440. Значит если там что-то не 440, соответствующие
# NOPASSWD-правила могли попросту не работать, даже если файл существует.
set -e

FILES=(
  /etc/sudoers.d/ai_permissions
  /etc/sudoers.d/krisha-web
  /etc/sudoers.d/nik-nopasswd
  /etc/sudoers.d/krisha-extra-services
)

for f in "${FILES[@]}"; do
    if [ -f "$f" ]; then
        sudo chmod 440 "$f"
        echo "chmod 440 -> $f"
    fi
done

echo ""
echo "Проверка синтаксиса и прав всех файлов sudoers.d:"
sudo visudo -c
