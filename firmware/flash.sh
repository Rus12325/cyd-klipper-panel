#!/usr/bin/env bash
# Прошивка CYD-Klipper на CYD (ESP32-2432S028R и родственные) через esptool.
# Это автоматизация «резервного способа» из раздела 3 гайда CYD-Klipper-CYD-2USB.md.
#
# Что делает:
#   1. проверяет, что есть python + esptool + свободный COM-порт;
#   2. (по желанию) закрывает приложения, которые держат порт, — по имени CYD-PC-Monitor.exe;
#   3. скачивает firmware.zip нужного релиза с GitHub (если его ещё нет);
#   4. распаковывает папку конкретной платы из архива;
#   5. опционально стирает флеш целиком (первая установка / после чужой прошивки);
#   6. пишет merged_firmware.bin на 0x0, затем перечитывает флеш и сверяет хеш;
#   7. слушает serial-консоль, чтобы подтвердить, что прошивка стартовала.
#
# Примеры:
#   bash flash.sh                                   # COM5, esp32-2432S028R, полная очистка
#   bash flash.sh --close-monitor                   # сначала закрыть CYD-PC-Monitor (держится COM5)
#   bash flash.sh -p COM7 --no-erase                # другой порт, без очистки (быстрее и без erase)
#   bash flash.sh --board esp32-3248S035C           # другая плата (папка из firmware.zip)
#   bash flash.sh --check                           # ничего не писать: только порт + чип
#   bash flash.sh --list-boards                     # какие папки есть в архиве релиза
#
# Nuance: образ merged_firmware.bin покрывает 0x0..~0x14xxxx и включает область nvs,
# поэтому Wi-Fi и настройки принтера обнуляются в любом случае — даже без erase-flash.

set -euo pipefail

# Внимание: имена переменных с префиксом CYD_ не случайны — простые PORT/BOARD уже
# встречаются в окружении (в Git Bash на Windows в PORT лежит '0'), и скрипт брал бы их.
PORT="${CYD_PORT:-COM5}"
BOARD="${CYD_BOARD:-esp32-2432S028R}"
VERSION="${CYD_VERSION:-v2.1.2}"
BAUD="${CYD_BAUD:-460800}"
DO_ERASE=1
CHECK_ONLY=0
CLOSE_MONITOR=0
SKIP_LOG=0
LIST_BOARDS=0

usage() {
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
}

while [ $# -gt 0 ]; do
    case "$1" in
        -p|--port)       PORT="$2"; shift 2 ;;
        --board)         BOARD="$2"; shift 2 ;;
        -V|--version)    VERSION="$2"; shift 2 ;;
        -B|--baud)       BAUD="$2"; shift 2 ;;
        --no-erase)      DO_ERASE=0; shift ;;
        --check)         CHECK_ONLY=1; shift ;;
        --close-monitor) CLOSE_MONITOR=1; shift ;;
        --no-log)        SKIP_LOG=1; shift ;;
        --list-boards)   LIST_BOARDS=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *) echo "Неизвестный параметр: $1"; echo; usage; exit 2 ;;
    esac
done

# Python на Windows по умолчанию пишет в OEM-кодировке консоли, из-за чего русские
# строки вперемешку с выводом bash выглядят кракозябрами. Один раз договариваемся о UTF-8.
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
ZIP="firmware.zip"
EXTRACTED="extracted"
PY="${CYD_PY:-python}"

log() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

# --- 0. инструменты -----------------------------------------------------------
if ! command -v "$PY" >/dev/null 2>&1; then
    for cand in python3 python; do
        command -v "$cand" >/dev/null 2>&1 && { PY="$cand"; break; }
    done
fi
command -v "$PY" >/dev/null 2>&1 || die "не найден python"
"$PY" -m esptool version >/dev/null 2>&1 || die "esptool не найден. Поставьте: $PY -m pip install esptool"
"$PY" -c "import serial" >/dev/null 2>&1 || die "pyserial не найден. Поставьте: $PY -m pip install pyserial"

# --- 1. освободить порт -------------------------------------------------------
if [ "$CLOSE_MONITOR" = 1 ]; then
    log "Закрываю приложения, которые обычно держат COM-порт (CYD-PC-Monitor)"
    if command -v taskkill >/dev/null 2>&1; then
        taskkill //IM CYD-PC-Monitor.exe //F 2>/dev/null && echo "  закрыт CYD-PC-Monitor.exe" || echo "  CYD-PC-Monitor.exe не запущен"
        sleep 2
    else
        echo "  taskkill недоступен (не Windows) — закройте приложение вручную"
    fi
fi

# --- 2. порт свободен? --------------------------------------------------------
log "Проверяю порт $PORT"
"$PY" - "$PORT" <<'EOF'
import sys
try:
    import serial
except ImportError:
    print("pyserial не установлен"); sys.exit(1)
port = sys.argv[1]
try:
    s = serial.Serial(port, 115200, timeout=0.2)
    s.close()
    print("  порт %s свободен" % port)
except Exception as exc:
    print("  порт %s открыть не удалось: %s" % (port, exc))
    print("  Порт держит другое приложение (монитор порта, IDE, скрипт PC-монитора).")
    print("  Закройте его и повторите; на этой машине это обычно CYD-PC-Monitor.exe")
    print("  из автозапуска (HKCU\\...\\Run\\CydPcMonitor) — либо запустите с --close-monitor.")
    sys.exit(1)
EOF

# --- 3. чип отвечает? ---------------------------------------------------------
log "Ищу чип на $PORT"
"$PY" -m esptool -p "$PORT" -b 115200 chip-id 2>&1 | grep -E "Chip type|Features|MAC|Crystal|error|Error" || true
if [ "$CHECK_ONLY" = 1 ]; then
    log "--check: дальше не иду, флеш не тронут"
    exit 0
fi

# --- 4. скачать релиз ---------------------------------------------------------
if [ ! -f "$ZIP" ]; then
    log "Скачиваю firmware.zip ($VERSION)"
    URL="https://github.com/suchmememanyskill/CYD-Klipper/releases/download/$VERSION/firmware.zip"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 -o "$ZIP" "$URL"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$ZIP" "$URL"
    else
        die "нет ни curl, ни wget — скачайте $URL вручную"
    fi
fi

# --- 5. распаковать папку платы ----------------------------------------------
log "Проверяю архив"
"$PY" - "$ZIP" "$EXTRACTED" "$BOARD" "$LIST_BOARDS" <<'EOF'
import sys, zipfile, os

zip_path, out_dir, board, list_only = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
with zipfile.ZipFile(zip_path) as z:
    boards = sorted({n.split("/")[0] for n in z.namelist() if "/" in n})
    if list_only:
        print("  Платы в архиве (%d):" % len(boards))
        for b in boards:
            print("   ", b)
        sys.exit(0)
    if board not in boards:
        print("  Папки '%s' в архиве нет. Доступные платы:" % board)
        for b in boards:
            print("   ", b)
        sys.exit(1)
    members = [n for n in z.namelist() if n.startswith(board + "/") and not n.endswith("/")]
    for n in members:
        z.extract(n, out_dir)
    print("  распаковано файлов: %d -> %s" % (len(members), os.path.join(out_dir, board)))
EOF

if [ "$LIST_BOARDS" = 1 ]; then
    exit 0
fi

IMG="$EXTRACTED/$BOARD/merged_firmware.bin"
[ -f "$IMG" ] || die "нет файла $IMG"
log "Образ: $IMG ($(wc -c < "$IMG") байт)"

# --- 6. очистка ---------------------------------------------------------------
if [ "$DO_ERASE" = 1 ]; then
    log "Стираю флеш целиком (это первая установка: чистая nvs, никаких следов прежней прошивки)"
    "$PY" -m esptool -p "$PORT" -b "$BAUD" erase-flash
else
    log "--no-erase: стирание пропущено (nvs всё равно перезапишется образом)"
fi

# --- 7. запись и проверка -----------------------------------------------------
log "Пишу образ на 0x0"
"$PY" -m esptool -p "$PORT" -b "$BAUD" write-flash 0x0 "$IMG"

log "Перечитываю флеш и сверяю хеш"
"$PY" -m esptool -p "$PORT" -b "$BAUD" verify-flash 0x0 "$IMG"

# --- 8. плата ожила? ----------------------------------------------------------
if [ "$SKIP_LOG" = 0 ]; then
    log "Слушаю serial-консоль 12 с (сброс платы, чтобы поймать загрузку)"
    # Путь именно относительный: мы уже в $DIR, а нативный Python на Windows
    # не понимает MSYS-путь вида /d/Proekt/... и читает его как D:\d\Proekt\...
    "$PY" serial_log.py -p "$PORT" --reset -s 12 2>&1 | tail -25 || true
    echo
    echo "Ожидаемая строка: CYD-Klipper $VERSION"
fi

log "Готово. Дальше: настроить Wi-Fi на тачскрине (раздел 4 гайда)"
echo "или проверить проводной режим через serial_server.py (раздел 7),"
echo "например против Python-мока из test_printer/mock_moonraker.py."
