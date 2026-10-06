#!/usr/bin/env bash
# Проводной прогон экрана: Python-мок Moonraker + serial-мост по USB.
# Вся логика — в wired_test.py, здесь только удобный вход и выбор интерпретатора.
#
#   bash test_printer/wired.sh              # поднять и оставить работать
#   bash test_printer/wired.sh --demo 30    # авто-прогон и остановка
#   bash test_printer/wired.sh --status     # что сейчас происходит
#   bash test_printer/wired.sh --stop       # остановить мок и мост
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

exec "$PY" wired_test.py "$@"
