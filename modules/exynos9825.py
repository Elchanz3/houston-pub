import logging
import struct
import sys
import threading
import time

import usb.core

logger = logging.getLogger(__name__)

EXYNOS9825_SOC = "Exynos9820\00"

EP_OUT = 0x02
EP_IN = 0x81
CAPTURE_SIZE = 0x4000

try:
    USBTimeoutError = usb.core.USBTimeoutError
except AttributeError:
    USBTimeoutError = usb.core.USBError


def _frame(magic, payload):
    total = len(payload) + 10
    frame = bytearray(total)
    frame[0:4] = magic
    struct.pack_into("<I", frame, 4, total)
    frame[8:-2] = payload
    struct.pack_into("<H", frame, total - 2, sum(payload) & 0xFFFF)
    return bytes(frame)


def _dnw_frame(payload):
    return _frame(b"\x1bDNW", payload)


def _stage_frame(payload):
    return _frame(struct.pack("<I", 0xFFFFFFFE), payload)


def _render(chunk):
    text = chunk.rstrip(b"\x00")
    if not text:
        return None
    return text.decode("ascii", "replace")


class _Ep1Reader(threading.Thread):
    def __init__(self, device, origin):
        super().__init__(daemon=True)
        self.device = device
        self.origin = origin
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            try:
                chunk = bytes(self.device.read(EP_IN, CAPTURE_SIZE, timeout=200))
            except USBTimeoutError:
                continue
            except usb.core.USBError:
                break
            rendered = _render(chunk)
            if rendered is not None:
                logger.info(f"[{time.monotonic() - self.origin:7.3f}s] device: {rendered!r}")


def _read_ready(device, origin):
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            chunk = bytes(device.read(EP_IN, CAPTURE_SIZE, timeout=500))
        except USBTimeoutError:
            continue
        except usb.core.USBError as error:
            logger.warning(f"=> EP1 IN read failed waiting for payload: {error}")
            return
        rendered = _render(chunk)
        if rendered is not None:
            logger.info(f"[{time.monotonic() - origin:7.3f}s] payload ready: {rendered!r}")
            return
    logger.warning("=> No ready record from the payload; sending the EPBL anyway.")


def run_chain(device, files):
    if len(files) < 2:
        logger.critical(
            "=> Exynos9820/9825 needs the EPBL plus at least one follow-up stage "
            "(e.g. epbl fwbl1 bl2 sboot el3_mon)."
        )
        sys.exit(-1)

    origin = time.monotonic()
    _read_ready(device, origin)

    epbl = open(files[0], "rb").read()
    logger.warning(f"=> Sending first stage (EPBL): {files[0]}")
    device.write(EP_OUT, _dnw_frame(epbl), timeout=10000)
    logger.info("=> EPBL sent.")

    reader = _Ep1Reader(device, origin)
    reader.start()

    for path in files[1:]:
        data = open(path, "rb").read()
        logger.warning(f"=> Sending stage: {path} (0x{len(data):x} bytes)")
        try:
            device.write(EP_OUT, _stage_frame(data), timeout=30000)
        except usb.core.USBError as error:
            logger.warning(
                f"=> Device released USB while sending {path}: {error} "
                "(expected once the bootloader hands off)."
            )
            break

    time.sleep(2.0)
    reader.stop_event.set()
    reader.join(1.0)
    logger.info("=> Chain delivered.")
