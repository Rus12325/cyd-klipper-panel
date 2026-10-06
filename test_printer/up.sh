#!/usr/bin/env bash
# Поднимает тестовый стенд (mock Moonraker из test_printer/) одной командой:
# Docker -> контейнеры -> патч виртуального принтера -> ожидание "ready".
#
# Запуск: bash test_printer/up.sh
# Остановка: cd test_printer && docker compose down
set -u

cd "$(dirname "$0")"

CONTAINER="${CONTAINER:-printer}"
MOONRAKER="${MOONRAKER:-http://localhost:7125}"
say() { echo "==> $*"; }

# --- 1. Docker должен быть запущен -----------------------------------------
if ! docker info >/dev/null 2>&1; then
    say "Docker не отвечает — пробую запустить Docker Desktop"
    if [ -x "/c/Program Files/Docker/Docker/Docker Desktop.exe" ]; then
        powershell -NoProfile -Command \
            "Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'" \
            >/dev/null 2>&1
    elif [ -d "/Applications/Docker.app" ]; then
        open -a Docker
    else
        echo "    не нашёл Docker Desktop. Запустите Docker вручную и повторите." >&2
        exit 1
    fi
    for _ in $(seq 1 36); do
        docker info >/dev/null 2>&1 && break
        sleep 5
    done
    if ! docker info >/dev/null 2>&1; then
        echo "    Docker так и не поднялся (ждём 180 с)." >&2
        exit 1
    fi
    say "    Docker поднялся"
fi

# --- 2. Контейнеры ----------------------------------------------------------
say "поднимаю контейнеры (printer + mainsail)"
docker compose up -d || exit 1

say "жду инициализацию контейнера ${CONTAINER}"
for _ in $(seq 1 60); do
    docker exec "${CONTAINER}" sh -c 'test -S /tmp/supervisor.sock' 2>/dev/null && break
    sleep 1
done
if ! docker exec "${CONTAINER}" sh -c 'test -S /tmp/supervisor.sock' 2>/dev/null; then
    echo "    контейнер не поднялся, смотрите: docker logs ${CONTAINER}" >&2
    exit 1
fi

# --- 3. Быстрый путь: уже поднято и пропатчено ------------------------------
need_patch=1
if ! docker exec "${CONTAINER}" grep -q "serial_dev.rts = rts" \
        /home/printer/klipper/klippy/serialhdl.py 2>/dev/null \
   && curl -s "${MOONRAKER}/printer/info" 2>/dev/null | grep -q '"state":"ready"'; then
    say "стенд уже поднят и пропатчен — ничего не перезапускаю"
    need_patch=0
fi

# --- 4. Патч + ожидание ready ---------------------------------------------
if [ "${need_patch}" = "1" ]; then
    bash apply-mock-fix.sh || exit 1
fi

# --- 5. Итог ---------------------------------------------------------------
echo
say "готово. Moonraker: ${MOONRAKER} | Mainsail: http://localhost:6969"
echo "    klippy: $(curl -s "${MOONRAKER}/printer/info" | grep -o '"state":"[a-z]*"' | head -1)"

# адрес того интерфейса, через который есть выход в локальную сеть
ips=""
if command -v powershell >/dev/null 2>&1; then
    ips=$(powershell -NoProfile -Command \
        "Get-NetIPConfiguration | Where-Object { \$_.IPv4DefaultGateway -ne \$null } | ForEach-Object { \$_.IPv4Address.IPAddress }" \
        2>/dev/null | tr -d '\r')
fi
if [ -z "$(echo "${ips}" | tr -d '[:space:]')" ]; then
    ips=$( { ip route get 1.1.1.1 2>/dev/null | grep -oE 'src ([0-9.]+)' | awk '{print $2}'; hostname -I 2>/dev/null; } \
            | tr ' ' '\n' | grep -E '^([0-9]{1,3}\.){3}[0-9]{1,3}$' )
fi
# выбрасываем адреса Docker/WSL/WSL-сетей, чтобы не путать
ips=$(echo "${ips}" | tr -d '\r' \
        | grep -vE '^(127\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.)')
if [ -n "$(echo "${ips}" | tr -d '[:space:]')" ]; then
    echo "    адрес ПК для экрана (в настройках экрана укажите его и порт 7125):"
    echo "${ips}" | sed 's/^/        /'
fi
echo
echo "    остановить: cd test_printer && docker compose down"
