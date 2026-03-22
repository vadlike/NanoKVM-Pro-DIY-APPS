import importlib
import base64
import glob
import hashlib
import hmac
import json
import mmap
import os
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from select import select

import numpy as np
from PIL import Image, ImageDraw, ImageFont


IMAGE_DIRECTORY = "/data"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_NONE = "/dev/mmcblk0p3"
ACCOUNT_FILE = "/etc/kvm/pwd"
APP_CONFIG_PATHS = [
    "/userapp/image-mounter/config.json",
]
API_LOGIN_SECRET = "nanokvm-sipeed-2024"
DEFAULT_API_PASSWORD = "admin"
UDC_CLASS_PATH = "/sys/class/udc"
SERVER_CONFIG_PATH = "/etc/kvm/server.yaml"
LOCAL_SERVER_BINARY = "/kvmapp/server/NanoKVM-Server"
USB_HELPER_SCRIPTS = [
    "/kvmapp/scripts/usbdev.sh",
    "/etc/init.d/S03usbdev",
    "/kvmapp/system/init.d/S03usbdev",
]
USB_DISK_FLAG = "/boot/usb.disk0"
MOUNT_DEVICE_GLOB = "/sys/kernel/config/usb_gadget/*/functions/mass_storage*/lun.0/file"
SUPPORTED_IMAGE_EXTENSIONS = (".iso", ".img", ".efi")
EFI_IMAGE_CACHE_DIRS = [
    "/tmp/image-mounter-efi",
    os.path.join(SCRIPT_DIR, ".cache", "efi"),
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


def measure_text(font, value):
    text = "-" if value is None else str(value)
    try:
        left, top, right, bottom = font.getbbox(text)
        return right - left, bottom - top, left, top
    except Exception:
        width, height = font.getmask(text).size
        return width, height, 0, 0


def format_size(num_bytes):
    if num_bytes is None:
        return "-"
    value = float(num_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return "{0:.0f} {1}".format(value, unit)
            if value >= 100:
                return "{0:.0f} {1}".format(value, unit)
            if value >= 10:
                return "{0:.1f} {1}".format(value, unit)
            return "{0:.2f} {1}".format(value, unit)
        value /= 1024.0
    return "-"


def is_supported_image_path(path):
    return str(path).lower().endswith(SUPPORTED_IMAGE_EXTENSIONS)


def is_efi_path(path):
    return str(path).lower().endswith(".efi")


def sanitize_filename_fragment(value):
    text = str(value or "").strip()
    cleaned = []
    for char in text:
        if char.isalnum() or char in ("-", "_", "."):
            cleaned.append(char)
        else:
            cleaned.append("_")
    result = "".join(cleaned).strip("._")
    return result or "efi"


def dos_name(value, length):
    text = str(value or "").upper()
    encoded = text.encode("ascii", "replace")[:length]
    return encoded.ljust(length, b" ")


def encode_fat_datetime(timestamp=None):
    current = time.localtime(timestamp if timestamp is not None else time.time())
    year = min(max(current.tm_year, 1980), 2107)
    fat_date = ((year - 1980) << 9) | (current.tm_mon << 5) | current.tm_mday
    fat_time = (current.tm_hour << 11) | (current.tm_min << 5) | (current.tm_sec // 2)
    return fat_date, fat_time


def build_directory_entry(name, ext="", attr=0x20, cluster=0, size=0, timestamp=None):
    fat_date, fat_time = encode_fat_datetime(timestamp)
    entry = bytearray(32)
    entry[0:8] = dos_name(name, 8)
    entry[8:11] = dos_name(ext, 3)
    entry[11] = attr
    struct.pack_into("<H", entry, 14, fat_time)
    struct.pack_into("<H", entry, 16, fat_date)
    struct.pack_into("<H", entry, 18, fat_date)
    struct.pack_into("<H", entry, 22, fat_time)
    struct.pack_into("<H", entry, 24, fat_date)
    struct.pack_into("<H", entry, 26, cluster & 0xFFFF)
    struct.pack_into("<I", entry, 28, size & 0xFFFFFFFF)
    return entry


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
                if event.value == 1:
                    self.touch_down = True
                elif event.value == 0:
                    self.touch_down = False
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
        return {"delta": delta, "press": press}

    def close(self):
        for device in (self.rotate_device, self.key_device):
            if device is None:
                continue
            try:
                device.ungrab()
            finally:
                device.close()


class ImageBackend:
    def __init__(self):
        self.last_setup_error = None
        self.server_config = self.read_server_config()
        self.api_base_url = self.build_api_base_url()
        self.api_token = self.build_api_token()
        self.api_username = self.read_account_username()
        self.app_config = self.read_app_config()

    def read_server_config(self):
        config = {
            "proto": "http",
            "authentication": "enable",
            "port.http": 80,
            "port.https": 443,
            "jwt.secretKey": "",
            "jwt.refreshTokenDuration": 2678400,
        }
        if not os.path.exists(SERVER_CONFIG_PATH):
            return config

        section = None
        try:
            with open(SERVER_CONFIG_PATH, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.split("#", 1)[0].rstrip()
                    if not line.strip():
                        continue

                    stripped = line.strip()
                    if stripped.endswith(":") and ":" not in stripped[:-1]:
                        section = stripped[:-1]
                        continue

                    if ":" not in stripped:
                        continue

                    key, value = stripped.split(":", 1)
                    key = key.strip()
                    value = value.strip().strip("\"'")
                    full_key = key
                    if section in ("port", "jwt"):
                        full_key = "{0}.{1}".format(section, key)

                    if full_key in ("port.http", "port.https", "jwt.refreshTokenDuration"):
                        try:
                            config[full_key] = int(value)
                        except ValueError:
                            pass
                    else:
                        config[full_key] = value
        except OSError:
            pass

        return config

    def build_api_base_url(self):
        proto = self.server_config.get("proto", "http") or "http"
        if proto not in ("http", "https"):
            proto = "http"
        port_key = "port.https" if proto == "https" else "port.http"
        port = int(self.server_config.get(port_key, 443 if proto == "https" else 80))
        default_port = 443 if proto == "https" else 80
        if port == default_port:
            return "{0}://127.0.0.1".format(proto)
        return "{0}://127.0.0.1:{1}".format(proto, port)

    def base64url(self, payload):
        return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")

    def build_api_token(self):
        if self.server_config.get("authentication", "enable") == "disable":
            return None

        secret = self.server_config.get("jwt.secretKey", "")
        if not secret:
            return None

        header = self.base64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
        expires_at = int(time.time()) + int(self.server_config.get("jwt.refreshTokenDuration", 2678400))
        payload = self.base64url(
            json.dumps({"username": "userapp", "exp": expires_at}, separators=(",", ":")).encode("utf-8")
        )
        signing_input = "{0}.{1}".format(header, payload).encode("ascii")
        signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        return "{0}.{1}.{2}".format(header, payload, self.base64url(signature))

    def read_account_username(self):
        if not os.path.exists(ACCOUNT_FILE):
            return "admin"
        try:
            with open(ACCOUNT_FILE, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload.get("username") or "admin"
        except Exception:
            return "admin"

    def read_app_config(self):
        config = {}
        for path in APP_CONFIG_PATHS:
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                if isinstance(payload, dict):
                    config.update(payload)
                    config["_path"] = path
                    return config
            except Exception:
                continue
        return config

    def cryptojs_encrypt_with_openssl(self, plaintext):
        process = subprocess.run(
            [
                "openssl",
                "enc",
                "-aes-256-cbc",
                "-md",
                "md5",
                "-salt",
                "-base64",
                "-A",
                "-pass",
                "pass:{0}".format(API_LOGIN_SECRET),
            ],
            input=plaintext,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(process.stderr.strip() or process.stdout.strip() or "openssl encrypt failed")
        return urllib.parse.quote(process.stdout.strip(), safe="")

    def cryptojs_encrypt_with_python(self, plaintext):
        AutoImport.import_package("pycryptodome", "Crypto")
        from Crypto.Cipher import AES

        salt = os.urandom(8)
        key_iv = b""
        block = b""
        passphrase = API_LOGIN_SECRET.encode("utf-8")
        while len(key_iv) < 48:
            block = hashlib.md5(block + passphrase + salt).digest()
            key_iv += block
        key = key_iv[:32]
        iv = key_iv[32:48]

        data = plaintext.encode("utf-8")
        pad = 16 - (len(data) % 16)
        data += bytes([pad]) * pad

        cipher = AES.new(key, AES.MODE_CBC, iv)
        encrypted = cipher.encrypt(data)
        output = base64.b64encode(b"Salted__" + salt + encrypted).decode("ascii")
        return urllib.parse.quote(output, safe="")

    def encrypt_login_password(self, plaintext):
        try:
            return self.cryptojs_encrypt_with_openssl(plaintext)
        except Exception:
            return self.cryptojs_encrypt_with_python(plaintext)

    def api_login(self):
        password = self.app_config.get("password") or DEFAULT_API_PASSWORD
        configured_username = self.app_config.get("username")
        candidate_usernames = []
        for value in (configured_username, "admin", self.api_username):
            if value and value not in candidate_usernames:
                candidate_usernames.append(value)

        last_error = None
        password_variants = [password]
        try:
            encrypted_password = self.encrypt_login_password(password)
            if encrypted_password not in password_variants:
                password_variants.append(encrypted_password)
        except Exception:
            pass

        for username in candidate_usernames:
            for password_value in password_variants:
                try:
                    data = self.api_request(
                        "POST",
                        "/api/auth/login",
                        {"username": username, "password": password_value},
                        timeout=30,
                        require_auth=False,
                    )
                    token = data.get("token") or ""
                    if not token:
                        raise RuntimeError("NanoKVM API login returned no token")
                    self.api_token = token
                    self.api_username = username
                    return token
                except Exception as exc:
                    last_error = str(exc)

        raise RuntimeError(last_error or "NanoKVM API login failed")

    def api_request(self, method, path, data=None, timeout=20, require_auth=True):
        if not os.path.exists(SERVER_CONFIG_PATH) and not os.path.exists(LOCAL_SERVER_BINARY):
            raise RuntimeError("NanoKVM-Server config not found")

        headers = {"Accept": "application/json"}
        payload = None
        if data is not None:
            payload = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"

        if require_auth and self.server_config.get("authentication", "enable") != "disable":
            if not self.api_token:
                self.api_login()
            if not self.api_token:
                raise RuntimeError("NanoKVM API auth is enabled, but jwt.secretKey is not available in /etc/kvm/server.yaml")
            headers["Cookie"] = "nano-kvm-token={0}".format(self.api_token)

        request = urllib.request.Request(
            "{0}{1}".format(self.api_base_url, path),
            data=payload,
            headers=headers,
            method=method,
        )

        try:
            ssl_context = None
            if self.api_base_url.startswith("https://"):
                ssl_context = ssl._create_unverified_context()
            with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace").strip()
            if exc.code == 401 and require_auth:
                self.api_token = None
            raise RuntimeError("HTTP {0}: {1}".format(exc.code, body or exc.reason))
        except urllib.error.URLError as exc:
            raise RuntimeError("NanoKVM API unavailable: {0}".format(exc.reason))

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError("Invalid API response")

        if payload.get("code") != 0:
            raise RuntimeError(payload.get("msg") or "NanoKVM API returned an error")
        return payload.get("data") or {}

    def api_status(self):
        files = self.api_request("GET", "/api/storage/image").get("files") or []
        mounted = self.api_request("GET", "/api/storage/image/mounted").get("file") or ""
        sizes = self.build_size_map(files)
        try:
            cdrom_value = self.api_request("GET", "/api/storage/cdrom").get("cdrom")
            cdrom_enabled = int(cdrom_value or 0) == 1
        except Exception:
            cdrom_enabled = self.current_cdrom()
        return {
            "files": files,
            "sizes": sizes,
            "mounted": mounted,
            "cdrom": cdrom_enabled,
            "readonly": self.current_readonly(),
            "storage_ready": True,
            "backend": "api",
        }

    def api_mount(self, file_path, cdrom, display_path=None):
        self.api_request("POST", "/api/storage/image/mount", {"file": file_path, "cdrom": bool(cdrom)}, timeout=45)
        shown_path = display_path or file_path
        return "Mounted {0}".format(os.path.basename(shown_path))

    def api_unmount(self):
        self.api_request("POST", "/api/storage/image/mount", {"file": "", "cdrom": False}, timeout=45)
        return "Image unmounted"

    def find_usb_helper(self):
        for path in USB_HELPER_SCRIPTS:
            if os.path.exists(path):
                if path.endswith("usbdev.sh"):
                    return path, "restart"
                return path, "stop_start"
        return None, None

    def run(self, args, timeout=20):
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
            raise RuntimeError(message)
        return process.stdout.strip()

    def ensure_efi_cache_dir(self):
        for path in EFI_IMAGE_CACHE_DIRS:
            try:
                os.makedirs(path, exist_ok=True)
                probe_path = os.path.join(path, ".write-probe")
                with open(probe_path, "w", encoding="utf-8") as handle:
                    handle.write("ok")
                os.remove(probe_path)
                return path
            except OSError:
                continue
        raise RuntimeError("No writable directory available for generated EFI images")

    def generated_efi_paths(self, source_path):
        cache_dir = self.ensure_efi_cache_dir()
        source_key = os.path.abspath(source_path)
        digest = hashlib.sha1(source_key.encode("utf-8")).hexdigest()[:12]
        stem = sanitize_filename_fragment(os.path.splitext(os.path.basename(source_path))[0])[:40]
        base_path = os.path.join(cache_dir, "{0}-{1}".format(stem, digest))
        return base_path + ".img", base_path + ".json"

    def read_generated_efi_metadata(self, image_path):
        metadata_path = os.path.splitext(image_path)[0] + ".json"
        if not os.path.exists(metadata_path):
            return None
        try:
            with open(metadata_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                return payload
        except Exception:
            return None
        return None

    def infer_boot_filename(self, source_path):
        basename = os.path.basename(source_path).upper()
        standard_names = {
            "BOOTX64.EFI",
            "BOOTIA32.EFI",
            "BOOTAA64.EFI",
            "BOOTARM.EFI",
            "BOOTRISCV64.EFI",
        }
        if basename in standard_names:
            return basename
        lowered = os.path.basename(source_path).lower()
        if "aa64" in lowered or "arm64" in lowered or "aarch64" in lowered:
            return "BOOTAA64.EFI"
        if "ia32" in lowered or "x86_32" in lowered:
            return "BOOTIA32.EFI"
        if "arm" in lowered and "arm64" not in lowered and "aa64" not in lowered:
            return "BOOTARM.EFI"
        if "riscv64" in lowered:
            return "BOOTRISCV64.EFI"
        return "BOOTX64.EFI"

    def select_fat16_geometry(self, payload_size):
        bytes_per_sector = 512
        root_entries = 128
        root_dir_sectors = (root_entries * 32 + (bytes_per_sector - 1)) // bytes_per_sector
        reserved_sectors = 1
        fat_count = 2
        best = None
        for sectors_per_cluster in (1, 2, 4, 8, 16, 32, 64):
            cluster_size = sectors_per_cluster * bytes_per_sector
            file_clusters = max(1, (payload_size + cluster_size - 1) // cluster_size)
            used_clusters = 2 + file_clusters
            cluster_count = max(4096, used_clusters + 32)
            if cluster_count > 65524:
                continue
            sectors_per_fat = ((cluster_count + 2) * 2 + (bytes_per_sector - 1)) // bytes_per_sector
            total_sectors = reserved_sectors + (fat_count * sectors_per_fat) + root_dir_sectors + (cluster_count * sectors_per_cluster)
            candidate = (
                total_sectors,
                cluster_size - (payload_size % cluster_size or cluster_size),
                sectors_per_cluster,
                sectors_per_fat,
                cluster_count,
                file_clusters,
            )
            if best is None or candidate < best:
                best = candidate
        if best is None:
            raise RuntimeError("EFI file is too large to wrap in a FAT16 image")
        _total_sectors, _slack, sectors_per_cluster, sectors_per_fat, cluster_count, file_clusters = best
        return {
            "bytes_per_sector": bytes_per_sector,
            "root_entries": root_entries,
            "root_dir_sectors": root_dir_sectors,
            "reserved_sectors": reserved_sectors,
            "fat_count": fat_count,
            "sectors_per_cluster": sectors_per_cluster,
            "sectors_per_fat": sectors_per_fat,
            "cluster_count": cluster_count,
            "file_clusters": file_clusters,
        }

    def build_fat16_efi_partition(self, source_path, boot_filename):
        with open(source_path, "rb") as handle:
            payload = handle.read()
        if not payload:
            raise RuntimeError("EFI file is empty: {0}".format(source_path))

        geometry = self.select_fat16_geometry(len(payload))
        bytes_per_sector = geometry["bytes_per_sector"]
        root_entries = geometry["root_entries"]
        root_dir_sectors = geometry["root_dir_sectors"]
        reserved_sectors = geometry["reserved_sectors"]
        fat_count = geometry["fat_count"]
        sectors_per_cluster = geometry["sectors_per_cluster"]
        sectors_per_fat = geometry["sectors_per_fat"]
        cluster_count = geometry["cluster_count"]
        file_clusters = geometry["file_clusters"]
        total_sectors = reserved_sectors + (fat_count * sectors_per_fat) + root_dir_sectors + (cluster_count * sectors_per_cluster)
        partition_start_sector = 2048
        image_size = (partition_start_sector + total_sectors) * bytes_per_sector
        image = bytearray(image_size)

        partition_offset = partition_start_sector * bytes_per_sector
        root_dir_offset = partition_offset + ((reserved_sectors + (fat_count * sectors_per_fat)) * bytes_per_sector)
        data_offset = partition_offset + ((reserved_sectors + (fat_count * sectors_per_fat) + root_dir_sectors) * bytes_per_sector)
        cluster_size = sectors_per_cluster * bytes_per_sector

        def cluster_offset(cluster_number):
            return data_offset + ((cluster_number - 2) * cluster_size)

        boot_sector = memoryview(image)[partition_offset : partition_offset + bytes_per_sector]
        boot_sector[0:3] = b"\xEB\x3C\x90"
        boot_sector[3:11] = b"MSDOS5.0"
        struct.pack_into("<H", boot_sector, 11, bytes_per_sector)
        boot_sector[13] = sectors_per_cluster
        struct.pack_into("<H", boot_sector, 14, reserved_sectors)
        boot_sector[16] = fat_count
        struct.pack_into("<H", boot_sector, 17, root_entries)
        if total_sectors < 0x10000:
            struct.pack_into("<H", boot_sector, 19, total_sectors)
            struct.pack_into("<I", boot_sector, 32, 0)
        else:
            struct.pack_into("<H", boot_sector, 19, 0)
            struct.pack_into("<I", boot_sector, 32, total_sectors)
        boot_sector[21] = 0xF8
        struct.pack_into("<H", boot_sector, 22, sectors_per_fat)
        struct.pack_into("<H", boot_sector, 24, 63)
        struct.pack_into("<H", boot_sector, 26, 255)
        struct.pack_into("<I", boot_sector, 28, partition_start_sector)
        boot_sector[36] = 0x80
        boot_sector[38] = 0x29
        struct.pack_into("<I", boot_sector, 39, int(time.time()) & 0xFFFFFFFF)
        boot_sector[43:54] = dos_name("NANOKVMEFI", 11)
        boot_sector[54:62] = b"FAT16   "
        boot_sector[510:512] = b"\x55\xAA"

        mbr = memoryview(image)[0:bytes_per_sector]
        partition_entry_offset = 446
        mbr[partition_entry_offset] = 0x80
        mbr[partition_entry_offset + 1 : partition_entry_offset + 4] = b"\x00\x02\x00"
        mbr[partition_entry_offset + 4] = 0xEF
        mbr[partition_entry_offset + 5 : partition_entry_offset + 8] = b"\xFE\xFF\xFF"
        struct.pack_into("<I", mbr, partition_entry_offset + 8, partition_start_sector)
        struct.pack_into("<I", mbr, partition_entry_offset + 12, total_sectors)
        mbr[510:512] = b"\x55\xAA"

        fat = bytearray(sectors_per_fat * bytes_per_sector)
        struct.pack_into("<H", fat, 0, 0xFFF8)
        struct.pack_into("<H", fat, 2, 0xFFFF)
        struct.pack_into("<H", fat, 4, 0xFFFF)
        struct.pack_into("<H", fat, 6, 0xFFFF)
        first_file_cluster = 4
        for index in range(file_clusters):
            cluster_number = first_file_cluster + index
            next_cluster = 0xFFFF if index == file_clusters - 1 else cluster_number + 1
            struct.pack_into("<H", fat, cluster_number * 2, next_cluster)
        fat_offset = partition_offset + (reserved_sectors * bytes_per_sector)
        for fat_index in range(fat_count):
            start = fat_offset + (fat_index * sectors_per_fat * bytes_per_sector)
            image[start : start + len(fat)] = fat

        image[root_dir_offset : root_dir_offset + 32] = build_directory_entry("EFI", attr=0x10, cluster=2)

        efi_dir_offset = cluster_offset(2)
        image[efi_dir_offset : efi_dir_offset + 32] = build_directory_entry(".", attr=0x10, cluster=2)
        image[efi_dir_offset + 32 : efi_dir_offset + 64] = build_directory_entry("..", attr=0x10, cluster=0)
        image[efi_dir_offset + 64 : efi_dir_offset + 96] = build_directory_entry("BOOT", attr=0x10, cluster=3)

        boot_dir_offset = cluster_offset(3)
        image[boot_dir_offset : boot_dir_offset + 32] = build_directory_entry(".", attr=0x10, cluster=3)
        image[boot_dir_offset + 32 : boot_dir_offset + 64] = build_directory_entry("..", attr=0x10, cluster=2)
        file_name, file_ext = os.path.splitext(boot_filename)
        image[boot_dir_offset + 64 : boot_dir_offset + 96] = build_directory_entry(
            file_name,
            file_ext.lstrip("."),
            attr=0x20,
            cluster=first_file_cluster,
            size=len(payload),
        )

        payload_offset = 0
        for index in range(file_clusters):
            cluster_number = first_file_cluster + index
            chunk = payload[payload_offset : payload_offset + cluster_size]
            start = cluster_offset(cluster_number)
            image[start : start + len(chunk)] = chunk
            payload_offset += len(chunk)

        return image

    def prepare_efi_mount_image(self, source_path):
        source_path = os.path.abspath(source_path)
        source_stat = os.stat(source_path)
        image_path, metadata_path = self.generated_efi_paths(source_path)
        metadata = self.read_generated_efi_metadata(image_path)
        if metadata and os.path.exists(image_path):
            if (
                metadata.get("source_path") == source_path
                and int(metadata.get("source_size", -1)) == int(source_stat.st_size)
                and float(metadata.get("source_mtime", -1)) == float(source_stat.st_mtime)
            ):
                return image_path

        os.makedirs(os.path.dirname(image_path), exist_ok=True)
        boot_filename = self.infer_boot_filename(source_path)
        image_bytes = self.build_fat16_efi_partition(source_path, boot_filename)
        temp_image_path = image_path + ".tmp"
        temp_metadata_path = metadata_path + ".tmp"
        with open(temp_image_path, "wb") as handle:
            handle.write(image_bytes)
        metadata = {
            "generated_from": "efi",
            "source_path": source_path,
            "source_size": int(source_stat.st_size),
            "source_mtime": float(source_stat.st_mtime),
            "image_path": image_path,
            "boot_filename": boot_filename,
        }
        with open(temp_metadata_path, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, indent=2, sort_keys=True)
        os.replace(temp_image_path, image_path)
        os.replace(temp_metadata_path, metadata_path)
        return image_path

    def resolve_mount_source(self, mounted_path):
        if not mounted_path:
            return ""
        metadata = self.read_generated_efi_metadata(mounted_path)
        if metadata and metadata.get("generated_from") == "efi":
            return metadata.get("source_path") or mounted_path
        return mounted_path

    def resolve_mount_target(self, file_path):
        if is_efi_path(file_path):
            return self.prepare_efi_mount_image(file_path)
        return file_path

    def list_images(self):
        images = []
        if not os.path.isdir(IMAGE_DIRECTORY):
            return images
        for root, _dirs, files in os.walk(IMAGE_DIRECTORY):
            for name in files:
                if is_supported_image_path(name):
                    images.append(os.path.join(root, name))
        images.sort()
        return images

    def build_size_map(self, files):
        sizes = {}
        for path in files:
            try:
                sizes[path] = os.path.getsize(path)
            except OSError:
                sizes[path] = None
        return sizes

    def read_text(self, path, default=""):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return default

    def write_text(self, path, value):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(value)

    def find_storage_paths(self):
        candidates = sorted(glob.glob(MOUNT_DEVICE_GLOB))
        if not candidates:
            return None
        mount_device = candidates[0]
        lun_dir = os.path.dirname(mount_device)
        function_dir = os.path.dirname(lun_dir)
        functions_dir = os.path.dirname(function_dir)
        gadget_dir = os.path.dirname(functions_dir)
        return {
            "mount_device": mount_device,
            "cdrom_flag": os.path.join(lun_dir, "cdrom"),
            "forced_eject": os.path.join(lun_dir, "forced_eject"),
            "inquiry_string": os.path.join(lun_dir, "inquiry_string"),
            "ro_flag": os.path.join(lun_dir, "ro"),
            "udc_path": os.path.join(gadget_dir, "UDC"),
        }

    def enable_virtual_disk(self):
        self.last_setup_error = None
        helper_path, helper_action = self.find_usb_helper()
        if not helper_path:
            self.last_setup_error = "Missing USB helper: {0}".format(", ".join(USB_HELPER_SCRIPTS))
            return False
        try:
            with open(USB_DISK_FLAG, "a", encoding="utf-8"):
                pass
            self.run([helper_path, helper_action], timeout=30)
            time.sleep(1.0)
        except Exception as exc:
            self.last_setup_error = str(exc)
            return False
        if self.find_storage_paths() is None:
            self.last_setup_error = "{0} completed, but mass_storage gadget was not created".format(
                os.path.basename(helper_path)
            )
            return False
        return True

    def require_storage_paths(self):
        paths = self.find_storage_paths()
        if paths:
            return paths
        previous_error = self.last_setup_error
        if self.enable_virtual_disk():
            paths = self.find_storage_paths()
            if paths:
                return paths
        if previous_error and self.last_setup_error and previous_error != self.last_setup_error:
            details = "{0}; {1}".format(previous_error, self.last_setup_error)
        else:
            details = self.last_setup_error or previous_error or "Enable Virtual U Disk or image mounting first."
        raise RuntimeError("USB mass storage is not available. {0}".format(details))

    def current_mount(self):
        paths = self.find_storage_paths()
        if not paths:
            return ""
        mounted = self.read_text(paths["mount_device"], "")
        if mounted == IMAGE_NONE:
            return ""
        return mounted

    def current_cdrom(self):
        paths = self.find_storage_paths()
        if not paths:
            return False
        return self.read_text(paths["cdrom_flag"], "0") == "1"

    def current_readonly(self):
        paths = self.find_storage_paths()
        if not paths:
            return False
        return self.read_text(paths["ro_flag"], "0") == "1"

    def set_udc_enabled(self, paths, enabled):
        if not enabled:
            for attempt in range(5):
                try:
                    self.write_text(paths["udc_path"], "")
                    time.sleep(1.0)
                    return
                except OSError:
                    if not self.current_udc(paths):
                        return
                    if attempt == 4:
                        raise
                    time.sleep(0.5)
            return
        udc_names = []
        if os.path.isdir(UDC_CLASS_PATH):
            udc_names = os.listdir(UDC_CLASS_PATH)
        if not udc_names:
            raise RuntimeError("USB UDC not found")
        for attempt in range(5):
            try:
                self.write_text(paths["udc_path"], udc_names[0])
                time.sleep(1.0)
                return
            except OSError:
                if self.current_udc(paths):
                    return
                if attempt == 4:
                    raise
                time.sleep(0.5)

    def reset_usb(self, paths):
        self.set_udc_enabled(paths, False)
        self.set_udc_enabled(paths, True)

    def current_udc(self, paths):
        return self.read_text(paths["udc_path"], "")

    def detach_current_image(self, paths):
        self.set_udc_enabled(paths, False)
        forced_eject_path = paths.get("forced_eject")
        if forced_eject_path and os.path.exists(forced_eject_path):
            try:
                self.write_text(forced_eject_path, "1")
            except OSError:
                pass
        try:
            self.write_text(paths["mount_device"], "\n")
        except OSError:
            pass
        time.sleep(1.0)

    def set_inquiry(self, paths, cdrom):
        vendor = "NanoKVM"
        product = "USB CD/DVD-ROM" if cdrom else "USB Mass Storage"
        version = 0x0520
        inquiry = "{0:<8}{1:<16}{2:04x}".format(vendor, product, version)
        self.write_text(paths["inquiry_string"], inquiry)

    def mount(self, file_path, cdrom):
        if not file_path:
            raise RuntimeError("No image selected")
        if not os.path.exists(file_path):
            raise RuntimeError("Image not found: {0}".format(file_path))
        mount_path = self.resolve_mount_target(file_path)

        try:
            return self.api_mount(mount_path, cdrom, display_path=file_path)
        except Exception as exc:
            self.last_setup_error = str(exc)

        paths = self.require_storage_paths()
        self.detach_current_image(paths)

        if cdrom:
            self.write_text(paths["ro_flag"], "1")
            self.write_text(paths["cdrom_flag"], "1")
        else:
            self.write_text(paths["ro_flag"], "0")
            self.write_text(paths["cdrom_flag"], "0")

        self.set_inquiry(paths, cdrom)
        self.write_text(paths["mount_device"], mount_path)
        self.set_udc_enabled(paths, True)
        return "Mounted {0}".format(os.path.basename(file_path))

    def unmount(self):
        try:
            return self.api_unmount()
        except Exception as exc:
            self.last_setup_error = str(exc)

        paths = self.require_storage_paths()
        self.detach_current_image(paths)
        self.write_text(paths["ro_flag"], "0")
        self.write_text(paths["cdrom_flag"], "0")
        self.set_inquiry(paths, False)
        self.write_text(paths["mount_device"], IMAGE_NONE)
        self.set_udc_enabled(paths, True)
        return "Image unmounted"

    def status(self):
        files = self.list_images()
        sizes = self.build_size_map(files)
        mounted = self.resolve_mount_source(self.current_mount())
        try:
            status = self.api_status()
            status["files"] = files
            status["sizes"] = sizes
            status["mounted"] = self.resolve_mount_source(status.get("mounted", ""))
            return status
        except Exception as exc:
            self.last_setup_error = str(exc)

        storage_ready = self.find_storage_paths() is not None
        helper_path, _helper_action = self.find_usb_helper()
        if storage_ready or helper_path:
            return {
                "files": files,
                "sizes": sizes,
                "mounted": mounted,
                "cdrom": self.current_cdrom(),
                "readonly": self.current_readonly(),
                "storage_ready": storage_ready,
                "backend": "sysfs",
            }
        return {
            "files": files,
            "sizes": sizes,
            "mounted": mounted,
            "cdrom": False,
            "readonly": False,
            "storage_ready": False,
            "backend": "offline",
        }


class ImageMounterApp:
    def __init__(self):
        self.font_small = load_font(10)
        self.font_medium = load_font(13)
        self.font_large = load_font(18)
        self.font_xlarge = load_font(20)
        self.backend = ImageBackend()
        self.data = self.backend.status()
        self.mode_cdrom = True
        if self.data.get("mounted"):
            self.mode_cdrom = bool(self.data.get("cdrom", False))
        self.message = "Choose image and mount"
        self.error = None
        self.busy = False
        self.render_cache = None
        self.render_key = None
        self.selected_index = 0
        self.scroll_offset = 0
        self.focus_index = 0
        self.screen = "main"
        self.last_checked = time.strftime("%H:%M:%S")
        self.main_buttons = {
            "mode": (12, 116, 104, 146),
            "mount": (112, 116, 212, 146),
            "library": (220, 116, 308, 146),
        }
        self.library_buttons = {
            "back": (14, 54, 82, 78),
        }
        self.file_panel = (8, 48, 312, 108)
        self.library_panel = (8, 48, 312, 164)
        self.gesture_start = None
        self.marquee_started_at = time.time()
        self.sync_selection()

    def reset_marquee(self):
        self.marquee_started_at = time.time()

    def sync_selection(self):
        files = self.data.get("files", [])
        if not files:
            self.selected_index = 0
            self.scroll_offset = 0
            self.normalize_focus()
            self.reset_marquee()
            return
        self.selected_index = max(0, min(self.selected_index, len(files) - 1))
        max_offset = max(0, len(files) - self.visible_rows())
        if self.selected_index < self.scroll_offset:
            self.scroll_offset = self.selected_index
        elif self.selected_index >= self.scroll_offset + self.visible_rows():
            self.scroll_offset = self.selected_index - (self.visible_rows() - 1)
        self.scroll_offset = max(0, min(self.scroll_offset, max_offset))
        self.normalize_focus()
        self.reset_marquee()

    def visible_rows(self):
        return 4

    def get_focus_items(self):
        if self.screen == "main":
            return [("button", "mode"), ("button", "mount"), ("button", "library")]

        items = [("button", "back")]
        files = self.data.get("files", [])
        for index in range(len(files)):
            items.append(("file", index))
        return items

    def normalize_focus(self):
        items = self.get_focus_items()
        if not items:
            self.focus_index = 0
            return
        self.focus_index %= len(items)

    def move_focus(self, delta):
        items = self.get_focus_items()
        if not items or delta == 0:
            return
        step = 1 if delta > 0 else -1
        self.focus_index = (self.focus_index + step) % len(items)
        kind, value = items[self.focus_index]
        if kind == "file":
            self.selected_index = value
            self.sync_selection()

    def activate_focus(self):
        items = self.get_focus_items()
        if not items:
            return
        self.normalize_focus()
        kind, value = items[self.focus_index]
        if kind == "button":
            self.activate(value)
            return
        self.selected_index = value
        self.sync_selection()
        self.activate("file")

    def refresh(self, message="Refreshed"):
        self.data = self.backend.status()
        if self.data.get("mounted"):
            self.mode_cdrom = bool(self.data.get("cdrom", False))
        else:
            self.data["cdrom"] = self.mode_cdrom
        self.error = None
        self.message = message
        self.last_checked = time.strftime("%H:%M:%S")
        self.sync_selection()

    def current_file(self):
        files = self.data.get("files", [])
        if not files:
            return ""
        return files[self.selected_index]

    def size_for(self, path):
        return (self.data.get("sizes") or {}).get(path)

    def action_for_file(self, selected, mode_cdrom):
        mounted = self.data.get("mounted", "")
        if selected and mounted == selected:
            return self.backend.unmount()
        return self.backend.mount(selected, mode_cdrom)

    def open_library(self):
        self.screen = "library"
        self.focus_index = 1 if self.data.get("files") else 0
        self.message = "Swipe right to go back"
        self.render_key = None
        self.sync_selection()

    def close_library(self, message=None):
        self.screen = "main"
        self.focus_index = 2
        if message:
            self.message = message
        self.last_checked = time.strftime("%H:%M:%S")
        self.render_key = None

    def toggle_mode(self):
        if self.busy:
            return self.message
        self.mode_cdrom = not self.mode_cdrom
        self.data["cdrom"] = self.mode_cdrom
        self.message = "Mode: {0}".format("CD-ROM" if self.mode_cdrom else "Mass Storage")
        return self.message

    def start_background_action(self, status_message, worker):
        if self.busy:
            return
        self.busy = True
        self.error = None
        self.message = status_message
        self.last_checked = time.strftime("%H:%M:%S")
        self.render_key = None

        def run_action():
            try:
                result = worker()
                self.refresh(result)
            except Exception as exc:
                self.error = str(exc)
                self.message = str(exc)
                self.last_checked = time.strftime("%H:%M:%S")
                self.render_key = None
            finally:
                self.busy = False

        threading.Thread(target=run_action, daemon=True).start()

    def activate(self, target):
        try:
            if self.busy:
                return
            if target == "mode":
                self.toggle_mode()
                return
            if target == "library":
                self.open_library()
                return
            if target == "back":
                self.close_library("Back to image view")
                return
            if target == "mount":
                selected = self.current_file()
                if not selected:
                    self.message = "No image selected"
                    self.last_checked = time.strftime("%H:%M:%S")
                    return
                mounted = self.data.get("mounted", "")
                status_message = "Unmounting..." if mounted == selected else "Mounting..."
                mode_cdrom = self.mode_cdrom
                self.start_background_action(
                    status_message,
                    lambda selected=selected, mode_cdrom=mode_cdrom: self.action_for_file(selected, mode_cdrom),
                )
                return
            if target == "file":
                selected = self.current_file()
                if not selected:
                    self.message = "No image selected"
                    self.last_checked = time.strftime("%H:%M:%S")
                    return
                self.close_library("Selected {0}".format(os.path.basename(selected)))
        except Exception as exc:
            self.error = str(exc)
            self.message = str(exc)
            self.last_checked = time.strftime("%H:%M:%S")

    def point_in_rect(self, point, rect):
        x, y = point
        left, top, right, bottom = rect
        return left <= x <= right and top <= y <= bottom

    def update(self, touch_state, knob_state):
        if knob_state.get("delta"):
            self.move_focus(knob_state["delta"])
        if knob_state.get("press"):
            self.activate_focus()
            return "continue"

        if touch_state.get("down") and touch_state.get("touch") and self.gesture_start is None:
            self.gesture_start = touch_state["touch"]

        if touch_state.get("released") and self.gesture_start and touch_state.get("released_at"):
            start_x, start_y = self.gesture_start
            end_x, end_y = touch_state["released_at"]
            dx = end_x - start_x
            dy = end_y - start_y
            self.gesture_start = None
            if abs(dx) >= 60 and abs(dx) > abs(dy) * 1.3:
                if dx < 0 and self.screen == "main":
                    self.open_library()
                    return "continue"
                if dx > 0 and self.screen == "library":
                    self.close_library("Back to image view")
                    return "continue"
        elif not touch_state.get("down") and not touch_state.get("released"):
            self.gesture_start = None

        tap = touch_state["tap"]
        if not tap:
            return "continue"
        if tap[0] < EXIT_SIZE + 10 and tap[1] < EXIT_SIZE + 10:
            return "exit"

        if self.screen == "main":
            for position, (name, rect) in enumerate(self.main_buttons.items()):
                if self.point_in_rect(tap, rect):
                    self.focus_index = position
                    self.activate(name)
                    return "continue"
            if self.point_in_rect(tap, self.file_panel):
                self.open_library()
                return "continue"
        else:
            if self.point_in_rect(tap, self.library_buttons["back"]):
                self.focus_index = 0
                self.activate("back")
                return "continue"
            for rect, index, _file in self.file_rows():
                if self.point_in_rect(tap, rect):
                    self.selected_index = index
                    self.sync_selection()
                    self.focus_index = 1 + index
                    self.activate("file")
                    return "continue"
        return "continue"

    def file_rows(self):
        rows = []
        files = self.data.get("files", [])
        start = self.scroll_offset
        visible = files[start : start + self.visible_rows()]
        for row_index, file_path in enumerate(visible):
            index = start + row_index
            top = 86 + (row_index * 18)
            rows.append(((14, top, 306, top + 16), index, file_path))
        return rows

    def marquee_tick(self):
        return int(max(0.0, time.time() - self.marquee_started_at) * 12)

    def marquee_offset(self, text_width, viewport_width):
        overflow = max(0, text_width - viewport_width)
        if overflow <= 0:
            return 0
        pause_ticks = 8
        travel_ticks = max(1, overflow // 2)
        cycle = (pause_ticks * 2) + (travel_ticks * 2)
        step = self.marquee_tick() % cycle
        if step < pause_ticks:
            return 0
        if step < pause_ticks + travel_ticks:
            return min(overflow, (step - pause_ticks) * 2)
        if step < (pause_ticks * 2) + travel_ticks:
            return overflow
        return max(0, overflow - ((step - ((pause_ticks * 2) + travel_ticks)) * 2))

    def should_scroll_library_name(self):
        if self.screen != "library":
            return False
        files = self.data.get("files", [])
        if not files or self.selected_index >= len(files):
            return False
        if not (self.scroll_offset <= self.selected_index < self.scroll_offset + self.visible_rows()):
            return False
        row_name = os.path.basename(files[self.selected_index])
        prefix = "* " if self.data.get("mounted", "") == files[self.selected_index] else ""
        width, _height, _left, _top = measure_text(self.font_medium, prefix + row_name)
        return width > 208

    def should_scroll_main_name(self):
        if self.screen != "main":
            return False
        current = self.current_file()
        current_name = os.path.basename(current) if current else "No image selected"
        width, _height, _left, _top = measure_text(self.font_xlarge, current_name)
        return width > 276

    def should_scroll_main_mounted(self):
        if self.screen != "main":
            return False
        mounted = self.data.get("mounted", "")
        mounted_name = os.path.basename(mounted) if mounted else "None"
        text = "Mounted: {0}".format(mounted_name)
        width, _height, _left, _top = measure_text(self.font_medium, text)
        return width > 276

    def draw_scrolling_text(self, canvas, rect, text, font, fill, background):
        viewport_width = max(1, rect[2] - rect[0])
        viewport_height = max(1, rect[3] - rect[1])
        text_width, text_height, text_left, text_top = measure_text(font, text)
        text_y = max(0, ((viewport_height - text_height) // 2) - text_top)
        if text_width <= viewport_width:
            draw = ImageDraw.Draw(canvas)
            draw.text((rect[0], rect[1] + text_y), text, fill=fill, font=font)
            return

        strip = Image.new("RGB", (text_width, viewport_height), background)
        strip_draw = ImageDraw.Draw(strip)
        strip_draw.text((0, text_y), text, fill=fill, font=font)
        offset = self.marquee_offset(text_width, viewport_width)
        segment = strip.crop((offset, 0, offset + viewport_width, viewport_height))
        canvas.paste(segment, (rect[0], rect[1]))

    def draw_button(self, draw, rect, label, fill, focused=False, disabled=False):
        button_fill = fill if not disabled else PANEL_ALT
        draw.rounded_rectangle(rect, radius=12, fill=button_fill)
        if focused:
            draw.rounded_rectangle(rect, radius=12, outline=TEXT, width=2)
        box = draw.textbbox((0, 0), label, font=self.font_small)
        width = box[2] - box[0]
        height = box[3] - box[1]
        x = rect[0] + ((rect[2] - rect[0] - width) // 2)
        y = rect[1] + ((rect[3] - rect[1] - height) // 2)
        text_fill = MUTED if disabled else BACKGROUND
        draw.text((x, y), label, fill=text_fill, font=self.font_small)

    def draw_header(self, draw, title, subtitle):
        draw.rounded_rectangle((8, 8, 312, 42), radius=12, fill=PANEL)
        draw.rounded_rectangle((10, 10, 10 + EXIT_SIZE, 10 + EXIT_SIZE), radius=8, fill=ERROR)
        draw.line((17, 17, 31, 31), fill=TEXT, width=3)
        draw.line((31, 17, 17, 31), fill=TEXT, width=3)
        draw.text((50, 13), title, fill=TEXT, font=self.font_medium)
        if subtitle:
            draw.text((232, 14), subtitle, fill=MUTED, font=self.font_small)

    def draw_main_screen(self, canvas, draw):
        draw.rounded_rectangle(self.file_panel, radius=16, fill=PANEL_ALT)
        draw.rounded_rectangle((8, 112, 312, 150), radius=16, fill=PANEL)
        draw.rounded_rectangle((8, 140, 312, 164), radius=16, fill=PANEL_ALT)

        self.draw_header(draw, "IMAGE MOUNTER", "SWIPE")

        current = self.current_file()
        current_name = os.path.basename(current) if current else "No image selected"
        current_size = format_size(self.size_for(current)) if current else "-"
        mounted = self.data.get("mounted", "")
        mounted_name = os.path.basename(mounted) if mounted else "None"
        status_text = "MOUNTED" if mounted and mounted == current else ("ACTIVE" if mounted else "IDLE")
        status_color = SUCCESS if mounted else ACCENT

        if self.should_scroll_main_name():
            self.draw_scrolling_text(canvas, (18, 52, 294, 74), current_name, self.font_xlarge, TEXT, PANEL_ALT)
        else:
            draw.text((18, 54), clip_to_width(draw, current_name, self.font_xlarge, 276), fill=TEXT, font=self.font_xlarge)

        mounted_text = "Mounted: {0}".format(mounted_name)
        if self.should_scroll_main_mounted():
            self.draw_scrolling_text(canvas, (18, 76, 294, 92), mounted_text, self.font_medium, ACCENT, PANEL_ALT)
        else:
            draw.text((18, 78), clip_to_width(draw, mounted_text, self.font_medium, 276), fill=ACCENT, font=self.font_medium)
        info_line = "{0} • {1}".format(status_text, current_size)
        draw.text((18, 94), clip_to_width(draw, info_line, self.font_small, 276), fill=status_color if not current_size else TEXT, font=self.font_small)

        focus_items = self.get_focus_items()
        current_focus = focus_items[self.focus_index] if focus_items else None
        self.draw_button(
            draw,
            self.main_buttons["mode"],
            "Mode",
            WARNING,
            focused=current_focus == ("button", "mode"),
            disabled=self.busy,
        )
        mount_label = "Unmount" if mounted and mounted == current else "Mount"
        mount_fill = ERROR if mount_label == "Unmount" else SUCCESS
        if self.busy:
            mount_label = "Working"
        self.draw_button(
            draw,
            self.main_buttons["mount"],
            mount_label,
            mount_fill,
            focused=current_focus == ("button", "mount"),
            disabled=self.busy,
        )
        self.draw_button(
            draw,
            self.main_buttons["library"],
            "List",
            ACCENT,
            focused=current_focus == ("button", "library"),
            disabled=self.busy,
        )

        footer = "{0} | {1} | checked {2}".format(
            "CD-ROM" if self.mode_cdrom else "Mass Storage",
            self.message,
            self.last_checked,
        )
        draw.text((18, 147), clip_to_width(draw, footer, self.font_small, 286), fill=MUTED if not self.error else ERROR, font=self.font_small)

    def draw_library_screen(self, canvas, draw):
        draw.rounded_rectangle(self.library_panel, radius=16, fill=PANEL_ALT)
        self.draw_header(draw, "IMAGE LIBRARY", "PICK")

        focus_items = self.get_focus_items()
        current_focus = focus_items[self.focus_index] if focus_items else None
        self.draw_button(
            draw,
            self.library_buttons["back"],
            "Back",
            ACCENT,
            focused=current_focus == ("button", "back"),
            disabled=self.busy,
        )

        files = self.data.get("files", [])
        if not files:
            draw.text((18, 82), "No .iso, .img, or .efi files in /data", fill=MUTED, font=self.font_medium)
            return

        rows = self.file_rows()
        for rect, index, file_path in rows:
            selected = index == self.selected_index
            focused = current_focus == ("file", index)
            fill = PANEL if selected else PANEL_ALT
            outline = TEXT if focused or selected else None
            draw.rounded_rectangle(rect, radius=10, fill=fill, outline=outline, width=1 if outline else 0)
            row_name = os.path.basename(file_path)
            mounted = self.data.get("mounted", "") == file_path
            size_text = format_size(self.size_for(file_path))
            name_fill = ACCENT if mounted else TEXT
            prefix = "* " if mounted else ""
            row_text = prefix + row_name
            if selected:
                self.draw_scrolling_text(canvas, (rect[0] + 8, rect[1], rect[0] + 216, rect[1] + 16), row_text, self.font_medium, name_fill, fill)
            else:
                draw.text((rect[0] + 8, rect[1] + 1), clip_to_width(draw, row_text, self.font_medium, 208), fill=name_fill, font=self.font_medium)
            draw.text((rect[2] - 56, rect[1] + 2), clip_to_width(draw, size_text, self.font_small, 48), fill=MUTED, font=self.font_small)

        total = len(files)
        page_text = "{0}/{1}".format(self.selected_index + 1, total)
        draw.text((258, 58), page_text, fill=MUTED, font=self.font_small)
        draw.text((216, 154), "Swipe right", fill=MUTED, font=self.font_small)

    def make_render_key(self):
        return (
            self.screen,
            tuple(self.data.get("files", [])),
            self.data.get("mounted", ""),
            self.data.get("cdrom", False),
            self.message,
            self.error,
            self.busy,
            self.focus_index,
            self.selected_index,
            self.scroll_offset,
            self.last_checked,
            self.marquee_tick() if self.should_scroll_main_name() else None,
            self.marquee_tick() if self.should_scroll_main_mounted() else None,
            self.marquee_tick() if self.should_scroll_library_name() else None,
        )

    def render(self):
        key = self.make_render_key()
        if key == self.render_key and self.render_cache is not None:
            return self.render_cache, False

        canvas = Image.new("RGB", (LOGICAL_WIDTH, LOGICAL_HEIGHT), BACKGROUND)
        draw = ImageDraw.Draw(canvas)

        if self.screen == "library":
            self.draw_library_screen(canvas, draw)
        else:
            self.draw_main_screen(canvas, draw)

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
    app = ImageMounterApp()

    try:
        while True:
            touch_state = touch.poll()
            knob_state = knob.poll()
            action = app.update(touch_state, knob_state)
            frame, changed = app.render()
            if changed:
                display.show_image(frame)
            if action == "exit":
                break
            time.sleep(0.1)
    finally:
        knob.close()
        touch.close()
        display.close()


if __name__ == "__main__":
    main()
