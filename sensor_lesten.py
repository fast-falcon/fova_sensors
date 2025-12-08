#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
مانیتور SHT30 (دما/رطوبت) و ADS1115 (بو/دود) روی CH341-I2C
- لیبل‌ها:  A<depth>D<idx> برای دما  |  A<depth>F<idx> برای دود
- نام‌گذاری ریشه‌ها پایدار (A,B, ...)
- continuous mode برای ADS1115
- rescan و toggle با backoff و رفتار محافظه‌کارانه
- ری‌اینیت داخلی به‌جای ری‌استارت پروسه (یک خط لاگ و شروع از اول داخل برنامه)
"""

import sys, os, time, subprocess
import usb.core, usb.util
from collections import deque, defaultdict
from statistics import mean
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Set

# ---------------------- تنظیمات ----------------------
VID, PID = 0x1A86, 0x5512  # CH341

ADDR_SHT30 = 0x44
ADDR_ADS   = 0x48

PGA_4V096 = False          # False => ±6.144V (LSB=0.1875mV) | True => ±4.096V (LSB=0.125mV)
ADS_DR = 0b100             # 160 SPS

BASELINE_WIN = 20
THRESH_VOLT = 0.10

LOG_INTERVAL = 1.0         # بازه‌ی چاپ
RESCAN_INTERVAL = 20.0     # بازه‌ی اسکن دوره‌ای USB (بیشتر=بهتر)
USB_TIMEOUT_MS = 1000
MAX_RETRIES = 3
RECOVER_COOLDOWN = 0.1

ERRORS_BEFORE_SENSOR_RESET = 3
ERRORS_BEFORE_TOGGLE = 6   # در این نسخه به‌جای toggle، ری‌اینیت داخلی می‌کنیم

# backoff نمایی برای toggle (حفظ شده ولی در ری‌اینیت استفاده نمی‌شود)
TOGGLE_BACKOFF_MIN = 60.0      # ثانیه
TOGGLE_BACKOFF_MAX = 600.0     # سقف تاخیر
PARENT_NUDGE_STALE = 120.0     # حداقل قدمت برای قلقلک والد

# CH341 I2C stream opcodes
CMD_I2C_STREAM = 0xAA
CMD_I2C_STM_STA = 0x74
CMD_I2C_STM_STO = 0x75
CMD_I2C_STM_END = 0x00
CMD_I2C_STM_OUT = 0x80
CMD_I2C_STM_IN  = 0xC0
CMD_I2C_STM_SET = 0x60

I2C_SPEED = 1  # 0≈20k,1=100k,2=400k,3≈750k

ADS_REG_CONV = 0x00
ADS_REG_CONF = 0x01

SHT_REP_DELAY = {"high": 0.020, "med": 0.010, "low": 0.006}
SHT_REP = {"high": (0x24, 0x00), "med": (0x24, 0x0B), "low": (0x24, 0x16)}
SHT_DEFAULT_REP = "med"

DISCONNECT_ERRNOS = {19, -19}                 # ENODEV
EPROTO_LIKE = {71, -71, 110, -110, 32, -32}   # EPROTO/ETIMEDOUT/EPIPE...

# ---------------------- کمک‌تابع‌ها ----------------------
def get_str(d, idx):
    try:
        return usb.util.get_string(d, idx) if idx else None
    except Exception:
        return None

def port_numbers(d: usb.core.Device) -> List[int]:
    pn = getattr(d, "port_numbers", None)
    try:
        if callable(pn):
            return pn() or []
        return pn or []
    except Exception:
        return []

def sysfs_path_for_dev(d: usb.core.Device) -> Optional[str]:
    try:
        bus = getattr(d, "bus", 0)
        ports = port_numbers(d)
        if not bus or not ports:
            return None
        path = f"{bus}-{ports[0]}"
        for p in ports[1:]:
            path += f".{p}"
        return path
    except Exception:
        return None

def parent_sysfs_path(sysfs_path: str) -> Optional[str]:
    if not sysfs_path or "." not in sysfs_path:
        return None
    return sysfs_path.rsplit(".", 1)[0]

def echo_root(cmd: str) -> int:
    return subprocess.call([cmd])

def toggle_authorized(sysfs_path: str, delay=0.5) -> bool:
    devdir = f"/sys/bus/usb/devices/{sysfs_path}"
    auth = os.path.join(devdir, "authorized")
    if not os.path.exists(auth):
        return False
    try:
        echo_root(f"echo 0 > {auth}")
        time.sleep(delay)
        echo_root(f"echo 1 > {auth}")
        return True
    except Exception:
        return False

def human_chain_letter(n: int) -> str:
    n -= 1
    s = ""
    while True:
        s = chr(ord('A') + (n % 26)) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s

def hub_identity_string(d: usb.core.Device) -> str:
    s = get_str(d, getattr(d, "iSerialNumber", 0))
    if s:
        return f"SER:{s}"
    m = get_str(d, getattr(d, "iManufacturer", 0)) or ""
    p = get_str(d, getattr(d, "iProduct", 0)) or ""
    if (m+p).strip():
        return f"MP:{m}|{p}"
    return f"ID:{d.idVendor:04x}:{d.idProduct:04x}"

# ---------------------- CH341Ctx ----------------------
class CH341Ctx:
    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.ep_out = None
        self.ep_in = None
        self.ep_int = None

        self.sysfs_path = sysfs_path_for_dev(dev)  # مثل 2-1.4.4.3
        self.chain_base = None  # مثل 2-1.4 (ریشه)
        self.chain_depth = 1
        self.chain_letter = "A"

        self._open()
        self._compute_chain_info()

    def _compute_chain_info(self):
        sp = self.sysfs_path
        if not sp:
            return
        bus, rest = sp.split("-", 1)
        segs = rest.split(".")
        if len(segs) >= 2:
            self.chain_base = f"{bus}-{segs[0]}.{segs[1]}"
            self.chain_depth = max(1, len(segs) - 2)
        else:
            self.chain_base = f"{bus}-{segs[0]}"
            self.chain_depth = 1

    def _drain_int(self):
        if not self.ep_int:
            return
        for _ in range(2):
            try:
                self.ep_int.read(self.ep_int.wMaxPacketSize, timeout=20)
            except usb.core.USBTimeoutError:
                break
            except usb.core.USBError:
                break

    def _open(self):
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except Exception:
            pass
        try:
            _ = self.dev.get_active_configuration()
        except usb.core.USBError:
            _ = None
        if _ is None:
            try:
                self.dev.set_configuration()
            except usb.core.USBError as e:
                if getattr(e, 'errno', None) != 16:
                    raise
        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]
        self.ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda e:
                usb.util.endpoint_direction(e.bEndpointAddress)==usb.util.ENDPOINT_OUT and
                usb.util.endpoint_type(e.bmAttributes)==usb.util.ENDPOINT_TYPE_BULK
        )
        self.ep_in = usb.util.find_descriptor(
            intf, custom_match=lambda e:
                usb.util.endpoint_direction(e.bEndpointAddress)==usb.util.ENDPOINT_IN and
                usb.util.endpoint_type(e.bmAttributes)==usb.util.ENDPOINT_TYPE_BULK
        )
        self.ep_int = usb.util.find_descriptor(
            intf, custom_match=lambda e:
                usb.util.endpoint_direction(e.bEndpointAddress)==usb.util.ENDPOINT_IN and
                usb.util.endpoint_type(e.bmAttributes)==usb.util.ENDPOINT_TYPE_INTR
        )
        if not self.ep_out or not self.ep_in:
            raise SystemExit("Bulk endpoints پیدا نشدند")
        self.set_i2c_speed(I2C_SPEED)
        self._drain_int()

    def clear_halts(self):
        try:
            usb.util.clear_halt(self.dev, self.ep_out.bEndpointAddress)
        except Exception:
            pass
        try:
            usb.util.clear_halt(self.dev, self.ep_in.bEndpointAddress)
        except Exception:
            pass

    def bulk_out(self, data: bytes):
        for _ in range(1, MAX_RETRIES+1):
            try:
                self.ep_out.write(data, timeout=USB_TIMEOUT_MS)
                return
            except usb.core.USBError as e:
                if getattr(e, 'errno', None) in DISCONNECT_ERRNOS:
                    raise
                self.clear_halts(); self._drain_int()
                time.sleep(RECOVER_COOLDOWN)
        raise

    def bulk_in(self, n: int) -> bytes:
        for _ in range(1, MAX_RETRIES+1):
            try:
                r = self.ep_in.read(n, timeout=USB_TIMEOUT_MS)
                return bytes(r)
            except usb.core.USBTimeoutError:
                self._drain_int()
            except usb.core.USBError as e:
                if getattr(e, 'errno', None) in DISCONNECT_ERRNOS:
                    raise
                self.clear_halts(); self._drain_int()
                time.sleep(RECOVER_COOLDOWN)
        raise

    def i2c_stream(self, chunks, read_sizes=()):
        payload = bytearray([CMD_I2C_STREAM])
        for ch in chunks:
            payload += bytes(ch)
        payload.append(CMD_I2C_STM_END)
        self.bulk_out(payload)
        reads = []
        for n in read_sizes:
            if n > 0:
                reads.append(self.bulk_in(n))
        return reads

    def set_i2c_speed(self, speed_code=1):
        self.i2c_stream([[CMD_I2C_STM_SET | (speed_code & 0x03)]])
        time.sleep(0.002)

    def i2c_write(self, addr7, data_bytes, send_stop=True):
        addr_w = (addr7 << 1) | 0
        chunks = [
            [CMD_I2C_STM_STA],
            [CMD_I2C_STM_OUT | (1 + len(data_bytes)), addr_w] + list(data_bytes),
        ]
        if send_stop:
            chunks.append([CMD_I2C_STM_STO])
        self.i2c_stream(chunks)
        self._drain_int(); time.sleep(0.001)

    def i2c_write_then_read(self, addr7, wr_bytes, rd_len) -> bytes:
        addr_w = (addr7 << 1) | 0
        addr_r = (addr7 << 1) | 1
        chunks = [
            [CMD_I2C_STM_STA],
            [CMD_I2C_STM_OUT | (1 + len(wr_bytes)), addr_w] + list(wr_bytes),
            [CMD_I2C_STM_STA],
            [CMD_I2C_STM_OUT | 0x01, addr_r],
            [CMD_I2C_STM_IN  | (rd_len & 0x1F)],
            [CMD_I2C_STM_STO],
        ]
        reads = self.i2c_stream(chunks, read_sizes=(rd_len,))
        self._drain_int(); time.sleep(0.001)
        return reads[0] if reads else b""

# ---------------------- SHT30 ----------------------
def crc8_sensirion(two_bytes):
    poly = 0x31
    crc = 0xFF
    for b in two_bytes:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if (crc & 0x80) else ((crc << 1) & 0xFF)
    return crc

class SHT30:
    def __init__(self, ctx: CH341Ctx, addr=ADDR_SHT30, rep=SHT_DEFAULT_REP):
        self.ctx = ctx
        self.addr = addr
        self.rep = rep
    def soft_reset(self):
        self.ctx.i2c_write(self.addr, bytes([0x30, 0xA2])); time.sleep(0.005)
    def read(self) -> Tuple[float, float]:
        cmd = SHT_REP[self.rep]
        self.ctx.i2c_write(self.addr, bytes([cmd[0], cmd[1]]))
        time.sleep(SHT_REP_DELAY[self.rep])
        data = self.ctx.i2c_write_then_read(self.addr, bytes([]), 6)
        if len(data) != 6:
            raise IOError(f"short read ({len(data)} B)")
        Traw = (data[0] << 8) | data[1]; RHraw = (data[3] << 8) | data[4]
        if crc8_sensirion(data[0:2]) != data[2]: raise IOError("Temp CRC fail")
        if crc8_sensirion(data[3:5]) != data[5]: raise IOError("RH CRC fail")
        temp = -45.0 + 175.0 * (Traw / 65535.0)
        rh = 100.0 * (RHraw / 65535.0)
        return temp, rh

# ---------------------- ADS1115 (continuous) ----------------------
class ADS1115:
    def __init__(self, ctx: CH341Ctx, addr=ADDR_ADS, pga_4v096=PGA_4V096):
        self.ctx = ctx; self.addr = addr; self.pga_4 = pga_4v096
        self.buf = deque(maxlen=BASELINE_WIN)
        self.continuous_started = False
        self.last_conf = 0

    def begin_continuous(self):
        # MUX=A0-GND (0b100), PGA, MODE=0 (continuous), DR=ADS_DR, COMP=disabled (0b11)
        conf = 0
        conf |= (0b100 << 12)                                   # MUX AIN0
        conf |= ((0b001 if self.pga_4 else 0b000) << 9)         # PGA
        conf |= (0 << 8)                                        # MODE=continuous
        conf |= (ADS_DR << 5)                                   # Data rate
        conf |= 0b11                                            # Comparator disabled
        conf |= (1 << 15)                                       # OS=1 (بی‌اثر در continuous، فقط یکبار می‌نویسیم)
        self.ctx.i2c_write(self.addr, bytes([ADS_REG_CONF, (conf >> 8) & 0xFF, conf & 0xFF]))
        self.last_conf = conf
        self.continuous_started = True
        time.sleep(0.02)  # یک تأخیر کوچک تا اولین نمونه‌ها پایدار شوند

    def reinit(self):
        self.continuous_started = False
        self.begin_continuous()

    def read_latest(self) -> Tuple[int, float, float]:
        if not self.continuous_started:
            self.begin_continuous()
        data = self.ctx.i2c_write_then_read(self.addr, bytes([ADS_REG_CONV]), 2)
        if len(data) != 2:
            time.sleep(0.005)
            data = self.ctx.i2c_write_then_read(self.addr, bytes([ADS_REG_CONV]), 2)
            if len(data) != 2:
                raise RuntimeError(f"Conversion read failed, got {len(data)} bytes")
        raw = (data[0] << 8) | data[1]
        if raw & 0x8000: raw -= 1 << 16
        lsb = 0.000125 if self.pga_4 else 0.0001875
        volts = raw * lsb
        self.buf.append(volts); base = mean(self.buf) if len(self.buf) else volts
        dv = volts - base
        return raw, volts, dv

# ---------------------- نام‌گذاری و اسکن ----------------------
def short_bus_addr(d) -> str:
    return f"{getattr(d,'bus',0)}:{getattr(d,'address',0)}"

def list_ch341_devices() -> List[usb.core.Device]:
    devs = list(usb.core.find(idVendor=VID, idProduct=PID, find_all=True) or [])
    devs.sort(key=lambda x: (getattr(x, "bus", 0), getattr(x, "address", 0)))
    return devs

def probe_sht30(ctx: CH341Ctx, addr=ADDR_SHT30) -> bool:
    try:
        cmd = SHT_REP[SHT_DEFAULT_REP]
        ctx.i2c_write(addr, bytes([cmd[0], cmd[1]])); time.sleep(SHT_REP_DELAY[SHT_DEFAULT_REP])
        data = ctx.i2c_write_then_read(addr, bytes([]), 6)
        if len(data) != 6: return False
        return (crc8_sensirion(data[0:2]) == data[2]) and (crc8_sensirion(data[3:5]) == data[5])
    except Exception:
        return False

def probe_ads1115(ctx: CH341Ctx, addr=ADDR_ADS) -> bool:
    try:
        # یک نوشتن سبک و سپس خواندن
        conf_w = (1<<15) | (0b100<<12) | (0b000<<9) | (0<<8) | (0b100<<5) | 0b11
        ctx.i2c_write(addr, bytes([ADS_REG_CONF, (conf_w >> 8) & 0xFF, conf_w & 0xFF])); time.sleep(0.002)
        rb = ctx.i2c_write_then_read(addr, bytes([ADS_REG_CONF]), 2)
        if len(rb) != 2: return False
        conf_r = (rb[0] << 8) | rb[1]; mask = (1<<8) | (0b111<<5) | 0b11
        return (conf_r & mask) == (conf_w & mask)
    except Exception:
        return False

root_letter_by_identity: Dict[str, str] = {}
root_identity_seen_order: List[str] = []
last_seen_device_parents: Dict[str, float] = {}  # parent sysfs path → last_seen_time

def build_root_letter_map(all_devs: List[usb.core.Device]) -> Dict[str, str]:
    base_rows = []
    for d in all_devs:
        sp = sysfs_path_for_dev(d)
        if not sp:
            continue
        bus, rest = sp.split("-", 1); segs = rest.split(".")
        if len(segs) >= 2:
            base = f"{bus}-{segs[0]}.{segs[1]}"
        else:
            base = f"{bus}-{segs[0]}"
        ident = hub_identity_string(d)
        base_rows.append((base, ident))

    base_to_identities: Dict[str, Set[str]] = defaultdict(set)
    for base, ident in base_rows:
        base_to_identities[base].add(ident)

    base_identity_pairs = []
    for base in sorted(base_to_identities.keys()):
        ident = sorted(list(base_to_identities[base]))[0]
        base_identity_pairs.append((base, ident))

    identities = sorted({ident for _, ident in base_identity_pairs})
    for ident in identities:
        if ident not in root_letter_by_identity:
            root_identity_seen_order.append(ident)
            letter = human_chain_letter(len(root_identity_seen_order))
            root_letter_by_identity[ident] = letter

    base_to_letter: Dict[str, str] = {}
    for base, ident in base_identity_pairs:
        base_to_letter[base] = root_letter_by_identity[ident]
    return base_to_letter

class SensorRef:
    def __init__(self, kind: str, index: int, ctx: CH341Ctx, sensor_obj, key: str):
        self.kind = kind     # 'temp' یا 'gas'
        self.index = index
        self.ctx = ctx
        self.obj = sensor_obj
        self.key = key
        self.consec_errors = 0
        self.just_added = False
        self.last_error_ts = 0.0

    def hub_name(self) -> str:
        return f"{self.ctx.chain_letter}{max(1, self.ctx.chain_depth)}"

    def label(self) -> str:
        tletter = "D" if self.kind == 'temp' else "F"
        return f"{self.hub_name()}{tletter}{self.index}"

def build_sensor_map(existing: Dict[str, SensorRef],
                     base_to_letter: Dict[str, str],
                     ads_addr=ADDR_ADS, sht_addr=ADDR_SHT30):
    devs = list_ch341_devices()
    new_map: Dict[str, SensorRef] = {}
    added_temp: List[str] = []
    added_gas: List[str] = []

    for d in devs:
        key_dev = short_bus_addr(d)
        try:
            matching = [sr for sr in existing.values() if sr.key.startswith(key_dev + "/")]
            if matching:
                ctx = matching[0].ctx
            else:
                ctx = CH341Ctx(d)
        except Exception:
            continue

        ctx.chain_letter = base_to_letter.get(ctx.chain_base or "", "A")

        parent = parent_sysfs_path(ctx.sysfs_path or "") or (ctx.sysfs_path or "")
        if parent:
            last_seen_device_parents[parent] = time.time()

        has_temp = probe_sht30(ctx, sht_addr)
        has_gas  = probe_ads1115(ctx, ads_addr)

        if has_temp:
            key = f"{key_dev}/temp@{sht_addr:02X}"
            if key in existing:
                sref = existing[key]; sref.ctx = ctx
                new_map[key] = sref
            else:
                sref = SensorRef('temp', 0, ctx, SHT30(ctx, addr=sht_addr), key)
                sref.just_added = True
                new_map[key] = sref; added_temp.append(key)

        if has_gas:
            key = f"{key_dev}/gas@{ads_addr:02X}"
            if key in existing:
                sref = existing[key]; sref.ctx = ctx
                if isinstance(sref.obj, ADS1115):
                    try: sref.obj.begin_continuous()
                    except Exception: pass
                new_map[key] = sref
            else:
                ads = ADS1115(ctx, addr=ads_addr, pga_4v096=PGA_4V096)
                try: ads.begin_continuous()
                except Exception: pass
                sref = SensorRef('gas', 0, ctx, ads, key)
                sref.just_added = True
                new_map[key] = sref; added_gas.append(key)

    # اندیس‌گذاری به ازای هر هاب
    groups = defaultdict(list)
    def hub_name_of_key(kk: str) -> str:
        sref = new_map[kk]
        return f"{sref.ctx.chain_letter}{max(1, sref.ctx.chain_depth)}"
    for kk, sref in new_map.items():
        groups[(hub_name_of_key(kk), sref.kind)].append(kk)
    for (hub_name, kind), key_list in groups.items():
        key_list.sort(key=lambda kk: (
            new_map[kk].ctx.chain_letter,
            new_map[kk].ctx.chain_depth,
            kk
        ))
        for idx, kk in enumerate(key_list, start=1):
            new_map[kk].index = idx

    return new_map, added_temp, added_gas

# ---------------------- حلقه‌ی اصلی ----------------------
def main():
    import argparse
    ap = argparse.ArgumentParser(description="مانیتور CH341 + SHT30/ADS1115 (continuous ADS) با لیبل‌های A1D1 / A2F1 و ریکاوری محافظه‌کارانه (ری‌اینیت داخلی)")
    ap.add_argument("--sht-addr", default="0x44", help="آدرس SHT30 (پیش‌فرض 0x44)")
    ap.add_argument("--ads-addr", default="0x48", help="آدرس ADS1115 (پیش‌فرض 0x48)")
    ap.add_argument("--i2c-speed", type=int, default=1, help="سرعت I2C: 0≈20k,1=100k,2=400k,3≈750k")
    ap.add_argument("--rep", choices=["low","med","high"], default=SHT_DEFAULT_REP, help="دقت single-shot SHT30")
    ap.add_argument("--log-interval", type=float, default=LOG_INTERVAL, help="بازه‌ی چاپ")
    ap.add_argument("--rescan", type=float, default=RESCAN_INTERVAL, help="بازه‌ی اسکن مجدد USB")
    ap.add_argument("--quiet", action="store_true", help="کم‌حرف")
    args = ap.parse_args()

    global I2C_SPEED
    I2C_SPEED = args.i2c_speed

    try:
        ads_addr = int(args.ads_addr, 0)
    except Exception:
        ads_addr = ADDR_ADS
    try:
        sht_addr = int(args.sht_addr, 0)
    except Exception:
        sht_addr = ADDR_SHT30

    sensors: Dict[str, SensorRef] = {}

    all_devs = list(usb.core.find(find_all=True) or [])
    base_to_letter = build_root_letter_map(all_devs)
    sensors, added_t, added_g = build_sensor_map(sensors, base_to_letter, ads_addr=ads_addr, sht_addr=sht_addr)

    n_temp = sum(1 for s in sensors.values() if s.kind=='temp')
    n_gas  = sum(1 for s in sensors.values() if s.kind=='gas')
    print(f"Found: Temp={n_temp}  Gas={n_gas}")

    last_rescan = time.time()
    last_log = 0.0
    force_rescan = False

    # backoff وضعیت toggle برای هر والد (حفظ، اما با ری‌اینیت داخلی ریست می‌شود)
    toggle_state: Dict[str, Dict[str, float]] = {}

    def can_toggle(parent: str, now: float) -> bool:
        st = toggle_state.get(parent, {"next": 0.0, "attempts": 0})
        return now >= st["next"]

    def mark_toggle(parent: str, now: float, ok: bool):
        st = toggle_state.get(parent, {"next": 0.0, "attempts": 0})
        att = st["attempts"] + 1
        delay = min(TOGGLE_BACKOFF_MIN * (2 ** (att-1)), TOGGLE_BACKOFF_MAX)
        toggle_state[parent] = {"next": now + delay, "attempts": att if not ok else 0}

    def other_sensor_recent_error(sref: SensorRef, horizon=5.0) -> bool:
        hub = sref.hub_name()
        now = time.time()
        for kk, vv in sensors.items():
            if vv is sref:
                continue
            if vv.hub_name() == hub and (now - vv.last_error_ts) <= horizon:
                return True
        return False

    # ——— ری‌اینیت داخلی (شبیه ری‌استارت) ———
    did_soft_restart_guard_until = 0.0
    def soft_restart(reason: str):
        """
        همهٔ stateها را تا جای ممکن مثل شروع برنامه ری‌اینیت می‌کند
        و فقط یک خط لاگ چاپ می‌کند.
        """
        nonlocal sensors, n_temp, n_gas, last_rescan, force_rescan, base_to_letter, toggle_state, did_soft_restart_guard_until
        now = time.time()
        # جلوگیری از ری‌اینیت‌های پشت‌سرهم:
        if now < did_soft_restart_guard_until:
            return
        did_soft_restart_guard_until = now + 2.0  # ۲ ثانیه گارد

        ts = datetime.now().strftime("%m/%d/%Y, %H:%M:%S")
        print(f"{ts}:  !! {reason} — ری‌اینیت داخلی و شروع از ابتدا ...", flush=True)

        # آزاد کردن منابع USB قدیمی
        try:
            for s in list(sensors.values()):
                try:
                    usb.util.dispose_resources(s.ctx.dev)
                except Exception:
                    pass
        except Exception:
            pass

        # پاک کردن stateهای سراسری نام‌گذاری/مشاهده
        try:
            root_letter_by_identity.clear()
            root_identity_seen_order.clear()
            last_seen_device_parents.clear()
        except Exception:
            pass
        toggle_state.clear()

        # پاک کردن سنسورها و اسکن از نو
        sensors = {}
        time.sleep(0.2)  # فاصلهٔ کوتاه

        all_devs_local = list(usb.core.find(find_all=True) or [])
        base_to_letter = build_root_letter_map(all_devs_local)
        sensors, _at, _ag = build_sensor_map(sensors, base_to_letter, ads_addr=ads_addr, sht_addr=sht_addr)

        n_temp = sum(1 for s in sensors.values() if s.kind=='temp')
        n_gas  = sum(1 for s in sensors.values() if s.kind=='gas')

        last_rescan = time.time()
        force_rescan = False

    def periodic_parent_nudge(now: float):
        # قلقلک خیلی محافظه‌کارانه و با backoff (حفظ شده)
        for parent, ts in list(last_seen_device_parents.items()):
            if now - ts > PARENT_NUDGE_STALE and can_toggle(parent, now):
                ok = toggle_authorized(parent)
                mark_toggle(parent, now, ok)
                if ok:
                    last_seen_device_parents[parent] = now
                    nonlocal force_rescan
                    force_rescan = True

    try:
        while True:
            now = time.time()
            did_soft_restart = False  # برای پرش از ادامهٔ iteration بعد از ری‌اینیت

            # اسکن دوره‌ای USB و بازسازی نام‌گذاری (یا اگر force_rescan)
            if force_rescan or (now - last_rescan >= args.rescan):
                all_devs = list(usb.core.find(find_all=True) or [])
                base_to_letter = build_root_letter_map(all_devs)
                prev_temp, prev_gas = n_temp, n_gas

                # برای تشخیص غیبت سنسور
                prev_sensors = sensors
                prev_keys = set(prev_sensors.keys())

                sensors, added_t, added_g = build_sensor_map(sensors, base_to_letter, ads_addr=ads_addr, sht_addr=sht_addr)

                new_keys = set(sensors.keys())
                removed_keys = list(prev_keys - new_keys)

                if removed_keys:
                    lost_labels = []
                    for rk in removed_keys:
                        try:
                            lost_labels.append(prev_sensors[rk].label())
                        except Exception:
                            pass
                    lost_labels.sort()
                    reason = f"سنسور{'ها' if len(lost_labels)>1 else ''} {'، '.join(lost_labels)} قطع شد"
                    soft_restart(reason)
                    did_soft_restart = True

                if did_soft_restart:
                    # iteration تازه را شروع کن تا state جدید استفاده شود
                    continue

                n_temp = sum(1 for s in sensors.values() if s.kind=='temp')
                n_gas  = sum(1 for s in sensors.values() if s.kind=='gas')

                msgs = []
                if (n_temp != prev_temp) or (n_gas != prev_gas):
                    if added_t:
                        items = [sensors[k].label() for k in added_t if k in sensors]
                        msgs.append("Added: " + " ".join(sorted(items)))
                    if added_g:
                        items = [sensors[k].label() for k in added_g if k in sensors]
                        msgs.append("Added: " + " ".join(sorted(items)))
                if msgs:
                    print(" | ".join(msgs))
                periodic_parent_nudge(now)
                last_rescan = now
                force_rescan = False

            fields: List[str] = []

            # دما (D)
            for k in sorted([k for k,v in sensors.items() if v.kind=='temp'],
                            key=lambda kk: (sensors[kk].ctx.chain_letter, sensors[kk].ctx.chain_depth, sensors[kk].index)):
                sref = sensors[k]; sht: SHT30 = sref.obj
                if sref.just_added:
                    sref.just_added = False
                    fields.append(f"{sref.label()}: --")
                    continue
                try:
                    t,h = sht.read()
                    fields.append(f"{sref.label()}: {t:.2f}°C {h:.1f}%RH")
                    sref.consec_errors = 0
                except Exception as e:
                    sref.consec_errors += 1
                    sref.last_error_ts = now
                    fields.append(f"{sref.label()}: nan")
                    if not args.quiet:
                        print(f"[{sref.label()}] read error {sref.consec_errors}: {e}", file=sys.stderr)
                    if sref.consec_errors == ERRORS_BEFORE_SENSOR_RESET:
                        try: sht.soft_reset()
                        except Exception: pass
                    elif sref.consec_errors >= ERRORS_BEFORE_TOGGLE:
                        # به‌جای toggle/USB-reset، ری‌اینیت داخلی کامل
                        soft_restart(f"سنسور {sref.label()} چندبار خطا داد")
                        did_soft_restart = True
                        break  # از حلقهٔ سنسورها بیرون برو تا iteration تازه شروع شود

            if did_soft_restart:
                continue

            # دود (F) - continuous
            for k in sorted([k for k,v in sensors.items() if v.kind=='gas'],
                            key=lambda kk: (sensors[kk].ctx.chain_letter, sensors[kk].ctx.chain_depth, sensors[kk].index)):
                sref = sensors[k]; ads: ADS1115 = sref.obj
                if sref.just_added:
                    sref.just_added = False
                    fields.append(f"{sref.label()}: --")
                    continue
                try:
                    raw, v, dv = ads.read_latest()
                    mark = " [HIGH]" if dv > THRESH_VOLT else ""
                    fields.append(f"{sref.label()}: V={v:.3f} Δ={dv:+.3f}{mark}")
                    sref.consec_errors = 0
                except Exception as e:
                    sref.consec_errors += 1
                    sref.last_error_ts = now
                    fields.append(f"{sref.label()}: nan")
                    if not args.quiet:
                        print(f"[{sref.label()}] read error {sref.consec_errors}: {e}", file=sys.stderr)
                    if sref.consec_errors == ERRORS_BEFORE_SENSOR_RESET:
                        try: ads.reinit()
                        except Exception: pass
                    elif sref.consec_errors >= ERRORS_BEFORE_TOGGLE:
                        soft_restart(f"سنسور {sref.label()} چندبار خطا داد")
                        did_soft_restart = True
                        break

            if did_soft_restart:
                continue

            if now - last_log >= args.log_interval:
                if fields:
                    ts = datetime.now().strftime("%m/%d/%Y, %H:%M:%S")
                    print(f"{ts}:  ) " +  "  ".join(fields), flush=True)
                last_log = now

            sleep_need = args.log_interval - (time.time() - last_log)
            time.sleep(0.01 if sleep_need < 0 else min(0.05, sleep_need))

    except KeyboardInterrupt:
        print("\nخروج توسط کاربر.")

if __name__ == "__main__":
    main()

