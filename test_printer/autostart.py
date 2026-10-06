#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Автозапуск проводного стенда на Windows: мок Moonraker + serial-мост к экрану.

Ставится один раз: `install` создаёт задачу планировщика «при входе в систему»
(а если прав не хватило — файл в автозагрузке), после чего стенд поднимается сам
и восстанавливается, если что-то отвалилось. Ручной wired.sh больше не нужен.

    bash test_printer/autostart.sh install     # поставить и запустить сейчас
    bash test_printer/autostart.sh status      # что установлено и что живо
    bash test_printer/autostart.sh stop        # выключить автозапуск и остановить стенд
    bash test_printer/autostart.sh start       # включить и запустить сейчас
    bash test_printer/autostart.sh uninstall   # убрать совсем

Почему не просто задача на wired.sh: официальный serial_server.py не выходит при
ошибках, а сам повторяет попытки каждые 5 с — «процесс жив» ничего не доказывает.
Поэтому живость канала супервизор определяет по трафику в логе моста: рабочий
экран опрашивает принтер примерно раз в секунду, так что тишина видна сразу.

Состояние: test_printer/.run/supervisor.log (что делал), .run/supervisor.json
(PID и отметка времени — по ней другие скрипты понимают, что автозапуск активен).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
START = time.time()
sys.path.insert(0, HERE)
import wired_test as wired  # noqa: E402  — общие куски: записи процессов, логи, HTTP

TASK_NAME = "CYD-Klipper wired test"
CONFIG_PATH = os.path.join(HERE, "autostart.json")
SUPERVISOR_LOG = os.path.join(wired.RUN_DIR, "supervisor.log")
HEARTBEAT = os.path.join(wired.RUN_DIR, wired.SUPERVISOR_RECORD)
STOP_FLAG = os.path.join(wired.RUN_DIR, "supervisor.stop")
MAX_LOG = 1000000  # столько байт на файл лога, дальше ротация в .log.1
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                           "Start Menu", "Programs", "Startup")
STARTUP_CMD = os.path.join(STARTUP_DIR, "cydklipper-wired.cmd")

DEFAULTS = {
    "mock_port": 7125,       # куда мост ходит и на чём слушает мок
    "port": None,            # COM-порт экрана; None — искать по USB-VID
    "print": False,          # крутить тестовую печать, чтобы экран был «в работе»
    "print_seconds": 120.0,  # сколько мок печатает один прогон
    "poll": 10.0,            # как часто супервизор проверяет стенд
    "bridge_ref": wired.BRIDGE_REF,
    "quiet": True,           # не печатать каждый запрос мока в mock.log
    "reset_on_silence": True,  # один раз дёрнуть RST экрана, если он не отвечает
    "traffic_timeout": 30.0,   # сколько ждать первый запрос от экрана
    "silent_limit": 300.0,     # тишина после живого обмена = мост пора перезапустить
}

XML_TASK = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>%(user)s</Author>
    <Description>Проводной стенд CYD-Klipper: мок Moonraker и serial-мост к экрану. Ставит test_printer/autostart.py.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>%(user)s</UserId>
      <Delay>PT5S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>%(user)s</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>99</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%(python)s</Command>
      <Arguments>%(args)s</Arguments>
      <WorkingDirectory>%(cwd)s</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

BOOT_CMD = """@echo off
rem CYD-Klipper wired test: starts test_printer/autostart.py at logon.
rem Installed by: bash test_printer/autostart.sh install
rem Disable:      bash test_printer/autostart.sh stop
cd /d "{cwd}"
start "" "{python}" "{script}" supervise
"""


def say(text=""):
    if sys.stdout is not None:
        print(text, flush=True)


def step(text):
    say()
    say("=== %s ===" % text)


def note(text):
    say("    " + text)


def plural(count, one, few, many):
    """Русское склонение: 1 запрос, 2 запроса, 5 запросов."""
    count = abs(int(count))
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


# --- мелочи для Windows -----------------------------------------------------

def pythonw():
    """pythonw.exe рядом с текущим питоном: запуск без окна консоли."""
    if os.name != "nt":
        return wired.PYTHON
    candidate = os.path.join(os.path.dirname(wired.PYTHON), "pythonw.exe")
    return candidate if os.path.isfile(candidate) else wired.PYTHON


def run(cmd, timeout=60):
    """(код, вывод) для консольных утилит Windows (schtasks и т.п.)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags)
    except Exception as exc:
        return 1, str(exc)
    raw = (proc.stdout or b"") + (proc.stderr or b"")
    for encoding in ("utf-8", "cp866"):
        try:
            return proc.returncode, raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return proc.returncode, raw.decode("utf-8", "replace").strip()


def spawn_detached(cmd):
    """Запустить процесс так, чтобы он пережил закрытие терминала/сессии агента.

    Вывод уходит в supervisor.log (пишет сам супервизор), поэтому stdout — в никуда:
    иначе каждая строка лога оказалась бы в файле дважды.
    """
    os.makedirs(wired.RUN_DIR, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            env=env, creationflags=flags)


# --- состояние супервизора --------------------------------------------------

def log(text):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), text)
    try:
        os.makedirs(wired.RUN_DIR, exist_ok=True)
        if os.path.getsize(SUPERVISOR_LOG) > MAX_LOG:
            os.replace(SUPERVISOR_LOG, SUPERVISOR_LOG + ".1")
    except OSError:
        pass
    try:
        with open(SUPERVISOR_LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    say(line)


def write_heartbeat(state=None):
    payload = {"pid": os.getpid(), "started_at": START, "heartbeat": time.time(),
               "config": CONFIG_PATH}
    if state:
        payload.update({k: v for k, v in state.items() if v is None or isinstance(v, (str, int, float, bool))})
    try:
        os.makedirs(wired.RUN_DIR, exist_ok=True)
        tmp = HEARTBEAT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, HEARTBEAT)
    except OSError:
        pass


def running_supervisor():
    return wired.supervisor_state()


def read_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def effective(args):
    cfg = dict(DEFAULTS)
    cfg.update(read_config())
    if getattr(args, "mock_port", None):
        cfg["mock_port"] = args.mock_port
    if getattr(args, "port", None):
        cfg["port"] = args.port
    if getattr(args, "print", None) is not None:
        cfg["print"] = args.print
    if getattr(args, "print_seconds", None):
        cfg["print_seconds"] = args.print_seconds
    if getattr(args, "poll", None):
        cfg["poll"] = args.poll
    if getattr(args, "bridge_ref", None):
        cfg["bridge_ref"] = args.bridge_ref
    if getattr(args, "quiet", None) is not None:
        cfg["quiet"] = args.quiet
    return cfg


# --- автозапуск: задача планировщика или файл в автозагрузке ----------------

def whoami():
    user = "%s\\%s" % (os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or "",
                       os.environ.get("USERNAME") or "")
    if user == "\\":
        code, out = run(["whoami"])
        if code == 0 and out:
            return out.splitlines()[0].strip()
    return user


def task_xml():
    script = os.path.join(HERE, "autostart.py")
    return XML_TASK % {
        "user": escape(whoami()),
        "python": escape(pythonw()),
        "args": escape('"%s" supervise' % script),
        "cwd": escape(HERE),
    }


def task_state():
    """(установлена_ли, включена_ли).

    Планировщик в выгрузке /XML опускает значения по умолчанию, поэтому тег
    <Enabled> появляется только когда задачу выключали: для нас это и значит
    «включена». Смотрим только блок <Settings> — там же, где флаг и живёт.
    """
    code, out = run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"])
    if code != 0:
        return False, None
    block = re.search(r"<Settings>(.*?)</Settings>", out, re.S)
    found = re.findall(r"<Enabled>(true|false)</Enabled>", block.group(1) if block else out, re.I)
    return True, (found[-1].lower() == "true") if found else True


def task_install():
    """Пробуем задачу планировщика; возвращаем (получилось_ли, что_именно, ошибка)."""
    xml_path = os.path.join(wired.RUN_DIR, "autostart-task.xml")
    try:
        os.makedirs(wired.RUN_DIR, exist_ok=True)
        with open(xml_path, "w", encoding="utf-16") as handle:
            handle.write(task_xml())
    except OSError as exc:
        return False, xml_path, "не смог записать XML задачи: %s" % exc
    code, out = run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"])
    return code == 0, xml_path, out


def task_remove():
    code, out = run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    return code == 0, out


def task_enable(enabled):
    code, out = run(["schtasks", "/Change", "/TN", TASK_NAME,
                     "/ENABLE" if enabled else "/DISABLE"])
    return code == 0, out


def startup_install():
    os.makedirs(STARTUP_DIR, exist_ok=True)
    with open(STARTUP_CMD, "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write(BOOT_CMD.format(cwd=HERE, python=pythonw(),
                                     script=os.path.join(HERE, "autostart.py")))
    disabled = STARTUP_CMD + ".disabled"
    if os.path.exists(disabled):
        os.remove(disabled)
    return STARTUP_CMD


def startup_remove():
    removed = []
    for path in (STARTUP_CMD, STARTUP_CMD + ".disabled"):
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    return removed


def startup_state():
    if os.path.isfile(STARTUP_CMD):
        return "включён"
    if os.path.isfile(STARTUP_CMD + ".disabled"):
        return "выключен"
    return None


def start_flag_path():
    return STOP_FLAG


def wait_supervisor(timeout=25.0):
    """Ждём, что супервизор действительно поднялся (и отметился heartbeat-ом)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = running_supervisor()
        if info:
            return info
        time.sleep(0.5)
    return None


# --- сам стенд --------------------------------------------------------------

def port_present(device):
    import serial.tools.list_ports as list_ports

    return any(p.device.upper() == str(device).upper() for p in list_ports.comports())


def find_screen(cfg):
    """(порт_или_None, причина)."""
    ports = wired.list_screen_ports()
    want = cfg.get("port")
    if want:
        for port in ports:
            if port.device.upper() == want.upper():
                return port, ""
        return None, "в системе нет указанного порта %s" % want
    if len(ports) == 1:
        return ports[0], ""
    if not ports:
        return None, "плата экрана не найдена (нет CH340/CH9102/CP210x на USB)"
    return None, "нашлось несколько плат (%s) — задайте --port" % ", ".join(p.device for p in ports)


def spawn_mock(cfg):
    cmd = [wired.PYTHON, "-u", os.path.join(HERE, "mock_moonraker.py"),
           "--host", "127.0.0.1", "--port", str(cfg["mock_port"]),
           "--files", os.path.join(HERE, "gcodes"),
           "--print-seconds", str(cfg["print_seconds"])]
    if cfg.get("quiet", True):
        cmd.append("--quiet")
    child = wired.spawn(cmd, "mock")
    wired.record("mock", child.pid, port=cfg["mock_port"], cmd=" ".join(cmd))
    return child


def ensure_mock(cfg):
    """'ours' — отвечает наш мок, 'foreign' — чужой Moonraker, 'down' — нет хоста."""
    port = cfg["mock_port"]
    busy, ours = wired.probe_port(port)
    if ours:
        return "ours"
    if busy:
        return "foreign"
    info = wired.recorded("mock")
    if info and wired.alive(info.get("pid"), info.get("started_at")):
        log("мок (PID %s) жив, но на 127.0.0.1:%d не отвечает — перезапускаю"
            % (info["pid"], port))
        wired.stop_all(("mock",))
        time.sleep(1.0)
    spawn_mock(cfg)
    deadline = time.time() + 20
    while time.time() < deadline:
        state = wired.mock_state(port)
        if state is not None:
            log("мок поднят на 127.0.0.1:%d (PID %s, файлы test_printer/gcodes)"
                % (port, (wired.recorded("mock") or {}).get("pid")))
            return "ours"
        time.sleep(0.4)
    log("мок не ответил за 20 с — смотрите %s" % os.path.join(wired.RUN_DIR, "mock.log"))
    return "down"


def ensure_bridge(cfg):
    """'ours' | 'no-screen' | 'busy' | 'down'. Запускает мост, если его нет."""
    info = wired.recorded("bridge")
    if info and wired.alive(info.get("pid"), info.get("started_at")):
        held = info.get("port")
        if held and not port_present(held):
            log("мост держит %s, а такого порта больше нет (экран переткнули?) — перезапускаю" % held)
            wired.stop_all(("bridge",))
            time.sleep(1.0)
        else:
            return "ours"

    screen, reason = find_screen(cfg)
    if screen is None:
        return "no-screen:" + reason
    free, why = wired.port_is_free(screen.device)
    if not free:
        return "busy:%s (%s)" % (screen.device, why)
    wired.start_bridge(screen.device, cfg["mock_port"])
    info = wired.recorded("bridge") or {}
    log("мост поднят: %s → 127.0.0.1:%d (PID %s)" % (screen.device, cfg["mock_port"], info.get("pid")))
    return "ours"


def traffic_since(info):
    return wired.bridge_traffic(info.get("log_offset", 0)) if info else (0, 0, 0, [])


def check_channel(cfg, st, info):
    """Свежий мост: ждём, что экран заговорил; при тишине один раз дёргаем RST."""
    port = info.get("port")
    asked = wait_traffic(cfg, info)
    if asked:
        log("экран отвечает: %d %s за %g с — канал живой"
            % (asked, plural(asked, "запрос", "запроса", "запросов"), cfg["traffic_timeout"]))
        st["ever_traffic"] = True
        st["reset_done"] = False
        st["traffic"] = asked
        return
    if not cfg.get("reset_on_silence", True) or st.get("reset_done"):
        log("экран молчит (%s) — жду следующей проверки" % port)
        return
    st["reset_done"] = True
    log("экран молчит %g с — дёргаю RST (%s) и поднимаю мост заново" % (cfg["traffic_timeout"], port))
    wired.stop_all(("bridge",))
    try:
        wired.pulse_reset(port)
    except Exception as exc:
        log("сбросить экран не получилось: %s" % exc)
    time.sleep(1.0)
    if ensure_bridge(cfg) != "ours":
        return
    info = wired.recorded("bridge") or {}
    asked = wait_traffic(cfg, info)
    if asked:
        log("после сброса экран отвечает: %d %s — канал живой"
            % (asked, plural(asked, "запрос", "запроса", "запросов")))
        st["ever_traffic"] = True
        st["traffic"] = asked
    else:
        log("экран всё ещё молчит: проверьте, что на нём выбран режим Klipper (Serial), "
            "а не Wi-Fi, и что COM-порт свободен (bash test_printer/autostart.sh status)")


def wait_traffic(cfg, info, timeout=None):
    """Сколько запросов от экрана пришло с момента запуска этого моста."""
    offset = info.get("log_offset", 0)
    deadline = time.time() + (timeout or cfg["traffic_timeout"])
    asked = 0
    while time.time() < deadline:
        asked = wired.bridge_traffic(offset)[0]
        if asked:
            return asked
        time.sleep(0.5)
    return asked


def maybe_print(cfg, state):
    """Тестовая печать по кругу, если включено в конфиге."""
    if not cfg.get("print") or state["print_state"] not in ("standby", "complete", "cancelled"):
        return
    filename = (state.get("files") or [None])[0]
    if not filename:
        return
    if state["state"] != "ready":
        wired.mock_action(cfg["mock_port"], "reset")
        time.sleep(0.5)
    code, body = wired.mock_action(cfg["mock_port"], "start", filename=filename)
    log("тестовая печать %s: %s" % (filename, "запущена" if code == 200 else "%s %s" % (code, body)))


def loop_once(cfg, st):
    mock = ensure_mock(cfg)
    if mock != st["mock"]:
        st["mock"] = mock
        if mock == "foreign":
            log("на 127.0.0.1:%d отвечает чужой Moonraker — свой мок не поднимаю, мост пойдёт в него"
                % cfg["mock_port"])
    if mock == "down":
        return
    if mock == "foreign":
        wired.stop_all(("mock",))  # наш мок на этом порту не нужен

    was = wired.recorded("bridge") or {}
    was_key = (was.get("pid"), was.get("started_at"))
    bridge = ensure_bridge(cfg)
    if bridge != "ours":
        if bridge != st["bridge"]:
            st["bridge"] = bridge
            if bridge.startswith("no-screen"):
                log("экран не подключён: %s" % bridge.split(":", 1)[1])
            elif bridge.startswith("busy"):
                log("COM-порт занят другим приложением: %s — освободите его, и мост поднимется сам"
                    % bridge.split(":", 1)[1])
            else:
                log("мост не поднялся: %s" % bridge)
        return

    st["bridge"] = "ours"
    info = wired.recorded("bridge") or {}
    fresh = (info.get("pid"), info.get("started_at")) != was_key
    if fresh:
        st["traffic"] = 0
        check_channel(cfg, st, info)
    else:
        asked = wired.bridge_traffic(info.get("log_offset", 0))[0]
        if asked != st.get("traffic"):
            st["traffic"] = asked
            st["ever_traffic"] = st.get("ever_traffic") or asked > 0
        else:
            # тишина после живого обмена = мост залип (запрос повис) — перезапускаем
            silent_since = st.get("silent_since") or time.time()
            st["silent_since"] = silent_since
            if st.get("ever_traffic") and time.time() - silent_since > cfg["silent_limit"]:
                log("экран молчит больше %g с — перезапускаю мост" % cfg["silent_limit"])
                wired.stop_all(("bridge",))
                st["silent_since"] = None
                return
            return
        st["silent_since"] = None

    if mock == "ours":
        state = wired.mock_state(cfg["mock_port"])
        if state:
            maybe_print(cfg, state)


def supervise(cfg):
    other = running_supervisor()
    if other and other.get("pid") != os.getpid():
        log("автозапуск уже работает (PID %s) — второму экземпляру делать нечего" % other["pid"])
        return 0
    try:
        os.makedirs(wired.RUN_DIR, exist_ok=True)
        os.remove(STOP_FLAG)
    except OSError:
        pass

    missing = wired.missing_deps()
    if missing:
        log("нет python-пакетов: %s — поставьте их: %s -m pip install pyserial requests"
            % (", ".join(missing), wired.PYTHON))
        return 1

    log("=== автозапуск стенда: старт (PID %d), конфиг %s ===" % (os.getpid(), CONFIG_PATH))
    wired.ensure_bridge(cfg["bridge_ref"])
    st = {"mock": None, "bridge": None, "traffic": 0, "ever_traffic": False,
          "reset_done": False, "silent_since": None}
    write_heartbeat(st)
    try:
        while True:
            if os.path.exists(STOP_FLAG):
                log("получена команда остановки — сворачиваюсь")
                break
            loop_once(cfg, st)
            write_heartbeat(st)
            time.sleep(max(2.0, float(cfg["poll"])))
    except KeyboardInterrupt:
        log("прервано (Ctrl+C)")
    except Exception as exc:  # супервизор обязан не падать от мелочей
        log("неожиданная ошибка: %r — выхожу, задача перезапустит меня" % exc)
        return 1
    finally:
        stopped = wired.stop_all()
        log("остановлено: %s" % (", ".join(stopped) if stopped else "нечего"))
        try:
            os.remove(HEARTBEAT)
        except OSError:
            pass
    return 0


# --- команды -----------------------------------------------------------------

def alive_child(name):
    """Жив ли мок/мост (с учётом того, что PID мог быть переиспользован)."""
    info = wired.recorded(name) or {}
    return wired.alive(info.get("pid"), info.get("started_at"))


def stop_everything(keep_autostart_off=False):
    """Остановить супервизор и детей.

    Возвращает (что_добили_сами, что_сделали_с_автозапуском, работал_ли_супервизор).
    """
    off = ""
    if keep_autostart_off:
        installed, enabled = task_state()
        if installed:
            ok, out = task_enable(False)
            off = "задача планировщика выключена" if ok else "не смог выключить задачу: %s" % out
        if os.path.isfile(STARTUP_CMD):
            try:
                os.replace(STARTUP_CMD, STARTUP_CMD + ".disabled")
                off = (off + "; " if off else "") + "файл автозагрузки отключён"
            except OSError as exc:
                off = (off + "; " if off else "") + "не смог отключить файл автозагрузки: %s" % exc
    try:
        os.makedirs(wired.RUN_DIR, exist_ok=True)
        with open(STOP_FLAG, "w", encoding="utf-8") as handle:
            handle.write("остановка по команде %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    except OSError as exc:
        note("не смог поставить флаг остановки: %s" % exc)

    info = running_supervisor()
    if info:
        deadline = time.time() + 25
        while time.time() < deadline and wired.alive(info["pid"]):
            time.sleep(0.5)
        if wired.alive(info["pid"]):
            note("супервизор не свернулся сам за 25 с — снимаю принудительно")
            wired.kill(info["pid"])
        # супервизор в своём финале сам гасит мок и мост: даём им доумереть,
        # иначе мы отчитаемся о чужих процессах как о своих
        deadline = time.time() + 6
        while time.time() < deadline and any(
                alive_child(name) for name in ("mock", "bridge")):
            time.sleep(0.5)
    stopped = wired.stop_all()
    try:
        os.remove(HEARTBEAT)
    except OSError:
        pass
    return stopped, off, bool(info)


def cmd_install(args):
    cfg = effective(args)
    write_config(cfg)
    step("Автозапуск: установка")
    try:
        os.remove(STOP_FLAG)
    except OSError:
        pass

    ok, xml_path, out = task_install()
    if ok:
        installed, enabled = task_state()
        note("задача планировщика «%s» создана и запускается при входе в систему" % TASK_NAME)
        note("включена: %s" % ("да" if enabled else "нет — включите: autostart.sh start"))
        for path in (STARTUP_CMD, STARTUP_CMD + ".disabled"):
            if os.path.exists(path):
                startup_remove()
                note("убрал прежний файл автозагрузки (задача его заменяет)")
    else:
        reason = (out.splitlines() or ["без подробностей"])[0]
        note("задачу планировщика создать не получилось: %s" % reason)
        step("Автозапуск: запасной путь через автозагрузку")
        path = startup_install()
        note("положил файл автозагрузки: %s" % path)
        note("он запускает то же самое при входе в систему")
        note("XML задачи остался для разбора: %s" % xml_path)

    note("конфиг: %s" % CONFIG_PATH)
    note("  мок на 127.0.0.1:%d, экран: %s, тестовая печать: %s"
         % (cfg["mock_port"], cfg["port"] or "искать по USB",
            "по кругу" if cfg["print"] else "выключена"))

    if args.no_start:
        note("сейчас не запускаю (--no-start)")
        return 0

    step("Автозапуск: запуск сейчас")
    return cmd_start(args, already=cfg)


def cmd_start(args, already=None):
    cfg = already or effective(args)
    if not already:  # start тоже умеет править конфиг: --print, --port, --mock-port...
        write_config(cfg)
    try:
        os.remove(STOP_FLAG)
    except OSError:
        pass

    installed, enabled = task_state()
    if installed and enabled is False:
        ok, _ = task_enable(True)
        note("задача планировщика включена" if ok else "не смог включить задачу планировщика")
    if os.path.isfile(STARTUP_CMD + ".disabled"):
        os.replace(STARTUP_CMD + ".disabled", STARTUP_CMD)
        note("файл автозагрузки включён")

    if running_supervisor():
        note("супервизор уже работает (PID %s)" % running_supervisor()["pid"])
        return 0

    ran = False
    if installed:
        code, out = run(["schtasks", "/Run", "/TN", TASK_NAME])
        if code == 0:
            note("задача планировщика запущена, жду стенд...")
            ran = True
        else:
            note("запустить задачу не получилось (%s) — запускаю супервизор напрямую" % out)

    if not ran:
        spawn_detached([pythonw(), os.path.join(HERE, "autostart.py"), "supervise"])
    info = wait_supervisor(30)
    if not info:
        note("супервизор не появился за 30 с. Лог: %s" % SUPERVISOR_LOG)
        return 1
    note("супервизор работает (PID %s), лог: %s" % (info["pid"], SUPERVISOR_LOG))

    deadline = time.time() + 40
    while time.time() < deadline:
        state = wired.mock_state(cfg["mock_port"])
        if state is not None:
            break
        time.sleep(1.0)
    show_status(cfg, brief=True)
    return 0


def cmd_stop(args):
    cfg = effective(args)
    step("Автозапуск: остановка")
    stopped, off, had = stop_everything(keep_autostart_off=True)
    if had:
        note("супервизор остановлен%s" % (" (мок и мост погасил он сам)" if not stopped else ""))
    else:
        note("супервизор не работал")
    if stopped:
        note("добил оставшиеся процессы: %s" % ", ".join(stopped))
    note("автозапуск: %s" % (off or "ничего не установлено"))
    note("COM-порт освобождён — экран можно занимать снова")
    note("включить обратно: bash test_printer/autostart.sh start")
    return 0


def cmd_uninstall(args):
    step("Автозапуск: удаление")
    stopped, _, had = stop_everything(keep_autostart_off=False)
    if had or stopped:
        note("стенд остановлен%s" % (": " + ", ".join(stopped) if stopped else " (супервизором)"))
    else:
        note("стенд и не работал")
    installed, _ = task_state()
    if installed:
        ok, out = task_remove()
        note("задача планировщика удалена" if ok else "не смог удалить задачу: %s" % out)
    else:
        note("задачи планировщика не было")
    removed = startup_remove()
    if removed:
        note("убрал из автозагрузки: %s" % ", ".join(os.path.basename(p) for p in removed))
    try:
        os.remove(STOP_FLAG)
    except OSError:
        pass
    note("конфиг оставил: %s (удалите вручную, если не нужен)" % CONFIG_PATH)
    return 0


def traffic_rate(seconds=2.0):
    """Сколько запросов/ответов идёт прямо сейчас (окно seconds)."""
    path = wired.bridge_log_path()
    start = os.path.getsize(path) if os.path.exists(path) else 0
    time.sleep(seconds)
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            handle.seek(start)
            text = handle.read()
    except OSError:
        return 0, 0
    asked = sum(1 for line in text.splitlines() if line.startswith(">>> ") and "HTTP_" in line)
    answered = sum(1 for line in text.splitlines()
                   if line.startswith("<<< ") and not line.startswith("(Ignored)"))
    return asked, answered


def show_status(cfg, brief=False):
    step("Что установлено")
    installed, enabled = task_state()
    if installed:
        note("задача планировщика «%s»: установлена, %s"
             % (TASK_NAME, "включена" if enabled else "выключена"))
    else:
        note("задача планировщика «%s»: нет" % TASK_NAME)
    state = startup_state()
    note("файл автозагрузки: %s" % (state if state else "нет"))
    if not installed and not state:
        note("автозапуск не поставлен: bash test_printer/autostart.sh install")

    step("Супервизор")
    info = running_supervisor()
    if info:
        note("работает: PID %s, запущен %s, heartbeat %g с назад"
             % (info["pid"], time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(info.get("started_at") or 0)),
                time.time() - float(info.get("heartbeat") or 0)))
        note("лог: %s (последние строки — --log)" % SUPERVISOR_LOG)
    else:
        note("не работает — стенд сейчас никто не переподнимает")
    note("конфиг: %s" % json.dumps({k: cfg[k] for k in sorted(cfg)}, ensure_ascii=False))

    if brief:
        return

    wired.show_status(cfg["mock_port"])
    asked, answered = traffic_rate()
    if asked:
        note("прямо сейчас: %d %s от экрана и %d %s за 2 с — канал живой"
             % (asked, plural(asked, "запрос", "запроса", "запросов"), answered,
                plural(answered, "ответ", "ответа", "ответов")))
    else:
        note("прямо сейчас: за 2 с от экрана ничего не пришло "
             "(экран выключен, не в USB-режиме или показывает настройки)")


def show_log(lines=20):
    step("Последние строки supervisor.log")
    try:
        with open(SUPERVISOR_LOG, encoding="utf-8", errors="replace") as handle:
            text = handle.read().splitlines()
    except OSError:
        note("лога ещё нет: %s" % SUPERVISOR_LOG)
        return
    for line in text[-lines:]:
        note(line)
    if len(text) > lines:
        note("... всего строк: %d, файл: %s" % (len(text), SUPERVISOR_LOG))


def main():
    try:  # чтобы русский текст на Windows не превращался в кракозябры
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Автозапуск проводного стенда CYD-Klipper (мок Moonraker + serial-мост)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    def config_options(target):
        target.add_argument("--mock-port", type=int, default=None, help="порт мока (7125)")
        target.add_argument("--port", default=None, help="COM-порт экрана (иначе искать по USB)")
        target.add_argument("--print", action=argparse.BooleanOptionalAction, default=None,
                            help="крутить тестовую печать по кругу (--print / --no-print)")
        target.add_argument("--print-seconds", type=float, default=None,
                            help="сколько мок печатает один прогон (120)")
        target.add_argument("--poll", type=float, default=None,
                            help="как часто проверять стенд, секунд (10)")
        target.add_argument("--bridge-ref", default=None,
                            help="тег/ветка CYD-Klipper для serial_server.py (%s)" % wired.BRIDGE_REF)

    for name, help_text in (("install", "поставить в автозапуск и запустить сейчас"),
                            ("start", "включить автозапуск и запустить сейчас"),
                            ("status", "что установлено и что живо"),
                            ("stop", "выключить автозапуск и остановить стенд"),
                            ("uninstall", "убрать из автозапуска совсем")):
        child = sub.add_parser(name, help=help_text)
        config_options(child)

    sub.choices["install"].add_argument("--no-start", action="store_true",
                                        help="не запускать сразу после установки")
    sub.choices["status"].add_argument("--log", nargs="?", type=int, const=20, default=None,
                                       metavar="СТРОК", help="показать хвост supervisor.log (20)")
    supervise_parser = sub.add_parser("supervise", help="(внутреннее) цикл сопровождения стенда")
    supervise_parser.add_argument("--config", default=CONFIG_PATH, help="файл конфигурации")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0

    if args.command == "supervise":
        cfg = dict(DEFAULTS)
        try:
            with open(args.config, encoding="utf-8") as handle:
                cfg.update(json.load(handle))
        except Exception:
            pass
        try:
            return supervise(cfg)
        except (Exception, SystemExit) as exc:  # падать без следа нельзя: пишем в лог
            import traceback
            log("супервизор упал на старте: %r\n%s" % (exc, traceback.format_exc().strip()))
            return 1

    if args.command == "status":
        cfg = effective(args)
        show_status(cfg)
        if args.log is not None:
            show_log(args.log)
        return 0
    if args.command == "stop":
        return cmd_stop(args)
    if args.command == "uninstall":
        return cmd_uninstall(args)
    if args.command == "install":
        return cmd_install(args)
    return cmd_start(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say()
        note("прервано")
        sys.exit(130)
