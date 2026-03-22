import importlib
import mmap
import os
import socket
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
    def import_package(pip_name, import_name=None, install_if_missing=False):
        import_name = import_name or pip_name
        try:
            return importlib.import_module(import_name)
        except ImportError:
            if not install_if_missing:
                return None

            subprocess = importlib.import_module("subprocess")
            print("Installing missing package:", pip_name)
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])
            return importlib.import_module(import_name)


evdev = AutoImport.import_package("evdev", install_if_missing=True)
if evdev is None:
    raise ImportError("evdev is required for speedtest app")

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


def decode_bytes(value):
    if not value:
        return "-"
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    size = float(value)
    unit = 0
    while size >= 1024.0 and unit < len(units) - 1:
        size /= 1024.0
        unit += 1
    return "{0:.2f} {1}".format(size, units[unit])


def decode_bits_megabits(value):
    if value is None:
        return "-"
    return "{0:.2f} Mbps".format(float(value) / 1_000_000.0)


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
        preferred_names = [
            "hyn_ts",
            "goodix_ts",
            "fts_ts",
            "gt9xxnew_ts",
        ]

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
        self.fb_mmap = mmap.mmap(
            self.fb_fd,
            self.fb_size,
            mmap.MAP_SHARED,
            mmap.PROT_WRITE,
        )
        self.fb_array = np.frombuffer(self.fb_mmap, dtype=np.uint16).reshape(
            (PHYSICAL_HEIGHT, PHYSICAL_WIDTH)
        )

    def clear_screen(self, color=0x0000):
        self.fb_array.fill(color)

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
        released = False
        rlist, _, _ = select([self.device], [], [], 0)
        if not rlist:
            return {"down": self.touch_down, "touch": self.last_touch, "tap": tapped, "released": released}

        for event in self.device.read():
            if event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH:
                if event.value == 1:
                    self.touch_down = True
                elif event.value == 0:
                    self.touch_down = False
                    self.last_touch = None
                    released = True
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

        return {"down": self.touch_down, "touch": self.last_touch, "tap": tapped, "released": released}

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


class SpeedtestWorker(threading.Thread):
    def __init__(self, state):
        super().__init__(daemon=True)
        self.state = state

    def set_status(self, message):
        with self.state["lock"]:
            self.state["status"] = message

    def run(self):
        try:
            self.set_status("Loading server list")
            speedtest_module = AutoImport.import_package("speedtest-cli", "speedtest")
            if speedtest_module is None:
                raise RuntimeError("speedtest-cli is not installed")

            tester = speedtest_module.Speedtest(secure=True)

            self.set_status("Selecting best server")
            tester.get_servers([])
            best_server = tester.get_best_server()

            self.set_status("Testing download")
            download_bps = tester.download()

            self.set_status("Testing upload")
            upload_bps = tester.upload(pre_allocate=False)

            raw = tester.results.dict()
            result = {
                "ping_ms": raw.get("ping"),
                "download_bps": download_bps,
                "upload_bps": upload_bps,
                "server_name": best_server.get("name", "-"),
                "server_sponsor": best_server.get("sponsor", "-"),
                "server_country": best_server.get("country", "-"),
                "distance_km": best_server.get("d", 0.0),
                "external_ip": raw.get("client", {}).get("ip", "-"),
                "isp": raw.get("client", {}).get("isp", "-"),
                "timestamp": time.strftime("%H:%M:%S"),
            }
            with self.state["lock"]:
                self.state["result"] = result
                self.state["status"] = "Test completed"
                self.state["running"] = False
        except Exception as exc:
            with self.state["lock"]:
                self.state["error"] = str(exc)
                self.state["status"] = "Test failed"
                self.state["running"] = False


class SpeedtestApp:
    def __init__(self):
        self.font_tiny = load_font(11)
        self.font_small = load_font(13)
        self.font_medium = load_font(16)
        self.font_large = load_font(22)
        self.spinner_frames = ["|", "/", "-", "\\"]
        self.spinner_index = 0
        self.last_knob_move_at = 0.0
        self.focus_index = 1
        self.run_button = (12, 148, 308, 166)
        self.state = {
            "lock": threading.Lock(),
            "running": False,
            "status": "Tap Run to start a test",
            "error": None,
            "result": None,
        }

    def start_test(self):
        with self.state["lock"]:
            if self.state["running"]:
                return
            self.state["running"] = True
            self.state["status"] = "Preparing speed test"
            self.state["error"] = None
            self.state["result"] = None

        worker = SpeedtestWorker(self.state)
        worker.start()

    def get_host_name(self):
        try:
            return socket.gethostname()
        except OSError:
            return "nanokvm"

    def draw_card(self, draw, rect, label, value, value_color=TEXT, value_font=None):
        value_font = value_font or self.font_medium
        draw.rounded_rectangle(rect, radius=10, fill=PANEL)

        x1, y1, x2, y2 = rect
        draw.text((x1 + 8, y1 + 4), label, fill=MUTED, font=self.font_tiny)
        label_box = draw.textbbox((0, 0), label, font=self.font_tiny)
        label_height = label_box[3] - label_box[1]
        value_box = draw.textbbox((0, 0), value, font=value_font)
        value_height = value_box[3] - value_box[1]
        value_y = y1 + 6 + label_height
        max_value_y = y2 - value_height - 4
        value_y = min(value_y, max_value_y)
        draw.text((x1 + 8, value_y), value, fill=value_color, font=value_font)

    def focus_items(self):
        return ["exit", "run"]

    def move_focus(self, step):
        items = self.focus_items()
        self.focus_index = (self.focus_index + step) % len(items)

    def activate_focus(self):
        current = self.focus_items()[self.focus_index]
        if current == "exit":
            return "exit"
        self.start_test()
        return "continue"

    def update(self, touch_state, knob_state=None):
        knob_state = knob_state or {"delta": 0, "press": False}

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

        x, y = tap
        if x < EXIT_SIZE + 10 and y < EXIT_SIZE + 10:
            self.focus_index = 0
            return "exit"

        if self.run_button[0] <= x <= self.run_button[2] and self.run_button[1] <= y <= self.run_button[3]:
            self.focus_index = 1
            self.start_test()

        return "continue"

    def render(self):
        with self.state["lock"]:
            running = self.state["running"]
            status = self.state["status"]
            error = self.state["error"]
            result = self.state["result"]

        if running:
            self.spinner_index = (self.spinner_index + 1) % len(self.spinner_frames)

        canvas = Image.new("RGB", (LOGICAL_WIDTH, LOGICAL_HEIGHT), BACKGROUND)
        draw = ImageDraw.Draw(canvas)

        draw.rounded_rectangle((8, 8, 312, 42), radius=12, fill=PANEL)
        draw.rounded_rectangle((8, 48, 312, 128), radius=16, fill=PANEL_ALT)
        draw.rounded_rectangle((12, 132, 308, 144), radius=8, fill=PANEL)

        exit_rect = (10, 10, 10 + EXIT_SIZE, 10 + EXIT_SIZE)
        draw.rounded_rectangle(exit_rect, radius=8, fill=ERROR)
        if self.focus_items()[self.focus_index] == "exit":
            draw.rounded_rectangle(exit_rect, radius=8, outline=TEXT, width=2)
        draw.line((17, 17, 31, 31), fill=TEXT, width=3)
        draw.line((31, 17, 17, 31), fill=TEXT, width=3)

        if running:
            header_color = WARNING
            header_text = "RUNNING {0}".format(self.spinner_frames[self.spinner_index])
            side_text = "active test"
        elif error:
            header_color = ERROR
            header_text = "FAILED"
            side_text = "needs retry"
        elif result:
            header_color = SUCCESS
            header_text = "COMPLETED"
            side_text = result["timestamp"]
        else:
            header_color = ACCENT
            header_text = "SPEEDTEST"
            side_text = ""

        draw.text((50, 11), header_text, fill=header_color if running or error or result else TEXT, font=self.font_large)
        if side_text:
            side_text = clip_to_width(draw, side_text, self.font_tiny, 96)
            side_box = draw.textbbox((0, 0), side_text, font=self.font_tiny)
            side_width = side_box[2] - side_box[0]
            draw.text((304 - side_width, 15), side_text, fill=MUTED, font=self.font_tiny)

        if error:
            draw.text((18, 58), "Speed test could not finish.", fill=TEXT, font=self.font_medium)
            draw.text((18, 80), clip_to_width(draw, error, self.font_small, 286), fill=ERROR, font=self.font_small)
        elif result:
            ping_box = (16, 52, 104, 92)
            down_box = (112, 52, 304, 92)
            upload_box = (16, 98, 152, 128)
            server_box = (160, 98, 304, 128)
            footer_box = (16, 132, 304, 144)

            self.draw_card(
                draw,
                ping_box,
                "Ping",
                "{0:.1f} ms".format(float(result["ping_ms"] or 0.0)),
                value_color=TEXT,
                value_font=self.font_medium,
            )
            self.draw_card(
                draw,
                down_box,
                "Download",
                clip_to_width(draw, decode_bits_megabits(result["download_bps"]), self.font_medium, 176),
                value_color=SUCCESS,
                value_font=self.font_medium,
            )
            self.draw_card(
                draw,
                upload_box,
                "Upload",
                clip_to_width(draw, decode_bits_megabits(result["upload_bps"]), self.font_small, 118),
                value_color=ACCENT,
                value_font=self.font_small,
            )

            draw.rounded_rectangle(server_box, radius=10, fill=PANEL)
            draw.text((168, 102), "Server", fill=MUTED, font=self.font_tiny)
            server_name = clip_to_width(draw, result["server_sponsor"], self.font_small, 128)
            draw.text((168, 114), server_name, fill=TEXT, font=self.font_small)

            draw.rounded_rectangle(footer_box, radius=8, fill=PANEL)
            isp_text = clip_to_width(
                draw,
                "ISP {0}  {1} km".format(result["isp"], int(float(result["distance_km"]))),
                self.font_tiny,
                270,
            )
            isp_box = draw.textbbox((0, 0), isp_text, font=self.font_tiny)
            isp_width = isp_box[2] - isp_box[0]
            draw.text((160 - (isp_width // 2), 134), isp_text, fill=MUTED, font=self.font_tiny)
        else:
            title = "Testing your connection" if running else "Network speed snapshot"
            line1 = "Checks ping, download and upload."
            line2 = "Best server is selected automatically."
            draw.text((18, 60), title, fill=TEXT, font=self.font_medium)
            draw.text((18, 81), line1, fill=MUTED, font=self.font_small)
            draw.text((18, 94), line2, fill=MUTED, font=self.font_small)
            footer = clip_to_width(draw, status, self.font_tiny, 286)
            footer_box = draw.textbbox((0, 0), footer, font=self.font_tiny)
            footer_width = footer_box[2] - footer_box[0]
            draw.text(((LOGICAL_WIDTH - footer_width) // 2, 134), footer, fill=MUTED if not error else ERROR, font=self.font_tiny)

        button_fill = WARNING if running else ACCENT
        button_text = "RUNNING..." if running else "RUN SPEED TEST"
        draw.rounded_rectangle(self.run_button, radius=14, fill=button_fill)
        if self.focus_items()[self.focus_index] == "run":
            draw.rounded_rectangle(self.run_button, radius=14, outline=TEXT, width=2)
        text_box = draw.textbbox((0, 0), button_text, font=self.font_medium)
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
        text_x = (LOGICAL_WIDTH - text_width) // 2
        text_y = 157 - (text_height // 2)
        draw.text((text_x, text_y), button_text, fill=(10, 15, 22), font=self.font_medium)

        return canvas


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
    app = SpeedtestApp()

    try:
        while True:
            state = touch.poll()
            knob_state = knob.poll()
            action = app.update(state, knob_state)
            display.show_image(app.render())
            if action == "exit":
                break
            time.sleep(1.0 / 20.0)
    finally:
        knob.close()
        touch.close()
        display.close()


if __name__ == "__main__":
    main()
