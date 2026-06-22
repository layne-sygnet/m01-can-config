#!/usr/bin/env python3
"""
m01config.py - configure an STW M01-CAN pressure transmitter without PEAK hardware.

Reverse-engineered connect handshake from M01_CANfigurator.exe:

  1. ABGLEICH flood — send "ABGLEICH" on 0x051 DLC=8 during sensor boot
  2. Node scan     — send [addr, 0x41] DLC=2 for addr 0x00..0xFE
  3. CompanyID     — send [node, 0x40, 0x06, 0x2A, 0x59] DLC=5
  4. Config mode   — standard EEPROM read/write with FIX bytes 0xFD 0x0A

After connect, EEPROM protocol:

    request  CAN ID 0x051 :  [NODE, 0xFD, 0x0A, CMD, addrLo, addrHi, value, 0x00]
    response CAN ID 0x052 :  echo with value at data[5]

    CMD 0x21 = write EEPROM byte     CMD 0x22 = read EEPROM byte
    CMD 0x24 = device info/firmware   CMD 0x44 = alive / pressure echo

Transport uses python-can (socketcan, slcan, pcan, kvaser, etc.)

    pip install python-can pyserial

Examples (Kvaser on socketcan):

    # connect and dump all parameters (power-cycle sensor when prompted)
    sudo python3 m01config.py -i socketcan -c can1 dump

    # set J1939 PGN / SPN / repetition rate / priority
    sudo python3 m01config.py -i socketcan -c can1 set --pgn 0xFF00 --spn 1234 --trr 100 --priority 6

    # raw single-byte read/write
    sudo python3 m01config.py -i socketcan -c can1 read  --addr 0x9D --size word
    sudo python3 m01config.py -i socketcan -c can1 write --addr 0xA5 --value 6 --size byte

NOTE: only ONE M01-CAN may be on the bus while configuring. After writing,
power-cycle the sensor so it boots with the new EEPROM values.
"""

import argparse
import sys
import time

try:
    import can
except ImportError:
    sys.exit("python-can is required:  pip install python-can pyserial")

REQ_ID = 0x051       # host -> sensor
RESP_ID = 0x052      # sensor -> host
FIX1, FIX2 = 0xFD, 0x0A

CMD_WRITE = 0x21
CMD_READ = 0x22
CMD_INFO = 0x24
CMD_CHECKSUM = 0x41   # "Update Checksums" — must be sent after EEPROM writes
CMD_ALIVE = 0x44

# EEPROM map: name -> (address, size_in_bytes)  (see M01_protocol_findings.md)
PARAMS = {
    # J1939 message config
    "pgn":            (0x9D, 2),
    "spn":            (0x9F, 2),
    "slot":           (0xA1, 1),
    "trr":            (0xA2, 2),   # transmission repetition rate, ms
    "data_length":    (0xA4, 1),
    "priority":       (0xA5, 1),
    "edp":            (0xA6, 1),
    "dp":             (0xA7, 1),
    "data_position":  (0xA8, 1),
    "src_addr_start": (0x91, 1),   # J1939 mode (overlaps CanID in STW-CAN mode)
    "src_addr_range": (0x92, 1),
    "span_enable":    (0xB1, 1),
    "span_min":       (0xB2, 4),
    "span_max":       (0xB6, 4),
    "filter_type":    (0x41, 1),
    "filter_const":   (0x42, 1),
    # STW-CAN config
    "can_bitrate_hdr":(0x24, 2),
    "can_id":         (0x91, 4),
    "can_type":       (0x95, 1),
    "can_bitrate":    (0x96, 2),
    "controlbyte":    (0x98, 1),
    "interval":       (0x99, 2),
    "pressure_prop":  (0x9B, 1),
    # read-only device info
    "pressure_low":   (0x51, 2),
    "pressure_high":  (0x53, 2),
    "adc_low":        (0x55, 2),
    "adc_high":       (0x57, 2),
}

# order used by 'dump'
DUMP_ORDER = [
    "pgn", "spn", "slot", "trr", "data_length", "priority", "edp", "dp",
    "data_position", "src_addr_start", "src_addr_range",
    "span_enable", "span_min", "span_max", "filter_type", "filter_const",
    "pressure_low", "pressure_high", "adc_low", "adc_high",
]


ABGLEICH = bytes([0x41, 0x42, 0x47, 0x4C, 0x45, 0x49, 0x43, 0x48])


class M01:
    def __init__(self, bus, node=0x01, timeout=0.1, verbose=False):
        self.bus = bus
        self.node = node
        self.timeout = timeout
        self.verbose = verbose
        self.connected = False

    # ---- low level -------------------------------------------------------
    def _send(self, data):
        """Send CAN frame with exact DLC."""
        msg = can.Message(arbitration_id=REQ_ID, is_extended_id=False,
                          data=bytes(data), dlc=len(data))
        if self.verbose:
            print(f"  TX 051 [{len(data)}]: {' '.join(f'{b:02X}' for b in data)}")
        for attempt in range(5):
            try:
                self.bus.send(msg)
                return True
            except can.CanError:
                time.sleep(0.005 * (attempt + 1))
        return False

    def _recv(self, timeout=None):
        """Wait for a standard 0x052 response."""
        t = timeout if timeout is not None else self.timeout
        deadline = time.time() + t
        while time.time() < deadline:
            r = self.bus.recv(timeout=max(0.001, deadline - time.time()))
            if r and r.arbitration_id == RESP_ID and not r.is_extended_id:
                if self.verbose:
                    d = ' '.join(f'{b:02X}' for b in r.data[:r.dlc])
                    print(f"  RX 052 [{r.dlc}]: {d}")
                return r
        return None

    def _drain(self):
        while self.bus.recv(timeout=0.01) is not None:
            pass

    def _txrx(self, data, timeout=None):
        self._send(data)
        return self._recv(timeout)

    def alive(self):
        return self._txrx([self.node, FIX1, FIX2, CMD_ALIVE, 0x00])

    def read_byte(self, addr):
        r = self._txrx([self.node, FIX1, FIX2, CMD_READ,
                        addr & 0xFF, (addr >> 8) & 0xFF, 0x00, 0x00])
        if r is None:
            raise IOError(f"no reply reading 0x{addr:04X}")
        return r.data[5]

    def write_byte(self, addr, value):
        r = self._txrx([self.node, FIX1, FIX2, CMD_WRITE,
                        addr & 0xFF, (addr >> 8) & 0xFF, value & 0xFF, 0x00])
        if r is None:
            raise IOError(f"no reply writing 0x{addr:04X}")
        return r

    def read_value(self, addr, size):
        v = 0
        for i in range(size):
            v = (v << 8) | self.read_byte(addr + i)
        return v

    def write_value(self, addr, value, size):
        for i in range(size):
            shift = 8 * (size - 1 - i)
            self.write_byte(addr + i, (value >> shift) & 0xFF)

    # ---- connect handshake -----------------------------------------------
    def enter_config(self):
        """Full connect handshake: ABGLEICH → node scan → CompanyID."""
        self._drain()

        input("  Unplug the sensor, press ENTER, then plug it back in within 5 seconds...")
        print("  Sending ABGLEICH — plug the sensor in NOW...")

        # Phase 1: ABGLEICH flood
        t0 = time.time()
        count = 0
        while time.time() - t0 < 5.0:
            self._send(ABGLEICH)
            count += 1
            time.sleep(0.010)
        print(f"  Sent {count} ABGLEICH frames.")

        # Phase 2: flush + settle
        self._drain()
        time.sleep(0.200)
        self._drain()

        # Phase 3: node scan
        print("  Scanning for node...")
        for n in range(0xFF):
            self._send([n, 0x41])
            time.sleep(0.002)

        # Phase 4: wait for wakeup response
        r = self._recv(timeout=0.500)
        if r and r.dlc >= 2 and r.data[1] == 0x41:
            self.node = r.data[0]
            print(f"  Found node 0x{self.node:02X}")
        elif r:
            self.node = r.data[0]
            print(f"  Response (unexpected data[1]=0x{r.data[1]:02X}), using node 0x{self.node:02X}")
        else:
            print(f"  No wakeup response. Using node 0x{self.node:02X} as fallback.")
            self.connected = False
            return False

        # drain extra responses
        self._drain()

        # Phase 5: wakeup confirm
        r = self._txrx([self.node, 0x41], timeout=0.100)
        if self.verbose and r:
            print(f"  WakeUp confirm OK")

        # Phase 6: CompanyID
        r = self._txrx([self.node, 0x40, 0x06, 0x2A, 0x59], timeout=0.100)
        if r and r.dlc >= 2 and r.data[1] == 0x40:
            print(f"  CompanyID accepted — config mode active!")
            self.connected = True
            return True
        else:
            print(f"  CompanyID failed.")
            self.connected = False
            return False

    def device_id(self):
        return "".join(chr(self.read_byte(0x0C + i)) for i in range(4))

    def firmware_version(self):
        r = self._txrx([self.node, FIX1, FIX2, CMD_INFO, 0x00])
        if r and r.dlc >= 7:
            return (r.data[5], r.data[6])
        return None

    def update_checksums(self):
        """CMD 0x41 — must be called after EEPROM writes to persist changes."""
        return self._txrx([self.node, FIX1, FIX2, CMD_CHECKSUM, 0x00])

    def get_param(self, name):
        addr, size = PARAMS[name]
        return self.read_value(addr, size)

    def set_param(self, name, value):
        addr, size = PARAMS[name]
        self.write_value(addr, value, size)


def open_bus(args):
    kwargs = dict(interface=args.interface, channel=args.channel)
    # python-can slcan/serial backends take bitrate; socketcan is preconfigured
    if args.bitrate and args.interface not in ("socketcan",):
        kwargs["bitrate"] = args.bitrate
    # Filter to only receive 0x052 replies; without this, the J1939 data stream
    # floods bus.recv() and the config responses get lost in the noise.
    kwargs["can_filters"] = [{"can_id": RESP_ID, "can_mask": 0x1FFFFFFF}]
    return can.Bus(**kwargs)


def cmd_scan(m01, args):
    """Connect via full handshake and report sensor info."""
    if not m01.enter_config():
        print("No sensor found. Check wiring, bitrate, termination, and that you "
              "power-cycled within the ABGLEICH window.")
        return 1
    print(f"  Node: 0x{m01.node:02X}")
    try:
        print(f"  Device-ID: {m01.device_id()!r}")
    except Exception as e:
        print(f"  (could not read device id: {e})")
    fw = m01.firmware_version()
    if fw:
        print(f"  Firmware: 0x{fw[0]:02X} 0x{fw[1]:02X}")
    r = m01.alive()
    if r:
        d = ' '.join(f'{b:02X}' for b in r.data[:r.dlc])
        print(f"  Alive: [{r.dlc}] {d}")
    return 0


def cmd_dump(m01, args):
    if not m01.enter_config():
        print("Sensor not responding. Retry and power-cycle within the ABGLEICH window.")
        return 1
    try:
        print(f"Device-ID : {m01.device_id()!r}")
    except Exception as e:
        print(f"Device-ID : <err {e}>")
    fw = m01.firmware_version()
    if fw:
        print(f"Firmware  : 0x{fw[0]:02X} 0x{fw[1]:02X}")
    for name in DUMP_ORDER:
        addr, size = PARAMS[name]
        try:
            v = m01.read_value(addr, size)
            print(f"  {name:<15} @0x{addr:02X}/{size}  = {v}  (0x{v:0{size*2}X})")
        except Exception as e:
            print(f"  {name:<15} @0x{addr:02X}/{size}  <err {e}>")
    return 0


def cmd_read(m01, args):
    if not m01.enter_config():
        print("Sensor not responding."); return 1
    size = {"byte": 1, "word": 2, "dword": 4}[args.size]
    v = m01.read_value(args.addr, size)
    print(f"0x{args.addr:04X}/{args.size} = {v} (0x{v:0{size*2}X})")
    return 0


def cmd_write(m01, args):
    if not m01.enter_config():
        print("Sensor not responding."); return 1
    size = {"byte": 1, "word": 2, "dword": 4}[args.size]
    m01.write_value(args.addr, args.value, size)
    m01.update_checksums()
    rb = m01.read_value(args.addr, size)
    ok = "OK" if rb == args.value else "MISMATCH"
    print(f"wrote 0x{args.value:X} -> 0x{args.addr:04X}/{args.size}; readback 0x{rb:X} [{ok}]")
    return 0 if rb == args.value else 2


def cmd_set(m01, args):
    if not m01.enter_config():
        print("Sensor not responding."); return 1
    todo = {k: getattr(args, k) for k in
            ("pgn", "spn", "slot", "trr", "data_length", "priority", "edp", "dp",
             "data_position", "src_addr_start", "src_addr_range", "span_enable",
             "span_min", "span_max", "filter_type", "filter_const")
            if getattr(args, k) is not None}
    if not todo:
        print("Nothing to set. Pass one or more of --pgn --spn --trr ... (see -h)")
        return 1
    for name, val in todo.items():
        m01.set_param(name, val)
        rb = m01.get_param(name)
        ok = "OK" if rb == val else "MISMATCH"
        print(f"  {name:<15} = {val} (0x{val:X}); readback {rb} [{ok}]")
    m01.update_checksums()
    print("Done. Power-cycle the sensor to apply the new EEPROM configuration.")
    return 0


def cmd_rawdump(m01, args):
    """Read every EEPROM byte in a range and display as hex dump."""
    if not m01.enter_config():
        print("Sensor not responding."); return 1
    try:
        print(f"Device-ID : {m01.device_id()!r}")
    except Exception:
        pass
    start = args.start_addr
    end = args.end_addr
    print(f"Reading EEPROM 0x{start:02X}..0x{end:02X}:")
    row = []
    for addr in range(start, end + 1):
        if addr % 16 == 0:
            if row:
                ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in row)
                print(f"  |{ascii_str}|")
                row = []
            print(f"  {addr:04X}: ", end="")
        try:
            v = m01.read_byte(addr)
            print(f" {v:02X}", end="")
            row.append(v)
        except IOError:
            print(" --", end="")
            row.append(0)
    if row:
        pad = 16 - len(row)
        print("   " * pad, end="")
        ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in row)
        print(f"  |{ascii_str}|")
    print()
    return 0


MENU_PARAMS = [
    ("J1939 Message Config", [
        ("pgn",            "PGN"),
        ("spn",            "SPN"),
        ("slot",           "Slot"),
        ("trr",            "Transmission Rate (ms)"),
        ("data_length",    "Data Length"),
        ("priority",       "Priority (0-7)"),
        ("edp",            "Ext. Data Page"),
        ("dp",             "Data Page"),
        ("data_position",  "Data Position (0-7)"),
        ("src_addr_start", "Source Addr Start"),
        ("src_addr_range", "Source Addr Range"),
    ]),
    ("Data Range", [
        ("span_enable",    "Span Enable (0/1)"),
        ("span_min",       "Span Min (0%)"),
        ("span_max",       "Span Max (100%)"),
    ]),
    ("Filter", [
        ("filter_type",    "Filter Type (0=none,1=mov,2=rep)"),
        ("filter_const",   "Filter Constant (0-255)"),
    ]),
    ("STW-CAN Config", [
        ("can_bitrate_hdr", "CAN Bitrate (header)"),
        ("can_id",          "CAN ID"),
        ("can_type",        "CAN Type"),
        ("can_bitrate",     "CAN Bitrate"),
        ("controlbyte",     "Controlbyte"),
        ("interval",        "Interval (ms)"),
        ("pressure_prop",   "Pressure Property"),
    ]),
]

READONLY_PARAMS = [
    ("pressure_low",  "Pressure Range Low"),
    ("pressure_high", "Pressure Range High"),
    ("adc_low",       "ADC Range Low"),
    ("adc_high",      "ADC Range High"),
]


def _read_all_params(m01):
    values = {}
    for _, params in MENU_PARAMS:
        for key, _ in params:
            try:
                values[key] = m01.get_param(key)
            except IOError:
                values[key] = None
    for key, _ in READONLY_PARAMS:
        try:
            values[key] = m01.get_param(key)
        except IOError:
            values[key] = None
    return values


def _print_menu(device_id, fw, values):
    print()
    fw_str = f"FW 0x{fw[0]:02X}.0x{fw[1]:02X}" if fw else "FW unknown"
    print(f"  STW M01-CAN — Device '{device_id}', {fw_str}")
    print(f"  {'=' * 58}")

    num = 1
    index = []
    for group_name, params in MENU_PARAMS:
        print(f"\n  {group_name}:")
        for key, label in params:
            addr, size = PARAMS[key]
            v = values.get(key)
            if v is not None:
                print(f"  {num:>2}) {label:<32} = {v:<8} (0x{v:0{size*2}X})")
            else:
                print(f"  {num:>2}) {label:<32} = <read error>")
            index.append(key)
            num += 1

    print(f"\n  Read-Only:")
    for key, label in READONLY_PARAMS:
        addr, size = PARAMS[key]
        v = values.get(key)
        if v is not None:
            print(f"      {label:<32} = {v:<8} (0x{v:0{size*2}X})")
        else:
            print(f"      {label:<32} = <read error>")

    print()
    return index


def cmd_menu(m01, args):
    if not m01.enter_config():
        print("Sensor not responding."); return 1

    try:
        device_id = m01.device_id()
    except IOError:
        device_id = "????"
    fw = m01.firmware_version()

    print("  Reading parameters...")
    values = _read_all_params(m01)
    index = _print_menu(device_id, fw, values)

    while True:
        try:
            choice = input("  Enter number to edit, 'r' to re-read, 'q' to quit: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice.lower() == 'q':
            break

        if choice.lower() == 'r':
            print("  Re-reading...")
            values = _read_all_params(m01)
            index = _print_menu(device_id, fw, values)
            continue

        try:
            n = int(choice)
        except ValueError:
            print("  Invalid input.")
            continue

        if n < 1 or n > len(index):
            print(f"  Pick 1-{len(index)}.")
            continue

        key = index[n - 1]
        addr, size = PARAMS[key]
        label = None
        for _, params in MENU_PARAMS:
            for k, l in params:
                if k == key:
                    label = l
                    break

        cur = values.get(key)
        cur_str = f"{cur} (0x{cur:0{size*2}X})" if cur is not None else "<unknown>"
        try:
            raw = input(f"  {label} [{cur_str}] new value (hex ok, blank=cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            continue

        if not raw:
            continue

        try:
            new_val = int(raw, 0)
        except ValueError:
            print("  Invalid number.")
            continue

        max_val = (1 << (size * 8)) - 1
        if new_val < 0 or new_val > max_val:
            print(f"  Value out of range (0-{max_val} / 0x{max_val:X}).")
            continue

        try:
            m01.set_param(key, new_val)
            m01.update_checksums()
            rb = m01.get_param(key)
            values[key] = rb
            ok = "OK" if rb == new_val else "MISMATCH"
            print(f"  Wrote {key} = {new_val} (0x{new_val:X}), readback {rb} [{ok}]")
        except IOError as e:
            print(f"  Write failed: {e}")

    print("  Power-cycle the sensor to apply changes.")
    return 0


def auto_int(x):
    return int(x, 0)


def main():
    p = argparse.ArgumentParser(description="Configure an STW M01-CAN over plain CAN (no PEAK needed).")
    p.add_argument("-i", "--interface", default="slcan",
                  help="python-can interface: slcan (CANable/USBtin), pcan, kvaser, vector, socketcan, ... (default slcan)")
    p.add_argument("-c", "--channel", required=True,
                  help="adapter channel: e.g. COM5 / /dev/ttyACM0 (slcan), PCAN_USBBUS1, can0 ...")
    p.add_argument("-b", "--bitrate", type=int, default=250000,
                  help="CAN bitrate; 250000 for J1939 units, 125000 for STW-CAN/CANopen (default 250000)")
    p.add_argument("--node", type=auto_int, default=0x01, help="sensor node/module address (default 0x01, auto-detected during handshake)")
    p.add_argument("--timeout", type=float, default=0.05, help="reply timeout seconds (default 0.05)")
    p.add_argument("-v", "--verbose", action="store_true", help="print every TX/RX frame")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="probe the bus for the sensor (power-cycle when prompted)")
    sub.add_parser("dump", help="read and decode all parameters")
    sub.add_parser("menu", help="interactive menu to read/edit parameters")

    prd = sub.add_parser("rawdump", help="hex dump a range of EEPROM bytes")
    prd.add_argument("--start", type=auto_int, default=0x00, dest="start_addr",
                     help="start address (default 0x00)")
    prd.add_argument("--end", type=auto_int, default=0xFF, dest="end_addr",
                     help="end address (default 0xFF)")

    pr = sub.add_parser("read", help="read a raw EEPROM value")
    pr.add_argument("--addr", type=auto_int, required=True)
    pr.add_argument("--size", choices=["byte", "word", "dword"], default="byte")

    pw = sub.add_parser("write", help="write a raw EEPROM value")
    pw.add_argument("--addr", type=auto_int, required=True)
    pw.add_argument("--value", type=auto_int, required=True)
    pw.add_argument("--size", choices=["byte", "word", "dword"], default="byte")

    ps = sub.add_parser("set", help="set named J1939 parameters")
    for name in ("pgn", "spn", "slot", "trr", "data_length", "priority", "edp", "dp",
                 "data_position", "src_addr_start", "src_addr_range", "span_enable",
                 "span_min", "span_max", "filter_type", "filter_const"):
        ps.add_argument("--" + name.replace("_", "-"), dest=name, type=auto_int, default=None)

    args = p.parse_args()
    bus = open_bus(args)
    m01 = M01(bus, node=args.node, timeout=args.timeout, verbose=args.verbose)
    try:
        fn = {"scan": cmd_scan, "dump": cmd_dump, "menu": cmd_menu,
              "rawdump": cmd_rawdump,
              "read": cmd_read, "write": cmd_write, "set": cmd_set}[args.cmd]
        return fn(m01, args)
    finally:
        bus.shutdown()


if __name__ == "__main__":
    sys.exit(main())
