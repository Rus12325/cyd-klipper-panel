"""
Самопроверка мока: прогоняет ровно ту последовательность запросов Moonraker,
которую делает прошивка CYD-Klipper (и проводной мост serial_server.py),
а в конце — кнопки веб-пульта (/ui, /mock/state, /mock/action, /mock/log).

Сначала запустите мок, потом:

    python mock_smoke_test.py                                   # http://127.0.0.1:7125
    MOCK_URL=http://127.0.0.1:7126 python mock_smoke_test.py    # другой порт

Если всё хорошо — выходит с кодом 0 и печатает список проверок.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.environ.get("MOCK_URL", "http://127.0.0.1:7125").rstrip("/")
failed = []


def call(path, method="GET", body=None, headers=None):
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read()


def get_json(path):
    status, raw = call(path)
    return status, json.loads(raw)


def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (" | " + str(extra) if extra else ""))
    if not cond:
        failed.append(name)


# 1. connection test прошивки
status, info = get_json("/printer/info")
check("GET /printer/info = 200", status == 200)
check("state=ready", info["result"]["state"] == "ready")

# 2. основной опрос
OBJECTS = ("extruder heater_bed toolhead gcode_move virtual_sdcard print_stats webhooks fan display_status").split()
status, data = get_json("/printer/objects/query?" + "&".join(OBJECTS))
status_obj = data["result"]["status"]
check("objects/query: все 9 объектов", set(OBJECTS) == set(status_obj), sorted(status_obj))
check("extruder.temperature/target/can_extrude",
      all(k in status_obj["extruder"] for k in ("temperature", "target", "can_extrude", "pressure_advance", "smooth_time")))
check("gcode_move.gcode_position/homing_origin/absolute_coordinates/speed_factor/extrude_factor/speed",
      all(k in status_obj["gcode_move"] for k in
          ("gcode_position", "homing_origin", "absolute_coordinates", "speed_factor", "extrude_factor", "speed")))
check("print_stats.info.total_layer/current_layer",
      "total_layer" in status_obj["print_stats"]["info"])
check("webhooks.state", status_obj["webhooks"]["state"] == "ready")

# 3. опрос для мультипринтера (fetch_min)
_, data = get_json("/printer/objects/query?webhooks&print_stats&virtual_sdcard")
check("fetch_min: только 3 объекта", len(data["result"]["status"]) == 3)

# 4. макросы
_, data = get_json("/printer/gcode/help")
macros = [k for k, v in data["result"].items() if v == "CYD_SCREEN_MACRO"]
check("gcode/help: макросы CYD_SCREEN_MACRO", len(macros) >= 1, macros)

# 5. power devices
_, data = get_json("/machine/device_power/devices")
check("device_power/devices", "devices" in data["result"], data["result"])

# 6. файлы
_, data = get_json("/server/files/list")
names = [f["path"] for f in data["result"]]
check("files/list содержит sample.gcode", "sample.gcode" in names, names)

_, data = get_json("/server/files/metadata?filename=sample.gcode")
check("metadata.estimated_time", data["result"]["estimated_time"] > 0, data["result"]["estimated_time"])

_, data = get_json("/server/files/thumbnails?filename=sample.gcode")
thumb = data["result"][0]
check("thumbnails 32x32 .png", thumb["width"] == 32 and thumb["height"] == 32 and thumb["thumbnail_path"].endswith(".png"),
      thumb["thumbnail_path"])

_, png = call("/server/files/gcodes/" + thumb["thumbnail_path"])
w, h = 0, 0
if png[:8] == b"\x89PNG\r\n\x1a\n":
    w, h = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
check("превью — валидный PNG 32x32", (w, h) == (32, 32), "size=%d" % len(png))

# 7. печать
status, _ = call("/printer/print/start?filename=sample.gcode", method="POST")
check("print/start = 200", status == 200)
_, data = get_json("/printer/objects/query?print_stats&virtual_sdcard&toolhead&gcode_move")
before = data["result"]["status"]
check("state=printing", before["print_stats"]["state"] == "printing")
time.sleep(4)
_, data = get_json("/printer/objects/query?print_stats&virtual_sdcard&toolhead")
after = data["result"]["status"]
check("прогресс растёт", after["virtual_sdcard"]["progress"] > before["virtual_sdcard"]["progress"],
      "%s -> %s" % (before["virtual_sdcard"]["progress"], after["virtual_sdcard"]["progress"]))
check("позиция меняется", after["toolhead"]["position"] != before["toolhead"]["position"],
      after["toolhead"]["position"])
check("слои считаются", after["print_stats"]["info"]["current_layer"] >= 1,
      after["print_stats"]["info"])

# 8. нагрев (то, что видит экран после нажатия кнопок)
call("/printer/gcode/script?script=" + urllib.parse.quote("M104 S200"))
call("/printer/gcode/script?script=" + urllib.parse.quote("SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=60"))
_, data = get_json("/printer/objects/query?extruder&heater_bed")
temps = data["result"]["status"]
check("M104 S200 -> target 200", temps["extruder"]["target"] == 200, temps["extruder"])
check("SET_HEATER_TEMPERATURE bed -> target 60", temps["heater_bed"]["target"] == 60, temps["heater_bed"])
time.sleep(3)
_, data = get_json("/printer/objects/query?extruder")
check("температура реально растёт", data["result"]["status"]["extruder"]["temperature"] > temps["extruder"]["temperature"],
      "%s -> %s" % (temps["extruder"]["temperature"], data["result"]["status"]["extruder"]["temperature"]))

# 9. Power device
call("/machine/device_power/device?device=printer&action=on", method="POST")
_, data = get_json("/machine/device_power/devices")
check("power device включён",
      [d for d in data["result"]["devices"] if d["device"] == "printer"][0]["status"] == "on")

# 10. пауза/возобновление/отмена
call("/printer/print/pause", method="POST")
_, data = get_json("/printer/objects/query?print_stats")
check("pause", data["result"]["status"]["print_stats"]["state"] == "paused")
call("/printer/print/resume", method="POST")
_, data = get_json("/printer/objects/query?print_stats")
check("resume", data["result"]["status"]["print_stats"]["state"] == "printing")
call("/printer/print/cancel", method="POST")
_, data = get_json("/printer/objects/query?print_stats")
check("cancel", data["result"]["status"]["print_stats"]["state"] == "cancelled")

# 11. Аварийный стоп и возврат в ready
call("/printer/emergency_stop")
_, data = get_json("/printer/objects/query?webhooks")
check("emergency_stop -> shutdown", data["result"]["status"]["webhooks"]["state"] == "shutdown")
call("/printer/gcode/script?script=" + urllib.parse.quote("FIRMWARE_RESTART"))
_, data = get_json("/printer/objects/query?webhooks")
check("FIRMWARE_RESTART -> ready", data["result"]["status"]["webhooks"]["state"] == "ready")

# 12. Манипуляции с осями и прошими командами
call("/printer/gcode/script?script=" + urllib.parse.quote("G28\nG90\nM106 S255\nM220 S150\nM117 Hello\nG1 X42 Y21 Z1.5 F6000"))
_, data = get_json("/printer/objects/query?toolhead&fan&gcode_move&display_status")
st = data["result"]["status"]
check("G28 -> homed_axes=xyz", st["toolhead"]["homed_axes"] == "xyz")
check("G1 -> позиция 42/21/1.5", st["toolhead"]["position"] == [42.0, 21.0, 1.5], st["toolhead"]["position"])
check("M106 S255 -> fan 1.0", st["fan"]["speed"] == 1.0, st["fan"])
check("M220 S150 -> speed_factor 1.5", st["gcode_move"]["speed_factor"] == 1.5)
check("M117 -> display_status.message", st["display_status"]["message"] == "Hello", st["display_status"])

# 13. Ошибки
try:
    call("/printer/print/start?filename=nope.gcode", method="POST")
    check("несуществующий файл -> 404", False)
except urllib.error.HTTPError as exc:
    check("несуществующий файл -> 404", exc.code == 404)
try:
    call("/server/files/gcodes/../mock_moonraker.py")
    check("выход из папки -> 404", False)
except urllib.error.HTTPError as exc:
    check("выход из папки -> 404", exc.code == 404)

# 14. Загрузка файла (как умеет Moonraker)
boundary = "----mockboundary"
body = (("--%s\r\n" % boundary).encode()
        + b'Content-Disposition: form-data; name="file"; filename="uploaded.gcode"\r\n'
        + b"Content-Type: application/octet-stream\r\n\r\n"
        + b"G28\nG1 X10 Y10 E1\n"
        + ("\r\n--%s--\r\n" % boundary).encode())
status, _ = call("/server/files/upload", method="POST", body=body,
                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
check("upload = 201", status == 201)
_, data = get_json("/server/files/list")
check("загруженный файл в списке", "uploaded.gcode" in [f["path"] for f in data["result"]])

# 15. Веб-пульт (/ui, /mock/*) — то же самое, но без curl
status, page = call("/ui")
check("GET /ui = 200", status == 200)
check("пульт — HTML с кнопками", b"mock-moonraker" in page and b'data-act="error"' in page)

_, data = get_json("/mock/state")
pult = data["result"]
check("mock/state: поля сводки",
      all(k in pult for k in ("state", "print_state", "progress", "extruder", "heater_bed",
                              "message", "position", "power", "files", "mock")))
check("mock/state: файлы видны пульту", len(pult["files"]) >= 1, pult["files"])

_, data = get_json("/mock/log?limit=5")
check("mock/log отдаёт журнал", isinstance(data["result"]["requests"], list),
      len(data["result"]["requests"]))
check("запросы пульта в журнал не попадают",
      all(not r["path"].startswith("/mock/") for r in data["result"]["requests"]))

# 15.5. Состояние автозапуска (/mock/autostart)
_, data = get_json("/mock/autostart")
a = data["result"]
check("mock/autostart: поля сводки",
      all(k in a for k in ("enabled", "task", "mock", "bridge", "rate_asked",
                           "rate_answered", "log", "total_lines")),
      sorted(a))
check("mock/autostart: rate — приращение, не весь лог",
      a["rate_asked"] <= a["total_lines"], a["rate_asked"])


def act(name, **params):
    params["action"] = name
    return get_json("/mock/action?" + urllib.parse.urlencode(params))


def webhooks_state():
    _, data = get_json("/printer/objects/query?webhooks")
    return data["result"]["status"]["webhooks"]["state"]


def print_state():
    _, data = get_json("/printer/objects/query?print_stats")
    return data["result"]["status"]["print_stats"]["state"]


# кнопки ошибок/сброса
act("error")
check("кнопка error -> webhooks=error", webhooks_state() == "error")
act("shutdown")
check("кнопка shutdown -> webhooks=shutdown", webhooks_state() == "shutdown")
act("startup")
check("кнопка startup -> webhooks=startup", webhooks_state() == "startup")
act("reset")
check("кнопка reset -> webhooks=ready", webhooks_state() == "ready")
act("emergency_stop")
check("кнопка emergency_stop -> shutdown", webhooks_state() == "shutdown")
act("reset")

act("error")
try:
    act("start", filename="sample.gcode")
    check("печать при webhooks=error -> 503 (как у Moonraker)", False)
except urllib.error.HTTPError as exc:
    check("печать при webhooks=error -> 503 (как у Moonraker)", exc.code == 503)
act("reset")

# печать по кнопкам: старт, прыжок прогресса, конец, отмена
act("start", filename="sample.gcode")
check("кнопка start -> printing", print_state() == "printing")
act("advance", value=0.5)
_, data = get_json("/printer/objects/query?virtual_sdcard&print_stats")
pult = data["result"]["status"]
check("кнопка advance -> прогресс >= 50%", pult["virtual_sdcard"]["progress"] >= 0.5,
      pult["virtual_sdcard"]["progress"])
check("кнопка advance -> счётчик слоёв подтянулся", pult["print_stats"]["info"]["current_layer"] >= 10,
      pult["print_stats"]["info"])
act("pause")
check("кнопка pause -> paused", print_state() == "paused")
act("resume")
check("кнопка resume -> printing", print_state() == "printing")
act("finish")
check("кнопка finish -> complete", print_state() == "complete")
act("cancel")
check("кнопка cancel -> cancelled", print_state() == "cancelled")
act("reset")
check("reset после печати -> standby", print_state() == "standby")

# температуры, вентилятор, M117 и питание из пульта
act("heat", extruder=210, bed=70)
_, data = get_json("/printer/objects/query?extruder&heater_bed")
st = data["result"]["status"]
check("кнопка heat -> target 210/70",
      (st["extruder"]["target"], st["heater_bed"]["target"]) == (210, 70))
act("heat", extruder=0, bed=0)
act("fan", value=1)
_, data = get_json("/printer/objects/query?fan")
check("кнопка fan -> 100%", data["result"]["status"]["fan"]["speed"] == 1.0)
act("message", text="Panel")
_, data = get_json("/printer/objects/query?display_status")
check("кнопка message -> M117 на экране", data["result"]["status"]["display_status"]["message"] == "Panel")
act("power", device="printer", state="on")
_, data = get_json("/machine/device_power/devices")
check("кнопка power -> устройство on",
      [d for d in data["result"]["devices"] if d["device"] == "printer"][0]["status"] == "on")

try:
    act("nonsense")
    check("неизвестное действие -> 400", False)
except urllib.error.HTTPError as exc:
    check("неизвестное действие -> 400", exc.code == 400)

try:
    act("start", filename="nope.gcode")
    check("пульт: несуществующий файл -> 404", False)
except urllib.error.HTTPError as exc:
    check("пульт: несуществующий файл -> 404", exc.code == 404)

print()
print("ИТОГ (%s): %s" % (BASE, "всё хорошо" if not failed
                         else "провалено %d проверок: %s" % (len(failed), failed)))
sys.exit(1 if failed else 0)
