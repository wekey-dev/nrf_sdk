#!/usr/bin/env python3
"""Make the WeKey 2.4 GHz USB dongle image for this keyboard.

Run for keyboards with NRF_DONGLE = yes in rules.mk: by build.sh (QMK writes
the setting to <target>.nrf.json), or by the QMK build itself when it is
pointed at a complete core SDK (NRF_CORE_SDK=<sdk>, from <sdk>/tools/). The complete core SDK (out/sdk) carries the
prebuilt dongle app:
    nrf52840/dongle/dongle.bin    raw image, starting at load_addr
    nrf52840/dongle/dongle.json   {"board", "version", "build", "load_addr"}
It runs on the Nordic nRF52840 Dongle (PCA10059) and the EBYTE E104-BT5040U,
whatever chip the keyboard has.

The image holds one preset block (the dongle's preset.c), found by its
magic. This script fills it with the keyboard's identity, read from the app
header (identity at offset 44: vid, pid, device_ver, manufacturer pointer,
product[32]), so the dongle enumerates as this keyboard from the first
plug-in; with --lock it pairs no keyboard with another VID/PID.

Writes <out>.hex and <out>.uf2 (WeKey UF2 bootloader, board wekey_dongle).
"""
import argparse
import json
import os
import struct
import sys

sys.dont_write_bytecode = True  # no __pycache__ next to the scripts
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nrf_app_full import MAGIC, HEADER_SIZE, load_hex, write_hex, write_uf2  # noqa: E402

DONGLE_FAMILY = 0xADA52840
OFF_IDENTITY = 44
STR_MAX = 32

PRESET_MAGIC = b'WeKey dongle cfg'
PRESET_VERSION = 1
PRESET_LOCK = 0x0001
PRESET_SIZE = 92


def fail(msg):
    sys.exit(f'nrf_dongle: error: {msg}')


def c_string(raw):
    s = raw.split(b'\0', 1)[0]
    return s[:STR_MAX - 1]


def app_identity(app):
    """(vid, pid, device_ver, manufacturer, product) from the app header"""
    start = min(app)
    hdr = bytes(app.get(start + i, 0xFF) for i in range(HEADER_SIZE))
    if struct.unpack_from('<I', hdr, 0)[0] != MAGIC:
        fail('no app header in the QMK image')
    vid, pid, ver, _, mfr_ptr = struct.unpack_from('<4HI', hdr, OFF_IDENTITY)
    product = c_string(hdr[OFF_IDENTITY + 12:OFF_IDENTITY + 12 + STR_MAX])
    manufacturer = b''
    if mfr_ptr:
        raw = bytearray()
        while len(raw) < STR_MAX and app.get(mfr_ptr + len(raw), 0) != 0:
            raw.append(app[mfr_ptr + len(raw)])
        manufacturer = c_string(bytes(raw))
    return vid, pid, ver, manufacturer, product


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('app_hex', help='packed QMK app image (.hex)')
    ap.add_argument('--sdk', required=True, help='core SDK directory')
    ap.add_argument('--out', required=True, help='output path without extension')
    ap.add_argument('--lock', action='store_true',
                    help='pair only keyboards with this VID/PID (NRF_DONGLE_LOCK)')
    args = ap.parse_args()

    for ext in ('.hex', '.uf2'):  # never leave a stale dongle image behind
        if os.path.exists(args.out + ext):
            os.remove(args.out + ext)

    dongle_bin = os.path.join(args.sdk, 'nrf52840', 'dongle', 'dongle.bin')
    dongle_json = os.path.join(args.sdk, 'nrf52840', 'dongle', 'dongle.json')
    if not (os.path.exists(dongle_bin) and os.path.exists(dongle_json)):
        print(f'nrf_dongle: warning: no dongle in {args.sdk}: no dongle image built '
              '(build it: run.sh dongle)', file=sys.stderr)
        return
    with open(dongle_json) as f:
        info = json.load(f)
    with open(dongle_bin, 'rb') as f:
        image = bytearray(f.read())

    off = image.find(PRESET_MAGIC)
    if off < 0 or image.find(PRESET_MAGIC, off + 1) >= 0:
        fail(f'{dongle_bin}: preset block missing or not unique (dongle too old?)')
    if struct.unpack_from('<H', image, off + 16)[0] != PRESET_VERSION:
        fail(f'{dongle_bin}: preset version {struct.unpack_from("<H", image, off + 16)[0]}, '
             f'this script writes {PRESET_VERSION}')

    vid, pid, ver, manufacturer, product = app_identity(load_hex(args.app_hex))
    if vid == 0 or pid == 0:
        fail('the keyboard declares no usb.vid / usb.pid (keyboard.json): '
             'the dongle cannot be made for it')

    flags = PRESET_LOCK if args.lock else 0
    preset = struct.pack('<16s6H32s32s', PRESET_MAGIC, PRESET_VERSION, flags, vid, pid, ver, 0,
                         manufacturer, product)
    assert len(preset) == PRESET_SIZE
    image[off:off + PRESET_SIZE] = preset

    load = int(info['load_addr'], 16)
    mem = {load + i: b for i, b in enumerate(image)}
    write_hex(mem, args.out + '.hex')
    write_uf2(mem, DONGLE_FAMILY, args.out + '.uf2')
    print(f'nrf_dongle: {os.path.basename(args.out)}.uf2 = dongle ({info.get("version", "?")}, '
          f'{info.get("build", "debug")}) for {vid:04x}:{pid:04x} '
          f'"{product.decode(errors="replace")}"{", locked" if args.lock else ""}')


if __name__ == '__main__':
    main()
