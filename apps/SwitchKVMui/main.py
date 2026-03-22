import importlib
import mmap
import os
import shutil
import socket
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
EXIT_SIZE = 28

BACKGROUND = (12, 16, 22)
PANEL = (25, 33, 43)
PANEL_ALT = (19, 25, 34)
TEXT = (241, 245, 249)
MUTED = (156, 167, 181)
ACCENT = (86, 185, 255)
SUCCESS = (70, 210, 145)
WARNING = (255, 196, 79)
ERROR = (240, 99, 99)

STATE_FILES = ["/etc/kvm/server.txt", "/boot/.server.txt"]
VALID_TARGETS = ("nanokvm", "pikvm")


class AutoImport:
    @staticmethod
    def import_package(pip_name, import_name=None):
        import_name = import_name or pip_name
        try:
            return importlib.import_module(import_name)
        except ImportError:
            print("Installing missing package:", pip_name)
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


def target_label(target):
    if target == "nanokvm":
        return "NanoKVM"
    if target == "pikvm":
        return "PiKVM"
    return "Unknown"


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
        preferred_names = ["hyn_ts", "goodix_ts", "fts_ts", "gt9xxnew_ts"]
        for name in preferred_names:
            if name in event_map:
                return event_map[name]
        for name, path in event_map.items():
            lowered = name.lower()
            if "touch" in lowered or "_ts" in lowered or lowered.endswith("ts"):
                return path
        return None

    def find_device_by_name(self, target_name):
        event_map = self.get_event_map()
        return event_map.get(target_name)


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
            return {"delta": delta, "press": press}

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


class SwitchBackend:
    def run(self, args, timeout=12, check=True):
        process = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check and process.returncode != 0:
            message = process.stderr.strip() or process.stdout.strip() or "Command failed"
            raise RuntimeError(message)
        return process.stdout.strip()

    def get_hostname(self):
        try:
            return socket.gethostname()
        except OSError:
            return "nanokvm"

    def get_marker_target(self):
        for path in STATE_FILES:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    value = handle.read().strip().lower()
            except OSError:
                continue
            if value in VALID_TARGETS:
                return value, path
        return "unknown", "-"

    def is_service_active(self, service_name):
        if not shutil.which("systemctl"):
            return False
        process = subprocess.run(
            ["systemctl", "is-active", service_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
            check=False,
        )
        return process.returncode == 0 and process.stdout.strip() == "active"

    def is_process_running(self, process_name):
        if shutil.which("pgrep"):
            process = subprocess.run(
                ["pgrep", "-f", process_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=8,
                check=False,
            )
            return process.returncode == 0
        return False

    def detect(self):
        marker_target, marker_path = self.get_marker_target()
        nanokvm_active = self.is_service_active("nanokvm.service") or self.is_process_running("NanoKVM-Server")
        pikvm_active = self.is_service_active("kvmd.service") or self.is_process_running("kvmd")

        if nanokvm_active and not pikvm_active:
            active = "nanokvm"
        elif pikvm_active and not nanokvm_active:
            active = "pikvm"
        elif marker_target in VALID_TARGETS:
            active = marker_target
        elif nanokvm_active:
            active = "nanokvm"
        elif pikvm_active:
            active = "pikvm"
        else:
            active = "unknown"

        return {
            "active": active,
            "marker": marker_target,
            "marker_path": marker_path,
            "hostname": self.get_hostname(),
            "nanokvm_active": nanokvm_active,
            "pikvm_active": pikvm_active,
        }

    def write_target(self, target):
        if target not in VALID_TARGETS:
            raise RuntimeError("Unsupported target: {0}".format(target))

        wrote_any = False
        last_error = None
        for path in STATE_FILES:
            try:
                directory = os.path.dirname(path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(target + "\n")
                wrote_any = True
            except OSError as exc:
                last_error = exc

        if not wrote_any:
            raise RuntimeError("Failed to write switch target: {0}".format(last_error or "no writable state file"))

    def request_reboot(self):
        reboot_cmd = shutil.which("reboot")
        if reboot_cmd:
            subprocess.Popen([reboot_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        if shutil.which("systemctl"):
            subprocess.Popen(["systemctl", "reboot"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        raise RuntimeError("Reboot command not found")

    def switch_to(self, target):
        target = target.lower().strip()
        self.write_target(target)
        self.run(["sync"], timeout=8, check=False)
        time.sleep(0.8)
        self.request_reboot()
        return "Switching to {0}. Rebooting now.".format(target_label(target))


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


class SwitchKVMuiApp:
    def __init__(self):
        self.font_small = load_font(10)
        self.font_medium = load_font(14)
        self.font_large = load_font(18)
        self.font_xlarge = load_font(22)
        self.font_button = load_font(16)
        self.backend = SwitchBackend()
        self.lock = threading.Lock()
        self.data = None
        self.busy = False
        self.message = "Tap target twice to confirm reboot."
        self.error = None
        self.last_checked = "-"
        self.pending_target = None
        self.pending_deadline = 0.0
        self.render_cache = None
        self.render_key = None
        self.focus_index = 1
        self.last_knob_move_at = 0.0
        self.buttons = {
            "nanokvm": (20, 58, 144, 154),
            "pikvm": (176, 58, 300, 154),
        }
        self.next_refresh_at = 0.0
        self.refresh()

    def refresh(self):
        try:
            self.data = self.backend.detect()
            self.error = None
            self.last_checked = time.strftime("%H:%M:%S")
        except Exception as exc:
            self.data = {
                "active": "unknown",
                "marker": "unknown",
                "marker_path": "-",
                "hostname": "-",
                "nanokvm_active": False,
                "pikvm_active": False,
            }
            self.error = str(exc)
            self.message = str(exc)
            self.last_checked = time.strftime("%H:%M:%S")
        self.next_refresh_at = time.time() + 2.0

    def point_in_rect(self, point, rect):
        x, y = point
        left, top, right, bottom = rect
        return left <= x <= right and top <= y <= bottom

    def clear_pending(self):
        self.pending_target = None
        self.pending_deadline = 0.0

    def start_action(self, callback, pending_message):
        with self.lock:
            if self.busy:
                return
            self.busy = True
            self.error = None
            self.message = pending_message
        ActionWorker(self, callback).start()

    def finish_action(self, message=None, error=None):
        try:
            self.data = self.backend.detect()
            self.last_checked = time.strftime("%H:%M:%S")
        except Exception as exc:
            if error is None:
                error = str(exc)
        with self.lock:
            self.busy = False
            if error:
                self.error = error
                self.message = error
            elif message:
                self.message = message
        self.clear_pending()

    def select_target(self, target):
        current = (self.data or {}).get("active", "unknown")
        if self.busy:
            return
        if current == target:
            self.clear_pending()
            self.error = None
            self.message = "{0} is already active.".format(target_label(target))
            return
        now = time.time()
        if self.pending_target == target and now <= self.pending_deadline:
            pending_message = "Switching to {0}".format(target_label(target))
            self.start_action(lambda target=target: self.backend.switch_to(target), pending_message)
            return
        self.pending_target = target
        self.pending_deadline = now + 4.0
        self.error = None
        self.message = "Tap {0} again to confirm reboot.".format(target_label(target))

    def focus_items(self):
        return ["exit", "nanokvm", "pikvm"]

    def move_focus(self, step):
        items = self.focus_items()
        self.focus_index = (self.focus_index + step) % len(items)

    def activate_focus(self):
        current = self.focus_items()[self.focus_index]
        if current == "exit":
            return "exit"
        self.select_target(current)
        return "continue"

    def update(self, touch_state, knob_state=None):
        knob_state = knob_state or {"delta": 0, "press": False}

        if time.time() >= self.next_refresh_at and not self.busy:
            self.refresh()

        if self.pending_target and time.time() > self.pending_deadline and not self.busy:
            self.clear_pending()
            if not self.error:
                self.message = "Tap target twice to confirm reboot."

        if knob_state.get("delta"):
            now = time.time()
            if now - self.last_knob_move_at >= 0.12:
                self.move_focus(knob_state["delta"])
                self.last_knob_move_at = now

        if knob_state.get("press"):
            return self.activate_focus()

        tap = touch_state["tap"]
        if not tap:
            return "continue"

        if tap[0] < EXIT_SIZE + 10 and tap[1] < EXIT_SIZE + 10:
            self.focus_index = 0
            return "exit"

        for target, rect in self.buttons.items():
            if self.point_in_rect(tap, rect):
                self.focus_index = self.focus_items().index(target)
                self.select_target(target)
                return "continue"

        return "continue"

    def make_render_key(self, data, busy, message, error):
        return (
            data.get("active", "unknown"),
            data.get("marker", "unknown"),
            data.get("marker_path", "-"),
            data.get("hostname", "-"),
            data.get("nanokvm_active", False),
            data.get("pikvm_active", False),
            busy,
            message,
            error,
            self.pending_target,
            int(max(self.pending_deadline - time.time(), 0)),
            self.last_checked,
            self.focus_index,
        )

    def draw_button(self, draw, rect, title, active, pending, focused=False):
        if active:
            fill = SUCCESS
            title_fill = BACKGROUND
            subtitle = "ACTIVE"
            subtitle_fill = BACKGROUND
        elif pending:
            fill = WARNING
            title_fill = BACKGROUND
            subtitle = "CONFIRM"
            subtitle_fill = BACKGROUND
        else:
            fill = PANEL
            title_fill = TEXT
            subtitle = "SWITCH"
            subtitle_fill = MUTED

        draw.rounded_rectangle(rect, radius=18, fill=fill)
        if active or pending or focused:
            draw.rounded_rectangle(rect, radius=18, outline=TEXT, width=2)

        left, top, right, bottom = rect
        subtitle_box = draw.textbbox((0, 0), subtitle, font=self.font_small)
        subtitle_width = subtitle_box[2] - subtitle_box[0]
        subtitle_x = left + ((right - left - subtitle_width) // 2)
        draw.text((subtitle_x, top + 12), subtitle, fill=subtitle_fill, font=self.font_small)

        title_box = draw.textbbox((0, 0), title, font=self.font_button)
        title_width = title_box[2] - title_box[0]
        title_height = title_box[3] - title_box[1]
        title_x = left + ((right - left - title_width) // 2)
        title_y = top + ((bottom - top - title_height) // 2) - 1
        draw.text((title_x, title_y), title, fill=title_fill, font=self.font_button)

        if active:
            badge = "NOW"
        elif pending:
            badge = "TAP AGAIN"
        else:
            badge = "READY"
        badge_box = draw.textbbox((0, 0), badge, font=self.font_small)
        badge_width = badge_box[2] - badge_box[0]
        badge_x = left + ((right - left - badge_width) // 2)
        draw.text((badge_x, bottom - 18), badge, fill=subtitle_fill, font=self.font_small)

    def render(self):
        with self.lock:
            busy = self.busy
            message = self.message
            error = self.error
            data = dict(self.data) if self.data else {}

        key = self.make_render_key(data, busy, message, error)
        if key == self.render_key and self.render_cache is not None:
            return self.render_cache, False

        canvas = Image.new("RGB", (LOGICAL_WIDTH, LOGICAL_HEIGHT), BACKGROUND)
        draw = ImageDraw.Draw(canvas)

        draw.rounded_rectangle((8, 8, 312, 42), radius=12, fill=PANEL)
        draw.rounded_rectangle((8, 46, 312, 166), radius=18, fill=PANEL_ALT)

        exit_rect = (10, 10, 10 + EXIT_SIZE, 10 + EXIT_SIZE)
        draw.rounded_rectangle(exit_rect, radius=8, fill=ERROR)
        if self.focus_items()[self.focus_index] == "exit":
            draw.rounded_rectangle(exit_rect, radius=8, outline=TEXT, width=2)
        draw.line((17, 17, 31, 31), fill=TEXT, width=3)
        draw.line((31, 17, 17, 31), fill=TEXT, width=3)

        draw.text((50, 13), "SwitchKVMui", fill=TEXT, font=self.font_medium)
        draw.text((212, 14), clip_to_width(draw, data.get("hostname", "-"), self.font_small, 88), fill=MUTED, font=self.font_small)

        active = data.get("active", "unknown")

        self.draw_button(
            draw,
            self.buttons["nanokvm"],
            "NanoKVM",
            active == "nanokvm",
            self.pending_target == "nanokvm",
            focused=self.focus_items()[self.focus_index] == "nanokvm",
        )
        self.draw_button(
            draw,
            self.buttons["pikvm"],
            "PiKVM",
            active == "pikvm",
            self.pending_target == "pikvm",
            focused=self.focus_items()[self.focus_index] == "pikvm",
        )

        self.render_key = key
        self.render_cache = canvas
        return canvas, True


def main():
    finder = InputDeviceFinder()
    touch_path = finder.find_touch_device()
    if not touch_path:
        print("Touch device not found.")
        sys.exit(1)

    rotate_path = finder.find_device_by_name("rotary@0")
    key_path = finder.find_device_by_name("gpio_keys")

    display = RGB565Display()
    touch = TouchReader(touch_path)
    knob = KnobReader(rotate_path, key_path)
    app = SwitchKVMuiApp()

    try:
        while True:
            state = touch.poll()
            knob_state = knob.poll()
            action = app.update(state, knob_state)
            frame, changed = app.render()
            if changed:
                display.show_image(frame)
            if action == "exit":
                break
            time.sleep(1.0 / 10.0)
    finally:
        knob.close()
        touch.close()
        display.close()


if __name__ == "__main__":
    main()
