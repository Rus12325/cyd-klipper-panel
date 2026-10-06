#!/usr/bin/env bash
# Патч виртуального принтера mainsail-crew/virtual-klipper-printer.
#
# Без него klippy не подключается к эмулируемому MCU (simulavr): при открытии
# псевдотерминала klippy выставляет RTS и exclusive-lock, после чего ответы MCU
# до хоста не доходят. Симптомы: Moonraker отвечает "Printer is not ready",
# в klippy.log — "Timeout on connect" / "Serial connection closed".
#
# Дополнительно скрипт перезапускает сам симулятор MCU: avrsim живёт между
# перезапусками klippy, а уже сконфигурированный MCU не принимает новую
# конфигурацию ("Can not update MCU 'mcu' config as it is shutdown").
#
# Скрипт идемпотентный. Запуск: bash test_printer/apply-mock-fix.sh
set -u

CONTAINER="${CONTAINER:-printer}"
MOONRAKER="${MOONRAKER:-http://localhost:7125}"

echo "==> патчу klippy/serialhdl.py внутри контейнера ${CONTAINER}"
docker exec -i "${CONTAINER}" /usr/local/bin/python3.12 - <<'PY'
p = "/home/printer/klipper/klippy/serialhdl.py"
s = open(p).read()
before = s
# 1) не выставлять RTS на псевдотерминале
s = s.replace("                serial_dev.rts = rts\n", "")
# 2) не брать exclusive-lock на псевдотерминале
s = s.replace(
    """serial_dev = serial.Serial(baudrate=baud, timeout=0,
                                           exclusive=True)""",
    "serial_dev = serial.Serial(baudrate=baud, timeout=0)")
# 3) эмулируемый MCU медленнее железа — расширяем окно ожидания identify
s = s.replace("completion.wait(self.reactor.monotonic() + 5.)",
              "completion.wait(self.reactor.monotonic() + 60.)")
if s == before:
    print("    уже пропатчено — ничего не меняю")
else:
    open(p, "w").write(s)
    print("    патч применён")
PY

echo "==> перезапускаю симулятор MCU (нужен свежий, не сконфигурированный MCU)"
docker exec "${CONTAINER}" supervisorctl restart simulavr >/dev/null 2>&1
sleep 14

echo "==> перезапускаю klippy"
docker exec "${CONTAINER}" supervisorctl restart klipper >/dev/null 2>&1

echo "==> жду состояние ready"
for i in $(seq 1 18); do
    sleep 5
    state=$(curl -s "${MOONRAKER}/printer/info" 2>/dev/null | grep -o '"state":"[a-z]*"' | head -1)
    echo "    t=$((i * 5))s ${state:-нет ответа}"
    case "${state}" in
        *'"state":"ready"'*)
            echo "==> OK: mock-принтер готов ($MOONRAKER)"
            exit 0
            ;;
    esac
done

echo "==> не дождался ready. Логи:"
echo "    docker exec ${CONTAINER} tail -30 /home/printer/printer_data/logs/klippy.log"
exit 1
