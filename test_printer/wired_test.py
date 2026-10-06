#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проводной прогон экрана: Python-мок Moonraker + serial-мост, всё на одном ПК.

Экран в режиме «Klipper (Serial)» сам HTTP не умеет: он посылает в USB-serial
строки вида `HTTP_REQUEST 1000 GET /printer/info`, а ответы ему отдаёт мост
`serial/` из репозитория CYD-Klipper. Скрипт поднимает обе половины и проверяет,
что до экрана доехал реальный ответ, поэтому экран можно проверить **без Wi-Fi
и без принтера**.

    bash test_printer/wired.sh                 # поднять и оставить работать
    bash test_printer/wired.sh --demo 30       # авто-прогон: печать 30 с и остановка
    bash test_printer/wired.sh --follow        # показывать логи экрана и моста
    bash test_printer/wired.sh --status        # что происходит сейчас
    bash test_printer/wired.sh --stop          # остановить мост и мок

Чтобы стенд поднимался сам при входе в систему (без ручного запуска), есть
`autostart.py`: он следит за моком и мостом и восстанавливает их после падений.

Зависимости: pyserial и requests (те же, что у моста). Мок и мост запускаются
отдельными процессами, их PID-ы и логи лежат в test_printer/.run/.
"""

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(HERE, ".run")
SERIAL_DIR = os.path.join(HERE, ".serial")
BRIDGE_PATH = os.path.join(SERIAL_DIR, "serial_server.py")
BRIDGE_URL = ("https://raw.githubusercontent.com/suchmememanyskill/CYD-Klipper/"
              "{ref}/serial/serial_server.py")
BRIDGE_REF = "v2.1.2"

# CH340 / CH9102 / CP210x — то, что стоит на платах CYD. Сам мост ищет только
# CH340 (1A86:7523) и CP210x (10C4:EA60), поэтому порт находим сами и передаём
# ему в ESP32_SERIAL.
SCREEN_CHIPS = ((0x1A86, 0x7523), (0x1A86, 0x55D4), (0x10C4, 0xEA60))

SUPERVISOR_RECORD = "supervisor.json"

PYTHON = sys.executable or "python"
DEVNULL = subprocess.DEVNULL


def say(text=""):
    print(text, flush=True)


def step(text):
    say()
    say("=== %s ===" % text)


def note(text):
    say("    " + text)


def fail(text, code=1):
    say()
    say("ОШИБКА: %s" % text)
    sys.exit(code)


# --- HTTP к моку ------------------------------------------------------------


def http(path, port, method="GET", timeout=4.0):
    """(код, тело) или (None, текст ошибки), если мок не ответил."""
    url = "http://127.0.0.1:%d%s" % (port, path)
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # не поднялся / порт занят / соединение сброшено
        return None, str(exc)


def mock_state(port):
    """Сводка мока или None, если на порту не наш мок."""
    code, body = http("/mock/state", port)
    if code != 200:
        return None
    try:
        return json.loads(body)["result"]
    except Exception:
        return None


def probe_port(port):
    """(занят_ли, наш_ли_мок)."""
    state = mock_state(port)
    if state is not None:
        return True, True
    code, _ = http("/printer/info", port, timeout=2.0)
    return code is not None, False


def mock_action(port, action, **params):
    params["action"] = action
    return http("/mock/action?" + urllib.parse.urlencode(params), port, method="POST")


# --- процессы ---------------------------------------------------------------


def record_path(name):
    return os.path.join(RUN_DIR, name + ".json")


def record(name, pid, **extra):
    os.makedirs(RUN_DIR, exist_ok=True)
    payload = {"pid": pid, "started_at": time.time()}
    payload.update(extra)
    with open(record_path(name), "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def recorded(name):
    try:
        with open(record_path(name), encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32)]


def process_created(pid):
    """Unix-время создания процесса или None, если так не получилось."""
    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created, exited, kernel, user = (FILETIME() for _ in range(4))
        ok = kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(kernel), ctypes.byref(user))
        if not ok:
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ticks / 1e7 - 11644473600.0  # FILETIME → unix
    finally:
        kernel32.CloseHandle(handle)


def alive(pid, started_at=None):
    """Жив ли процесс. С started_at — ещё и не переиспользован ли его PID.

    Windows быстро отдаёт освободившийся PID другому процессу, и тогда
    OpenProcess радостно подтвердит «жив» умершему моку или мосту — поэтому
    сравниваем время создания процесса с тем, что записали при запуске.
    """
    if not pid:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) на Windows — это не проверка, а TerminateProcess.
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        if started_at:
            created = process_created(pid)
            if created is not None and abs(created - float(started_at)) > 10:
                return False
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def kill(pid):
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=DEVNULL, stderr=DEVNULL, check=False)
    else:
        try:
            os.kill(int(pid), 15)
        except OSError:
            pass


def spawn(cmd, log_name, env=None):
    os.makedirs(RUN_DIR, exist_ok=True)
    log_path = os.path.join(RUN_DIR, log_name + ".log")
    handle = open(log_path, "ab", buffering=0)
    handle.write(("\n--- запуск %s ---\n" % time.strftime("%H:%M:%S")).encode("utf-8"))
    child_env = dict(os.environ)
    child_env["PYTHONUNBUFFERED"] = "1"  # иначе лог до самого конца останется пустым
    if env:
        child_env.update(env)
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    return subprocess.Popen(cmd, cwd=HERE, stdout=handle, stderr=subprocess.STDOUT,
                            stdin=DEVNULL, env=child_env, creationflags=creationflags)


def stop_all(only=None):
    # Внимание: only=[] — это «ничего», а не «всё»: иначе --demo валил бы чужой
    # стенд, поднятый не этим запуском (only or (... ) тут не годится).
    names = ("bridge", "mock") if only is None else tuple(only)
    stopped = []
    for name in names:
        info = recorded(name)
        if not info or not alive(info.get("pid"), info.get("started_at")):
            continue
        kill(info["pid"])
        stopped.append("%s (PID %s)" % (name, info["pid"]))
    return stopped


# --- мост -------------------------------------------------------------------


def ensure_bridge(ref):
    """Мост — файл из репозитория CYD-Klipper; при первом запуске скачиваем."""
    if os.path.isfile(BRIDGE_PATH):
        return
    os.makedirs(SERIAL_DIR, exist_ok=True)
    url = BRIDGE_URL.format(ref=ref)
    say("    скачиваю serial_server.py (%s)..." % ref)
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read()
    except Exception as exc:
        fail("не смог скачать %s: %s\nМост лежит в репозитории CYD-Klipper: "
             "serial/serial_server.py" % (url, exc))
    text = data.decode("utf-8")
    compile(text, "serial_server.py", "exec")  # что скачали, то и запустим
    with open(BRIDGE_PATH, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    with open(os.path.join(SERIAL_DIR, "SOURCE"), "w", encoding="utf-8") as handle:
        handle.write(url + "\n")


def missing_deps():
    """Чего не хватает из питоновских пакетов (без падения — нужно автостарту)."""
    missing = []
    for module in ("serial", "requests"):
        try:
            __import__(module)
        except Exception:
            missing.append(module)
    return missing


def check_deps():
    missing = missing_deps()
    if missing:
        fail("нет python-пакетов: %s\nПоставьте их: %s -m pip install pyserial requests"
             % (", ".join(missing), PYTHON))


def list_screen_ports():
    """Все платы экрана на USB (CH340/CH9102/CP210x) — без падений и подсказок."""
    import serial.tools.list_ports as list_ports

    return [p for p in list_ports.comports()
            if (getattr(p, "vid", None), getattr(p, "pid", None)) in SCREEN_CHIPS]


def supervisor_state(max_age=180.0):
    """Жив ли автозапуск (autostart.py supervise).

    Если он работает, --stop бесполезен: супервизор поднимет стенд заново.
    """
    try:
        with open(os.path.join(RUN_DIR, SUPERVISOR_RECORD), encoding="utf-8") as handle:
            info = json.load(handle)
    except Exception:
        return None
    if not alive(info.get("pid"), info.get("started_at")):
        return None
    if max_age and time.time() - float(info.get("heartbeat") or 0) > max_age:
        return None
    return info


def find_screen_port(explicit=None):
    ports = list_screen_ports()
    if explicit:
        for port in ports:
            if port.device.upper() == explicit.upper():
                return port
        fail("порта %s нет в системе. Есть: %s"
             % (explicit, ", ".join(p.device for p in ports) or "ничего"))
    found = ports
    if not found:
        fail("не нашёл плату экрана (CH340/CH9102/CP210x).\n"
             "Подключите экран по USB и повторите либо задайте порт: --port COM5")
    if len(found) > 1:
        fail("нашёл несколько плат: %s\nЗадайте нужную: --port %s"
             % (", ".join(p.device for p in found), found[0].device))
    return found[0]


def port_is_free(device):
    import serial

    try:
        port = serial.Serial(device, 115200, timeout=0.2)
    except Exception as exc:
        return False, str(exc)
    port.close()
    return True, ""


def pulse_reset(device):
    """Дёрнуть RTS → на CYD это кнопка RST, экран перезагружается."""
    import serial

    port = serial.Serial(device, 115200, timeout=0.2)
    try:
        port.setDTR(False)
        port.setRTS(True)
        time.sleep(0.15)
        port.setRTS(False)
    finally:
        port.close()


def bridge_log_path():
    return os.path.join(RUN_DIR, "bridge.log")


def log_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def bridge_traffic(offset=0):
    """Что экран попросил и что ему ответили, начиная с байта offset.

    Возвращает (запросов, ответов, ответов_с_200, список_запросов).
    """
    try:
        with open(bridge_log_path(), encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            text = handle.read()
    except OSError:
        return 0, 0, 0, []
    requests = [line[4:].strip() for line in text.splitlines()
                if line.startswith(">>> ") and ("HTTP_REQUEST" in line or "HTTP_BINARY" in line)]
    replies = [line for line in text.splitlines()
               if line.startswith("<<< ") and not line.startswith("(Ignored)")]
    good = [line for line in replies if " 200 " in line or "Binary data" in line]
    return len(requests), len(replies), len(good), requests


def wait_for_screen(offset, base_answered, timeout, poll=0.5):
    """Ждём роста числа ответов: это и есть доказательство живого канала."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        asked, answered, good, requests = bridge_traffic(offset)
        if answered > base_answered:
            return asked, answered, good, requests
        time.sleep(poll)
    return bridge_traffic(offset)


# --- сценарий ---------------------------------------------------------------


def start_mock(port, print_seconds, quiet=False):
    cmd = [PYTHON, "-u", os.path.join(HERE, "mock_moonraker.py"),
           "--host", "127.0.0.1", "--port", str(port),
           "--files", os.path.join(HERE, "gcodes"),
           "--print-seconds", str(print_seconds)]
    if quiet:
        cmd.append("--quiet")
    child = spawn(cmd, "mock")
    record("mock", child.pid, port=port, cmd=" ".join(cmd))
    deadline = time.time() + 15
    while time.time() < deadline:
        state = mock_state(port)
        if state is not None:
            return child, state
        if child.poll() is not None:
            fail("мок завершился сразу (код %s). Лог: %s"
                 % (child.poll(), os.path.join(RUN_DIR, "mock.log")))
        time.sleep(0.3)
    fail("мок не ответил за 15 с. Лог: %s" % os.path.join(RUN_DIR, "mock.log"))


def start_bridge(device, mock_port):
    env = {
        "ESP32_SERIAL": device,
        "KLIPPER_HOST": "127.0.0.1",
        "KLIPPER_PORT": str(mock_port),
        "KLIPPER_PROTOCOL": "http",
    }
    start_offset = log_size(bridge_log_path())  # чтобы --status считал обмен этого запуска
    child = spawn([PYTHON, "-u", BRIDGE_PATH], "bridge", env=env)
    record("bridge", child.pid, port=device, mock_port=mock_port, cmd="serial_server.py",
           log_offset=start_offset)
    return child


def start_print_loop(mock_port, filename, pause=3.0):
    """Перезапускать тестовую печать, чтобы экран всё время что-то показывал.

    Состояние klippy не трогаем: если вы из пульта устроили ошибку или паузу,
    цикл просто ждёт — иначе крайние случаи было бы не проверить.
    """
    def run():
        while True:
            time.sleep(2.0)
            state = mock_state(mock_port)
            if state is None:
                return
            if state["print_state"] in ("complete", "cancelled"):
                time.sleep(pause)
                mock_action(mock_port, "start", filename=filename)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def idle_until_interrupt():
    """Держим скрипт живым (для --loop), не мешая работать моку и мосту."""
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        say()
        note("вышли. Мок и мост продолжают работать: остановить — bash test_printer/wired.sh --stop")


def show_status(mock_port):
    step("Состояние")
    busy, ours = probe_port(mock_port)
    if ours:
        state = mock_state(mock_port)
        note("мок на 127.0.0.1:%d — жив: klippy=%s, печать=%s, прогресс=%.1f%%"
             % (mock_port, state["state"], state["print_state"], state["progress"] * 100))
        note("пульт: http://127.0.0.1:%d/ui" % mock_port)
    elif busy:
        note("порт %d занят, но это не наш мок (Docker-стенд или реальный Moonraker)" % mock_port)
    else:
        note("мок на 127.0.0.1:%d не запущен" % mock_port)

    info = recorded("bridge")
    if info and alive(info.get("pid"), info.get("started_at")):
        asked, answered, good, requests = bridge_traffic(info.get("log_offset", 0))
        note("мост жив (PID %s, порт %s): за этот запуск экран прислал %d запросов, мост отдал %d ответов (успешных %d)"
             % (info["pid"], info.get("port"), asked, answered, good))
        for line in requests[-5:]:
            note("  ← " + line)
    else:
        note("мост не запущен — экран в USB-режиме будет показывать таймауты")

    sup = supervisor_state()
    if sup:
        note("автозапуск работает (PID %s, следит с %s)"
             % (sup["pid"], time.strftime("%H:%M:%S", time.localtime(sup.get("started_at") or 0))))
    else:
        note("автозапуск не работает — стенд придётся поднимать вручную "
             "(bash test_printer/autostart.sh start)")


def follow(names=("bridge", "mock")):
    step("Живой лог (Ctrl+C — выйти, процессы останутся работать)")
    offsets = {}
    paths = {}
    for name in names:
        path = os.path.join(RUN_DIR, name + ".log")
        paths[name] = path
        offsets[name] = log_size(path)
    try:
        while True:
            for name, path in paths.items():
                if not os.path.exists(path):
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offsets[name])
                    chunk = handle.read()
                    offsets[name] = handle.tell()
                for line in chunk.splitlines():
                    if line.strip():
                        say("[%s] %s" % (name, line))
            time.sleep(0.4)
    except KeyboardInterrupt:
        say()
        note("вышли из режима логов; остановить стенд: bash test_printer/wired.sh --stop")


def main():
    try:  # чтобы русский текст на Windows не превращался в кракозябры
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Проводной прогон экрана CYD-Klipper: Python-мок + serial-мост по USB",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-p", "--port", default=None, help="COM-порт экрана (иначе найдётся сам)")
    parser.add_argument("--mock-port", type=int, default=7125, help="порт мока (7125)")
    parser.add_argument("--file", default=None,
                        help="что печатать (по умолчанию первый файл из gcodes/)")
    parser.add_argument("--print-seconds", type=float, default=120.0,
                        help="сколько мок «печатает» файл, секунд (120)")
    parser.add_argument("--timeout", type=float, default=45.0,
                        help="сколько ждать запросов от экрана, секунд (45)")
    parser.add_argument("--demo", type=float, default=None, metavar="СЕК",
                        help="авто-прогон: печать, наблюдение СЕК секунд, затем остановка")
    parser.add_argument("--no-print", action="store_true", help="не запускать тестовую печать")
    parser.add_argument("--loop", action="store_true",
                        help="перезапускать тестовую печать, пока скрипт работает (Ctrl+C — выйти)")
    parser.add_argument("--no-reset", action="store_true",
                        help="не сбрасывать экран, если он молчит")
    parser.add_argument("--follow", action="store_true", help="показывать логи экрана и моста")
    parser.add_argument("--quiet", action="store_true", help="не печатать каждый запрос в логе мока")
    parser.add_argument("--status", action="store_true", help="показать состояние и выйти")
    parser.add_argument("--stop", action="store_true", help="остановить мост и мок")
    parser.add_argument("--bridge-ref", default=BRIDGE_REF,
                        help="тег/ветка CYD-Klipper для serial_server.py (%s)" % BRIDGE_REF)
    args = parser.parse_args()

    if args.stop:
        step("Останавливаю стенд")
        stopped = stop_all()
        note("остановлено: %s" % (", ".join(stopped) if stopped else "нечего"))
        if stopped:
            note("COM-порт освобождён — экран можно занимать снова")
        sup = supervisor_state()
        if sup:
            note("но работает автозапуск (PID %s): он поднимет стенд снова, "
                 "чтобы выключить — bash test_printer/autostart.sh stop" % sup["pid"])
        return 0

    if args.status:
        show_status(args.mock_port)
        return 0

    started = []

    # 1. мок
    step("1/5. Python-мок Moonraker")
    busy, ours = probe_port(args.mock_port)
    if ours:
        state = mock_state(args.mock_port)
        note("уже работает на 127.0.0.1:%d (klippy=%s, печать=%s)"
             % (args.mock_port, state["state"], state["print_state"]))
    elif busy:
        fail("порт %d занят не нашим моком (Docker-стенд или реальный Moonraker).\n"
             "Либо остановите его, либо возьмите другой порт: --mock-port 7126" % args.mock_port)
    else:
        child, state = start_mock(args.mock_port, args.print_seconds, args.quiet)
        started.append("mock")
        note("поднят на 127.0.0.1:%d (PID %s), файлы: test_printer/gcodes"
             % (args.mock_port, child.pid))

    # 2. мост
    step("2/5. Serial-мост к экрану")
    check_deps()
    ensure_bridge(args.bridge_ref)
    bridge_info = recorded("bridge")
    if bridge_info and alive(bridge_info.get("pid"), bridge_info.get("started_at")):
        note("мост уже запущен (PID %s, порт %s)" % (bridge_info["pid"], bridge_info.get("port")))
        offset = 0  # старый лог тоже считаем: важно только, растёт ли число ответов
    else:
        screen = find_screen_port(args.port)
        free, reason = port_is_free(screen.device)
        if not free:
            fail("порт %s занят: %s\nЗакройте приложение, которое его держит "
                 "(монитор экрана, serial-терминал, второй мост), и повторите."
                 % (screen.device, reason))
        note("экран: %s — %s (%04x:%04x)"
             % (screen.device, screen.description, screen.vid or 0, screen.pid or 0))
        offset = log_size(bridge_log_path())
        start_bridge(screen.device, args.mock_port)
        started.append("bridge")
        note("мост запущен (PID %s)" % recorded("bridge")["pid"])

    # 3. ждём, что экран спросил и получил ответ
    step("3/5. Экран спрашивает через USB")

    def traffic():
        """Счётчики обмена с текущей позиции лога (offset читается из внешней области)."""
        asked, answered, good, requests = bridge_traffic(offset)
        return asked, answered, good, requests

    base = traffic()
    asked, answered, good, requests = wait_for_screen(offset, base[1], args.timeout)
    if answered <= base[1] and not args.no_reset:
        note("тишина — сбрасываю экран (RTS) и жду ещё %g с" % args.timeout)
        info = recorded("bridge") or {}
        device = info.get("port") or args.port
        if not device:
            device = find_screen_port(None).device
        was_ours = "bridge" in started
        stop_all(("bridge",))
        if was_ours:
            started.remove("bridge")
        time.sleep(1.0)
        try:
            pulse_reset(device)
        except Exception as exc:
            note("сбросить не получилось: %s" % exc)
        offset = log_size(bridge_log_path())
        start_bridge(device, args.mock_port)
        started.append("bridge")
        base = traffic()
        asked, answered, good, requests = wait_for_screen(offset, base[1], args.timeout)

    # Считаем только новый обмен: если мост уже работал, в его логе лежит история.
    fresh_asked = asked - base[0]
    fresh_answered = answered - base[1]
    fresh_good = good - base[2]
    fresh_requests = requests[base[0]:]
    if fresh_answered == 0:
        say()
        note("запросов от экрана: %d, ответов отдано: %d" % (fresh_asked, fresh_answered))
        tail = []
        try:
            with open(bridge_log_path(), encoding="utf-8", errors="replace") as handle:
                tail = [line for line in handle.read().splitlines() if line.strip()][-8:]
        except OSError:
            pass
        for line in tail:
            note("  " + line)
        note("Что проверить:")
        note("  - экран подключён по USB и показывает «Klipper (Serial) Setup»;")
        note("  - на экране выбран именно USB/serial вариант, а не Wi-Fi;")
        note("  - COM-порт не занят другим приложением (bash test_printer/wired.sh --status);")
        note("  - сырой serial: python firmware/serial_log.py -p %s --reset" % (args.port or "COM5"))
        if started:
            stop_all(started)
        return 1

    note("экран прислал %d запрос%s, мост отдал %d ответ%s (успешных %d) — канал живой"
         % (fresh_asked, "" if fresh_asked == 1 else "ов",
            fresh_answered, "" if fresh_answered == 1 else "ов", fresh_good))
    for line in fresh_requests[:4]:
        note("  ← " + line)

    # 4. печать
    step("4/5. Тестовая печать")
    state = mock_state(args.mock_port)
    if state is None:
        fail("мок пропал. Лог: %s" % os.path.join(RUN_DIR, "mock.log"))
    if args.no_print:
        note("пропущено (--no-print), экран показывает состояние принтера")
    else:
        if state["state"] != "ready":
            note("klippy в состоянии %s — делаю FIRMWARE_RESTART" % state["state"])
            mock_action(args.mock_port, "reset")
            time.sleep(0.5)
            state = mock_state(args.mock_port)
        filename = args.file or (state["files"][0] if state["files"] else None)
        if not filename:
            fail("в test_printer/gcodes нет .gcode — положите файл или укажите --file")
        code, body = mock_action(args.mock_port, "start", filename=filename)
        if code != 200:
            fail("мок не запустил печать %s: %s %s" % (filename, code, body))
        note("печатаю %s (в моке это %g с)" % (filename, args.print_seconds))

    # 5. итог
    step("5/5. Что видно")
    state = mock_state(args.mock_port)
    note("klippy=%s, печать=%s, файл=%s, прогресс=%.1f%%, слой %s/%s"
         % (state["state"], state["print_state"], state["filename"],
            state["progress"] * 100, state["current_layer"], state["total_layer"]))
    note("экран должен показывать: сопло %.1f °C / цель %.1f °C, стол %.1f / %.1f"
         % (state["extruder"]["temperature"], state["extruder"]["target"],
            state["heater_bed"]["temperature"], state["heater_bed"]["target"]))
    note("позиция %s, сообщение %r" % (state["position"], state["message"]))

    if args.loop and not args.no_print and args.demo is None:
        filename = args.file or state["filename"]
        start_print_loop(args.mock_port, filename)
        note("печать будет запускаться заново после конца (--loop): экран всё время что-то показывает")

    if args.demo is not None:
        say()
        note("наблюдаю %g с, чтобы прогресс вырос..." % args.demo)
        demo_offset = log_size(bridge_log_path())
        first = state["progress"]
        deadline = time.time() + args.demo
        while time.time() < deadline:
            time.sleep(2.0)
            now = mock_state(args.mock_port)
            if now is None:
                note("мок перестал отвечать")
                break
            asked, answered, _, _ = bridge_traffic(demo_offset)
            note("прогресс %.1f%% · слой %s/%s · экран прислал %d запросов"
                 % (now["progress"] * 100, now["current_layer"], now["total_layer"], asked))
        final = mock_state(args.mock_port)
        asked, answered, good, _ = bridge_traffic(demo_offset)
        grew = bool(final) and final["progress"] > first
        say()
        note("прогресс %.1f%% → %.1f%% (%s), запросов от экрана за прогон %d, успешных ответов %d"
             % (first * 100, (final or {}).get("progress", 0) * 100,
                "растёт" if grew else "не вырос", asked, good))
        stopped = stop_all(started)
        note("остановлено: %s"
             % (", ".join(stopped) if stopped else "нечего (мок и мост подняты не этим запуском)"))
        say()
        if not grew:
            say("Итог: до экрана доходили ответы, но тестовая печать не сдвинулась — смотрите логи.")
            return 1
        say("Итог: проводной путь проверен — экран получал ответы мока, тестовая печать шла.")
        return 0

    say()
    note("Оставлено работать: мок http://127.0.0.1:%d/ui, мост на %s"
         % (args.mock_port, (recorded("bridge") or {}).get("port")))
    note("Остановить: bash test_printer/wired.sh --stop")
    note("Пока мост остановлен, экран в USB-режиме покажет таймауты: сам он HTTP не умеет.")

    if args.follow:
        follow()
    elif args.loop and not args.demo:
        say()
        note("Ctrl+C — выйти из скрипта (мок и мост останутся работать)")
        idle_until_interrupt()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say()
        note("прервано. Остановить стенд: bash test_printer/wired.sh --stop")
        sys.exit(130)
