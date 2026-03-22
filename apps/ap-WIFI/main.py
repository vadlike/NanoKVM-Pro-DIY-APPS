import importlib
import json
import mmap
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from select import select

import numpy as np
from PIL import Image, ImageDraw, ImageFont


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
WIFI_SCRIPT_PATH = "/kvmcomm/scripts/wifi.sh"
APP_RUNTIME_DIR = "/userapp/ap-WIFI"
LOG_PATHS = [
    os.path.join(APP_RUNTIME_DIR, "error.log"),
    os.path.join(SCRIPT_DIR, "error.log"),
]

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


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}, "config.json not found: {0}".format(CONFIG_PATH)

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        return {}, "Failed to read config.json: {0}".format(exc)

    if not isinstance(data, dict):
        return {}, "config.json must contain a JSON object."

    return data, None


CONFIG, CONFIG_ERROR = load_config()
TARGET_SSID = str(CONFIG.get("TARGET_SSID") or CONFIG.get("target_ssid") or "").strip()
TARGET_PASSWORD = str(CONFIG.get("TARGET_PASSWORD") or CONFIG.get("target_password") or "").strip()


def require_target_config():
    if CONFIG_ERROR:
        raise RuntimeError(CONFIG_ERROR)
    if not TARGET_SSID:
        raise RuntimeError("TARGET_SSID is missing in config.json")
    if not TARGET_PASSWORD:
        raise RuntimeError("TARGET_PASSWORD is missing in config.json")


def redact_value(value):
    text = str(value)
    if TARGET_PASSWORD:
        text = text.replace(TARGET_PASSWORD, "<redacted>")
    return text


def write_log(level, message, details=None):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = ["[{0}] {1}: {2}".format(timestamp, level, redact_value(message))]
    if details:
        if isinstance(details, (list, tuple)):
            for item in details:
                lines.append("  {0}".format(redact_value(item)))
        else:
            lines.append("  {0}".format(redact_value(details)))
    for path in LOG_PATHS:
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            return
        except OSError:
            continue


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


def clip(value, limit):
    if value is None:
        return "-"
    value = str(value)
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3] + "..."


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


class CommandBackend:
    name = "unknown"

    def run(self, args, timeout=20):
        safe_args = [redact_value(arg) for arg in args]
        process = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        if process.returncode != 0:
            message = process.stderr.strip() or process.stdout.strip() or "Command failed"
            write_log(
                "ERROR",
                "Command failed",
                [
                    "backend={0}".format(self.name),
                    "args={0}".format(" ".join(safe_args)),
                    "returncode={0}".format(process.returncode),
                    "stdout={0}".format(process.stdout.strip() or "-"),
                    "stderr={0}".format(process.stderr.strip() or "-"),
                ],
            )
            raise RuntimeError(message)
        return process.stdout.strip()

    def get_hostname(self):
        try:
            return socket.gethostname()
        except OSError:
            return "nanokvm"

    def get_ip_for_interface(self, iface):
        if not iface:
            return "-"
        try:
            output = self.run(["ip", "-4", "addr", "show", "dev", iface], timeout=10)
        except Exception:
            return "-"
        match = re.search(r"inet\s+([0-9.]+)", output)
        return match.group(1) if match else "-"

    def detect_interface_from_sysfs(self):
        base = "/sys/class/net"
        if not os.path.isdir(base):
            return None
        candidates = []
        for name in os.listdir(base):
            lowered = name.lower()
            if lowered.startswith(("wl", "wlan", "wlp")):
                candidates.append(name)
        return sorted(candidates)[0] if candidates else None

    def refresh(self):
        raise NotImplementedError

    def connect_target(self):
        raise NotImplementedError

    def disconnect_target(self):
        raise NotImplementedError

    def get_current_ssid(self, iface):
        if shutil.which("iwgetid"):
            try:
                ssid = self.run(["iwgetid", iface, "-r"], timeout=10).strip()
                if ssid:
                    return ssid
            except Exception:
                pass

        if shutil.which("wpa_cli"):
            try:
                output = self.run(["wpa_cli", "-i", iface, "status"], timeout=10)
                for line in output.splitlines():
                    if line.startswith("ssid="):
                        return line.split("=", 1)[1].strip()
            except Exception:
                pass

        if shutil.which("iw"):
            try:
                output = self.run(["iw", "dev", iface, "link"], timeout=10)
                for line in output.splitlines():
                    line = line.strip()
                    if line.startswith("SSID:"):
                        return line.split(":", 1)[1].strip()
            except Exception:
                pass

        return "-"

    def get_signal_for_interface(self, iface):
        if not iface:
            return "-"

        if shutil.which("iw"):
            try:
                output = self.run(["iw", "dev", iface, "link"], timeout=10)
                for line in output.splitlines():
                    line = line.strip()
                    if line.startswith("signal:"):
                        return line.split(":", 1)[1].strip()
            except Exception:
                pass

        wireless_path = "/proc/net/wireless"
        if os.path.exists(wireless_path):
            try:
                with open(wireless_path, "r", encoding="utf-8") as handle:
                    for line in handle.readlines()[2:]:
                        if ":" not in line:
                            continue
                        name, payload = line.split(":", 1)
                        if name.strip() != iface:
                            continue
                        fields = payload.split()
                        if len(fields) >= 3:
                            return "{0} dBm".format(fields[2].rstrip("."))
            except OSError:
                pass

        return "-"

    def request_dhcp_lease(self, iface):
        if not iface:
            raise RuntimeError("Wi-Fi interface not found")

        candidates = []
        if shutil.which("udhcpc"):
            candidates.append((["udhcpc", "-i", iface, "-n", "-q"], 25))
        if shutil.which("dhclient"):
            candidates.append((["dhclient", "-v", iface], 30))
        if shutil.which("dhcpcd"):
            candidates.append((["dhcpcd", "-n", iface], 25))

        if not candidates:
            return "No DHCP client found"

        last_error = None
        for args, timeout in candidates:
            try:
                self.run(args, timeout=timeout)
                return "DHCP lease requested"
            except Exception as exc:
                last_error = str(exc)

        raise RuntimeError(last_error or "Failed to request DHCP lease")

    def wait_for_target(self, timeout_seconds=15):
        deadline = time.time() + timeout_seconds
        last_state = None
        while time.time() < deadline:
            state = self.refresh()
            last_state = state
            if state.get("connected"):
                return state
            time.sleep(1.0)
        return last_state

    def wait_for_ip(self, iface, timeout_seconds=20):
        deadline = time.time() + timeout_seconds
        last_ip = self.get_ip_for_interface(iface)
        while time.time() < deadline:
            current_ip = self.get_ip_for_interface(iface)
            last_ip = current_ip
            if current_ip and current_ip != "-":
                return current_ip
            time.sleep(1.0)
        return last_ip


class NmcliBackend(CommandBackend):
    name = "nmcli"

    def parse_multiline_blocks(self, text):
        blocks = []
        current = {}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                if current:
                    blocks.append(current)
                    current = {}
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            current[key.strip()] = value.strip()
        if current:
            blocks.append(current)
        return blocks

    def detect_interface(self):
        output = self.run(["nmcli", "-m", "multiline", "-f", "DEVICE,TYPE", "device", "status"])
        for block in self.parse_multiline_blocks(output):
            if block.get("TYPE") == "wifi":
                return block.get("DEVICE")
        return self.detect_interface_from_sysfs()

    def refresh(self):
        iface = self.detect_interface()
        if not iface:
            raise RuntimeError("Wi-Fi interface not found")
        output = self.run(["nmcli", "-m", "multiline", "-f", "GENERAL.CONNECTION,GENERAL.STATE", "device", "show", iface])
        block = self.parse_multiline_blocks(output)[0]
        ssid = block.get("GENERAL.CONNECTION", "-")
        if ssid == "--":
            ssid = "-"
        return {
            "backend": self.name,
            "hostname": self.get_hostname(),
            "iface": iface,
            "ssid": ssid,
            "ip": self.get_ip_for_interface(iface),
            "signal": self.get_signal_for_interface(iface),
            "state": block.get("GENERAL.STATE", "-"),
            "connected": ssid == TARGET_SSID and self.get_ip_for_interface(iface) != "-",
            "associated": ssid == TARGET_SSID,
        }

    def connect_target(self):
        require_target_config()
        iface = self.detect_interface()
        if not iface:
            raise RuntimeError("Wi-Fi interface not found")
        self.run(
            ["nmcli", "device", "wifi", "connect", TARGET_SSID, "password", TARGET_PASSWORD, "ifname", iface],
            timeout=45,
        )
        connected_state = self.wait_for_target()
        if connected_state and connected_state.get("connected"):
            return "Connected to {0}".format(TARGET_SSID)
        if connected_state and connected_state.get("associated"):
            return "Associated with AP, but no local IP was obtained."
        return "Connection command sent, but target SSID is not active yet."

    def disconnect_target(self):
        iface = self.detect_interface()
        if not iface:
            raise RuntimeError("Wi-Fi interface not found")
        try:
            self.run(["nmcli", "device", "disconnect", iface], timeout=25)
        except Exception:
            self.run(["nmcli", "connection", "down", TARGET_SSID], timeout=25)
        return "Disconnect requested"


class WpaCliBackend(CommandBackend):
    name = "wpa_cli"

    def detect_interface(self):
        iface = self.detect_interface_from_sysfs()
        if iface:
            return iface
        return "wlan0"

    def run_wpa(self, iface, args, timeout=20):
        if not iface:
            raise RuntimeError("Wi-Fi interface not found")
        return self.run(["wpa_cli", "-i", iface] + list(args), timeout=timeout)

    def parse_status(self, text):
        result = {}
        for line in text.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
        return result

    def get_or_create_network_id(self, iface):
        output = self.run_wpa(iface, ["list_networks"])
        for line in output.splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1] == TARGET_SSID:
                return parts[0]
        return self.run_wpa(iface, ["add_network"]).strip()

    def refresh(self):
        iface = self.detect_interface()
        if not iface:
            raise RuntimeError("Wi-Fi interface not found")
        status = self.parse_status(self.run_wpa(iface, ["status"]))
        ssid = status.get("ssid", "-")
        ip_address = self.get_ip_for_interface(iface)
        return {
            "backend": self.name,
            "hostname": self.get_hostname(),
            "iface": iface,
            "ssid": ssid,
            "ip": ip_address,
            "signal": self.get_signal_for_interface(iface),
            "state": status.get("wpa_state", "-"),
            "connected": ssid == TARGET_SSID and ip_address != "-",
            "associated": ssid == TARGET_SSID,
        }

    def connect_target(self):
        require_target_config()
        iface = self.detect_interface()
        if not iface:
            raise RuntimeError("Wi-Fi interface not found")
        network_id = self.get_or_create_network_id(iface)
        quoted_ssid = '"{0}"'.format(TARGET_SSID)
        quoted_password = '"{0}"'.format(TARGET_PASSWORD)
        self.run_wpa(iface, ["set_network", network_id, "ssid", quoted_ssid], timeout=20)
        self.run_wpa(iface, ["set_network", network_id, "psk", quoted_password], timeout=20)
        self.run_wpa(iface, ["set_network", network_id, "key_mgmt", "WPA-PSK"], timeout=20)
        self.run_wpa(iface, ["enable_network", network_id], timeout=20)
        self.run_wpa(iface, ["select_network", network_id], timeout=20)
        try:
            self.run_wpa(iface, ["save_config"], timeout=20)
        except Exception:
            pass
        self.run_wpa(iface, ["reconnect"], timeout=20)
        connected_state = self.wait_for_target()
        if connected_state and connected_state.get("associated") and not connected_state.get("connected"):
            self.request_dhcp_lease(iface)
            ip_address = self.wait_for_ip(iface)
            if ip_address and ip_address != "-":
                connected_state = self.refresh()
        if connected_state and connected_state.get("connected"):
            return "Connected to {0}".format(TARGET_SSID)
        if connected_state and connected_state.get("associated"):
            return "Associated with AP, but no local IP was obtained."
        return "Connect requested, waiting for association."

    def disconnect_target(self):
        iface = self.detect_interface()
        if not iface:
            raise RuntimeError("Wi-Fi interface not found")
        self.run_wpa(iface, ["disconnect"], timeout=20)
        return "Disconnect requested"


class FallbackBackend(CommandBackend):
    name = "readonly"

    def refresh(self):
        iface = self.detect_interface_from_sysfs()
        if not iface:
            raise RuntimeError("Wi-Fi interface not found")
        return {
            "backend": self.name,
            "hostname": self.get_hostname(),
            "iface": iface,
            "ssid": "-",
            "ip": self.get_ip_for_interface(iface),
            "signal": self.get_signal_for_interface(iface),
            "state": "Unsupported backend",
            "connected": False,
            "associated": False,
        }

    def connect_target(self):
        raise RuntimeError("Need wifi.sh, nmcli, or wpa_cli on the device to connect to Wi-Fi.")

    def disconnect_target(self):
        raise RuntimeError("Disconnect is not supported by the current backend.")


class WifiScriptBackend(CommandBackend):
    name = "wifi.sh"

    def detect_interface(self):
        iface = self.detect_interface_from_sysfs()
        return iface or "wlan0"

    def refresh(self):
        iface = self.detect_interface()
        if not iface:
            raise RuntimeError("Wi-Fi interface not found")

        ssid = self.get_current_ssid(iface)
        ip_address = self.get_ip_for_interface(iface)
        associated = ssid == TARGET_SSID
        return {
            "backend": self.name,
            "hostname": self.get_hostname(),
            "iface": iface,
            "ssid": ssid,
            "ip": ip_address,
            "signal": self.get_signal_for_interface(iface),
            "state": "Managed by wifi.sh",
            "connected": associated and ip_address != "-",
            "associated": associated,
        }

    def connect_target(self):
        require_target_config()
        if not os.path.exists(WIFI_SCRIPT_PATH):
            raise RuntimeError("wifi.sh script not found: {0}".format(WIFI_SCRIPT_PATH))

        self.run([WIFI_SCRIPT_PATH, "connect_start", TARGET_SSID, TARGET_PASSWORD], timeout=60)
        connected_state = self.wait_for_target(timeout_seconds=25)
        if connected_state and connected_state.get("connected"):
            return "Connected to {0}".format(TARGET_SSID)
        if connected_state and connected_state.get("associated"):
            return "Associated with AP, but no local IP was obtained."
        return "wifi.sh started, waiting for connection."

    def disconnect_target(self):
        if not os.path.exists(WIFI_SCRIPT_PATH):
            raise RuntimeError("wifi.sh script not found: {0}".format(WIFI_SCRIPT_PATH))
        self.run([WIFI_SCRIPT_PATH, "connect_stop"], timeout=40)
        return "Wi-Fi disconnect requested"


def make_backend():
    if os.path.exists(WIFI_SCRIPT_PATH):
        return WifiScriptBackend()
    if shutil.which("nmcli"):
        return NmcliBackend()
    if shutil.which("wpa_cli"):
        return WpaCliBackend()
    return FallbackBackend()


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
            write_log(
                "ERROR",
                "Action failed",
                [
                    "backend={0}".format(self.app.backend.name),
                    "message={0}".format(str(exc)),
                    traceback.format_exc().strip(),
                ],
            )
            self.app.finish_action(error=str(exc))


class TplinkConnectApp:
    def __init__(self):
        self.font_small = load_font(10)
        self.font_medium = load_font(14)
        self.font_large = load_font(18)
        self.font_xlarge = load_font(22)
        self.backend = make_backend()
        self.lock = threading.Lock()
        self.data = None
        self.busy = False
        self.message = "Tap Connect to join {0}".format(TARGET_SSID) if TARGET_SSID else "Set TARGET_SSID and TARGET_PASSWORD in config.json"
        self.error = None
        self.last_checked = "-"
        self.last_knob_move_at = 0.0
        self.focus_index = 1
        self.render_cache = None
        self.render_key = None
        self.buttons = {
            "connect": (18, 106, 152, 136),
            "disconnect": (168, 106, 302, 136),
        }
        self.refresh()
        write_log("INFO", "Wi-Fi quick connect started", ["backend={0}".format(self.backend.name), "target_ssid={0}".format(TARGET_SSID or "-")])

    def refresh(self):
        try:
            self.data = self.backend.refresh()
            self.error = None
            self.last_checked = time.strftime("%H:%M:%S")
        except Exception as exc:
            self.data = {
                "backend": self.backend.name,
                "hostname": "-",
                "iface": "-",
                "ssid": "-",
                "ip": "-",
                "signal": "-",
                "state": "-",
                "connected": False,
                "associated": False,
            }
            self.error = str(exc)
            self.message = str(exc)
            self.last_checked = time.strftime("%H:%M:%S")
            write_log(
                "ERROR",
                "Refresh failed",
                [
                    "backend={0}".format(self.backend.name),
                    "message={0}".format(str(exc)),
                    traceback.format_exc().strip(),
                ],
            )

    def start_action(self, callback, pending_message):
        with self.lock:
            if self.busy:
                return
            self.busy = True
            self.error = None
            self.message = pending_message
        write_log("INFO", pending_message, "backend={0}".format(self.backend.name))
        ActionWorker(self, callback).start()

    def finish_action(self, message=None, error=None):
        try:
            self.data = self.backend.refresh()
        except Exception as exc:
            if error is None:
                error = str(exc)
            write_log(
                "ERROR",
                "Post-action refresh failed",
                [
                    "backend={0}".format(self.backend.name),
                    "message={0}".format(str(exc)),
                    traceback.format_exc().strip(),
                ],
            )
        with self.lock:
            self.busy = False
            if error:
                self.error = error
                self.message = error
                write_log("ERROR", error, "backend={0}".format(self.backend.name))
            elif message:
                self.message = message
                write_log("INFO", message, "backend={0}".format(self.backend.name))

    def point_in_rect(self, point, rect):
        x, y = point
        left, top, right, bottom = rect
        return left <= x <= right and top <= y <= bottom

    def focus_items(self):
        return ["exit", "connect", "disconnect"]

    def move_focus(self, step):
        items = self.focus_items()
        self.focus_index = (self.focus_index + step) % len(items)
        current = items[self.focus_index]
        if current == "exit":
            self.message = "Exit"
        elif current == "connect":
            self.message = "Connect"
        else:
            self.message = "Disconnect"

    def activate_focus(self):
        current = self.focus_items()[self.focus_index]
        if current == "exit":
            return "exit"
        if current == "connect":
            self.start_action(self.backend.connect_target, "Connecting to target AP")
            return "continue"
        self.start_action(self.backend.disconnect_target, "Disconnecting from Wi-Fi")
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

        if tap[0] < EXIT_SIZE + 10 and tap[1] < EXIT_SIZE + 10:
            self.focus_index = 0
            return "exit"

        if self.point_in_rect(tap, self.buttons["connect"]):
            self.focus_index = 1
            self.start_action(self.backend.connect_target, "Connecting to target AP")
            return "continue"

        if self.point_in_rect(tap, self.buttons["disconnect"]):
            self.focus_index = 2
            self.start_action(self.backend.disconnect_target, "Disconnecting from Wi-Fi")
            return "continue"

        return "continue"

    def make_render_key(self, data, busy, message, error):
        return (
            data.get("backend", "-"),
            data.get("hostname", "-"),
            data.get("iface", "-"),
            data.get("ssid", "-"),
            data.get("ip", "-"),
            data.get("signal", "-"),
            data.get("state", "-"),
            data.get("connected", False),
            data.get("associated", False),
            busy,
            message,
            error,
            self.last_checked,
        )

    def draw_button(self, draw, rect, label, fill, focused=False):
        draw.rounded_rectangle(rect, radius=12, fill=fill)
        if focused:
            draw.rounded_rectangle(rect, radius=12, outline=TEXT, width=2)
        box = draw.textbbox((0, 0), label, font=self.font_small)
        width = box[2] - box[0]
        height = box[3] - box[1]
        x = rect[0] + ((rect[2] - rect[0] - width) // 2)
        y = rect[1] + ((rect[3] - rect[1] - height) // 2)
        draw.text((x, y), label, fill=BACKGROUND, font=self.font_small)

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
        draw.rounded_rectangle((8, 48, 312, 100), radius=16, fill=PANEL_ALT)
        draw.rounded_rectangle((8, 104, 312, 140), radius=16, fill=PANEL)
        draw.rounded_rectangle((8, 140, 312, 164), radius=16, fill=PANEL_ALT)

        exit_rect = (10, 10, 10 + EXIT_SIZE, 10 + EXIT_SIZE)
        draw.rounded_rectangle(exit_rect, radius=8, fill=ERROR)
        if self.focus_items()[self.focus_index] == "exit":
            draw.rounded_rectangle(exit_rect, radius=8, outline=TEXT, width=2)
        draw.line((17, 17, 31, 31), fill=TEXT, width=3)
        draw.line((31, 17, 17, 31), fill=TEXT, width=3)

        draw.text((50, 13), "ap-WIFI", fill=TEXT, font=self.font_medium)
        draw.text((204, 14), clip_to_width(draw, data.get("hostname", "-"), self.font_small, 96), fill=MUTED, font=self.font_small)

        if busy:
            status_color = WARNING
            status_text = "CONNECTING"
        elif data.get("connected"):
            status_color = SUCCESS
            status_text = "CONNECTED"
        elif data.get("associated"):
            status_color = WARNING
            status_text = "NO IP ADDRESS"
        else:
            status_color = ERROR
            status_text = "NOT CONNECTED"

        current_ssid = data.get("ssid", "-")
        current_ip = data.get("ip", "-")
        signal_text = "sig {0}".format(data.get("signal", "-"))

        draw.text((18, 52), clip_to_width(draw, current_ssid, self.font_xlarge, 284), fill=TEXT, font=self.font_xlarge)
        draw.text((18, 76), clip_to_width(draw, current_ip, self.font_large, 220), fill=ACCENT, font=self.font_large)
        draw.text((244, 80), clip_to_width(draw, signal_text, self.font_small, 56), fill=MUTED, font=self.font_small)
        draw.text((18, 92), status_text, fill=status_color, font=self.font_small)

        self.draw_button(draw, self.buttons["connect"], "Connect", SUCCESS, focused=self.focus_items()[self.focus_index] == "connect")
        self.draw_button(draw, self.buttons["disconnect"], "Disconnect", WARNING, focused=self.focus_items()[self.focus_index] == "disconnect")

        footer_message = clip_to_width(draw, message if not error else error, self.font_small, 286)
        footer_color = ERROR if error else MUTED
        draw.text((18, 142), footer_message, fill=footer_color, font=self.font_small)
        footer_state = "{0} | {1} | {2}".format(data.get("backend", "-"), data.get("iface", "-"), self.last_checked)
        draw.text((18, 153), clip_to_width(draw, footer_state, self.font_small, 286), fill=MUTED, font=self.font_small)

        self.render_key = key
        self.render_cache = canvas
        return canvas, True


def main():
    write_log("INFO", "Application entrypoint reached")
    finder = InputDeviceFinder()
    touch_path = finder.find_touch_device()
    if not touch_path:
        write_log("ERROR", "Touch device not found")
        print("Touch device not found.")
        sys.exit(1)

    write_log("INFO", "Using touch device", touch_path)
    print("Using touch device:", touch_path)

    rotate_path = finder.find_device_by_name("rotary@0")
    key_path = finder.find_device_by_name("gpio_keys")
    write_log(
        "INFO",
        "Using knob devices",
        [
            "rotate={0}".format(rotate_path or "-"),
            "key={0}".format(key_path or "-"),
        ],
    )

    display = RGB565Display()
    touch = TouchReader(touch_path)
    knob = KnobReader(rotate_path, key_path)
    app = TplinkConnectApp()

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
