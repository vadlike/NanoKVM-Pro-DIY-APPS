import glob
import importlib
import mmap
import os
import socket
import subprocess
import sys
import threading
import time
from select import select

import numpy as np
from PIL import Image, ImageDraw, ImageFont


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_LIBRARY_DIR = os.path.join(SCRIPT_DIR, "scripts")
SCRIPT_EXTENSIONS = (".duck", ".ds", ".txt", ".bat", ".ps1", ".exe")
DEFAULT_HID_PATHS = ("/dev/hidg0", "/dev/hidg1")
DEFAULT_MOUSE_HID_PATHS = ("/dev/hidg1", "/dev/hidg2", "/dev/hidg0")
DEFAULT_CONSUMER_HID_PATHS = ("/dev/hidg2", "/dev/hidg3", "/dev/hidg1")

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

HID_KEY_PRESS_SECONDS = 0.018
HID_KEY_RELEASE_SECONDS = 0.008
RUN_DIALOG_READY_SECONDS = 0.35
SHELL_LAUNCH_READY_SECONDS = 0.55
SHELL_LINE_DELAY_SECONDS = 0.03
MOUSE_CLICK_SECONDS = 0.03
TOUCHPAD_GAIN = 0.65
TOUCHPAD_SMOOTHING = 0.55
TOUCHPAD_DEADZONE = 0.18
TOUCHPAD_IDLE_FPS = 12.0
TOUCHPAD_ACTIVE_FPS = 60.0

MODIFIER_BITS = {
    "CTRL": 0x01,
    "CONTROL": 0x01,
    "SHIFT": 0x02,
    "ALT": 0x04,
    "GUI": 0x08,
    "WIN": 0x08,
    "WINDOWS": 0x08,
    "COMMAND": 0x08,
}

KEY_CODES = {
    "ENTER": 0x28,
    "RETURN": 0x28,
    "ESC": 0x29,
    "ESCAPE": 0x29,
    "BACKSPACE": 0x2A,
    "TAB": 0x2B,
    "SPACE": 0x2C,
    "CAPSLOCK": 0x39,
    "PRINTSCREEN": 0x46,
    "SCROLLLOCK": 0x47,
    "PAUSE": 0x48,
    "BREAK": 0x48,
    "INSERT": 0x49,
    "HOME": 0x4A,
    "PAGEUP": 0x4B,
    "DELETE": 0x4C,
    "DEL": 0x4C,
    "END": 0x4D,
    "PAGEDOWN": 0x4E,
    "RIGHT": 0x4F,
    "RIGHTARROW": 0x4F,
    "LEFT": 0x50,
    "LEFTARROW": 0x50,
    "DOWN": 0x51,
    "DOWNARROW": 0x51,
    "UP": 0x52,
    "UPARROW": 0x52,
    "NUMLOCK": 0x53,
    "MENU": 0x65,
    "APP": 0x65,
    "F1": 0x3A,
    "F2": 0x3B,
    "F3": 0x3C,
    "F4": 0x3D,
    "F5": 0x3E,
    "F6": 0x3F,
    "F7": 0x40,
    "F8": 0x41,
    "F9": 0x42,
    "F10": 0x43,
    "F11": 0x44,
    "F12": 0x45,
    "VOLUMEUP": 0x80,
    "VOLUP": 0x80,
    "VOLUMEDOWN": 0x81,
    "VOLDOWN": 0x81,
    "MUTE": 0x7F,
}

SHIFTED_SYMBOLS = {
    "!": 0x1E,
    "@": 0x1F,
    "#": 0x20,
    "$": 0x21,
    "%": 0x22,
    "^": 0x23,
    "&": 0x24,
    "*": 0x25,
    "(": 0x26,
    ")": 0x27,
    "_": 0x2D,
    "+": 0x2E,
    "{": 0x2F,
    "}": 0x30,
    "|": 0x31,
    ":": 0x33,
    '"': 0x34,
    "~": 0x35,
    "<": 0x36,
    ">": 0x37,
    "?": 0x38,
}

PLAIN_SYMBOLS = {
    " ": 0x2C,
    "-": 0x2D,
    "=": 0x2E,
    "[": 0x2F,
    "]": 0x30,
    "\\": 0x31,
    ";": 0x33,
    "'": 0x34,
    "`": 0x35,
    ",": 0x36,
    ".": 0x37,
    "/": 0x38,
}

MOUSE_BUTTON_BITS = {
    "LEFT": 0x01,
    "RIGHT": 0x02,
    "MIDDLE": 0x04,
}

CONSUMER_CODES = {
    "MUTE": 0x00E2,
    "VOLUMEUP": 0x00E9,
    "VOLUMEDOWN": 0x00EA,
}


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


def prettify_name(file_name):
    stem = os.path.splitext(os.path.basename(file_name))[0]
    return stem.replace("_", " ").replace("-", " ").title()


def script_kind_from_name(file_name):
    extension = os.path.splitext(file_name)[1].lower()
    if extension in (".duck", ".ds", ".txt"):
        return "duckyscript"
    if extension == ".bat":
        return "batch"
    if extension == ".ps1":
        return "powershell"
    if extension == ".exe":
        return "executable"
    return "unknown"


def get_hostname():
    try:
        return socket.gethostname()
    except OSError:
        return "nanokvm"


def locate_hid_keyboard():
    for path in DEFAULT_HID_PATHS:
        if os.path.exists(path):
            return path

    matches = sorted(glob.glob("/dev/hidg*"))
    for path in matches:
        if os.path.exists(path):
            return path

    return None


def locate_hid_mouse(exclude_path=None):
    for path in DEFAULT_MOUSE_HID_PATHS:
        if path == exclude_path:
            continue
        if os.path.exists(path):
            return path

    matches = sorted(glob.glob("/dev/hidg*"))
    for path in matches:
        if path == exclude_path:
            continue
        if os.path.exists(path):
            return path

    return None


def locate_hid_consumer(exclude_paths=None):
    excluded = set(exclude_paths or [])

    for path in DEFAULT_CONSUMER_HID_PATHS:
        if path in excluded:
            continue
        if os.path.exists(path):
            return path

    matches = sorted(glob.glob("/dev/hidg*"))
    for path in matches:
        if path in excluded:
            continue
        if os.path.exists(path):
            return path

    return None


def parse_command_count(script_text):
    count = 0
    for raw_line in script_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper == "REM" or upper.startswith("REM ") or upper.startswith("//"):
            continue
        count += 1
    return count


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
        return self.get_event_map().get(target_name)


class RGB565Display:
    def __init__(self, fb_device="/dev/fb0"):
        self.fb_size = PHYSICAL_WIDTH * PHYSICAL_HEIGHT * (BPP // 8)
        self.fb_fd = os.open(fb_device, os.O_RDWR)
        self.fb_mmap = mmap.mmap(
            self.fb_fd,
            self.fb_size,
            mmap.MAP_SHARED,
            mmap.PROT_WRITE,
        )
        self.fb_array = np.frombuffer(self.fb_mmap, dtype=np.uint16).reshape(
            (PHYSICAL_HEIGHT, PHYSICAL_WIDTH)
        )

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
        self.released_at = None
        self.raw_x = None
        self.raw_y = None

    def poll(self):
        tapped = None
        released = False
        self.released_at = None
        rlist, _, _ = select([self.device], [], [], 0)
        if not rlist:
            return {"down": self.touch_down, "touch": self.last_touch, "tap": tapped, "released": released, "released_at": self.released_at}

        for event in self.device.read():
            if event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH:
                self.touch_down = event.value == 1
                if event.value == 0:
                    self.released_at = self.last_touch
                    released = True
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

        return {"down": self.touch_down, "touch": self.last_touch, "tap": tapped, "released": released, "released_at": self.released_at}

    def close(self):
        try:
            self.device.ungrab()
        finally:
            self.device.close()


class KnobReader:
    def __init__(self, rotate_path, key_path):
        self.rotate_device = InputDevice(rotate_path) if rotate_path else None
        self.key_device = InputDevice(key_path) if key_path else None
        if self.rotate_device:
            self.rotate_device.grab()
        if self.key_device:
            self.key_device.grab()

    def poll(self):
        devices = [device for device in (self.rotate_device, self.key_device) if device is not None]
        if not devices:
            return {"delta": 0, "press": False}

        delta = 0
        press = False
        rlist, _, _ = select(devices, [], [], 0)
        if not rlist:
            return {"delta": delta, "press": press}

        for device in rlist:
            for event in device.read():
                if event.type == ecodes.EV_REL and event.code == ecodes.REL_X:
                    delta += int(event.value)
                elif event.type == ecodes.EV_KEY and event.code == ecodes.KEY_ENTER and event.value == 1:
                    press = True

        if delta > 0:
            delta = 1
        elif delta < 0:
            delta = -1

        return {"delta": delta, "press": press}

    def close(self):
        for device in (self.rotate_device, self.key_device):
            if device is None:
                continue
            try:
                device.ungrab()
            finally:
                device.close()


class ScriptLibrary:
    def __init__(self, root):
        self.root = root

    def load_scripts(self):
        scripts = []
        if not os.path.isdir(self.root):
            return scripts

        for file_name in sorted(os.listdir(self.root)):
            lower_name = file_name.lower()
            if not lower_name.endswith(SCRIPT_EXTENSIONS):
                continue

            path = os.path.join(self.root, file_name)
            if not os.path.isfile(path):
                continue

            script_kind = script_kind_from_name(file_name)
            content = ""
            if script_kind != "executable":
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        content = handle.read()
                except OSError:
                    continue

            title = None
            description = None
            preview = None
            if script_kind == "executable":
                preview = file_name
            else:
                for raw_line in content.splitlines():
                    stripped = raw_line.strip()
                    if not stripped:
                        continue

                    upper = stripped.upper()
                    if upper.startswith("REM TITLE:") or upper.startswith("REM NAME:"):
                        title = stripped.split(":", 1)[1].strip()
                        continue
                    if upper.startswith("REM DESC:") or upper.startswith("REM DESCRIPTION:"):
                        description = stripped.split(":", 1)[1].strip()
                        continue
                    if upper == "REM" or upper.startswith("REM ") or upper.startswith("//"):
                        continue
                    if preview is None:
                        preview = stripped

            if script_kind == "batch":
                default_description = "Runs each line inside cmd.exe on the target."
            elif script_kind == "powershell":
                default_description = "Runs each line inside Windows PowerShell on the target."
            elif script_kind == "executable":
                default_description = "Launches this executable name or path through the Windows Run dialog."
            else:
                default_description = "No description in script header."

            scripts.append(
                {
                    "title": title or prettify_name(file_name),
                    "description": description or default_description,
                    "preview": preview or "REM only",
                    "path": path,
                    "file_name": file_name,
                    "command_count": 1 if script_kind == "executable" else parse_command_count(content),
                    "kind": script_kind,
                }
            )

        return scripts


class HIDKeyboard:
    def __init__(self, device_path):
        self.device_path = device_path
        self.handle = open(device_path, "wb", buffering=0)

    def send_report(self, modifier=0, keycodes=None):
        keycodes = list(keycodes or [])
        keycodes = keycodes[:6]
        while len(keycodes) < 6:
            keycodes.append(0x00)
        report = bytes([modifier & 0xFF, 0x00] + [code & 0xFF for code in keycodes])
        self.handle.write(report)
        self.handle.flush()

    def release_all(self):
        self.send_report(0x00, [])

    def tap(self, modifier, keycode, base_modifier=0x00, base_keys=None):
        keys = list(base_keys or [])
        if keycode:
            keys.append(keycode)
        self.send_report(base_modifier | modifier, keys)
        time.sleep(HID_KEY_PRESS_SECONDS)
        self.send_report(base_modifier, list(base_keys or []))
        time.sleep(HID_KEY_RELEASE_SECONDS)

    def type_text(self, text, base_modifier=0x00, base_keys=None):
        for char in text:
            modifier, keycode = resolve_character(char)
            self.tap(modifier, keycode, base_modifier=base_modifier, base_keys=base_keys)

    def close(self):
        try:
            self.release_all()
        finally:
            self.handle.close()


class HIDMouse:
    def __init__(self, device_path):
        self.device_path = device_path
        self.handle = open(device_path, "wb", buffering=0)
        self.buttons = 0x00

    def encode_signed_byte(self, value):
        value = int(value)
        value = max(-127, min(127, value))
        return value & 0xFF

    def send_report(self, buttons=None, x=0, y=0, wheel=0):
        if buttons is None:
            buttons = self.buttons
        report = bytes(
            [
                buttons & 0xFF,
                self.encode_signed_byte(x),
                self.encode_signed_byte(y),
                self.encode_signed_byte(wheel),
            ]
        )
        self.handle.write(report)
        self.handle.flush()

    def release_all(self):
        self.buttons = 0x00
        self.send_report(buttons=self.buttons, x=0, y=0, wheel=0)

    def split_axis_steps(self, value):
        pending = int(value)
        steps = []
        while pending != 0:
            chunk = max(-127, min(127, pending))
            steps.append(chunk)
            pending -= chunk
        return steps or [0]

    def move(self, dx, dy):
        x_steps = self.split_axis_steps(dx)
        y_steps = self.split_axis_steps(dy)
        count = max(len(x_steps), len(y_steps))
        while len(x_steps) < count:
            x_steps.append(0)
        while len(y_steps) < count:
            y_steps.append(0)
        for step_x, step_y in zip(x_steps, y_steps):
            self.send_report(buttons=self.buttons, x=step_x, y=step_y, wheel=0)
            time.sleep(HID_KEY_RELEASE_SECONDS)
        self.send_report(buttons=self.buttons, x=0, y=0, wheel=0)

    def wheel(self, delta):
        for step in self.split_axis_steps(delta):
            self.send_report(buttons=self.buttons, x=0, y=0, wheel=step)
            time.sleep(HID_KEY_RELEASE_SECONDS)
        self.send_report(buttons=self.buttons, x=0, y=0, wheel=0)

    def button_down(self, button_name):
        self.buttons |= self.button_bit(button_name)
        self.send_report(buttons=self.buttons, x=0, y=0, wheel=0)

    def button_up(self, button_name):
        self.buttons &= ~self.button_bit(button_name)
        self.send_report(buttons=self.buttons, x=0, y=0, wheel=0)

    def click(self, button_name):
        self.button_down(button_name)
        time.sleep(MOUSE_CLICK_SECONDS)
        self.button_up(button_name)

    def button_bit(self, button_name):
        upper = str(button_name or "").upper()
        if upper not in MOUSE_BUTTON_BITS:
            raise RuntimeError("Unsupported mouse button: {0}".format(button_name))
        return MOUSE_BUTTON_BITS[upper]

    def close(self):
        try:
            self.release_all()
        finally:
            self.handle.close()


class HIDConsumer:
    def __init__(self, device_path):
        self.device_path = device_path
        self.handle = open(device_path, "wb", buffering=0)

    def send_report(self, usage=0x0000):
        report = bytes([usage & 0xFF, (usage >> 8) & 0xFF])
        self.handle.write(report)
        self.handle.flush()

    def tap(self, usage):
        self.send_report(usage)
        time.sleep(HID_KEY_PRESS_SECONDS)
        self.send_report(0x0000)
        time.sleep(HID_KEY_RELEASE_SECONDS)

    def close(self):
        try:
            self.send_report(0x0000)
        finally:
            self.handle.close()


def resolve_character(char):
    if len(char) != 1:
        raise RuntimeError("Expected a single character, got: {0}".format(char))

    if "a" <= char <= "z":
        return 0x00, 0x04 + (ord(char) - ord("a"))
    if "A" <= char <= "Z":
        return 0x02, 0x04 + (ord(char.lower()) - ord("a"))
    if "1" <= char <= "9":
        return 0x00, 0x1E + (ord(char) - ord("1"))
    if char == "0":
        return 0x00, 0x27
    if char in PLAIN_SYMBOLS:
        return 0x00, PLAIN_SYMBOLS[char]
    if char in SHIFTED_SYMBOLS:
        return 0x02, SHIFTED_SYMBOLS[char]

    raise RuntimeError("Unsupported character in STRING: {0}".format(repr(char)))


class DuckyScriptRunner:
    def __init__(self, keyboard, mouse_factory=None):
        self.keyboard = keyboard
        self.mouse_factory = mouse_factory
        self.mouse = None
        self.default_delay_ms = 0
        self.last_action = None
        self.enter_code = KEY_CODES["ENTER"]
        self.held_modifier = 0x00
        self.held_keys = []

    def parse_delay(self, value, line_number, command_name):
        try:
            delay_ms = int(str(value).strip())
        except ValueError:
            raise RuntimeError(
                "Line {0}: {1} expects an integer, got {2!r}".format(line_number, command_name, value)
            )

        if delay_ms < 0:
            raise RuntimeError("Line {0}: {1} cannot be negative.".format(line_number, command_name))

        return delay_ms

    def sleep_ms(self, delay_ms):
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

    def apply_default_delay(self):
        self.sleep_ms(self.default_delay_ms)

    def send_held_state(self):
        self.keyboard.send_report(self.held_modifier, self.held_keys)

    def get_mouse(self):
        if self.mouse is None:
            if self.mouse_factory is None:
                raise RuntimeError("Mouse HID device not available for this script.")
            self.mouse = self.mouse_factory()
        return self.mouse

    def add_held_key(self, keycode, line_number):
        if keycode in self.held_keys:
            return
        if len(self.held_keys) >= 6:
            raise RuntimeError("Line {0}: too many held keys for HID report.".format(line_number))
        self.held_keys.append(keycode)

    def parse_hold_tokens(self, tokens, line_number, command_name):
        if not tokens:
            raise RuntimeError("Line {0}: {1} needs at least one token.".format(line_number, command_name))

        modifier = 0x00
        keycodes = []
        for token in tokens:
            upper = token.upper()
            if upper in MODIFIER_BITS:
                modifier |= MODIFIER_BITS[upper]
                continue

            resolved_keycode = KEY_CODES.get(upper)
            resolved_modifier = 0x00
            if resolved_keycode is None and len(token) == 1:
                resolved_modifier, resolved_keycode = resolve_character(token)

            if resolved_keycode is None:
                raise RuntimeError("Line {0}: unsupported token {1!r}".format(line_number, token))
            if resolved_modifier:
                modifier |= resolved_modifier
            keycodes.append(resolved_keycode)

        return modifier, keycodes

    def execute_action(self, callback):
        callback()
        self.last_action = callback
        self.apply_default_delay()

    def run_script(self, text):
        for line_number, raw_line in enumerate(text.splitlines(), 1):
            self.execute_line(raw_line, line_number)

    def execute_line(self, raw_line, line_number):
        line = raw_line.rstrip("\r\n")
        command_line = line.lstrip()
        stripped = command_line.strip()
        if not stripped:
            return

        upper = stripped.upper()
        if upper == "REM" or upper.startswith("REM ") or upper.startswith("//"):
            return

        if upper.startswith("DEFAULT_DELAY ") or upper.startswith("DEFAULTDELAY "):
            _, value = stripped.split(None, 1)
            self.default_delay_ms = self.parse_delay(value, line_number, "DEFAULT_DELAY")
            return

        if upper.startswith("DELAY "):
            _, value = stripped.split(None, 1)
            delay_ms = self.parse_delay(value, line_number, "DELAY")
            self.sleep_ms(delay_ms)
            return

        if upper.startswith("WAIT "):
            _, value = stripped.split(None, 1)
            delay_ms = self.parse_delay(value, line_number, "WAIT")
            self.sleep_ms(delay_ms)
            return

        if upper.startswith("MOUSE "):
            self.execute_mouse_command(stripped, line_number)
            return

        if upper == "STRING":
            self.execute_action(lambda: None)
            return

        if upper.startswith("STRING "):
            text = command_line[7:]
            self.execute_action(
                lambda text=text: self.keyboard.type_text(
                    text,
                    base_modifier=self.held_modifier,
                    base_keys=self.held_keys,
                )
            )
            return

        if upper == "STRINGLN":
            self.execute_action(
                lambda: self.keyboard.tap(
                    0x00,
                    self.enter_code,
                    base_modifier=self.held_modifier,
                    base_keys=self.held_keys,
                )
            )
            return

        if upper.startswith("STRINGLN "):
            text = command_line[9:]
            self.execute_action(
                lambda text=text: (
                    self.keyboard.type_text(
                        text,
                        base_modifier=self.held_modifier,
                        base_keys=self.held_keys,
                    ),
                    self.keyboard.tap(
                        0x00,
                        self.enter_code,
                        base_modifier=self.held_modifier,
                        base_keys=self.held_keys,
                    ),
                )
            )
            return

        if upper.startswith("HOLD "):
            tokens = stripped.split()[1:]
            modifier, keycodes = self.parse_hold_tokens(tokens, line_number, "HOLD")

            def hold_action(modifier=modifier, keycodes=tuple(keycodes)):
                self.held_modifier |= modifier
                for keycode in keycodes:
                    self.add_held_key(keycode, line_number)
                self.send_held_state()

            self.execute_action(hold_action)
            return

        if upper == "RELEASE":
            raise RuntimeError("Line {0}: RELEASE needs a token or ALL.".format(line_number))

        if upper == "RELEASE ALL":
            self.execute_action(self.release_all)
            return

        if upper.startswith("RELEASE "):
            tokens = stripped.split()[1:]
            modifier, keycodes = self.parse_hold_tokens(tokens, line_number, "RELEASE")

            def release_action(modifier=modifier, keycodes=tuple(keycodes)):
                self.held_modifier &= ~modifier
                self.held_keys = [key for key in self.held_keys if key not in keycodes]
                self.send_held_state()

            self.execute_action(release_action)
            return

        if upper.startswith("REPEAT "):
            _, value = stripped.split(None, 1)
            repeat_count = self.parse_delay(value, line_number, "REPEAT")
            if self.last_action is None:
                raise RuntimeError("Line {0}: REPEAT has no previous command.".format(line_number))
            for _ in range(repeat_count):
                self.last_action()
                self.apply_default_delay()
            return

        self.execute_combo(stripped, line_number)

    def execute_combo(self, line, line_number):
        modifier = 0x00
        keycode = None
        tokens = line.split()

        for token in tokens:
            upper = token.upper()
            if upper in MODIFIER_BITS:
                modifier |= MODIFIER_BITS[upper]
                continue

            resolved_keycode = KEY_CODES.get(upper)
            resolved_modifier = 0x00

            if resolved_keycode is None and len(token) == 1:
                resolved_modifier, resolved_keycode = resolve_character(token)

            if resolved_keycode is None:
                raise RuntimeError("Line {0}: unsupported token {1!r}".format(line_number, token))

            if keycode is not None:
                raise RuntimeError(
                    "Line {0}: only one non-modifier key is supported per command.".format(line_number)
                )

            modifier |= resolved_modifier
            keycode = resolved_keycode

        if keycode is None:
            raise RuntimeError("Line {0}: no key provided.".format(line_number))

        self.execute_action(
            lambda modifier=modifier, keycode=keycode: self.keyboard.tap(
                modifier,
                keycode,
                base_modifier=self.held_modifier,
                base_keys=self.held_keys,
            )
        )

    def release_all(self):
        self.held_modifier = 0x00
        self.held_keys = []
        self.send_held_state()

    def execute_mouse_command(self, line, line_number):
        tokens = line.split()
        if len(tokens) < 2:
            raise RuntimeError("Line {0}: MOUSE command is incomplete.".format(line_number))

        action = tokens[1].upper()
        mouse = self.get_mouse()

        if action == "MOVE":
            if len(tokens) != 4:
                raise RuntimeError("Line {0}: MOUSE MOVE needs dx and dy.".format(line_number))
            try:
                dx = int(tokens[2])
                dy = int(tokens[3])
            except ValueError:
                raise RuntimeError("Line {0}: MOUSE MOVE expects integer dx and dy.".format(line_number))
            self.execute_action(lambda dx=dx, dy=dy: mouse.move(dx, dy))
            return

        if action == "WHEEL":
            if len(tokens) != 3:
                raise RuntimeError("Line {0}: MOUSE WHEEL needs one integer delta.".format(line_number))
            try:
                delta = int(tokens[2])
            except ValueError:
                raise RuntimeError("Line {0}: MOUSE WHEEL expects an integer.".format(line_number))
            self.execute_action(lambda delta=delta: mouse.wheel(delta))
            return

        if action in ("CLICK", "DOWN", "UP"):
            if len(tokens) != 3:
                raise RuntimeError("Line {0}: MOUSE {1} needs LEFT, RIGHT, or MIDDLE.".format(line_number, action))
            button_name = tokens[2].upper()
            if action == "CLICK":
                self.execute_action(lambda button_name=button_name: mouse.click(button_name))
            elif action == "DOWN":
                self.execute_action(lambda button_name=button_name: mouse.button_down(button_name))
            else:
                self.execute_action(lambda button_name=button_name: mouse.button_up(button_name))
            return

        raise RuntimeError("Line {0}: unsupported MOUSE action {1!r}".format(line_number, action))

    def close(self):
        self.release_all()
        if self.mouse is not None:
            self.mouse.close()
            self.mouse = None


class WindowsShellRunner:
    def __init__(self, keyboard):
        self.keyboard = keyboard

    def open_run_dialog(self):
        self.keyboard.tap(MODIFIER_BITS["GUI"], resolve_character("r")[1])
        time.sleep(RUN_DIALOG_READY_SECONDS)

    def launch_shell(self, command_text):
        self.open_run_dialog()
        self.keyboard.type_text(command_text)
        self.keyboard.tap(0x00, KEY_CODES["ENTER"])
        time.sleep(SHELL_LAUNCH_READY_SECONDS)

    def normalize_lines(self, text, shell_kind):
        lines = []
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue

            upper = stripped.upper()
            if upper.startswith("REM TITLE:") or upper.startswith("REM NAME:"):
                continue
            if upper.startswith("REM DESC:") or upper.startswith("REM DESCRIPTION:") or upper.startswith("REM NOTE:"):
                continue
            if shell_kind == "powershell" and (upper == "REM" or upper.startswith("REM ")):
                continue

            lines.append(raw_line.rstrip())
        return lines

    def run_batch(self, text):
        lines = self.normalize_lines(text, "batch")
        if not lines:
            raise RuntimeError("Batch script has no executable lines.")
        if len(lines) == 1:
            self.launch_shell('cmd /k "{0}"'.format(lines[0]))
            return

        self.launch_shell("cmd")
        for line in lines:
            self.keyboard.type_text(line)
            self.keyboard.tap(0x00, KEY_CODES["ENTER"])
            time.sleep(SHELL_LINE_DELAY_SECONDS)

    def run_powershell(self, text):
        lines = self.normalize_lines(text, "powershell")
        if not lines:
            raise RuntimeError("PowerShell script has no executable lines.")
        if len(lines) == 1:
            one_liner = lines[0].replace('"', '`"')
            self.launch_shell(
                'powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -NoExit -Command "{0}"'.format(one_liner)
            )
            return

        self.launch_shell("powershell -NoLogo -NoProfile -ExecutionPolicy Bypass")
        for line in lines:
            self.keyboard.type_text(line)
            self.keyboard.tap(0x00, KEY_CODES["ENTER"])
            time.sleep(SHELL_LINE_DELAY_SECONDS)

    def run_executable(self, command_text):
        launch_text = str(command_text).strip()
        if not launch_text:
            raise RuntimeError("Executable command is empty.")
        self.launch_shell(launch_text)


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


class KVMPilotApp:
    def __init__(self):
        self.font_small = load_font(10)
        self.font_medium = load_font(14)
        self.font_large = load_font(18)
        self.font_xlarge = load_font(21)
        self.lock = threading.Lock()
        self.library = ScriptLibrary(SCRIPT_LIBRARY_DIR)
        self.host_name = get_hostname()
        self.screen = "main"
        self.busy = False
        self.error = None
        self.message = "Use the top buttons."
        self.last_run = "-"
        self.confirm_until = 0.0
        self.last_knob_move_at = 0.0
        self.selected_index = 0
        self.scroll_offset = 0
        self.mouse = None
        self.ui_keyboard = None
        self.consumer = None
        self.keyboard_layout_index = 0
        self.keyboard_shift = False
        self.keyboard_modifier_mask = 0x00
        self.keyboard_focus_index = 0
        self.touchpad_last_point = None
        self.touchpad_scroll_last_y = None
        self.touchpad_moved = False
        self.touchpad_accum_x = 0.0
        self.touchpad_accum_y = 0.0
        self.touchpad_velocity_x = 0.0
        self.touchpad_velocity_y = 0.0
        self.touchpad_scroll_accum = 0.0
        self.touchpad_drag_active = False
        self.main_panel = (8, 48, 312, 108)
        self.library_panel = (8, 8, 312, 164)
        self.keyboard_panel = (8, 48, 312, 164)
        self.touchpad_panel = (8, 8, 312, 132)
        self.touchpad_move_panel = (8, 8, 270, 132)
        self.touchpad_scroll_panel = (272, 8, 312, 132)
        self.back_button = (14, 54, 82, 78)
        self.prev_button = (14, 114, 84, 158)
        self.header_exit_rect = (10, 10, 38, 38)
        self.keyboard_button = (52, 10, 136, 38)
        self.mouse_button = (142, 10, 226, 38)
        self.list_button = (232, 10, 302, 38)
        self.run_button = (96, 114, 224, 158)
        self.next_button = (236, 114, 306, 158)
        self.touchpad_drag_button = (14, 138, 82, 162)
        self.touchpad_left_button = (90, 138, 158, 162)
        self.touchpad_mid_button = (166, 138, 216, 162)
        self.touchpad_right_button = (224, 138, 274, 162)
        self.touchpad_menu_button = (282, 138, 306, 162)
        self.keyboard_prev_button = (10, 138, 74, 162)
        self.keyboard_next_button = (246, 138, 310, 162)
        self.keyboard_layout_label_rect = (82, 138, 238, 162)
        self.reload_scripts()

    def reload_scripts(self):
        scripts = self.library.load_scripts()
        if not scripts:
            self.scripts = []
            self.selected_index = 0
            self.scroll_offset = 0
            self.message = "No scripts found in kvm-pilot/scripts."
            return

        self.scripts = scripts
        self.selected_index = max(0, min(self.selected_index, len(self.scripts) - 1))
        self.sync_selection()

    def selected_script(self):
        if not self.scripts:
            return None
        return self.scripts[self.selected_index]

    def point_in_rect(self, point, rect):
        x, y = point
        left, top, right, bottom = rect
        return left <= x <= right and top <= y <= bottom

    def clear_confirmation(self):
        self.confirm_until = 0.0

    def visible_rows(self):
        return 6

    def sync_selection(self):
        if not self.scripts:
            self.selected_index = 0
            self.scroll_offset = 0
            return

        self.selected_index = max(0, min(self.selected_index, len(self.scripts) - 1))
        max_offset = max(0, len(self.scripts) - self.visible_rows())
        if self.selected_index < self.scroll_offset:
            self.scroll_offset = self.selected_index
        elif self.selected_index >= self.scroll_offset + self.visible_rows():
            self.scroll_offset = self.selected_index - (self.visible_rows() - 1)
        self.scroll_offset = max(0, min(self.scroll_offset, max_offset))

    def open_library(self):
        self.screen = "library"
        self.sync_selection()
        self.message = "Tap Back to return. Tap a row to select."

    def close_library(self, message=None):
        self.screen = "main"
        if message:
            self.message = message

    def open_touchpad(self):
        self.screen = "touchpad"
        self.touchpad_last_point = None
        self.touchpad_scroll_last_y = None
        self.touchpad_moved = False
        self.touchpad_accum_x = 0.0
        self.touchpad_accum_y = 0.0
        self.touchpad_velocity_x = 0.0
        self.touchpad_velocity_y = 0.0
        self.touchpad_scroll_accum = 0.0
        self.message = "Touchpad mode"

    def open_keyboard(self):
        self.screen = "keyboard"
        self.keyboard_modifier_mask = 0x00
        self.keyboard_focus_index = 0
        self.message = "Keyboard"

    def close_keyboard(self, message=None):
        self.screen = "main"
        self.keyboard_modifier_mask = 0x00
        if message:
            self.message = message

    def return_to_main_menu(self):
        if self.screen == "touchpad":
            self.close_touchpad("Back to launcher")
            return
        if self.screen == "library":
            self.close_library("Back to launcher")
            return
        if self.screen == "keyboard":
            self.close_keyboard("Back to launcher")

    def close_touchpad(self, message=None):
        self.release_touchpad_drag()
        self.screen = "main"
        self.touchpad_last_point = None
        self.touchpad_scroll_last_y = None
        self.touchpad_moved = False
        self.touchpad_accum_x = 0.0
        self.touchpad_accum_y = 0.0
        self.touchpad_velocity_x = 0.0
        self.touchpad_velocity_y = 0.0
        self.touchpad_scroll_accum = 0.0
        if message:
            self.message = message

    def file_rows(self):
        rows = []
        visible = self.scripts[self.scroll_offset : self.scroll_offset + self.visible_rows()]
        for row_index, script in enumerate(visible):
            index = self.scroll_offset + row_index
            top = 14 + (row_index * 24)
            rows.append(((14, top, 306, top + 22), index, script))
        return rows

    def move_selection(self, step):
        if self.busy or len(self.scripts) <= 1:
            return
        self.selected_index = (self.selected_index + step) % len(self.scripts)
        self.sync_selection()
        self.clear_confirmation()
        self.error = None
        script = self.selected_script()
        self.message = "Selected {0}".format(script["title"]) if script else self.message

    def start_action(self, callback, pending_message):
        with self.lock:
            if self.busy:
                return
            self.busy = True
            self.error = None
            self.message = pending_message
        ActionWorker(self, callback).start()

    def get_app_mouse(self):
        if self.mouse is not None:
            return self.mouse

        keyboard_path = locate_hid_keyboard()
        mouse_path = locate_hid_mouse(exclude_path=keyboard_path)
        if not mouse_path:
            raise RuntimeError("Mouse HID device not found. Expected another /dev/hidg* gadget.")
        self.mouse = HIDMouse(mouse_path)
        return self.mouse

    def get_app_keyboard(self):
        if self.ui_keyboard is not None:
            return self.ui_keyboard

        keyboard_path = locate_hid_keyboard()
        if not keyboard_path:
            raise RuntimeError("HID keyboard device not found. Expected /dev/hidg0.")
        self.ui_keyboard = HIDKeyboard(keyboard_path)
        return self.ui_keyboard

    def close_app_mouse(self):
        if self.mouse is not None:
            self.mouse.close()
            self.mouse = None

    def close_app_keyboard(self):
        if self.ui_keyboard is not None:
            self.ui_keyboard.close()
            self.ui_keyboard = None

    def get_app_consumer(self):
        if self.consumer is not None:
            return self.consumer

        keyboard_path = locate_hid_keyboard()
        mouse_path = locate_hid_mouse(exclude_path=keyboard_path)
        consumer_path = locate_hid_consumer(exclude_paths=[keyboard_path, mouse_path])
        if not consumer_path:
            raise RuntimeError("Consumer HID device not found.")
        self.consumer = HIDConsumer(consumer_path)
        return self.consumer

    def close_app_consumer(self):
        if self.consumer is not None:
            self.consumer.close()
            self.consumer = None

    def release_touchpad_drag(self):
        if not self.touchpad_drag_active:
            return
        try:
            self.get_app_mouse().button_up("LEFT")
        except Exception:
            pass
        self.touchpad_drag_active = False

    def send_ui_keyboard_key(self, label, modifier, keycode):
        try:
            keyboard = self.get_app_keyboard()
            keyboard.tap(self.keyboard_modifier_mask | (modifier or 0x00), keycode)
            self.error = None
            self.message = "Sent {0}".format(label)
        except Exception as exc:
            self.error = str(exc)
            self.message = str(exc)

    def send_ui_modifier_chord(self, label, modifier_mask):
        try:
            keyboard = self.get_app_keyboard()
            keyboard.tap(modifier_mask, 0x00)
            self.error = None
            self.message = "Sent {0}".format(label)
        except Exception as exc:
            self.error = str(exc)
            self.message = str(exc)

    def send_ui_keyboard_char(self, char):
        try:
            keyboard = self.get_app_keyboard()
            modifier, keycode = resolve_character(char)
            keyboard.tap(self.keyboard_modifier_mask | modifier, keycode)
            self.error = None
            self.message = "Typed {0}".format(char)
        except Exception as exc:
            self.error = str(exc)
            self.message = str(exc)

    def run_windows_hidden_command(self, command_text):
        keyboard = self.get_app_keyboard()
        runner = WindowsShellRunner(keyboard)
        runner.launch_shell(command_text)

    def send_volume_step(self, step):
        try:
            if step > 0:
                command_text = (
                    "powershell -WindowStyle Hidden -NoProfile -Command "
                    "\"$s='using System;using System.Runtime.InteropServices;public class K{[DllImport(\\\"user32.dll\\\")]public static extern void keybd_event(byte v,byte s,uint f,UIntPtr e);}';"
                    "Add-Type $s -ErrorAction SilentlyContinue;[K]::keybd_event(0xAF,0,0,[UIntPtr]::Zero);[K]::keybd_event(0xAF,0,2,[UIntPtr]::Zero)\""
                )
                self.run_windows_hidden_command(command_text)
                self.message = "Volume up"
            elif step < 0:
                command_text = (
                    "powershell -WindowStyle Hidden -NoProfile -Command "
                    "\"$s='using System;using System.Runtime.InteropServices;public class K{[DllImport(\\\"user32.dll\\\")]public static extern void keybd_event(byte v,byte s,uint f,UIntPtr e);}';"
                    "Add-Type $s -ErrorAction SilentlyContinue;[K]::keybd_event(0xAE,0,0,[UIntPtr]::Zero);[K]::keybd_event(0xAE,0,2,[UIntPtr]::Zero)\""
                )
                self.run_windows_hidden_command(command_text)
                self.message = "Volume down"
            self.error = None
        except Exception as exc:
            self.error = str(exc)
            self.message = str(exc)

    def keyboard_layout_name(self):
        return ["BIOS NAV", "BIOS EDIT", "ABC", "123"][self.keyboard_layout_index]

    def keyboard_layout_badge(self):
        return ["NAV", "EDIT", "ABC", "123"][self.keyboard_layout_index]

    def switch_keyboard_layout(self, step):
        layouts = 4
        self.keyboard_layout_index = (self.keyboard_layout_index + step) % layouts
        self.keyboard_shift = False
        self.keyboard_modifier_mask = 0x00
        self.keyboard_focus_index = 0
        self.message = "Layout {0}".format(self.keyboard_layout_name())

    def keyboard_button_specs(self):
        if self.keyboard_layout_index == 0:
            return [
                {"label": "ESC", "rect": (46, 10, 98, 36), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["ESC"]},
                {"label": "DEL", "rect": (101, 10, 153, 36), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["DELETE"]},
                {"label": "TAB", "rect": (156, 10, 208, 36), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["TAB"]},
                {"label": "ENT", "rect": (211, 10, 263, 36), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["ENTER"]},
                {"label": "F12", "rect": (266, 10, 308, 36), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["F12"]},
                {"label": "F1", "rect": (10, 42, 68, 68), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["F1"]},
                {"label": "F2", "rect": (72, 42, 130, 68), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["F2"]},
                {"label": "F8", "rect": (134, 42, 192, 68), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["F8"]},
                {"label": "F10", "rect": (196, 42, 254, 68), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["F10"]},
                {"label": "F11", "rect": (258, 42, 310, 68), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["F11"]},
                {"label": "HOME", "rect": (10, 74, 68, 100), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["HOME"]},
                {"label": "PGUP", "rect": (72, 74, 130, 100), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["PAGEUP"]},
                {"label": "UP", "rect": (134, 74, 192, 100), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["UP"]},
                {"label": "PGDN", "rect": (196, 74, 254, 100), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["PAGEDOWN"]},
                {"label": "END", "rect": (258, 74, 310, 100), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["END"]},
                {"label": "-", "rect": (10, 106, 68, 134), "type": "char", "char": "-"},
                {"label": "LEFT", "rect": (72, 106, 130, 134), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["LEFT"]},
                {"label": "DOWN", "rect": (134, 106, 192, 134), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["DOWN"]},
                {"label": "RIGHT", "rect": (196, 106, 254, 134), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["RIGHT"]},
                {"label": "+", "rect": (258, 106, 310, 134), "type": "char", "char": "+"},
            ]

        if self.keyboard_layout_index == 1:
            return [
                {"label": "F5", "rect": (46, 10, 109, 36), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["F5"]},
                {"label": "F6", "rect": (113, 10, 176, 36), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["F6"]},
                {"label": "F9", "rect": (180, 10, 243, 36), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["F9"]},
                {"label": "INS", "rect": (247, 10, 310, 36), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["INSERT"]},
                {"label": "+", "rect": (10, 42, 82, 68), "type": "char", "char": "+"},
                {"label": "-", "rect": (86, 42, 158, 68), "type": "char", "char": "-"},
                {"label": "Y", "rect": (162, 42, 234, 68), "type": "char", "char": "y"},
                {"label": "N", "rect": (238, 42, 310, 68), "type": "char", "char": "n"},
                {"label": "CTRL", "rect": (10, 74, 82, 100), "type": "modifier", "modifier_bit": MODIFIER_BITS["CTRL"]},
                {"label": "ALT", "rect": (86, 74, 158, 100), "type": "modifier", "modifier_bit": MODIFIER_BITS["ALT"]},
                {"label": "TAB", "rect": (162, 74, 234, 100), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["TAB"]},
                {"label": "DEL", "rect": (238, 74, 310, 100), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["DELETE"]},
                {"label": "BKSP", "rect": (10, 106, 82, 134), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["BACKSPACE"]},
                {"label": "ESC", "rect": (86, 106, 158, 134), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["ESC"]},
                {"label": "ENT", "rect": (162, 106, 234, 134), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["ENTER"]},
                {"label": "F12", "rect": (238, 106, 310, 134), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["F12"]},
            ]

        if self.keyboard_layout_index == 2:
            letters = [
                ["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"],
                ["a", "s", "d", "f", "g", "h", "j", "k", "l"],
                ["z", "x", "c", "v", "b", "n", "m"],
            ]
            if self.keyboard_shift:
                letters = [[char.upper() for char in row] for row in letters]
            specs = []
            row_layouts = [
                {"y": 8, "start_x": 46, "step": 26, "width": 25, "height": 28},
                {"y": 40, "start_x": 10, "step": 33, "width": 30, "height": 28},
                {"y": 72, "start_x": 16, "step": 42, "width": 38, "height": 28},
            ]
            for row_index, row in enumerate(letters):
                row_layout = row_layouts[row_index]
                for index, char in enumerate(row):
                    specs.append(
                        {
                            "label": char.upper() if len(char) == 1 else char,
                            "rect": (
                                row_layout["start_x"] + (index * row_layout["step"]),
                                row_layout["y"],
                                row_layout["start_x"] + (index * row_layout["step"]) + row_layout["width"],
                                row_layout["y"] + row_layout["height"],
                            ),
                            "type": "char",
                            "char": char,
                        }
                    )
            specs.extend(
                [
                    {"label": "SHIFT", "rect": (10, 104, 66, 134), "type": "shift"},
                    {"label": "ALT", "rect": (70, 104, 126, 134), "type": "modifier", "modifier_bit": MODIFIER_BITS["ALT"]},
                    {"label": "CTRL", "rect": (130, 104, 186, 134), "type": "modifier", "modifier_bit": MODIFIER_BITS["CTRL"]},
                    {"label": "BKSP", "rect": (190, 104, 246, 134), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["BACKSPACE"]},
                    {"label": "ENT", "rect": (250, 104, 310, 134), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["ENTER"]},
                ]
            )
            return specs

        return [
            {"label": "1", "rect": (46, 10, 71, 36), "type": "char", "char": "1"},
            {"label": "2", "rect": (72, 10, 97, 36), "type": "char", "char": "2"},
            {"label": "3", "rect": (98, 10, 123, 36), "type": "char", "char": "3"},
            {"label": "4", "rect": (124, 10, 149, 36), "type": "char", "char": "4"},
            {"label": "5", "rect": (150, 10, 175, 36), "type": "char", "char": "5"},
            {"label": "6", "rect": (176, 10, 201, 36), "type": "char", "char": "6"},
            {"label": "7", "rect": (202, 10, 227, 36), "type": "char", "char": "7"},
            {"label": "8", "rect": (228, 10, 253, 36), "type": "char", "char": "8"},
            {"label": "9", "rect": (254, 10, 279, 36), "type": "char", "char": "9"},
            {"label": "0", "rect": (280, 10, 305, 36), "type": "char", "char": "0"},
            {"label": "-", "rect": (10, 42, 38, 68), "type": "char", "char": "-"},
            {"label": "=", "rect": (40, 42, 68, 68), "type": "char", "char": "="},
            {"label": "[", "rect": (70, 42, 98, 68), "type": "char", "char": "["},
            {"label": "]", "rect": (100, 42, 128, 68), "type": "char", "char": "]"},
            {"label": "\\", "rect": (130, 42, 158, 68), "type": "char", "char": "\\"},
            {"label": ";", "rect": (160, 42, 188, 68), "type": "char", "char": ";"},
            {"label": "'", "rect": (190, 42, 218, 68), "type": "char", "char": "'"},
            {"label": ",", "rect": (220, 42, 248, 68), "type": "char", "char": ","},
            {"label": ".", "rect": (250, 42, 278, 68), "type": "char", "char": "."},
            {"label": "/", "rect": (280, 42, 308, 68), "type": "char", "char": "/"},
            {"label": "!", "rect": (10, 74, 38, 100), "type": "char", "char": "!"},
            {"label": "@", "rect": (40, 74, 68, 100), "type": "char", "char": "@"},
            {"label": "#", "rect": (70, 74, 98, 100), "type": "char", "char": "#"},
            {"label": "$", "rect": (100, 74, 128, 100), "type": "char", "char": "$"},
            {"label": "%", "rect": (130, 74, 158, 100), "type": "char", "char": "%"},
            {"label": "^", "rect": (160, 74, 188, 100), "type": "char", "char": "^"},
            {"label": "&", "rect": (190, 74, 218, 100), "type": "char", "char": "&"},
            {"label": "*", "rect": (220, 74, 248, 100), "type": "char", "char": "*"},
            {"label": "(", "rect": (250, 74, 278, 100), "type": "char", "char": "("},
            {"label": ")", "rect": (280, 74, 308, 100), "type": "char", "char": ")"},
            {"label": "DEL", "rect": (10, 106, 66, 134), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["DELETE"]},
            {"label": "ALT", "rect": (70, 106, 126, 134), "type": "modifier", "modifier_bit": MODIFIER_BITS["ALT"]},
            {"label": "CTRL", "rect": (130, 106, 186, 134), "type": "modifier", "modifier_bit": MODIFIER_BITS["CTRL"]},
            {"label": "BKSP", "rect": (190, 106, 246, 134), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["BACKSPACE"]},
            {"label": "ENT", "rect": (250, 106, 310, 134), "type": "key", "modifier": 0x00, "keycode": KEY_CODES["ENTER"]},
        ]

    def keyboard_controls(self):
        controls = [
            {"label": "HOME", "rect": self.header_exit_rect, "type": "ui", "action": "home"},
        ]
        controls.extend(self.keyboard_button_specs())
        controls.extend(
            [
                {"label": "<", "rect": self.keyboard_prev_button, "type": "ui", "action": "layout_prev"},
                {"label": "SPACE", "rect": self.keyboard_layout_label_rect, "type": "ui", "action": "space"},
                {"label": ">", "rect": self.keyboard_next_button, "type": "ui", "action": "layout_next"},
            ]
        )
        return controls

    def move_keyboard_focus(self, step):
        controls = self.keyboard_controls()
        if not controls:
            return
        self.keyboard_focus_index = (self.keyboard_focus_index + step) % len(controls)
        self.message = controls[self.keyboard_focus_index]["label"]

    def activate_keyboard_control(self, control):
        if control["type"] == "ui":
            action = control["action"]
            if action == "home":
                self.return_to_main_menu()
            elif action == "layout_prev":
                self.switch_keyboard_layout(-1)
            elif action == "layout_next":
                self.switch_keyboard_layout(1)
            elif action == "space":
                self.send_ui_keyboard_key("SPACE", 0x00, KEY_CODES["SPACE"])
            return
        self.handle_keyboard_button(control)

    def handle_keyboard_button(self, spec):
        action_type = spec["type"]
        if action_type == "key":
            self.send_ui_keyboard_key(spec["label"], spec.get("modifier", 0x00), spec["keycode"])
            return
        if action_type == "char":
            self.send_ui_keyboard_char(spec["char"])
            return
        if action_type == "modifier":
            modifier_bit = spec["modifier_bit"]
            shift_bit = MODIFIER_BITS["SHIFT"]
            alt_bit = MODIFIER_BITS["ALT"]
            current_mask = self.keyboard_modifier_mask

            if modifier_bit == alt_bit and current_mask & shift_bit:
                self.send_ui_modifier_chord("Shift+Alt", shift_bit | alt_bit)
                self.keyboard_modifier_mask &= ~(shift_bit | alt_bit)
                self.keyboard_shift = False
                return
            if modifier_bit == shift_bit and current_mask & alt_bit:
                self.send_ui_modifier_chord("Shift+Alt", shift_bit | alt_bit)
                self.keyboard_modifier_mask &= ~(shift_bit | alt_bit)
                self.keyboard_shift = False
                return

            if self.keyboard_modifier_mask & modifier_bit:
                self.keyboard_modifier_mask &= ~modifier_bit
                self.message = "{0} OFF".format(spec["label"])
            else:
                self.keyboard_modifier_mask |= modifier_bit
                self.message = "{0} ON".format(spec["label"])
            return
        if action_type == "shift":
            shift_bit = MODIFIER_BITS["SHIFT"]
            self.keyboard_shift = not self.keyboard_shift
            if self.keyboard_shift:
                self.keyboard_modifier_mask |= shift_bit
            else:
                self.keyboard_modifier_mask &= ~shift_bit
            self.message = "Shift {0}".format("ON" if self.keyboard_shift else "OFF")

    def finish_action(self, message=None, error=None):
        self.reload_scripts()
        with self.lock:
            self.busy = False
            self.clear_confirmation()
            if error:
                self.error = error
                self.message = error
            elif message:
                self.last_run = time.strftime("%H:%M:%S")
                self.message = message

    def select_script(self, index):
        if not self.scripts:
            return
        self.selected_index = max(0, min(index, len(self.scripts) - 1))
        self.sync_selection()
        self.clear_confirmation()
        self.error = None
        script = self.selected_script()
        if script:
            self.message = "Selected {0}".format(script["title"])

    def run_selected_script(self):
        script = self.selected_script()
        if script is None:
            self.error = "No script selected."
            self.message = self.error
            return

        now = time.time()
        if now > self.confirm_until:
            self.confirm_until = now + 4.0
            self.error = None
            self.message = "Tap Run again to send {0}".format(script["title"])
            return

        script_path = script["path"]
        script_title = script["title"]
        self.start_action(
            lambda path=script_path, title=script_title: self.execute_script(path, title),
            "Injecting {0}".format(script_title),
        )

    def execute_script(self, script_path, script_title):
        self.release_touchpad_drag()
        self.close_app_mouse()
        self.close_app_keyboard()
        self.close_app_consumer()
        hid_path = locate_hid_keyboard()
        if not hid_path:
            raise RuntimeError("HID keyboard device not found. Expected /dev/hidg0.")
        mouse_path = locate_hid_mouse(exclude_path=hid_path)

        def open_mouse():
            if not mouse_path:
                raise RuntimeError("Mouse HID device not found. Expected another /dev/hidg* gadget.")
            return HIDMouse(mouse_path)

        script_kind = script_kind_from_name(script_path)
        command_name = os.path.basename(script_path)
        script_text = ""
        if script_kind != "executable":
            try:
                with open(script_path, "r", encoding="utf-8") as handle:
                    script_text = handle.read()
            except OSError as exc:
                raise RuntimeError("Failed to read script: {0}".format(exc))

        keyboard = HIDKeyboard(hid_path)
        runner = None
        try:
            if script_kind == "duckyscript":
                runner = DuckyScriptRunner(keyboard, mouse_factory=open_mouse)
                runner.run_script(script_text)
            elif script_kind == "batch":
                runner = WindowsShellRunner(keyboard)
                runner.run_batch(script_text)
            elif script_kind == "powershell":
                runner = WindowsShellRunner(keyboard)
                runner.run_powershell(script_text)
            elif script_kind == "executable":
                runner = WindowsShellRunner(keyboard)
                runner.run_executable(command_name)
            else:
                raise RuntimeError("Unsupported script type: {0}".format(script_kind))
        finally:
            if isinstance(runner, DuckyScriptRunner):
                runner.close()
            keyboard.close()

        return "Sent {0} via {1}".format(script_title, hid_path)

    def update(self, touch_state):
        knob_state = touch_state.get("knob", {"delta": 0, "press": False})
        if self.confirm_until and time.time() > self.confirm_until and not self.busy:
            self.clear_confirmation()
            if not self.error:
                self.message = "Use the top buttons."

        if knob_state.get("delta") and self.screen in ("main", "library", "keyboard"):
            now = time.time()
            if now - self.last_knob_move_at >= 0.12:
                if self.screen == "keyboard":
                    self.move_keyboard_focus(knob_state["delta"])
                else:
                    self.move_selection(knob_state["delta"])
                self.last_knob_move_at = now

        if knob_state.get("press"):
            if self.screen == "library":
                if self.selected_script():
                    self.close_library("Selected {0}".format(self.selected_script()["title"]))
            elif self.screen == "keyboard":
                controls = self.keyboard_controls()
                if controls:
                    self.activate_keyboard_control(controls[self.keyboard_focus_index % len(controls)])
            elif self.screen == "touchpad":
                try:
                    self.get_app_mouse().click("LEFT")
                    self.message = "Touchpad left click"
                except Exception as exc:
                    self.error = str(exc)
                    self.message = str(exc)
            else:
                if not self.busy:
                    self.run_selected_script()
            return "continue"

        if self.screen == "touchpad":
            return self.update_touchpad(touch_state)

        tap = touch_state["tap"]
        if not tap:
            return "continue"

        if self.screen == "main" and self.point_in_rect(tap, self.header_exit_rect):
            return "exit"
        if self.screen == "keyboard":
            controls = self.keyboard_controls()
            for index, control in enumerate(controls):
                if self.point_in_rect(tap, control["rect"]):
                    self.keyboard_focus_index = index
                    self.activate_keyboard_control(control)
                    return "continue"
            return "continue"

        if self.screen == "library":
            for rect, index, _script in self.file_rows():
                if self.point_in_rect(tap, rect):
                    self.select_script(index)
                    self.close_library("Selected {0}".format(self.selected_script()["title"]))
                    return "continue"
            return "continue"

        if self.point_in_rect(tap, self.mouse_button):
            self.open_touchpad()
            return "continue"

        if self.point_in_rect(tap, self.keyboard_button):
            self.open_keyboard()
            return "continue"

        if self.point_in_rect(tap, self.list_button):
            self.open_library()
            return "continue"

        if self.point_in_rect(tap, self.main_panel):
            self.open_library()
            return "continue"

        if self.point_in_rect(tap, self.prev_button):
            self.move_selection(-1)
            return "continue"

        if self.point_in_rect(tap, self.next_button):
            self.move_selection(1)
            return "continue"

        if self.point_in_rect(tap, self.run_button):
            if not self.busy:
                self.run_selected_script()
            return "continue"

        return "continue"

    def update_touchpad(self, touch_state):
        tap = touch_state["tap"]
        touch = touch_state.get("touch")

        if tap and self.point_in_rect(tap, self.touchpad_left_button):
            try:
                self.get_app_mouse().click("LEFT")
                self.message = "Touchpad left click"
                self.error = None
            except Exception as exc:
                self.error = str(exc)
                self.message = str(exc)
            return "continue"

        if tap and self.point_in_rect(tap, self.touchpad_drag_button):
            try:
                mouse = self.get_app_mouse()
                if self.touchpad_drag_active:
                    mouse.button_up("LEFT")
                    self.touchpad_drag_active = False
                    self.message = "Drag released"
                else:
                    mouse.button_down("LEFT")
                    self.touchpad_drag_active = True
                    self.message = "Drag active"
                self.error = None
            except Exception as exc:
                self.error = str(exc)
                self.message = str(exc)
            return "continue"

        if tap and self.point_in_rect(tap, self.touchpad_mid_button):
            try:
                self.get_app_mouse().click("MIDDLE")
                self.message = "Middle click"
                self.error = None
            except Exception as exc:
                self.error = str(exc)
                self.message = str(exc)
            return "continue"

        if tap and self.point_in_rect(tap, self.touchpad_right_button):
            try:
                self.get_app_mouse().click("RIGHT")
                self.message = "Touchpad right click"
                self.error = None
            except Exception as exc:
                self.error = str(exc)
                self.message = str(exc)
            return "continue"

        if tap and self.point_in_rect(tap, self.touchpad_menu_button):
            self.close_touchpad("Back to launcher")
            return "continue"

        if touch_state.get("down") and touch and self.point_in_rect(touch, self.touchpad_scroll_panel):
            if self.touchpad_scroll_last_y is not None:
                delta_y = touch[1] - self.touchpad_scroll_last_y
                if abs(delta_y) >= TOUCHPAD_DEADZONE:
                    self.touchpad_scroll_accum += (-delta_y * 0.22)
                    wheel_delta = int(self.touchpad_scroll_accum)
                    self.touchpad_scroll_accum -= wheel_delta
                    if wheel_delta != 0:
                        try:
                            self.get_app_mouse().wheel(wheel_delta)
                            self.error = None
                            self.touchpad_moved = True
                        except Exception as exc:
                            self.error = str(exc)
                            self.message = str(exc)
            self.touchpad_scroll_last_y = touch[1]
            self.touchpad_last_point = None
            return "continue"

        if touch_state.get("down") and touch and self.point_in_rect(touch, self.touchpad_move_panel):
            if self.touchpad_last_point is not None:
                delta_x = touch[0] - self.touchpad_last_point[0]
                delta_y = touch[1] - self.touchpad_last_point[1]
                if abs(delta_x) >= TOUCHPAD_DEADZONE or abs(delta_y) >= TOUCHPAD_DEADZONE:
                    target_x = delta_x * TOUCHPAD_GAIN
                    target_y = delta_y * TOUCHPAD_GAIN
                    self.touchpad_velocity_x = (
                        (self.touchpad_velocity_x * TOUCHPAD_SMOOTHING)
                        + (target_x * (1.0 - TOUCHPAD_SMOOTHING))
                    )
                    self.touchpad_velocity_y = (
                        (self.touchpad_velocity_y * TOUCHPAD_SMOOTHING)
                        + (target_y * (1.0 - TOUCHPAD_SMOOTHING))
                    )
                    self.touchpad_accum_x += self.touchpad_velocity_x
                    self.touchpad_accum_y += self.touchpad_velocity_y
                    scaled_x = int(self.touchpad_accum_x)
                    scaled_y = int(self.touchpad_accum_y)
                    self.touchpad_accum_x -= scaled_x
                    self.touchpad_accum_y -= scaled_y
                    if scaled_x != 0 or scaled_y != 0:
                        try:
                            self.get_app_mouse().move(scaled_x, scaled_y)
                            self.error = None
                            self.touchpad_moved = True
                        except Exception as exc:
                            self.error = str(exc)
                            self.message = str(exc)
                else:
                    self.touchpad_velocity_x *= TOUCHPAD_SMOOTHING
                    self.touchpad_velocity_y *= TOUCHPAD_SMOOTHING
            self.touchpad_last_point = touch
            self.touchpad_scroll_last_y = None
            return "continue"

        if touch_state.get("released"):
            released_at = touch_state.get("released_at")
            if (
                released_at
                and self.point_in_rect(released_at, self.touchpad_move_panel)
                and not self.touchpad_moved
                and not self.touchpad_drag_active
            ):
                try:
                    self.get_app_mouse().click("LEFT")
                    self.message = "Touchpad tap click"
                    self.error = None
                except Exception as exc:
                    self.error = str(exc)
                    self.message = str(exc)
            self.touchpad_last_point = None
            self.touchpad_moved = False
            self.touchpad_accum_x = 0.0
            self.touchpad_accum_y = 0.0
            self.touchpad_velocity_x = 0.0
            self.touchpad_velocity_y = 0.0
            self.touchpad_scroll_last_y = None
            self.touchpad_scroll_accum = 0.0
            return "continue"

        if not touch_state.get("down"):
            self.touchpad_last_point = None
            self.touchpad_accum_x = 0.0
            self.touchpad_accum_y = 0.0
            self.touchpad_velocity_x = 0.0
            self.touchpad_velocity_y = 0.0
            self.touchpad_scroll_last_y = None
            self.touchpad_scroll_accum = 0.0

        return "continue"

    def draw_button(self, draw, rect, label, fill, focused=False, disabled=False, font=None):
        button_fill = PANEL_ALT if disabled else fill
        text_fill = MUTED if disabled else BACKGROUND
        font = font or self.font_medium
        draw.rounded_rectangle(rect, radius=12, fill=button_fill)
        if focused:
            draw.rounded_rectangle(rect, radius=12, outline=TEXT, width=2)
        box = draw.textbbox((0, 0), label, font=font)
        width = box[2] - box[0]
        height = box[3] - box[1]
        x = rect[0] + ((rect[2] - rect[0] - width) // 2)
        y = rect[1] + ((rect[3] - rect[1] - height) // 2)
        draw.text((x, y), label, fill=text_fill, font=font)

    def draw_header(self, draw, title=None, subtitle=None, menu_mode=False):
        draw.rounded_rectangle((8, 8, 312, 42), radius=12, fill=PANEL)
        button_fill = ACCENT if menu_mode else ERROR
        draw.rounded_rectangle(self.header_exit_rect, radius=8, fill=button_fill)
        if menu_mode:
            draw.polygon([(18, 26), (24, 18), (30, 26)], fill=TEXT)
            draw.rectangle((19, 26, 29, 31), outline=TEXT, width=2)
        else:
            draw.line((17, 17, 31, 31), fill=TEXT, width=3)
            draw.line((31, 17, 17, 31), fill=TEXT, width=3)
        if title:
            draw.text((50, 12), title, fill=TEXT, font=self.font_large)
        if subtitle:
            draw.text((170, 14), clip_to_width(draw, subtitle, self.font_small, 66), fill=MUTED, font=self.font_small)

    def draw_main_screen(self, canvas, draw):
        script = self.selected_script()
        draw.rounded_rectangle(self.main_panel, radius=16, fill=PANEL_ALT)
        draw.rounded_rectangle((8, 112, 312, 164), radius=16, fill=PANEL)
        self.draw_header(draw)
        self.draw_button(draw, self.keyboard_button, "KEYBOARD", WARNING, disabled=self.busy, font=self.font_small)
        self.draw_button(draw, self.mouse_button, "MOUSE", WARNING, disabled=self.busy, font=self.font_small)
        self.draw_button(draw, self.list_button, "LIST", WARNING, disabled=self.busy, font=self.font_medium)

        if self.busy:
            status_color = WARNING
            status_text = "INJECTING"
        elif self.error:
            status_color = ERROR
            status_text = "ERROR"
        elif script:
            status_color = SUCCESS
            status_text = "READY"
        else:
            status_color = ERROR
            status_text = "NO SCRIPTS"

        draw.text((18, 54), status_text, fill=status_color, font=self.font_small)
        draw.text((238, 54), "{0}/{1}".format(self.selected_index + 1 if script else 0, len(self.scripts)), fill=MUTED, font=self.font_small)

        if script:
            draw.text((18, 66), clip_to_width(draw, script["title"], self.font_xlarge, 246), fill=TEXT, font=self.font_xlarge)
            meta_text = "{0} | {1} cmds".format(script["kind"], script["command_count"])
            draw.text((18, 88), clip_to_width(draw, meta_text, self.font_small, 214), fill=ACCENT, font=self.font_small)
            desc = script["description"] or script["preview"]
            draw.text((18, 98), clip_to_width(draw, desc, self.font_small, 276), fill=MUTED, font=self.font_small)
        else:
            draw.text((18, 74), "Add .duck, .bat, .ps1 or .exe", fill=TEXT, font=self.font_medium)
            draw.text((18, 92), "to kvm-pilot/scripts.", fill=MUTED, font=self.font_small)

        nav_disabled = self.busy or len(self.scripts) <= 1
        self.draw_button(draw, self.prev_button, "<", WARNING, disabled=nav_disabled, font=self.font_large)

        if self.busy:
            run_label = "WORKING"
        elif time.time() <= self.confirm_until:
            run_label = "CONFIRM"
        else:
            run_label = "RUN"
        self.draw_button(
            draw,
            self.run_button,
            run_label,
            ACCENT,
            focused=time.time() <= self.confirm_until,
            disabled=self.busy or not script,
            font=self.font_large,
        )

        self.draw_button(draw, self.next_button, ">", WARNING, disabled=nav_disabled, font=self.font_large)

    def draw_library_screen(self, canvas, draw):
        draw.rounded_rectangle(self.library_panel, radius=16, fill=PANEL_ALT)

        page_text = "{0}/{1}".format(self.selected_index + 1 if self.scripts else 0, len(self.scripts))
        draw.text((258, 16), page_text, fill=MUTED, font=self.font_small)

        if not self.scripts:
            draw.text((52, 74), "No scripts in kvm-pilot/scripts", fill=MUTED, font=self.font_medium)
            return

        rows = self.file_rows()
        for rect, index, script in rows:
            selected = index == self.selected_index
            fill = PANEL if selected else PANEL_ALT
            outline = TEXT if selected else None
            draw.rounded_rectangle(rect, radius=10, fill=fill, outline=outline, width=1 if outline else 0)
            title_fill = ACCENT if selected else TEXT
            draw.text((rect[0] + 8, rect[1] + 3), clip_to_width(draw, script["title"], self.font_medium, 180), fill=title_fill, font=self.font_medium)
            type_text = script["kind"]
            draw.text((rect[2] - 74, rect[1] + 5), clip_to_width(draw, type_text, self.font_small, 66), fill=MUTED, font=self.font_small)

    def draw_keyboard_screen(self, canvas, draw):
        draw.rounded_rectangle(self.keyboard_panel, radius=16, fill=PANEL_ALT)
        controls = self.keyboard_controls()
        focused_rect = None
        if controls:
            focused_rect = controls[self.keyboard_focus_index % len(controls)]["rect"]

        header_outline = TEXT if focused_rect == self.header_exit_rect else None
        draw.rounded_rectangle(self.header_exit_rect, radius=8, fill=ACCENT, outline=header_outline, width=1 if header_outline else 0)
        draw.polygon([(18, 26), (24, 18), (30, 26)], fill=TEXT)
        draw.rectangle((19, 26, 29, 31), outline=TEXT, width=2)
        for spec in self.keyboard_button_specs():
            label = spec["label"]
            rect = spec["rect"]
            if spec["type"] == "shift":
                fill = ACCENT if self.keyboard_shift else WARNING
                focused = self.keyboard_shift
            elif spec["type"] == "modifier":
                fill = ACCENT if self.keyboard_modifier_mask & spec["modifier_bit"] else WARNING
                focused = bool(self.keyboard_modifier_mask & spec["modifier_bit"])
            elif self.keyboard_layout_index in (0, 1) and (label.startswith("F") or label in ("DEL", "ESC", "UP", "LEFT", "DOWN", "RIGHT", "PGUP", "PGDN", "HOME", "END", "INS")):
                fill = ACCENT
                focused = False
            elif label in ("BKSP", "ENT", "TAB"):
                fill = WARNING
                focused = False
            else:
                fill = WARNING
                focused = False
            focused = focused or (rect == focused_rect)
            font = self.font_small if len(label) > 4 else self.font_medium
            self.draw_button(draw, rect, label, fill, focused=focused, font=font)

        self.draw_button(draw, self.keyboard_prev_button, "<", WARNING, focused=self.keyboard_prev_button == focused_rect, font=self.font_large)
        self.draw_button(draw, self.keyboard_next_button, ">", WARNING, focused=self.keyboard_next_button == focused_rect, font=self.font_large)
        self.draw_button(draw, self.keyboard_layout_label_rect, "SPACE", ACCENT, focused=self.keyboard_layout_label_rect == focused_rect, font=self.font_medium)
        draw.text(
            (self.keyboard_layout_label_rect[0] + 8, self.keyboard_layout_label_rect[1] + 3),
            self.keyboard_layout_badge(),
            fill=TEXT,
            font=self.font_small,
        )

    def draw_touchpad_screen(self, canvas, draw):
        draw.rounded_rectangle((8, 8, 312, 164), radius=16, fill=PANEL_ALT)
        draw.rounded_rectangle(self.touchpad_panel, radius=18, fill=PANEL)
        draw.rounded_rectangle(self.touchpad_panel, radius=18, outline=(64, 78, 96), width=2)
        draw.rounded_rectangle(self.touchpad_scroll_panel, radius=14, fill=(30, 40, 52))
        draw.line((291, 22, 291, 118), fill=(92, 108, 126), width=3)
        draw.polygon([(291, 18), (285, 28), (297, 28)], fill=(140, 152, 168))
        draw.polygon([(291, 122), (285, 112), (297, 112)], fill=(140, 152, 168))

        if self.touchpad_drag_active:
            draw.text((18, 116), "DRAG ON", fill=WARNING, font=self.font_medium)
        elif self.error:
            draw.text((18, 116), clip_to_width(draw, self.error, self.font_small, 248), fill=ERROR, font=self.font_small)
        else:
            draw.text((18, 116), "Right strip scrolls", fill=MUTED, font=self.font_small)

        self.draw_button(draw, self.touchpad_left_button, "LEFT", ACCENT, font=self.font_medium)
        self.draw_button(
            draw,
            self.touchpad_drag_button,
            "DRAG",
            WARNING if self.touchpad_drag_active else PANEL,
            focused=self.touchpad_drag_active,
            font=self.font_medium,
        )
        self.draw_button(draw, self.touchpad_mid_button, "MID", ACCENT, font=self.font_small)
        self.draw_button(draw, self.touchpad_right_button, "RIGHT", WARNING, font=self.font_medium)
        self.draw_button(draw, self.touchpad_menu_button, "MENU", WARNING, font=self.font_small)

    def render(self):
        canvas = Image.new("RGB", (LOGICAL_WIDTH, LOGICAL_HEIGHT), BACKGROUND)
        draw = ImageDraw.Draw(canvas)
        if self.screen == "library":
            self.draw_library_screen(canvas, draw)
        elif self.screen == "keyboard":
            self.draw_keyboard_screen(canvas, draw)
        elif self.screen == "touchpad":
            self.draw_touchpad_screen(canvas, draw)
        else:
            self.draw_main_screen(canvas, draw)
        return canvas

    def close(self):
        self.release_touchpad_drag()
        self.close_app_mouse()
        self.close_app_keyboard()
        self.close_app_consumer()


def main():
    finder = InputDeviceFinder()
    touch_path = finder.find_touch_device()
    if not touch_path:
        print("Touch device not found.")
        sys.exit(1)

    print("Using touch device:", touch_path)
    rotate_path = finder.find_device_by_name("rotary@0")
    key_path = finder.find_device_by_name("gpio_keys")

    display = RGB565Display()
    touch = TouchReader(touch_path)
    knob = KnobReader(rotate_path, key_path)
    app = KVMPilotApp()

    try:
        while True:
            state = touch.poll()
            knob_state = knob.poll()
            state["knob"] = knob_state
            action = app.update(state)
            display.show_image(app.render())
            if action == "exit":
                break
            if app.screen == "touchpad":
                time.sleep(1.0 / TOUCHPAD_ACTIVE_FPS)
            else:
                time.sleep(1.0 / TOUCHPAD_IDLE_FPS)
    finally:
        app.close()
        knob.close()
        touch.close()
        display.close()


if __name__ == "__main__":
    main()
