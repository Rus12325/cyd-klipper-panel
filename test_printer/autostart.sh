#!/usr/bin/env bash
# Автозапуск проводного стенда CYD-Klipper при входе в систему (Windows).
# Вся логика — в autostart.py, здесь только удобный вход и выбор интерпретатора.
#
#   bash test_printer/autostart.sh install     # поставить и запустить сейчас
#   bash test_printer/autostart.sh status      # что установлено и что живо
#   bash test_printer/autostart.sh stop        # выключить автозапуск и остановить стенд
#   bash test_printer/autostart.sh start       # включить и запустить сейчас
#   bash test_printer/autostart.sh uninstall   # убрать из автозапуска
set -euo pipefail

cd "$(dirname "$0")"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python >/dev/null 2>&1; then
    PY=python
  else
    PY=python3
  fi
fi

exec "$PY" autostart.py "$@"
