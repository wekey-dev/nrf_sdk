#!/usr/bin/env python3
"""Merge the QMK app image with the matching prebuilt core.

Run by build.sh after the QMK build, and by the QMK build itself when it is
pointed at a complete core SDK (NRF_CORE_SDK=<sdk>, from <sdk>/tools/). The complete core SDK (out/sdk: what
the QMK repository gets, plus the prebuilt cores and dongle, distributed on
their own) carries the prebuilt cores:
    <soc>/core/<variant>.bin    raw core image, starting at load_addr
    <soc>/core/<variant>.json   {"board", "abi_hash", "version", "build",
                                 "load_addr"} (0x1000: after the MBR)
with <variant> "rc" (32.768 kHz RC oscillator) or "xtal" (crystal).

The app header says which one the keyboard needs: chip (offset 12) and
power.lfclk_xtal (offset 108, NRF_LFCLK_CRYSTAL in config.h). The core is
merged only if its ABI hash equals the app's (offset 40); otherwise, or if
the SDK has no such core, a warning is printed and no full image is written
(the plain QMK image is still usable to update a board that has a core).

Writes <out>.hex and <out>.uf2 (WeKey UF2 bootloader, drag and drop).
"""
import argparse
import json
import os
import struct
import sys

MAGIC = 0x414B4D51
HEADER_SIZE = 116
OFF_CHIP, OFF_ABI_HASH, OFF_LFCLK_XTAL = 12, 40, 108
CHIPS = {52840: ('nrf52840', 0xADA52840), 52833: ('nrf52833', 0x621E937A)}

UF2_MAGIC0, UF2_MAGIC1, UF2_MAGIC_END = 0x0A324655, 0x9E5D5157, 0x0AB16F30
UF2_FLAG_FAMILY = 0x00002000
UF2_PAYLOAD = 256


def warn(msg):
    print(f'nrf_app_full: warning: {msg}', file=sys.stderr)


def load_hex(path):
    mem, base = {}, 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line.startswith(':'):
                continue
            n, addr, rtype = int(line[1:3], 16), int(line[3:7], 16), int(line[7:9], 16)
            data = bytes.fromhex(line[9:9 + 2 * n])
            if rtype == 0:
                for i, b in enumerate(data):
                    mem[base + addr + i] = b
            elif rtype == 2:
                base = int.from_bytes(data, 'big') << 4
            elif rtype == 4:
                base = int.from_bytes(data, 'big') << 16
    return mem


def load_bin(path, addr):
    with open(path, 'rb') as f:
        return {addr + i: b for i, b in enumerate(f.read())}


def write_hex(mem, path):
    def rec(rtype, addr, payload):
        raw = bytes([len(payload), (addr >> 8) & 0xFF, addr & 0xFF, rtype]) + payload
        return ':' + raw.hex().upper() + f'{(-sum(raw)) & 0xFF:02X}\n'

    lines, upper, addrs, i = [], None, sorted(mem), 0
    while i < len(addrs):
        start = addrs[i]
        chunk = bytearray([mem[start]])
        i += 1
        while i < len(addrs) and addrs[i] == start + len(chunk) and len(chunk) < 16 \
                and ((start + len(chunk)) & 0xFFFF) != 0:
            chunk.append(mem[addrs[i]])
            i += 1
        if start >> 16 != upper:
            upper = start >> 16
            lines.append(rec(4, 0, upper.to_bytes(2, 'big')))
        lines.append(rec(0, start & 0xFFFF, bytes(chunk)))
    lines.append(':00000001FF\n')
    with open(path, 'w') as f:
        f.writelines(lines)


def write_uf2(mem, family, path):
    pages = sorted({a & ~(UF2_PAYLOAD - 1) for a in mem})
    blocks = []
    for n, page in enumerate(pages):
        data = bytes(mem.get(page + i, 0xFF) for i in range(UF2_PAYLOAD))
        hdr = struct.pack('<8I', UF2_MAGIC0, UF2_MAGIC1, UF2_FLAG_FAMILY, page,
                          UF2_PAYLOAD, n, len(pages), family)
        blocks.append(hdr + data + bytes(476 - UF2_PAYLOAD) + struct.pack('<I', UF2_MAGIC_END))
    with open(path, 'wb') as f:
        f.write(b''.join(blocks))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('app_hex', help='packed QMK app image (.hex)')
    ap.add_argument('--sdk', required=True, help='core SDK directory')
    ap.add_argument('--out', required=True, help='output path without extension')
    args = ap.parse_args()

    for ext in ('.hex', '.uf2'):  # never leave a stale full image behind
        if os.path.exists(args.out + ext):
            os.remove(args.out + ext)

    app = load_hex(args.app_hex)
    start = min(app)
    hdr = bytes(app.get(start + i, 0xFF) for i in range(HEADER_SIZE))
    if struct.unpack_from('<I', hdr, 0)[0] != MAGIC:
        sys.exit(f'nrf_app_full: error: no app header in {args.app_hex}')
    chip = struct.unpack_from('<I', hdr, OFF_CHIP)[0]
    abi_hash = struct.unpack_from('<I', hdr, OFF_ABI_HASH)[0]
    soc, family = CHIPS[chip]
    variant = 'xtal' if hdr[OFF_LFCLK_XTAL] else 'rc'
    board = f'nrf/{soc}' + ('/xtal' if variant == 'xtal' else '')

    core_bin = os.path.join(args.sdk, soc, 'core', f'{variant}.bin')
    core_json = os.path.join(args.sdk, soc, 'core', f'{variant}.json')
    if not (os.path.exists(core_bin) and os.path.exists(core_json)):
        warn(f'no core {board} in {args.sdk}: no full image '
             f'(build it: run.sh core, or run.sh sdk for all four)')
        return
    with open(core_json) as f:
        info = json.load(f)
    if int(info['abi_hash'], 16) != abi_hash:
        warn(f'core {board} in the SDK has ABI hash {info["abi_hash"]}, this image '
             f'0x{abi_hash:08x}: not merged (rebuild the core)')
        return

    core = load_bin(core_bin, int(info['load_addr'], 16))
    overlap = set(core) & set(app)
    if overlap:
        sys.exit(f'nrf_app_full: error: core and app overlap at 0x{min(overlap):x}')
    full = dict(core)
    full.update(app)
    write_hex(full, args.out + '.hex')
    write_uf2(full, family, args.out + '.uf2')
    print(f'nrf_app_full: {os.path.basename(args.out)}.uf2 = core {board} '
          f'({info.get("version", "?")}, {info.get("build", "debug")}) + QMK')


if __name__ == '__main__':
    main()
