# m01-can-config

Configure STW M01-CAN pressure transmitters over plain CAN — no PEAK hardware or Windows required.

The STW M01-CAN is a pressure transmitter commonly used in industrial and mobile hydraulic applications. The official configuration tool (`M01_CANfigurator.exe`) requires Windows and a PEAK PCAN-USB adapter. This script replaces it with a cross-platform Python tool that works with **any** CAN adapter supported by [python-can](https://python-can.readthedocs.io/) (Kvaser, CANable/SLCAN, USBtin, Peak, Vector, socketcan, etc.).

The proprietary configuration protocol was fully reverse-engineered from the official Windows binary.

## Requirements

- Python 3.6+
- A CAN adapter connected to the sensor's bus
- `python-can` (and `pyserial` for SLCAN adapters)

```bash
pip install python-can pyserial
```

## CAN Bus Setup

### socketcan (Linux — Kvaser, Peak, etc.)

```bash
sudo ip link set can1 type can bitrate 250000
sudo ip link set can1 up
```

### SLCAN (CANable, USBtin)

No OS-level setup needed — python-can handles the serial connection directly.

## Usage

Every command requires a power-cycle of the sensor during the connect handshake. The script will prompt you when to unplug and replug.

### Scan — connect and show sensor info

```bash
sudo python3 m01config.py -i socketcan -c can1 scan
```

### Dump — read all named J1939 parameters

```bash
sudo python3 m01config.py -i socketcan -c can1 dump
```

### Raw EEPROM hex dump

```bash
# Full dump (0x00–0xFF)
sudo python3 m01config.py -i socketcan -c can1 rawdump

# Partial range
sudo python3 m01config.py -i socketcan -c can1 rawdump --start 0x90 --end 0xBF
```

### Set J1939 parameters

```bash
# Set PGN, SPN, transmission rate, and priority
sudo python3 m01config.py -i socketcan -c can1 set \
    --pgn 0xFF00 --spn 1234 --trr 100 --priority 6

# Set source address and filter
sudo python3 m01config.py -i socketcan -c can1 set \
    --src-addr-start 0x80 --filter-type 1 --filter-const 30
```

### Raw EEPROM read/write

```bash
# Read a word at address 0x9D (PGN)
sudo python3 m01config.py -i socketcan -c can1 read --addr 0x9D --size word

# Write a single byte
sudo python3 m01config.py -i socketcan -c can1 write --addr 0xA5 --value 6
```

### Verbose mode

Add `-v` to see every CAN frame sent and received:

```bash
sudo python3 m01config.py -i socketcan -c can1 -v dump
```

## Adapter Examples

```bash
# Kvaser USBcan Light via socketcan
sudo python3 m01config.py -i socketcan -c can1 dump

# CANable via SLCAN
sudo python3 m01config.py -i slcan -c /dev/ttyACM0 dump

# PEAK PCAN-USB
sudo python3 m01config.py -i pcan -c PCAN_USBBUS1 dump

# Vector (Windows)
python m01config.py -i vector -c 0 dump
```

## Settable Parameters

| Parameter | Flag | EEPROM Addr | Size | Notes |
|-----------|------|-------------|------|-------|
| PGN | `--pgn` | 0x9D | word | J1939 Parameter Group Number |
| SPN | `--spn` | 0x9F | word | Suspect Parameter Number |
| Slot | `--slot` | 0xA1 | byte | Slot index (see J1939 slot tables) |
| Transmission Rate | `--trr` | 0xA2 | word | Repetition rate in ms |
| Data Length | `--data-length` | 0xA4 | byte | |
| Priority | `--priority` | 0xA5 | byte | 0–7 (default 6) |
| Ext. Data Page | `--edp` | 0xA6 | byte | |
| Data Page | `--dp` | 0xA7 | byte | |
| Data Position | `--data-position` | 0xA8 | byte | Start byte in PGN (0–7) |
| Source Addr Start | `--src-addr-start` | 0x91 | byte | |
| Source Addr Range | `--src-addr-range` | 0x92 | byte | |
| Span Enable | `--span-enable` | 0xB1 | byte | 1=enable custom range |
| Span Min | `--span-min` | 0xB2 | dword | 0% pressure output value |
| Span Max | `--span-max` | 0xB6 | dword | 100% pressure output value |
| Filter Type | `--filter-type` | 0x41 | byte | 0=none, 1=moving avg, 2=repeating avg |
| Filter Constant | `--filter-const` | 0x42 | byte | 0–255 (default 30) |

## Protocol Overview

The M01-CAN has a proprietary EEPROM configuration protocol over standard 11-bit CAN:

- **Request ID:** 0x051 — **Response ID:** 0x052
- **Bitrate:** 250 kbps (J1939) or 125 kbps (STW-CAN/CANopen)

### Connect Handshake

The sensor must be power-cycled during the connect sequence:

1. **ABGLEICH flood** — send `"ABGLEICH"` (ASCII) on 0x051 every 10ms while the sensor boots
2. **Node scan** — send `[addr, 0x41]` for addr 0x00–0xFE to discover the sensor's node address
3. **CompanyID auth** — send `[node, 0x40, 0x06, 0x2A, 0x59]` to enter config mode

### EEPROM Commands

| CMD | Function | Frame (0x051) |
|-----|----------|---------------|
| 0x22 | Read byte | `[node, 0xFD, 0x0A, 0x22, addr_lo, addr_hi, 0, 0]` |
| 0x21 | Write byte | `[node, 0xFD, 0x0A, 0x21, addr_lo, addr_hi, value, 0]` |
| 0x41 | Update checksums | `[node, 0xFD, 0x0A, 0x41, 0x00]` |
| 0x24 | Device info | `[node, 0xFD, 0x0A, 0x24, 0x00]` |
| 0x44 | Alive/echo | `[node, 0xFD, 0x0A, 0x44, 0x00]` |

Response value is at **data[5]** in the 0x052 reply.

## License

MIT
