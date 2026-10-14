# -*- coding: utf-8 -*-
"""调试:dump 指定文件名的所有 MFT 记录的 $FN 列表与 $DATA。"""
import ctypes
import struct
import sys

from everything_ai import ntfs
from everything_ai.ntfs import _iter_attributes, usa_fixup


def main():
    targets = {t.lower() for t in sys.argv[1:]} or {"itss.dll"}
    h = ntfs.open_volume("C")
    vol = ntfs.read_volume_info(h, "C")
    rec0 = ntfs._read_at(h, vol.mft_lcn * vol.bytes_per_cluster, vol.bytes_per_record)
    rec0ba = bytearray(rec0)
    usa_fixup(rec0ba, len(rec0ba))
    runs = []
    for t, l, a in _iter_attributes(bytes(rec0ba)):
        if t == 0x80 and a[8] & 1:
            runs = ntfs.parse_mapping_pairs(a[struct.unpack_from("<H", a, 0x20)[0]: l], vol.bytes_per_cluster)
            break

    CHUNK = 1 << 22
    carry = b""
    rn = 0
    found = 0
    for off, length in runs:
        pos, end = off, off + length
        while pos < end and found < 6:
            n = min(CHUNK, end - pos)
            data = carry + ntfs._read_at(h, pos, n)
            pos += n
            nrec = len(data) // vol.bytes_per_record
            carry = data[nrec * vol.bytes_per_record:]
            for i in range(nrec):
                rn += 1
                if rn <= 15:
                    continue
                raw = data[i * vol.bytes_per_record: (i + 1) * vol.bytes_per_record]
                if raw[0:4] != b"FILE":
                    continue
                rec = bytearray(raw)
                try:
                    usa_fixup(rec, len(rec))
                except ValueError:
                    continue
                base_frn = struct.unpack_from("<Q", rec, 0x20)[0] & 0xFFFFFFFFFFFF
                names = []
                size = 0
                attr_list = False
                for t, l, a in _iter_attributes(bytes(rec)):
                    if t == 0x20:
                        attr_list = True
                    elif t == 0x30:
                        co = struct.unpack_from("<H", a, 0x14)[0]
                        c = a[co:]
                        nl, ns = c[0x40], c[0x41]
                        if 0 < nl <= 255 and 0x42 + nl * 2 <= len(c):
                            names.append((struct.unpack_from("<Q", c, 0)[0] & 0xFFFFFFFFFFFF, ns, c[0x42: 0x42 + nl * 2].tobytes().decode("utf-16-le", "replace")))
                    elif t == 0x80 and a[8] & 1 and a[9] == 0 and not size:
                        size = struct.unpack_from("<Q", a, 0x30)[0]
                if any(nm.lower() in targets for _p, _ns, nm in names):
                    print(f"rec={rn} base_frn={base_frn} attr_list={attr_list} size={size}")
                    for p, ns, nm in names:
                        print(f"    parent={p} ns={ns} name={nm!r}")
                    found += 1
                if found >= 6:
                    break
    ctypes.WinDLL("kernel32").CloseHandle(h)
    print("found:", found)


if __name__ == "__main__":
    main()
