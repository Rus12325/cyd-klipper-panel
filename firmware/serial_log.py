#!/usr/bin/env python3
"""Чтение serial-консоли CYD — чтобы убедиться, что после прошивки плата жива.

Примеры:
    python serial_log.py -p COM5                 # просто слушать 10 секунд
    python serial_log.py -p COM5 --reset -s 15   # сбросить плату и поймать лог загрузки
    python serial_log.py -p COM5 --send help     # отправить строку в консоль

Зависимостей нет, кроме pyserial (он же нужен esptool).
"""

import argparse
import sys
import time

import serial


def main():
    ap = argparse.ArgumentParser(description="Читать serial-консоль CYD-Klipper")
    ap.add_argument("-p", "--port", default="COM5", help="COM-порт (по умолчанию COM5)")
    ap.add_argument("-b", "--baud", type=int, default=115200, help="скорость (115200)")
    ap.add_argument("-s", "--seconds", type=float, default=10.0, help="сколько слушать")
    ap.add_argument("--reset", action="store_true", help="дёрнуть DTR/RTS, чтобы поймать загрузку")
    ap.add_argument("--send", default=None, help="отправить строку в консоль (например help)")
    ap.add_argument("--raw", action="store_true", help="не декодировать, писать байты как hex")
    args = ap.parse_args()

    def open_port():
        return serial.Serial(args.port, args.baud, timeout=0.25)

    try:
        port = open_port()
    except Exception as exc:  # порт занят / нет драйвера
        print("Не удалось открыть %s: %s" % (args.port, exc))
        print("Порт почти всегда держит другое приложение — закройте его и повторите.")
        return 1

    if args.reset and port is not None:
        # На платах CYD CH340 подключён к EN/GPIO0 через транзисторы, поэтому
        # такая последовательность = нажатие RST (не BOOT).
        port.dtr = False
        port.rts = True
        time.sleep(0.15)
        port.rts = False
        time.sleep(0.15)
    if args.send:
        port.write((args.send.rstrip("\r\n") + "\r\n").encode())
        port.flush()

    buf = bytearray()
    started = time.time()
    reopens = 0
    while time.time() - started < args.seconds:
        try:
            chunk = port.read(4096)
        except serial.SerialException as exc:
            # Windows: порт CH340 периодически пропадает из системы на несколько
            # секунд (питание/переподключение шины) и возвращается. Не падаем —
            # переоткрываем и продолжаем.
            reopens += 1
            print("\n--- [%.1f с] порт отвалился (%s), переоткрываю... ---" % (time.time() - started, exc))
            try:
                port.close()
            except Exception:
                pass
            for _ in range(30):
                time.sleep(1)
                try:
                    port = open_port()
                    print("--- порт снова открыт, продолжаю ---")
                    if args.send:
                        port.write((args.send.rstrip("\r\n") + "\r\n").encode())
                    break
                except Exception:
                    continue
            else:
                print("--- порт так и не вернулся, выхожу ---")
                break
            continue
        if chunk:
            buf += chunk
            # печатаем сразу, чтобы видеть лог в реальном времени
            sys.stdout.write(chunk.hex(" ") if args.raw else chunk.decode("utf-8", "replace"))
            sys.stdout.flush()

    try:
        port.close()
    except Exception:
        pass

    print()
    print("--- прочитано байт: %d за %.1f с, переоткрытий: %d ---" % (len(buf), args.seconds, reopens))
    if not buf:
        print("Тишина в порту. Это нормально, если плата уже загрузилась и никто её не сбрасывал:")
        print("CYD-Klipper печатает в консоль только события. Повторите с --reset.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
