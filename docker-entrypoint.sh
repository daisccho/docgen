#!/bin/sh
set -e

# Применяем миграции (с ретраями — race condition при старте двух контейнеров)
for i in 1 2 3; do
    if python3 manage.py migrate --noinput 2>&1; then
        MIGRATED=1
        break
    fi
    echo "[docgen] Повторная попытка миграций ($i/3)..."
    sleep 2
done
if [ -z "${MIGRATED:-}" ]; then
    echo "[docgen] Не удалось применить миграции"
    exit 1
fi

# Создаём admin-пользователя с рандомным паролем, если его нет
python3 manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(username='admin').exists():
    import secrets
    password = secrets.token_urlsafe(16)
    User.objects.create_superuser('admin', '', password)
    print(f'[docgen] Админ создан: admin / {password}')
else:
    print('[docgen] Админ уже существует')
"

exec "$@"
