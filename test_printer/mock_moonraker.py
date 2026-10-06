#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Мок Moonraker для проверки экрана CYD-Klipper без принтера и без Docker.

Отвечает ровно на те эндпоинты Moonraker, которые вызывает прошивка CYD-Klipper
(CYD-Klipper/src/core/klipper/klipper_printer_integration.cpp), и держит минимум
состояния: температуры греются к заданной, gcode-команды разбираются, "печать"
файла идёт по таймеру, позиция инструмента едет по квадрату, превью 32x32
генерируется на ходу.

Зависимостей нет — только стандартная библиотека Python 3.

    python3 mock_moonraker.py
    python3 mock_moonraker.py --files ~/printer_data/gcodes --port 7125
    python3 mock_moonraker.py --api-key secret      # проверка ветки с API-ключом

Проверка:  curl -s http://localhost:7125/printer/info

Веб-пульт: http://localhost:7125/ui — состояние принтера и кнопки, которые дёргают
крайние случаи (ошибка klippy, shutdown, пауза, конец печати, сброс), плюс живой
журнал запросов экрана. То же самое доступно и из curl: GET /mock/state,
GET|POST /mock/action?action=..., GET /mock/log.
"""

import argparse
import collections
import json
import os
import random
import re
import struct
import subprocess
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

AMBIENT = 24.0        # °C, от этой температуры всё греется и остывает
HEAT_RATE = 8.0       # °C/с
COOL_RATE = 4.0       # °C/с

# /printer/gcode/help: прошивка берёт из ответа только те команды,
# у которых описание ровно "CYD_SCREEN_MACRO" — это и есть макросы на экране.
GCODE_HELP = {
    "G28": "Home the printer.",
    "G90": "Absolute coordinates.",
    "G91": "Relative coordinates.",
    "M18": "Disable steppers.",
    "M104": "Set extruder temperature.",
    "M106": "Set fan speed.",
    "M117": "Show a message on the display.",
    "M140": "Set bed temperature.",
    "M220": "Set speed factor.",
    "M221": "Set extrude factor.",
    "PAUSE": "Pause the current print.",
    "RESUME": "Resume the current print.",
    "CANCEL_PRINT": "Cancel the current print.",
    "RESTART": "Reload the config and restart klippy.",
    "FIRMWARE_RESTART": "Restart the firmware and klippy.",
    "CYD_LOAD_FILAMENT": "CYD_SCREEN_MACRO",
    "CYD_UNLOAD_FILAMENT": "CYD_SCREEN_MACRO",
    "CYD_HEAT_SOAK": "CYD_SCREEN_MACRO",
    "CYD_CLEAN_NOZZLE": "CYD_SCREEN_MACRO",
    "CYD_END_PRINT": "CYD_SCREEN_MACRO",
}


def render_thumbnail_png(seed, size=32):
    """PNG 32x32 без внешних библиотек: цвет из хеша имени + шахматка."""
    digest = zlib.crc32(seed.encode("utf-8")) & 0xFFFFFFFF
    base_r, base_g, base_b = (digest >> 16) & 0xFF, (digest >> 8) & 0xFF, digest & 0xFF
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            k = 1.0 if ((x // 4) + (y // 4)) % 2 else 0.35
            row += bytes((int(base_r * k), int(base_g * k), int(base_b * k), 255))
        rows.append(bytes(row))
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def number(name, text, default):
    """Значение параметра, как в gcode: "S200", "X-10.5", "TARGET=250"."""
    m = re.search(name + r"\s*=?\s*(-?\d*\.?\d+)", text)
    return float(m.group(1)) if m else default


def query_text(query, name, default=""):
    """Параметр строки запроса как строка (веб-пульт и curl ходят обычным query)."""
    value = query.get(name, [None])[0]
    return default if value is None else value


def query_number(query, name, default=None):
    """Параметр строки запроса как число; None, если его нет или он не число."""
    try:
        return float(query.get(name, [None])[0])
    except (TypeError, ValueError):
        return default


class Printer:
    """Состояние виртуального принтера: то, что экран видит в objects/query."""

    def __init__(self, files_dir, print_seconds=120.0):
        self.lock = threading.RLock()
        self.files_dir = files_dir
        self.print_seconds = print_seconds
        # температуры
        self.ext_t, self.ext_target = AMBIENT, 0.0
        self.bed_t, self.bed_target = AMBIENT, 0.0
        # gcode_move / toolhead
        self.position = [0.0, 0.0, 0.0]
        self.homing_origin = [0.0, 0.0, 0.0]
        self.absolute = True
        self.homed = ""
        self.fan = 0.0
        self.speed_factor = 1.0
        self.extrude_factor = 1.0
        self.speed = 3000.0          # mm/min, как отдаёт Klipper
        # webhooks
        self.state = "ready"
        self.state_message = "Printer is ready"
        self.message = ""            # M117
        # print_stats / virtual_sdcard
        self.print_state = "standby"
        self.filename = ""
        self.progress = 0.0
        self.print_duration = 0.0
        self.total_duration = 0.0
        self.filament_used = 0.0
        self.total_layer = 0
        self.current_layer = 0
        # [power] устройства из moonraker.conf
        self.power = {"printer": "off", "psu": "on"}

    # --- разбор gcode ---------------------------------------------------
    def run_gcode(self, script):
        for line in script.replace("\\n", "\n").splitlines():
            line = line.split(";")[0].strip()
            if line:
                self.run_line(line)

    def run_line(self, command):
        up = command.upper()
        with self.lock:
            if up.startswith("M104") or up.startswith("M109"):
                self.ext_target = number("S", up, self.ext_target)
            elif up.startswith("M140") or up.startswith("M190"):
                self.bed_target = number("S", up, self.bed_target)
            elif up.startswith("M106"):
                self.fan = max(0.0, min(1.0, number("S", up, 255.0) / 255.0))
            elif up.startswith("M107"):
                self.fan = 0.0
            elif up.startswith("M220"):
                self.speed_factor = number("S", up, 100.0) / 100.0
            elif up.startswith("M221"):
                self.extrude_factor = number("S", up, 100.0) / 100.0
            elif up.startswith("M117"):
                self.message = command[4:].strip()
            elif up.startswith("M112"):
                self.emergency_stop()
            elif up.startswith("G28"):
                self.homed = "xyz"
                self.position = [110.0, 110.0, 10.0]
            elif up.startswith("M18") or up.startswith("M84"):
                self.homed = ""
            elif up.startswith("G90"):
                self.absolute = True
            elif up.startswith("G91"):
                self.absolute = False
            elif up.startswith("G0") or up.startswith("G1"):
                self.move(up)
            elif up.startswith("SET_HEATER_TEMPERATURE"):
                target = number("TARGET", up, None)
                heater = re.search(r"HEATER=(\w+)", up)
                if target is not None:
                    if heater and "BED" in heater.group(1):
                        self.bed_target = target
                    else:
                        self.ext_target = target
            elif up.startswith("SET_GCODE_OFFSET"):
                adjust = number("Z_ADJUST", up, None)
                z = number("Z", up, None)
                if adjust is not None:
                    self.homing_origin[2] += adjust
                elif z is not None:
                    self.homing_origin[2] = z
            elif up.startswith("RESTART") or up.startswith("FIRMWARE_RESTART"):
                self.state = "ready"
                self.state_message = "Printer is ready"
                self.message = ""
            elif up.startswith("PAUSE"):
                if self.print_state == "printing":
                    self.print_state = "paused"
            elif up.startswith("RESUME"):
                if self.print_state == "paused":
                    self.print_state = "printing"
            elif up.startswith("CANCEL_PRINT") or up.startswith("STOP_PRINT"):
                self.print_state = "cancelled"
                self.progress = 0.0
                self.filename = ""
                self.message = ""
                self.ext_target = self.bed_target = 0.0
            # остальные команды просто принимаются молча

    def move(self, up):
        values = {axis: number(axis, up, None) for axis in "XYZE"}
        for i, axis in enumerate("XYZ"):
            if values[axis] is None:
                continue
            self.position[i] = (self.position[i] + values[axis]) if not self.absolute else values[axis]
        if values["E"] is not None and self.print_state != "printing":
            self.filament_used += abs(values["E"])

    def emergency_stop(self):
        with self.lock:
            self.state = "shutdown"
            self.state_message = "Shutdown due to M112 command"
            self.print_state = "cancelled"
            self._cooldown()

    # --- крайние случаи, которые дёргает веб-пульт (/ui) -----------------
    def _cooldown(self):
        """Нагреватели и вентилятор в ноль — так же ведёт себя настоящий принтер."""
        self.ext_target = self.bed_target = 0.0
        self.fan = 0.0

    def klippy_error(self, message="injected by the mock panel"):
        """Ошибка klippy: на экране должна появиться панель ошибки с этим текстом."""
        with self.lock:
            self.state = "error"
            self.state_message = "Error: %s" % message
            if self.print_state == "printing":
                self.print_state = "error"
            self._cooldown()

    def shutdown(self, message="MCU 'mcu' shutdown: injected by the mock panel"):
        """Аварийный останов со стороны MCU — второй вид панели ошибки."""
        with self.lock:
            self.state = "shutdown"
            self.state_message = message
            if self.print_state == "printing":
                self.print_state = "error"
            self._cooldown()

    def startup(self):
        """Промежуточное состояние klippy: экран должен показать «загружается»."""
        with self.lock:
            self.state = "startup"
            self.state_message = "Starting up (injected by the mock panel)"

    def reset(self):
        """FIRMWARE_RESTART: klippy снова ready, неудачная печать сброшена."""
        with self.lock:
            self.state = "ready"
            self.state_message = "Printer is ready"
            self.message = ""
            if self.print_state in ("error", "cancelled", "complete"):
                self.print_state = "standby"
                self.progress = 0.0
                self.filename = ""
            self._cooldown()

    def finish_print(self):
        """Мгновенный конец печати: экран должен показать print_stats.state=complete."""
        with self.lock:
            if self.print_state not in ("printing", "paused"):
                return False
            self.progress = 1.0
            self.current_layer = self.total_layer
            self.print_state = "complete"
            self.message = ""
            self._cooldown()
            return True

    def advance(self, delta):
        """Прыжок прогресса — чтобы не ждать таймер, проверяя прогресс-бар и слои."""
        with self.lock:
            if self.print_state not in ("printing", "paused"):
                return False
            self.progress = max(0.0, min(1.0, self.progress + delta))
            total = self.total_layer or 1
            self.current_layer = max(1, min(total, int(self.progress * total) + 1))
            return True

    def set_temp(self, extruder=None, bed=None):
        """None означает «не трогать»: пульт греет только то, что ему сказали."""
        with self.lock:
            if extruder is not None:
                self.ext_target = float(extruder)
            if bed is not None:
                self.bed_target = float(bed)

    def set_fan(self, speed):
        with self.lock:
            self.fan = max(0.0, min(1.0, float(speed)))

    def set_message(self, text):
        with self.lock:
            self.message = text

    def snapshot(self):
        """Компактная сводка для веб-пульта (это не протокол Moonraker)."""
        with self.lock:
            return {
                "state": self.state,
                "state_message": self.state_message,
                "print_state": self.print_state,
                "filename": self.filename,
                "progress": round(self.progress, 4),
                "current_layer": self.current_layer,
                "total_layer": self.total_layer,
                "extruder": {"temperature": round(self.ext_t, 2), "target": self.ext_target},
                "heater_bed": {"temperature": round(self.bed_t, 2), "target": self.bed_target},
                "fan": round(self.fan, 3),
                "speed_factor": self.speed_factor,
                "extrude_factor": self.extrude_factor,
                "message": self.message,
                "position": [round(v, 3) for v in self.position],
                "homed": self.homed,
                "power": dict(self.power),
                "files": [item["path"] for item in self.file_list()],
            }

    # --- файлы ----------------------------------------------------------
    def file_path(self, name):
        """Путь внутри папки gcode-файлов или None (защита от ../)."""
        candidate = os.path.abspath(os.path.join(self.files_dir, name))
        root = os.path.abspath(self.files_dir)
        if candidate != root and not candidate.startswith(root + os.sep):
            return None
        return candidate if os.path.isfile(candidate) else None

    def file_list(self):
        found = []
        for folder, _dirs, names in os.walk(self.files_dir):
            for name in names:
                if not name.lower().endswith((".gcode", ".g", ".gco")):
                    continue
                path = os.path.join(folder, name)
                found.append({
                    "path": os.path.relpath(path, self.files_dir).replace(os.sep, "/"),
                    "modified": os.path.getmtime(path),
                    "size": os.path.getsize(path),
                    "permissions": "rw",
                })
        found.sort(key=lambda item: item["modified"], reverse=True)
        return found

    def metadata(self, filename):
        path = self.file_path(filename)
        if path is None:
            return None
        size = os.path.getsize(path)
        return {
            "filename": filename,
            "size": size,
            "modified": os.path.getmtime(path),
            "slicer": "mock-moonraker",
            "layer_height": 0.2,
            "first_layer_height": 0.25,
            "object_height": 10.0,
            "filament_total": round(size / 20.0, 1),
            "estimated_time": self.print_seconds,
            "thumbnails": self.thumbnails(filename),
        }

    def thumbnails(self, filename):
        if self.file_path(filename) is None:
            return []
        base = os.path.splitext(filename)[0]
        return [{
            "width": 32,
            "height": 32,
            "size": 300,
            "thumbnail_path": "%s-32x32.png" % base,
        }]

    def start_print(self, filename):
        path = self.file_path(filename)
        if path is None:
            return False
        with self.lock:
            self.filename = filename
            self.print_state = "printing"
            self.progress = 0.0
            self.print_duration = 0.0
            self.total_duration = 0.0
            self.filament_used = 0.0
            self.total_layer = 20
            self.current_layer = 0
            self.homed = "xyz"
            self.position = [60.0, 60.0, 0.2]
        return True

    # --- то, что отдаётся в /printer/objects/query ----------------------
    def state_objects(self):
        with self.lock:
            return {
                "webhooks": {"state": self.state, "state_message": self.state_message},
                "extruder": {
                    "temperature": round(self.ext_t, 2),
                    "target": self.ext_target,
                    "power": 0.4 if self.ext_target else 0.0,
                    "can_extrude": self.ext_t >= 170.0,
                    "pressure_advance": 0.045,
                    "smooth_time": 0.04,
                },
                "heater_bed": {
                    "temperature": round(self.bed_t, 2),
                    "target": self.bed_target,
                    "power": 0.3 if self.bed_target else 0.0,
                },
                "toolhead": {
                    "homed_axes": self.homed,
                    "position": [round(v, 3) for v in self.position],
                    "extruder": "extruder",
                    "print_time": round(self.print_duration, 2),
                    "estimated_print_time": round(self.print_duration + 1.0, 2),
                    "axis_minimum": [0.0, 0.0, 0.0, 0.0],
                    "axis_maximum": [220.0, 220.0, 250.0, 0.0],
                },
                "gcode_move": {
                    "gcode_position": [round(v, 3) for v in self.position],
                    "homing_origin": [round(v, 4) for v in self.homing_origin],
                    "absolute_coordinates": self.absolute,
                    "speed_factor": self.speed_factor,
                    "extrude_factor": self.extrude_factor,
                    "speed": self.speed,
                },
                "virtual_sdcard": {
                    "progress": round(self.progress, 4),
                    "is_active": self.print_state == "printing",
                    "file_path": self.filename,
                    "file_position": 0,
                    "file_size": 0,
                },
                "print_stats": {
                    "filename": self.filename,
                    "state": self.print_state,
                    "message": "",
                    "total_duration": round(self.total_duration, 2),
                    "print_duration": round(self.print_duration, 2),
                    "filament_used": round(self.filament_used, 2),
                    "info": {
                        "total_layer": self.total_layer or None,
                        "current_layer": self.current_layer or None,
                    },
                },
                "fan": {"speed": round(self.fan, 3), "rpm": None},
                "display_status": {
                    "progress": round(self.progress, 4),
                    "message": self.message,
                },
            }

    # --- симуляция ------------------------------------------------------
    def step(self, dt):
        with self.lock:
            self.ext_t = self._approach(self.ext_t, self.ext_target, dt)
            self.bed_t = self._approach(self.bed_t, self.bed_target, dt)
            if self.print_state != "printing":
                return
            self.total_duration += dt
            self.print_duration += dt
            self.filament_used += dt * 0.6
            self.progress = min(1.0, self.progress + dt / self.print_seconds)
            self.current_layer = max(1, min(self.total_layer, int(self.progress * self.total_layer) + 1))
            corners = [(60.0, 60.0), (160.0, 60.0), (160.0, 160.0), (60.0, 160.0)]
            pos = self.progress * 4.0
            seg, frac = int(pos) % 4, pos % 1.0
            start, end = corners[seg], corners[(seg + 1) % 4]
            self.position = [
                round(start[0] + (end[0] - start[0]) * frac, 3),
                round(start[1] + (end[1] - start[1]) * frac, 3),
                round(0.2 + 0.2 * self.current_layer, 3),
            ]
            self.speed = 3000.0 * self.speed_factor
            if self.progress >= 1.0:
                self.print_state = "complete"
                self.message = ""
                self.ext_target = self.bed_target = 0.0

    @staticmethod
    def _approach(temp, target, dt):
        if target > 0 and abs(target - temp) <= 0.4:
            return round(target + random.uniform(-0.3, 0.3), 2)
        rate = HEAT_RATE if target > temp else COOL_RATE
        step = max(-rate * dt, min(rate * dt, target - temp))
        return round(max(AMBIENT, temp + step), 2)


class MockServer(ThreadingHTTPServer):
    daemon_threads = True
    # на Windows SO_REUSEADDR позволяет «утащить» уже занятый порт — не даём
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, printer, api_key=None, auth_code=401, quiet=False):
        super().__init__(address, RequestHandler)
        self.printer = printer
        self.api_key = api_key
        self.auth_code = auth_code
        self.quiet = quiet
        # журнал запросов для веб-пульта: запросы самого пульта в него не попадают
        self.requests = collections.deque(maxlen=300)
        self.log_lock = threading.Lock()


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mock-moonraker/1.0"

    # --- инфраструктура -------------------------------------------------
    def log_request(self, code="-", size="-"):
        if not self.server.quiet:
            print("%s %s -> %s" % (self.command, self.path, code), flush=True)
        # свои же запросы пульта в журнал не пишем — он нужен, чтобы видеть экран
        if self.path.startswith("/mock/") or self.path.startswith("/ui"):
            return
        with self.server.log_lock:
            self.server.requests.append({
                "time": time.strftime("%H:%M:%S"),
                "method": self.command,
                "path": self.path,
                "code": str(code),
            })

    def log_message(self, fmt, *args):
        if not self.server.quiet:
            print("    " + (fmt % args), flush=True)

    def reply(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.reply_raw(body, status, "application/json")

    def reply_raw(self, body, status=200, content_type="application/octet-stream"):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)

        try:
            # пульт (/ui и /mock/*) ключа не требует: иначе при --api-key он был бы
            # закрыт, а проверяют экран как раз им
            if (self.server.api_key and path != "/ui" and not path.startswith("/mock/")
                    and self.headers.get("X-Api-Key") != self.server.api_key):
                # Moonraker на неверный ключ отвечает 401; прошивка распознаёт
                # как "неверная авторизация" только 403 (см. --auth-code).
                self.reply({"error": {"code": self.server.auth_code, "message": "Unauthorized"}},
                           self.server.auth_code)
                return
            self.dispatch(method, path, query)
        except Exception as exc:  # мок: любую ошибку показываем в ответе, а не в трейсбеке
            self.reply({"error": {"code": 500, "message": "%s: %s" % (type(exc).__name__, exc)}}, 500)

    # --- маршруты -------------------------------------------------------
    def dispatch(self, method, path, query):
        printer = self.server.printer

        if path == "/ui":
            self.reply_panel()
        elif path == "/":
            # браузеру отдаём пульт, а curl (у него нет Accept: text/html) — список эндпоинтов
            if "text/html" in self.headers.get("Accept", ""):
                self.reply_panel()
                return
            self.reply({"mock": "moonraker", "panel": "/ui", "endpoints": sorted(ENDPOINTS)})
        elif path == "/mock/state":
            self.reply({"result": self.mock_snapshot()})
        elif path == "/mock/log":
            if query.get("clear", [""])[0]:
                with self.server.log_lock:
                    self.server.requests.clear()
            limit = int(query_number(query, "limit", 60) or 60)
            with self.server.log_lock:
                items = list(self.server.requests)[-max(1, limit):]
            self.reply({"result": {"requests": items}})
        elif path == "/mock/action":
            self.mock_action(query)
        elif path == "/mock/autostart":
            self.reply({"result": autostart_snapshot()})
        elif path == "/server/info":
            self.reply({"result": {
                "klippy_state": printer.state,
                "components": ["virtual_sdcard", "power", "history"],
                "failed_components": [],
                "registered_directories": ["gcodes", "config", "logs"],
                "warnings": [],
                "moonraker_version": "v0.9.3-mock",
                "api_version": [1, 5, 0],
            }})
        elif path == "/machine/system_info":
            self.reply({"result": {"system_info": {
                "cpu_info": "mock cpu",
                "instance_ids": {"moonraker": "mock"},
                "system_uptime": int(time.monotonic()),
            }}})
        elif path == "/printer/info":
            self.reply({"result": {
                "state": printer.state,
                "state_message": printer.state_message,
                "hostname": "mock-moonraker",
                "klipper_path": "/opt/klipper",
                "python_path": "/usr/bin/python3",
                "log_file": "/tmp/klippy.log",
                "config_file": "/tmp/printer.cfg",
                "software_version": "v0.12.0-mock",
                "cpu_info": "mock cpu",
            }})
        elif path == "/printer/objects/query":
            wanted = set(query.keys())
            status = {name: value for name, value in printer.state_objects().items() if name in wanted}
            self.reply({"result": {"status": status, "eventtime": time.time()}})
        elif path == "/printer/objects/list":
            self.reply({"result": {"objects": sorted(printer.state_objects())}})
        elif path == "/printer/gcode/help":
            self.reply({"result": GCODE_HELP})
        elif path == "/printer/gcode/script":
            script = query.get("script", [None])[0]
            if script is None and method == "POST":
                script = self.read_body().decode("utf-8", "replace")
            printer.run_gcode(script or "")
            self.reply({"result": "ok"})
        elif path == "/printer/emergency_stop":
            printer.emergency_stop()
            self.reply({"result": "ok"})
        elif path == "/machine/device_power/devices":
            devices = [{
                "device": name,
                "status": status,
                "type": "klipper_device",
                "locked_while_printing": False,
                "socket": None,
            } for name, status in sorted(printer.power.items())]
            self.reply({"result": {"devices": devices}})
        elif path == "/machine/device_power/device":
            name = query.get("device", [""])[0]
            action = query.get("action", [""])[0]
            if name not in printer.power or action not in ("on", "off"):
                self.reply({"error": {"code": 400, "message": "Unknown device or action"}}, 400)
                return
            printer.power[name] = action
            self.reply({"result": {}})
        elif path == "/server/files/list":
            self.reply({"result": printer.file_list()})
        elif path == "/server/files/metadata":
            filename = query.get("filename", [""])[0]
            metadata = printer.metadata(filename)
            if metadata is None:
                self.reply({"error": {"code": 404, "message": "File not found"}}, 404)
                return
            self.reply({"result": metadata})
        elif path == "/server/files/thumbnails":
            self.reply({"result": printer.thumbnails(query.get("filename", [""])[0])})
        elif path.startswith("/server/files/gcodes/"):
            self.serve_gcode_file(path[len("/server/files/gcodes/"):])
        elif path == "/server/files/upload":
            self.handle_upload(method, query)
        elif path == "/printer/print/start":
            # как настоящий Moonraker: пока klippy не ready, печать не стартует
            if printer.state != "ready":
                self.reply({"error": {"code": 503, "message": "Klippy is not ready"}}, 503)
                return
            filename = query.get("filename", [""])[0]
            if not printer.start_print(filename):
                self.reply({"error": {"code": 404, "message": "File not found"}}, 404)
                return
            self.reply({"result": "ok"})
        elif path in ("/printer/print/pause", "/printer/print/resume", "/printer/print/cancel"):
            printer.run_line({"pause": "PAUSE", "resume": "RESUME", "cancel": "CANCEL_PRINT"}[path.rsplit("/", 1)[-1]])
            self.reply({"result": "ok"})
        else:
            self.reply({"error": {"code": 404, "message": "Unhandled endpoint %s" % path}}, 404)

    # --- веб-пульт: состояние и кнопки для крайних случаев ---------------
    def reply_panel(self):
        self.reply_raw(PANEL_HTML.encode("utf-8"), 200, "text/html; charset=utf-8")

    def mock_snapshot(self):
        printer = self.server.printer
        payload = printer.snapshot()
        payload["mock"] = {
            "panel": "/ui",
            "host_header": self.headers.get("Host", ""),
            "print_seconds": printer.print_seconds,
            "files_dir": printer.files_dir,
            "api_key_required": bool(self.server.api_key),
        }
        return payload

    def mock_action(self, query):
        """Действия пульта. Это отдельные маршруты — прошивка их не зовёт."""
        printer = self.server.printer
        action = query_text(query, "action")

        if action == "start":
            if printer.state != "ready":
                self.reply({"error": {"code": 503,
                                      "message": "klippy не готов (%s)" % printer.state}}, 503)
                return
            filename = query_text(query, "filename") or (printer.snapshot()["files"] or [""])[0]
            if not filename or not printer.start_print(filename):
                self.reply({"error": {"code": 404, "message": "файл не найден: %s" % filename}}, 404)
                return
        elif action == "pause":
            printer.run_line("PAUSE")
        elif action == "resume":
            printer.run_line("RESUME")
        elif action == "cancel":
            printer.run_line("CANCEL_PRINT")
        elif action == "finish":
            printer.finish_print()
        elif action == "advance":
            printer.advance(query_number(query, "value", 0.1))
        elif action == "error":
            printer.klippy_error(query_text(query, "message") or "injected by the mock panel")
        elif action == "shutdown":
            printer.shutdown(query_text(query, "message")
                             or "MCU 'mcu' shutdown: injected by the mock panel")
        elif action == "startup":
            printer.startup()
        elif action in ("reset", "restart"):
            # для экрана RESTART и FIRMWARE_RESTART выглядят одинаково: снова ready
            printer.run_line("FIRMWARE_RESTART" if action == "reset" else "RESTART")
            printer.reset()
        elif action == "emergency_stop":
            printer.emergency_stop()
        elif action == "heat":
            printer.set_temp(extruder=query_number(query, "extruder"),
                             bed=query_number(query, "bed"))
        elif action == "fan":
            printer.set_fan(query_number(query, "value", 0.0))
        elif action == "message":
            printer.set_message(query_text(query, "text"))
        elif action == "power":
            device, state = query_text(query, "device"), query_text(query, "state")
            if device not in printer.power or state not in ("on", "off"):
                self.reply({"error": {"code": 400,
                                      "message": "неизвестное устройство или состояние"}}, 400)
                return
            printer.power[device] = state
        else:
            self.reply({"error": {"code": 400, "message": "неизвестное действие: %s" % action}}, 400)
            return
        self.reply({"result": "ok", "action": action})

    # --- файлы ----------------------------------------------------------
    def serve_gcode_file(self, name):
        printer = self.server.printer
        name = unquote(name)
        if name.endswith(".png"):
            # превью генерируем на ходу: имя файла работает как зерно цвета
            self.reply_raw(render_thumbnail_png(name), 200, "image/png")
            return
        path = printer.file_path(name)
        if path is None:
            self.reply({"error": {"code": 404, "message": "File not found"}}, 404)
            return
        with open(path, "rb") as handle:
            self.reply_raw(handle.read(), 200, "text/plain")

    def handle_upload(self, method, query):
        if method != "POST":
            self.reply({"error": {"code": 405, "message": "Method not allowed"}}, 405)
            return
        body = self.read_body()
        name = query.get("filename", [""])[0]
        boundary = re.search(r'boundary="?([^";]+)', self.headers.get("Content-Type", ""))
        if boundary:
            marker = b"--" + boundary.group(1).encode("utf-8")
            for part in body.split(marker):
                if b'name="file"' not in part:
                    continue
                head, _, payload = part.partition(b"\r\n\r\n")
                found = re.search(rb'filename="([^"]*)"', head)
                if found:
                    name = os.path.basename(found.group(1).decode("utf-8"))
                body = payload[:-2] if payload.endswith(b"\r\n") else payload
                break
        name = os.path.basename(name)
        if not name:
            self.reply({"error": {"code": 400, "message": "No filename in the upload"}}, 400)
            return
        target = self.server.printer.file_path(name) or os.path.join(self.server.printer.files_dir, name)
        with open(target, "wb") as handle:
            handle.write(body)
        self.reply({"result": {"item": {"path": name, "size": len(body)}, "action": "create"}}, 201)

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        return self.rfile.read(length) if length > 0 else b""


PANEL_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mock-moonraker — пульт проверки экрана</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: #14161a; color: #e6e8eb;
       font: 14px/1.45 "Segoe UI", system-ui, sans-serif; }
.top { display: flex; flex-wrap: wrap; gap: 14px; align-items: baseline; margin-bottom: 14px; }
h1 { font-size: 18px; margin: 0; }
h2 { font-size: 12px; margin: 0 0 8px; color: #8b93a1; letter-spacing: .07em; text-transform: uppercase; }
.hint { color: #8b93a1; }
code { background: #23262c; padding: 1px 5px; border-radius: 4px; }
.grid { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); }
.card { background: #1b1e24; border: 1px solid #2b2f36; border-radius: 10px; padding: 14px; }
.wide { grid-column: 1 / -1; }
.badges { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.badge { padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; background: #2b2f36; }
.badge.ready, .badge.standby, .badge.complete { background: #1f5c3d; }
.badge.printing { background: #1c4a7a; }
.badge.paused, .badge.startup { background: #6b5312; }
.badge.error, .badge.shutdown, .badge.cancelled { background: #7a2222; }
.bar { height: 10px; margin: 6px 0 4px; border-radius: 6px; background: #2b2f36; overflow: hidden; }
.bar > div { height: 100%; width: 0; background: linear-gradient(90deg, #2f7ad6, #4fb3ff); }
.kv { display: grid; grid-template-columns: auto 1fr; gap: 4px 14px; margin: 12px 0 0; }
.kv dt { color: #8b93a1; white-space: nowrap; }
.kv dd { margin: 0; font-variant-numeric: tabular-nums; }
.buttons { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 0 0 14px; }
button, select, input { font: inherit; color: inherit; }
button { padding: 7px 12px; background: #2b3038; border: 1px solid #3a4049; border-radius: 8px; cursor: pointer; }
button:hover { background: #343a44; }
button:active { transform: translateY(1px); }
button.primary { background: #1d4f7c; border-color: #2f6ea6; }
button.danger { background: #47201f; border-color: #6d3230; }
button.danger:hover { background: #582624; }
select, input[type=text] { padding: 6px 8px; background: #23262c; border: 1px solid #3a4049; border-radius: 8px; }
.log { max-height: 250px; overflow: auto; font: 12px/1.5 Consolas, "Courier New", monospace; }
.log div { padding: 1px 4px; border-bottom: 1px solid #23262c; white-space: nowrap; }
.log .t { color: #6d7583; }
.log .p { color: #62b0ff; }
.log .c2 { color: #6fcf97; }
.log .c3 { color: #f2c94c; }
.log .c4, .log .c5 { color: #eb5757; }
.log .s-line { color: #9aa3b0; }
.s-ok { background: #1f5c3d; }
.s-warn { background: #6b5312; }
.s-bad { background: #7a2222; }
.s-off { background: #3a3f47; }
</style>
</head>
<body>
<div class="top">
  <h1>mock-moonraker</h1>
  <span class="hint">на экране CYD-Klipper введите IP <code id="host">…</code>
    и порт <code id="port">…</code></span>
  <span class="hint" id="conn">связь…</span>
</div>

<div class="grid">
  <section class="card">
    <h2>Состояние</h2>
    <div class="badges">
      <span class="badge" id="b-webhooks">webhooks: …</span>
      <span class="badge" id="b-print">print_stats: …</span>
      <span class="badge" id="b-files">файлов: …</span>
    </div>
    <div class="bar"><div id="progress"></div></div>
    <div class="hint" id="lines">файл не выбран</div>
    <dl class="kv">
      <dt>state_message</dt><dd id="state-message">—</dd>
      <dt>M117</dt><dd id="message">—</dd>
      <dt>экструдер</dt><dd id="extruder">—</dd>
      <dt>стол</dt><dd id="bed">—</dd>
      <dt>вентилятор</dt><dd id="fan">—</dd>
      <dt>позиция</dt><dd id="position">—</dd>
      <dt>скорость / поток</dt><dd id="factors">—</dd>
      <dt>power</dt><dd id="power">—</dd>
    </dl>
  </section>

  <section class="card">
    <h2>Печать</h2>
    <div class="buttons">
      <select id="file"></select>
      <button class="primary" data-act="start">Печать файла</button>
      <button data-act="pause">Пауза</button>
      <button data-act="resume">Продолжить</button>
      <button data-act="advance" data-value="0.1">+10 % прогресса</button>
      <button class="danger" data-act="cancel">Отмена печати</button>
      <button class="danger" data-act="finish">Конец печати (complete)</button>
    </div>

    <h2>Крайние случаи</h2>
    <div class="buttons">
      <button class="danger" data-act="error">Ошибка klippy (error)</button>
      <button class="danger" data-act="shutdown">MCU shutdown</button>
      <button data-act="startup">Startup</button>
      <button class="primary" data-act="reset">Сброс (FIRMWARE_RESTART)</button>
      <button class="danger" data-act="emergency_stop">Emergency stop (M112)</button>
    </div>

    <h2>Температуры и прочее</h2>
    <div class="buttons">
      <button data-act="heat" data-extruder="200" data-bed="60">Греть 200 / 60</button>
      <button data-act="heat" data-extruder="250" data-bed="100">Греть 250 / 100</button>
      <button data-act="heat" data-extruder="0" data-bed="0">Остудить (target 0)</button>
      <button data-act="fan" data-value="1">Вентилятор 100 %</button>
      <button data-act="fan" data-value="0">Вентилятор 0 %</button>
      <button data-act="power" data-device="printer" data-state="on">Питание on</button>
      <button data-act="power" data-device="printer" data-state="off">Питание off</button>
      <input id="msg" type="text" size="14" placeholder="M117 сообщение">
      <button data-act="message">Отправить M117</button>
    </div>
    <p class="hint" id="act-result">Кнопки меняют состояние мока — экран увидит
      это на следующем опросе (примерно раз в 0.8 с).</p>
  </section>

  <section class="card wide">
    <h2>Журнал запросов экрана <span class="hint" id="log-count"></span></h2>
    <div class="buttons">
      <button id="log-pause">Пауза</button>
      <button id="log-clear">Очистить</button>
      <span class="hint">запросы самого пульта в журнал не пишутся</span>
    </div>
    <div class="log" id="log"></div>
  </section>

  <section class="card wide">
    <h2>Автозапуск стенда <span class="hint">test_printer/autostart.py</span></h2>
    <div class="badges">
      <span class="badge" id="s-sup">супервизор: …</span>
      <span class="badge" id="s-task">задача планировщика: …</span>
      <span class="badge" id="s-mock">мок: …</span>
      <span class="badge" id="s-bridge">мост: …</span>
      <span class="badge" id="s-traffic">трафик: …</span>
    </div>
    <p class="hint" id="s-note">Стенд сопровождает супервизор (autostart.py): он следит за моком и мостом
      по трафику экрана и сам их перезапускает. Здесь — его мнение прямо сейчас и хвост supervisor.log.</p>
    <div class="log" id="s-log" style="max-height:170px"></div>
  </section>
</div>

<script>
const $ = (id) => document.getElementById(id);
let logPaused = false;
let lastFileList = "";
let lastError = "";

async function api(path, options) {
  const resp = await fetch(path, options || {});
  const type = resp.headers.get("content-type") || "";
  const data = type.includes("json") ? await resp.json() : await resp.text();
  if (!resp.ok) throw new Error(resp.status + (data && data.error ? ": " + data.error.message : ""));
  return data;
}

const num = (value, digits) => Number(value).toFixed(digits === undefined ? 1 : digits);
const ratio = (value) => (Number(value) * 100).toFixed(1) + " %";

function badge(element, cls, text) {
  element.className = "badge " + cls;
  element.textContent = text;
}

function renderFiles(files) {
  const list = files.join("\n");
  if (list === lastFileList) return;
  lastFileList = list;
  const select = $("file");
  const previous = select.value;
  select.textContent = "";
  for (const name of files) {
    const option = document.createElement("option");
    option.value = option.textContent = name;
    select.append(option);
  }
  if (files.includes(previous)) select.value = previous;
}

function render(state) {
  $("host").textContent = location.hostname;
  $("port").textContent = location.port || "7125";
  badge($("b-webhooks"), state.state, "webhooks: " + state.state);
  badge($("b-print"), state.print_state, "print_stats: " + state.print_state);
  badge($("b-files"), "", "файлов: " + state.files.length);
  $("progress").style.width = (state.progress * 100).toFixed(1) + "%";
  $("lines").textContent = state.filename
    ? state.filename + " · слои " + state.current_layer + "/" + (state.total_layer || "?") +
      " · прогресс " + ratio(state.progress)
    : "файл не выбран";
  $("state-message").textContent = state.state_message || "—";
  $("message").textContent = state.message || "—";
  $("extruder").textContent = num(state.extruder.temperature) + " °C → " + num(state.extruder.target) + " °C";
  $("bed").textContent = num(state.heater_bed.temperature) + " °C → " + num(state.heater_bed.target) + " °C";
  $("fan").textContent = (state.fan * 100).toFixed(0) + " %";
  $("position").textContent = state.position.map((v) => num(v)).join(" / ") +
    "  (homed: " + (state.homed || "нет") + ")";
  $("factors").textContent = num(state.speed_factor, 2) + " / " + num(state.extrude_factor, 2);
  $("power").textContent = Object.entries(state.power).map((e) => e[0] + ": " + e[1]).join(", ") || "—";
  renderFiles(state.files);
}

async function refreshState() {
  try {
    render((await api("/mock/state")).result);
    lastError = "";
    $("conn").textContent = "связь есть";
  } catch (err) {
    if (lastError !== String(err)) {
      lastError = String(err);
      $("conn").textContent = "нет связи с моком: " + err;
    }
  }
}

async function refreshLog() {
  if (logPaused) return;
  let items;
  try {
    items = (await api("/mock/log?limit=80")).result.requests;
  } catch (err) {
    return;
  }
  const box = $("log");
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 6;
  box.textContent = "";
  for (const item of items) {
    const row = document.createElement("div");
    const time = document.createElement("span");
    time.className = "t";
    time.textContent = item.time + "  ";
    const method = document.createElement("span");
    method.textContent = item.method + " ";
    const path = document.createElement("span");
    path.className = "p";
    path.textContent = item.path;
    const code = document.createElement("span");
    code.className = "c" + String(item.code).charAt(0);
    code.textContent = " → " + item.code;
    row.append(time, method, path, code);
    box.append(row);
  }
  $("log-count").textContent = "последние " + items.length;
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function renderAutostart(st) {
  const sup = $("s-sup"), task = $("s-task"), mockB = $("s-mock"),
        bridge = $("s-bridge"), traffic = $("s-traffic");
  if (st.enabled) {
    badge(sup, "s-ok", "супервизор: работает (PID " + st.pid + ", heartbeat " + st.heartbeat_age + " с назад)");
  } else {
    const reason = st.pid
      ? "PID " + st.pid + " молчит " + (st.heartbeat_age === null ? "давно" : Math.round(st.heartbeat_age) + " с")
      : "не запущен";
    badge(sup, "s-off", "супервизор: " + reason);
  }
  const t = st.task || {};
  if (!t.installed) {
    badge(task, "s-off", "задача планировщика: не установлена (autostart.sh install)");
  } else if (t.enabled === false) {
    badge(task, "s-warn", "задача планировщика: выключена (autostart.sh start включает)");
  } else {
    badge(task, "s-ok", "задача планировщика: включена");
  }
  badge(mockB, st.mock === "ours" ? "s-ok" : (st.mock ? "s-warn" : "s-off"),
        "мок: " + (st.mock || "—"));
  if (!st.bridge) {
    badge(bridge, "s-off", "мост: нет экрана на USB");
  } else if (st.bridge === "ours") {
    badge(bridge, "s-ok", "мост: работает" + (st.traffic ? " · " + st.traffic + " запр. от экрана" : ""));
  } else {
    badge(bridge, st.bridge.startsWith("no-screen") ? "s-off" : "s-warn", "мост: " + st.bridge);
  }
  badge(traffic, st.rate_asked ? "s-ok" : "s-off",
        "трафик: " + (st.rate_asked
          ? st.rate_asked + " запр. / " + st.rate_answered + " отв. — канал живой"
          : "от экрана тишина"));
  const box = $("s-log");
  box.textContent = "";
  for (const line of st.log) {
    const row = document.createElement("div");
    row.className = "s-line";
    row.textContent = line;
    box.append(row);
  }
  box.scrollTop = box.scrollHeight;
}

async function refreshAutostart() {
  try {
    renderAutostart((await api("/mock/autostart")).result);
  } catch (err) { /* мок мог умереть — /mock/state покажет */ }
}



async function doAction(button) {
  const params = Object.assign({}, button.dataset);
  const action = params.act;
  delete params.act;
  if (action === "start") params.filename = $("file").value;
  if (action === "message") params.text = $("msg").value || "Hello from the mock";
  $("act-result").textContent = "… " + action;
  try {
    await api("/mock/action?" + new URLSearchParams(Object.assign({ action }, params)).toString(),
              { method: "POST" });
    $("act-result").textContent = "выполнено: " + action;
  } catch (err) {
    $("act-result").textContent = "не выполнено: " + err;
  }
  await refreshState();
  await refreshLog();
}

for (const button of document.querySelectorAll("[data-act]")) {
  button.addEventListener("click", () => doAction(button));
}
$("log-pause").addEventListener("click", () => {
  logPaused = !logPaused;
  $("log-pause").textContent = logPaused ? "Продолжить" : "Пауза";
});
$("log-clear").addEventListener("click", async () => {
  try { await api("/mock/log?clear=1"); } catch (err) { /* мок уже недоступен */ }
  await refreshLog();
});

refreshState();
refreshLog();
refreshAutostart();
setInterval(refreshState, 700);
setInterval(refreshLog, 900);
setInterval(refreshAutostart, 3000);
</script>
</body>
</html>
"""


# --- состояние автозапуска (супервизор пишет рядом, в .run/) ---------------

RUN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".run")
AUTOSTART_TASK = "CYD-Klipper wired test"


def read_json_file(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def task_state():
    """(установлена_ли, включена_ли) задача планировщика автозапуска.

    Тот же разбор /XML, что в autostart.py: планировщик опускает <Enabled> у
    включённой задачи, тег появляется только после /DISABLE.
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(["schtasks", "/Query", "/TN", AUTOSTART_TASK, "/XML"],
                              capture_output=True, timeout=10, creationflags=flags)
    except Exception:
        return False, None
    if proc.returncode != 0:
        return False, None
    raw = (proc.stdout or b"") + (proc.stderr or b"")
    text = ""
    for encoding in ("utf-16", "utf-8", "cp866"):
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        text = raw.decode("utf-8", "replace")
    block = re.search(r"<Settings>(.*?)</Settings>", text, re.S)
    found = re.findall(r"<Enabled>(true|false)</Enabled>", block.group(1) if block else text, re.I)
    return True, (found[-1].lower() == "true") if found else True


_task_cache = (0.0, False, None)


def task_state_cached():
    """schtasks стоит ~0.1 с — при опросе пультом раз в 3 с кэшируем на 10 с."""
    global _task_cache
    now = time.monotonic()
    if now - _task_cache[0] > 10.0:
        installed, enabled = task_state()
        _task_cache = (now, installed, enabled)
    return _task_cache[1], _task_cache[2]


# Приращение запросов экрана между опросами пульта: мост пишет в bridge.log без
# меток времени, поэтому «за 2 с» честно не посчитать — считаем разницу между
# приходами пульта (он ходит раз в 3 с) и читаем файл с прошлого смещения.
_traffic = {"offset": 0, "size": None, "asked": 0, "answered": 0}


def traffic_rate():
    """Сколько >>>/<<< добавилось в bridge.log с прошлого опроса пульта."""
    path = os.path.join(RUN_DIR, "bridge.log")
    try:
        size = os.path.getsize(path)
    except OSError:
        _traffic.update(offset=0, size=None, asked=0, answered=0)
        return 0, 0
    if _traffic["size"] is None:  # первый опрос — точка отсчёта, приращения ещё нет
        _traffic["size"] = size
        _traffic["offset"] = size
        return 0, 0
    if size < _traffic["size"]:  # лог ротировали/пересоздали — считаем с начала
        _traffic["offset"] = 0
    _traffic["size"] = size
    asked = answered = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            handle.seek(_traffic["offset"])
            text = handle.read()
            _traffic["offset"] = handle.tell()
    except OSError:
        return 0, 0
    for line in text.splitlines():
        if line.startswith(">>> ") and "HTTP_" in line:
            asked += 1
        elif line.startswith("<<< ") and not line.startswith("<<< (Ignored)"):
            answered += 1
    _traffic["asked"], _traffic["answered"] = asked, answered
    return asked, answered


def autostart_snapshot():
    """Живое состояние автозапуска для веб-пульта.

    Никакого IPC: супервизор (test_printer/autostart.py) пишет
    .run/supervisor.json (PID, heartbeat, мнение о моке и мосте) и
    .run/supervisor.log — мок их просто читает.
    """
    heartbeat = read_json_file(os.path.join(RUN_DIR, "supervisor.json"))
    result = {"enabled": False, "heartbeat_age": None, "pid": None,
              "started_at": None, "mock": None, "bridge": None,
              "traffic": None, "log": [], "total_lines": 0}
    if heartbeat:
        try:
            age = time.time() - float(heartbeat.get("heartbeat") or 0)
        except (TypeError, ValueError):
            age = None
        # супервизор пишет heartbeat раз в poll (по умолчанию 10 с)
        if age is not None and 0 <= age < 60:
            result["enabled"] = True
            result["heartbeat_age"] = round(age, 1)
        result["pid"] = heartbeat.get("pid")
        result["started_at"] = heartbeat.get("started_at")
        result["mock"] = heartbeat.get("mock")
        result["bridge"] = heartbeat.get("bridge")
        result["traffic"] = heartbeat.get("traffic")
    installed, enabled = task_state_cached()
    result["task"] = {"installed": installed, "enabled": enabled}
    result["rate_asked"], result["rate_answered"] = traffic_rate()
    try:
        with open(os.path.join(RUN_DIR, "supervisor.log"),
                  encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
        result["total_lines"] = len(lines)
        result["log"] = lines[-14:]
    except OSError:
        pass
    return result


ENDPOINTS = {
    "GET /printer/info",
    "GET /printer/objects/query?<scope>&<scope>...",
    "GET /printer/gcode/script?script=...",
    "GET /printer/gcode/help",
    "GET /printer/emergency_stop",
    "GET /machine/device_power/devices",
    "POST /machine/device_power/device?device=...&action=on|off",
    "GET /server/files/list",
    "GET /server/files/metadata?filename=...",
    "GET /server/files/thumbnails?filename=...",
    "GET /server/files/gcodes/<path>",
    "POST /printer/print/start?filename=...",
    "POST /server/files/upload",
    "GET /ui (веб-пульт: состояние и кнопки крайних случаев)",
    "GET /mock/state",
    "GET|POST /mock/action?action=start|pause|resume|cancel|finish|advance|error|shutdown|startup|reset|emergency_stop|heat|fan|message|power",
    "GET /mock/log?limit=80&clear=1 (журнал запросов экрана)",
    "GET /mock/autostart (состояние автозапуска: супервизор, мост, хвост supervisor.log)",
}


def main():
    try:  # чтобы русский текст в консоли/--help на Windows не превращался в кракозябры
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Мок Moonraker для проверки экрана CYD-Klipper без принтера (без Docker)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="адрес прослушивания (по умолчанию 0.0.0.0, чтобы видел экран)")
    parser.add_argument("--port", type=int, default=7125, help="порт (по умолчанию 7125)")
    parser.add_argument("--files", default=None,
                        help="папка с gcode-файлами (по умолчанию gcodes/ рядом со скриптом)")
    parser.add_argument("--api-key", default=None,
                        help="требовать заголовок X-Api-Key с этим значением")
    parser.add_argument("--auth-code", type=int, default=401,
                        help="код ответа при неверном ключе (401 как у Moonraker; 403 = ветка "
                             "'Incorrect authorisation' на экране)")
    parser.add_argument("--print-seconds", type=float, default=120.0,
                        help="сколько 'длится' печать файла в моке (по умолчанию 120 с)")
    parser.add_argument("--quiet", action="store_true", help="не печатать каждый запрос")
    args = parser.parse_args()

    files_dir = args.files or os.path.join(os.path.dirname(os.path.abspath(__file__)), "gcodes")
    files_dir = os.path.abspath(files_dir)
    os.makedirs(files_dir, exist_ok=True)

    printer = Printer(files_dir, print_seconds=args.print_seconds)

    def simulate():
        last = time.monotonic()
        while True:
            time.sleep(0.5)
            now = time.monotonic()
            printer.step(now - last)
            last = now

    threading.Thread(target=simulate, daemon=True).start()
    try:
        server = MockServer((args.host, args.port), printer,
                            api_key=args.api_key, auth_code=args.auth_code, quiet=args.quiet)
    except OSError as exc:
        print("не смог занять %s:%d (%s)" % (args.host, args.port, exc), file=sys.stderr)
        print("порт занят — работающий Moonraker, докер-стенд или другой экземпляр мока?"
              " Возьмите другой порт через --port.", file=sys.stderr)
        return 1

    print("mock-moonraker: http://%s:%d" % (args.host, args.port))
    print("gcode-файлы:    %s" % files_dir)
    print("проверка:       curl -s http://127.0.0.1:%d/printer/info" % args.port)
    print("веб-пульт:      http://127.0.0.1:%d/ui — состояние и кнопки крайних случаев" % args.port)
    print("на экране:      IP этого компьютера и порт %d" % args.port)
    if args.api_key:
        print("требуется ключ: X-Api-Key: %s (иначе %d)" % (args.api_key, args.auth_code))
    print("Ctrl+C — остановить")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
