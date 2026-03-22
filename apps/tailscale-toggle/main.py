import importlib
import json
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


class TailscaleBackend:
    def run(self, args, timeout=20, check=True):
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

    def command_exists(self, command):
        return shutil.which(command) is not None

    def is_service_active(self, service_name):
        if not self.command_exists("systemctl"):
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
        if not self.command_exists("pgrep"):
            return False
        process = subprocess.run(
            ["pgrep", "-f", process_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
            check=False,
        )
        return process.returncode == 0

    def detect(self):
        hostname = self.get_hostname()
        installed = self.command_exists("tailscale")
        service_active = self.is_service_active("tailscaled.service") or self.is_process_running("tailscaled")

        data = {
            "hostname": hostname,
            "installed": installed,
            "service_active": service_active,
            "backend_state": "Not installed" if not installed else ("Stopped" if not service_active else "Starting"),
            "connected": False,
            "ip": "-",
            "device": "-",
            "user": "-",
            "version": "-",
        }

        if not installed:
            return data

        try:
            data["version"] = self.run(["tailscale", "version"], timeout=10, check=False).splitlines()[0].strip() or "-"
        except Exception:
            pass

        try:
            raw = json.loads(self.run(["tailscale", "status", "--json"], timeout=12))
        except Exception:
            return data

        backend_state = str(raw.get("BackendState") or "Unknown")
        self_data = raw.get("Self") or {}
        ips = self_data.get("TailscaleIPs") or []
        user = self_data.get("UserLoginName") or self_data.get("LoginName") or "-"
        device = self_data.get("DNSName") or self_data.get("HostName") or "-"
        connected = backend_state.lower() == "running" and bool(ips)

        data.update(
            {
                "backend_state": backend_state,
                "connected": connected,
                "ip": ips[0] if ips else "-",
                "device": device,
                "user": user,
            }
        )
        return data

    def enable(self):
        if not self.command_exists("tailscale"):
            raise RuntimeError("tailscale is not installed")

        if self.command_exists("systemctl"):
            subprocess.run(
                ["systemctl", "start", "tailscaled.service"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )

        self.run(["tailscale", "up"], timeout=60, check=True)
        return "Tailscale enabled."

    def disable(self):
        if not self.command_exists("tailscale"):
            raise RuntimeError("tailscale is not installed")

        self.run(["tailscale", "down"], timeout=20, check=False)

        if self.command_exists("systemctl"):
            subprocess.run(
                ["systemctl", "stop", "tailscaled.service"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )

        return "Tailscale disabled."


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


class TailscaleToggleApp:
    def __init__(self):
        self.font_small = load_font(11)
        self.font_medium = load_font(14)
        self.font_large = load_font(18)
        self.font_xlarge = load_font(22)
        self.font_button = load_font(16)
        self.backend = TailscaleBackend()
        self.lock = threading.Lock()
        self.data = None
        self.busy = False
        self.message = "Use ON/OFF to control Tailscale."
        self.error = None
        self.last_checked = "-"
        self.focus_index = 1
        self.last_knob_move_at = 0.0
        self.next_refresh_at = 0.0
        self.buttons = {
            "on": (20, 118, 146, 154),
            "off": (174, 118, 300, 154),
        }
        self.refresh()

    def refresh(self):
        try:
            self.data = self.backend.detect()
            self.error = None
            self.last_checked = time.strftime("%H:%M:%S")
            if not self.busy:
                self.message = self.default_message(self.data)
        except Exception as exc:
            self.data = {
                "hostname": "-",
                "installed": False,
                "service_active": False,
                "backend_state": "Error",
                "connected": False,
                "ip": "-",
                "device": "-",
                "user": "-",
                "version": "-",
            }
            self.error = str(exc)
            self.message = str(exc)
            self.last_checked = time.strftime("%H:%M:%S")
        self.next_refresh_at = time.time() + 2.0

    def default_message(self, data):
        if not data.get("installed"):
            return "tailscale is not installed on this system."
        if data.get("connected"):
            return "Tailscale is connected."
        if data.get("service_active"):
            return "Service is running, but not connected."
        return "Tailscale is disabled."

    def point_in_rect(self, point, rect):
        x, y = point
        left, top, right, bottom = rect
        return left <= x <= right and top <= y <= bottom

    def start_action(self, callback, pending_message):
        with self.lock:
            if self.busy:
                return
            self.busy = True
            self.error = None
            self.message = pending_message
        ActionWorker(self, callback).start()

    def finish_action(self, message=None, error=None):
        self.refresh()
        with self.lock:
            self.busy = False
            if error:
                self.error = error
                self.message = error
            elif message:
                self.message = message

    def activate_button(self, name):
        data = self.data or {}
        if self.busy:
            return "continue"
        if name == "on":
            if data.get("connected"):
                self.error = None
                self.message = "Tailscale is already connected."
                return "continue"
            self.start_action(self.backend.enable, "Enabling Tailscale")
            return "continue"
        if name == "off":
            if not data.get("service_active") and not data.get("connected"):
                self.error = None
                self.message = "Tailscale is already disabled."
                return "continue"
            self.start_action(self.backend.disable, "Disabling Tailscale")
            return "continue"
        return "continue"

    def focus_items(self):
        return ["exit", "on", "off"]

    def move_focus(self, step):
        items = self.focus_items()
        self.focus_index = (self.focus_index + step) % len(items)

    def activate_focus(self):
        current = self.focus_items()[self.focus_index]
        if current == "exit":
            return "exit"
        return self.activate_button(current)

    def update(self, touch_state, knob_state=None):
        knob_state = knob_state or {"delta": 0, "press": False}

        if time.time() >= self.next_refresh_at and not self.busy:
            self.refresh()

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

        for name, rect in self.buttons.items():
            if self.point_in_rect(tap, rect):
                self.focus_index = self.focus_items().index(name)
                return self.activate_button(name)

        return "continue"

    def draw_button(self, draw, rect, title, style, focused=False):
        if style == "success":
            fill = SUCCESS
            title_fill = BACKGROUND
        elif style == "warning":
            fill = WARNING
            title_fill = BACKGROUND
        else:
            fill = PANEL
            title_fill = TEXT

        draw.rounded_rectangle(rect, radius=16, fill=fill)
        if focused:
            draw.rounded_rectangle(rect, radius=16, outline=TEXT, width=2)

        title_box = draw.textbbox((0, 0), title, font=self.font_button)
        title_width = title_box[2] - title_box[0]
        title_height = title_box[3] - title_box[1]
        left, top, right, bottom = rect
        title_x = left + ((right - left - title_width) // 2)
        title_y = top + ((bottom - top - title_height) // 2) - 1
        draw.text((title_x, title_y), title, fill=title_fill, font=self.font_button)

    def render(self):
        with self.lock:
            data = dict(self.data) if self.data else {}
            busy = self.busy
            message = self.message
            error = self.error

        canvas = Image.new("RGB", (LOGICAL_WIDTH, LOGICAL_HEIGHT), BACKGROUND)
        draw = ImageDraw.Draw(canvas)

        draw.rounded_rectangle((8, 8, 312, 42), radius=12, fill=PANEL)
        draw.rounded_rectangle((8, 48, 312, 108), radius=16, fill=PANEL_ALT)
        draw.rounded_rectangle((8, 112, 312, 166), radius=16, fill=PANEL)

        exit_rect = (10, 10, 10 + EXIT_SIZE, 10 + EXIT_SIZE)
        draw.rounded_rectangle(exit_rect, radius=8, fill=ERROR)
        if self.focus_items()[self.focus_index] == "exit":
            draw.rounded_rectangle(exit_rect, radius=8, outline=TEXT, width=2)
        draw.line((17, 17, 31, 31), fill=TEXT, width=3)
        draw.line((31, 17, 17, 31), fill=TEXT, width=3)

        draw.text((50, 12), "TAILSCALE", fill=TEXT, font=self.font_large)

        state_text = str(data.get("backend_state", "-"))
        connected = bool(data.get("connected"))
        if connected:
            status_color = SUCCESS
            status_label = "CONNECTED"
        elif busy:
            status_color = WARNING
            status_label = "WORKING"
        elif data.get("service_active"):
            status_color = ACCENT
            status_label = "SERVICE ON"
        elif data.get("installed"):
            status_color = MUTED
            status_label = "OFF"
        else:
            status_color = ERROR
            status_label = "NOT INSTALLED"

        draw.text((18, 54), status_label, fill=status_color, font=self.font_medium)
        draw.text((18, 73), clip_to_width(draw, "State: {0}".format(state_text), self.font_small, 286), fill=TEXT, font=self.font_small)
        draw.text((18, 86), clip_to_width(draw, "IP: {0}".format(data.get("ip", "-")), self.font_small, 286), fill=ACCENT if connected else MUTED, font=self.font_small)
        draw.text((18, 99), clip_to_width(draw, "Host: {0}".format(data.get("hostname", "-")), self.font_small, 286), fill=MUTED, font=self.font_small)

        on_style = "success" if connected else "neutral"
        off_style = "warning" if not connected else "neutral"
        self.draw_button(draw, self.buttons["on"], "ON", on_style, focused=self.focus_items()[self.focus_index] == "on")
        self.draw_button(draw, self.buttons["off"], "OFF", off_style, focused=self.focus_items()[self.focus_index] == "off")

        footer_text = message if not error else error
        footer_color = ERROR if error else MUTED
        footer = clip_to_width(draw, footer_text, self.font_small, 286)
        draw.text((18, 152), footer, fill=footer_color, font=self.font_small)

        return canvas


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
    app = TailscaleToggleApp()

    try:
        while True:
            touch_state = touch.poll()
            knob_state = knob.poll()
            action = app.update(touch_state, knob_state)
            display.show_image(app.render())
            if action == "exit":
                break
            time.sleep(1.0 / 10.0)
    finally:
        knob.close()
        touch.close()
        display.close()


if __name__ == "__main__":
    main()
