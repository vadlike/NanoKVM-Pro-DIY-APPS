import importlib
import mmap
import os
import subprocess
import sys
import threading
import time
from select import select

import numpy as np
from PIL import Image, ImageDraw, ImageFont


PHYSICAL_WIDTH = 172
PHYSICAL_HEIGHT = 320
LOGICAL_WIDTH = 320
LOGICAL_HEIGHT = 172
BPP = 16

BACKGROUND = (12, 16, 22)
PANEL = (25, 33, 43)
PANEL_ALT = (19, 25, 34)
TEXT = (241, 245, 249)
MUTED = (156, 167, 181)
ACCENT = (86, 185, 255)
SUCCESS = (70, 210, 145)
WARNING = (255, 196, 79)
ERROR = (240, 99, 99)

USB_HELPER = "/kvmapp/scripts/usbdev.sh"
USB_HELPER_ACTION = "restart"
USB_DISK0_FLAG = "/boot/usb.disk0"
USB_DISK_SD_FLAG = "/boot/usb.disk1.sd"
USB_DISK_GENERIC_FLAG = "/boot/usb.disk1"
USB_DISK_EMMC_FLAG = "/boot/usb.disk1.emmc"
USB_MODE_FLAGS = (USB_DISK_SD_FLAG, USB_DISK_GENERIC_FLAG, USB_DISK_EMMC_FLAG)
SDCARD_PARTITION = "/dev/mmcblk1p1"
EMMC_IMAGE = "/exfat.img"
MASS_STORAGE_LUN = "/sys/kernel/config/usb_gadget/g0/configs/c.1/mass_storage.disk1/lun.0/file"


class AutoImport:
    @staticmethod
    def import_package(pip_name, import_name=None):
        import_name = import_name or pip_name
        try:
            return importlib.import_module(import_name)
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])
            return importlib.import_module(import_name)


evdev = AutoImport.import_package("evdev")
InputDevice = evdev.InputDevice
ecodes = evdev.ecodes


def load_font(size):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/freefont/FreeMono.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default()


def clip_to_width(draw, value, font, max_width):
    text = "-" if value is None else str(value)
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text

    while len(text) > 1:
        candidate = text[:-1].rstrip() + "..."
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            return candidate
        text = text[:-1]
    return "..."


class InputDeviceFinder:
    def __init__(self, input_root="/sys/class/input"):
        self.input_root = input_root

    def get_event_map(self):
        event_map = {}
        if not os.path.isdir(self.input_root):
            return event_map

        for entry in os.scandir(self.input_root):
            if not entry.is_dir() or not entry.name.startswith("event"):
                continue

            name_path = os.path.join(entry.path, "device", "name")
            if not os.path.exists(name_path):
                continue

            try:
                with open(name_path, "r", encoding="utf-8") as handle:
                    name = handle.readline().strip()
            except OSError:
                continue

            if name:
                event_map[name] = "/dev/input/{0}".format(entry.name)

        return event_map

    def find_touch_device(self):
        event_map = self.get_event_map()
        for name in ("hyn_ts", "goodix_ts", "fts_ts", "gt9xxnew_ts"):
            if name in event_map:
                return event_map[name]
        for name, path in event_map.items():
            lowered = name.lower()
            if "touch" in lowered or "_ts" in lowered or lowered.endswith("ts"):
                return path
        return None

    def find_device_by_name(self, target_name):
        return self.get_event_map().get(target_name)


class RGB565Display:
    def __init__(self, fb_device="/dev/fb0"):
        self.fb_size = PHYSICAL_WIDTH * PHYSICAL_HEIGHT * (BPP // 8)
        self.fb_fd = os.open(fb_device, os.O_RDWR)
        self.fb_mmap = mmap.mmap(self.fb_fd, self.fb_size, mmap.MAP_SHARED, mmap.PROT_WRITE)
        self.fb_array = np.frombuffer(self.fb_mmap, dtype=np.uint16).reshape((PHYSICAL_HEIGHT, PHYSICAL_WIDTH))

    def show_image(self, logical_img):
        physical_img = logical_img.rotate(90, expand=True)
        rgb_array = np.array(physical_img)
        r = (rgb_array[:, :, 0] >> 3).astype(np.uint16)
        g = (rgb_array[:, :, 1] >> 2).astype(np.uint16)
        b = (rgb_array[:, :, 2] >> 3).astype(np.uint16)
        self.fb_array[:, :] = (r << 11) | (g << 5) | b

    def close(self):
        self.fb_mmap.close()
        os.close(self.fb_fd)


class TouchReader:
    def __init__(self, device_path):
        self.device = InputDevice(device_path)
        self.device.grab()
        self.touch_down = False
        self.last_touch = None
        self.raw_x = None
        self.raw_y = None

    def poll(self):
        tapped = None
        rlist, _, _ = select([self.device], [], [], 0)
        if not rlist:
            return {"tap": tapped}

        for event in self.device.read():
            if event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH:
                self.touch_down = event.value == 1
                if event.value == 0:
                    self.last_touch = None
            elif event.type == ecodes.EV_ABS:
                if event.code == ecodes.ABS_MT_POSITION_X:
                    self.raw_x = event.value
                elif event.code == ecodes.ABS_MT_POSITION_Y:
                    self.raw_y = event.value
            elif event.type == ecodes.EV_SYN:
                if self.touch_down and self.raw_x is not None and self.raw_y is not None:
                    x = LOGICAL_WIDTH - 1 - self.raw_y
                    y = self.raw_x
                    x = max(0, min(LOGICAL_WIDTH - 1, x))
                    y = max(0, min(LOGICAL_HEIGHT - 1, y))
                    point = (x, y)
                    if self.last_touch is None:
                        tapped = point
                    self.last_touch = point

        return {"tap": tapped}

    def close(self):
        try:
            self.device.ungrab()
        finally:
            self.device.close()


class KnobReader:
    def __init__(self, rotate_path, key_path):
        self.rotate_device = None
        self.key_device = None

        if rotate_path:
            try:
                self.rotate_device = InputDevice(rotate_path)
                self.rotate_device.grab()
            except OSError:
                self.rotate_device = None

        if key_path:
            try:
                self.key_device = InputDevice(key_path)
                self.key_device.grab()
            except OSError:
                self.key_device = None

    def poll(self):
        devices = [device for device in (self.rotate_device, self.key_device) if device is not None]
        if not devices:
            return {"delta": 0, "press": False}

        delta = 0
        press = False
        try:
            rlist, _, _ = select(devices, [], [], 0)
        except OSError:
            return {"delta": 0, "press": False}
        if not rlist:
            return {"delta": 0, "press": False}

        for device in rlist:
            try:
                events = device.read()
            except OSError:
                continue
            for event in events:
                if event.type == ecodes.EV_REL and event.code == ecodes.REL_X:
                    if event.value > 0:
                        delta = 1
                    elif event.value < 0:
                        delta = -1
                elif event.type == ecodes.EV_KEY and event.code == ecodes.KEY_ENTER and event.value == 1:
                    press = True

        return {"delta": delta, "press": press}

    def close(self):
        for device in (self.rotate_device, self.key_device):
            if device is None:
                continue
            try:
                device.ungrab()
            finally:
                device.close()


class VirtualDiskBackend:
    def __init__(self):
        self.helper_path = USB_HELPER

    def current_mode(self):
        if os.path.exists(USB_DISK_SD_FLAG) or os.path.exists(USB_DISK_GENERIC_FLAG):
            return "sdcard"
        if os.path.exists(USB_DISK_EMMC_FLAG):
            return "emmc"
        return "disable"

    def current_target(self):
        try:
            with open(MASS_STORAGE_LUN, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return ""

    def sdcard_available(self):
        return os.path.exists(SDCARD_PARTITION)

    def emmc_available(self):
        return os.path.exists(EMMC_IMAGE)

    def image_mount_active(self):
        return os.path.exists(USB_DISK0_FLAG)

    def status(self):
        mode = self.current_mode()
        if mode == "sdcard":
            detail = "Remote host will see /sdcard"
        elif mode == "emmc":
            detail = "Remote host will see /exfat.img"
        else:
            detail = "Virtual Disk is disabled"
        if self.image_mount_active():
            detail = "ISO/Image mount is active; disk1 is blocked"
        return {
            "mode": mode,
            "detail": detail,
            "sdcard_available": self.sdcard_available(),
            "emmc_available": self.emmc_available(),
            "image_mount_active": self.image_mount_active(),
            "target": self.current_target(),
        }

    def remove_flag(self, path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def touch_flag(self, path):
        with open(path, "a", encoding="utf-8"):
            pass

    def run_helper(self):
        if not os.path.exists(self.helper_path):
            raise RuntimeError("USB helper not found: {0}".format(self.helper_path))
        process = subprocess.run(
            [self.helper_path, USB_HELPER_ACTION],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45,
            check=False,
        )
        if process.returncode != 0:
            message = process.stderr.strip() or process.stdout.strip() or "usbdev restart failed"
            raise RuntimeError(message)

    def set_mode(self, mode):
        if mode not in ("disable", "emmc", "sdcard"):
            raise RuntimeError("Unsupported mode: {0}".format(mode))

        if self.image_mount_active():
            raise RuntimeError("Disable ISO/Image mount first.")
        if mode == "sdcard" and not self.sdcard_available():
            raise RuntimeError("SD Card is not available.")
        if mode == "emmc" and not self.emmc_available():
            raise RuntimeError("eMMC image is not available.")

        for path in USB_MODE_FLAGS:
            self.remove_flag(path)

        if mode == "sdcard":
            self.touch_flag(USB_DISK_SD_FLAG)
        elif mode == "emmc":
            self.touch_flag(USB_DISK_EMMC_FLAG)

        self.run_helper()
        time.sleep(1.0)

        labels = {
            "disable": "Virtual Disk disabled",
            "emmc": "Virtual Disk -> eMMC",
            "sdcard": "Virtual Disk -> SD Card",
        }
        return labels[mode]


class ActionWorker(threading.Thread):
    def __init__(self, app, callback):
        super().__init__(daemon=True)
        self.app = app
        self.callback = callback

    def run(self):
        try:
            message = self.callback()
            self.app.finish_action(message=message)
        except Exception as exc:
            self.app.finish_action(error=str(exc))


class VirtualDiskSwitchApp:
    def __init__(self):
        self.font_small = load_font(10)
        self.font_medium = load_font(14)
        self.font_large = load_font(18)
        self.font_xlarge = load_font(20)
        self.backend = VirtualDiskBackend()
        self.lock = threading.Lock()
        self.busy = False
        self.error = None
        self.message = "Choose a source for Virtual Disk."
        self.focus_index = 1
        self.last_knob_at = 0.0
        self.header_exit_rect = (10, 10, 38, 38)
        self.disable_button = (16, 116, 100, 156)
        self.emmc_button = (110, 116, 204, 156)
        self.sdcard_button = (214, 116, 304, 156)
        self.buttons = [
            {"label": "Disable", "mode": "disable", "rect": self.disable_button},
            {"label": "eMMC", "mode": "emmc", "rect": self.emmc_button},
            {"label": "SD Card", "mode": "sdcard", "rect": self.sdcard_button},
        ]
        self.refresh_status()

    def refresh_status(self):
        self.state = self.backend.status()
        modes = [button["mode"] for button in self.buttons]
        if self.state["mode"] in modes:
            self.focus_index = modes.index(self.state["mode"])

    def start_action(self, mode):
        with self.lock:
            if self.busy:
                return
            self.busy = True
            self.error = None
            self.message = "Switching to {0}...".format(mode)
        ActionWorker(self, lambda mode=mode: self.backend.set_mode(mode)).start()

    def finish_action(self, message=None, error=None):
        self.refresh_status()
        with self.lock:
            self.busy = False
            if error:
                self.error = error
                self.message = error
            elif message:
                self.error = None
                self.message = message

    def point_in_rect(self, point, rect):
        x, y = point
        left, top, right, bottom = rect
        return left <= x <= right and top <= y <= bottom

    def move_focus(self, step):
        self.focus_index = (self.focus_index + step) % len(self.buttons)
        self.message = self.buttons[self.focus_index]["label"]

    def activate_button(self, button):
        if self.busy:
            return
        self.start_action(button["mode"])

    def update(self, touch_state):
        knob_state = touch_state.get("knob", {"delta": 0, "press": False})

        if knob_state.get("delta"):
            now = time.time()
            if now - self.last_knob_at >= 0.12:
                self.move_focus(knob_state["delta"])
                self.last_knob_at = now

        if knob_state.get("press"):
            self.activate_button(self.buttons[self.focus_index])
            return "continue"

        tap = touch_state.get("tap")
        if not tap:
            return "continue"

        if self.point_in_rect(tap, self.header_exit_rect):
            return "exit"

        for index, button in enumerate(self.buttons):
            if self.point_in_rect(tap, button["rect"]):
                self.focus_index = index
                self.activate_button(button)
                return "continue"

        return "continue"

    def draw_button(self, draw, rect, label, fill, focused=False, disabled=False, font=None):
        actual_fill = PANEL_ALT if disabled else fill
        text_fill = MUTED if disabled else BACKGROUND
        font = font or self.font_medium
        draw.rounded_rectangle(rect, radius=12, fill=actual_fill)
        if focused:
            draw.rounded_rectangle(rect, radius=12, outline=TEXT, width=2)
        box = draw.textbbox((0, 0), label, font=font)
        width = box[2] - box[0]
        height = box[3] - box[1]
        x = rect[0] + ((rect[2] - rect[0] - width) // 2)
        y = rect[1] + ((rect[3] - rect[1] - height) // 2)
        draw.text((x, y), label, fill=text_fill, font=font)

    def render(self):
        canvas = Image.new("RGB", (LOGICAL_WIDTH, LOGICAL_HEIGHT), BACKGROUND)
        draw = ImageDraw.Draw(canvas)

        draw.rounded_rectangle((8, 8, 312, 42), radius=12, fill=PANEL)
        draw.rounded_rectangle(self.header_exit_rect, radius=8, fill=ERROR)
        draw.line((17, 17, 31, 31), fill=TEXT, width=3)
        draw.line((31, 17, 17, 31), fill=TEXT, width=3)
        draw.text((50, 12), "VIRTUAL DISK", fill=TEXT, font=self.font_large)

        draw.rounded_rectangle((8, 48, 312, 108), radius=16, fill=PANEL_ALT)
        draw.text((18, 58), "Source", fill=SUCCESS if not self.error else ERROR, font=self.font_small)
        draw.text((18, 72), {"disable": "Disable", "emmc": "eMMC", "sdcard": "SD Card"}.get(self.state["mode"], "-"), fill=TEXT, font=self.font_xlarge)
        draw.text((18, 94), clip_to_width(draw, self.state["detail"], self.font_small, 276), fill=MUTED, font=self.font_small)

        for index, button in enumerate(self.buttons):
            mode = button["mode"]
            active = self.state["mode"] == mode
            if mode == "sdcard":
                available = self.state["sdcard_available"]
            elif mode == "emmc":
                available = self.state["emmc_available"]
            else:
                available = True
            fill = ACCENT if active else WARNING
            focused = index == self.focus_index
            self.draw_button(
                draw,
                button["rect"],
                button["label"],
                fill,
                focused=focused,
                disabled=(self.busy or not available),
                font=self.font_small if button["label"] == "SD Card" else self.font_medium,
            )

        draw.rounded_rectangle((8, 112, 312, 164), radius=16, outline=(64, 78, 96), width=2)
        footer_color = ERROR if self.error else MUTED
        draw.text((18, 18 + 130), clip_to_width(draw, self.message, self.font_small, 276), fill=footer_color, font=self.font_small)

        target = self.state.get("target") or "-"
        draw.text((18, 150), clip_to_width(draw, target, self.font_small, 220), fill=MUTED, font=self.font_small)
        if self.state["image_mount_active"]:
            draw.text((234, 150), "ISO ACTIVE", fill=WARNING, font=self.font_small)

        return canvas


def main():
    finder = InputDeviceFinder()
    touch_path = finder.find_touch_device()
    if not touch_path:
        raise RuntimeError("Touch input device not found.")

    knob = KnobReader(
        finder.find_device_by_name("rotary@0"),
        finder.find_device_by_name("gpio_keys"),
    )
    touch = TouchReader(touch_path)
    display = RGB565Display()
    app = VirtualDiskSwitchApp()

    try:
        while True:
            touch_state = touch.poll()
            touch_state["knob"] = knob.poll()
            action = app.update(touch_state)
            display.show_image(app.render())
            if action == "exit":
                break
            time.sleep(0.03)
    finally:
        knob.close()
        touch.close()
        display.close()


if __name__ == "__main__":
    main()
