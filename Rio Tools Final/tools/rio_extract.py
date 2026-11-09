import struct
import sys
from pathlib import Path

import rio_index

LIST_OFFSETS = range(0x48, 0xB0, 8)
RECORD_SIZE = 20
INDEX_NAME = "resource_index.tsv"
OUTPUT_NAME = "extracted_assets"


class Bits:
    def __init__(self, data, big_endian):
        self.data = data
        self.big_endian = big_endian
        self.pos = self.value = self.count = 0

    def fill(self, count):
        while self.count < count:
            if self.pos >= len(self.data):
                raise ValueError("truncated EDL2 stream")
            raw = self.data[self.pos : self.pos + 4]
            self.pos += len(raw)
            order = "big" if self.big_endian else "little"
            self.value |= int.from_bytes(raw, order) << self.count
            self.count += len(raw) * 8

    def read(self, count):
        self.fill(count)
        result = self.value & ((1 << count) - 1)
        self.value >>= count
        self.count -= count
        return result

    def peek(self, count):
        self.fill(count)
        return self.value & ((1 << count) - 1)


def decode_edl2(data, big_endian):
    bits = Bits(data, big_endian)
    out = bytearray()
    while True:
        if bits.read(1) == 0:
            out.append(bits.read(8))
            continue
        if bits.read(1):
            second = bits.read(1)
            length = 2
            has_prefix = bool(second)
            if second:
                length = 3
                if bits.read(1):
                    length = bits.read(8)
                    if length == 0:
                        return bytes(out)
                    length += 8
        else:
            length = 4 + bits.read(1)
            has_prefix = True
            if bits.read(1):
                length = 2 * (length - 1) + bits.read(1)
                if length == 9:
                    for _ in range(bits.read(4) * 4 + 12):
                        out.append(bits.read(8))
                    continue
        base = 0
        if has_prefix and bits.read(1):
            look = bits.peek(5)
            b1, b2, b3, b4, b5 = ((look >> n) & 1 for n in range(5))
            if b2:
                if b4:
                    bits.read(4)
                    base = 0x400 + b1 * 0x200 + b3 * 0x100
                else:
                    bits.read(5)
                    base = 0x800 + b1 * 0x400 + b3 * 0x200 + b5 * 0x100
            elif b1:
                bits.read(2)
                base = 0x100
            else:
                bits.read(3)
                base = 0x200 + b3 * 0x100
        distance = base + bits.read(8) + 1
        if distance > len(out):
            out.extend(b"\0" * length)
        else:
            pattern = out[-distance:]
            out.extend((pattern * ((length + distance - 1) // distance))[:length])


def u32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def signed32(value):
    return value - (1 << 32) if value & 0x80000000 else value


def relative_target(data, pointer_field):
    return pointer_field + signed32(u32(data, pointer_field))


def packed_target(pointer_field, value):
    delta = value >> 8
    if delta & 0x800000:
        delta -= 1 << 24
    return pointer_field + delta


def embedded_names(data):
    footer = u32(data, len(data) - 4)
    count = u32(data, footer)
    names = []
    for index in range(count):
        record = relative_target(data, footer + 0x10 + index * 4)
        address = relative_target(data, record)
        names.append(data[address : data.index(0, address)].decode("utf-8"))
    return names


def asset_path(output, embedded_name):
    return output / Path(embedded_name.lstrip("*").replace("\\", "/"))


def decode_part(data, source, stored):
    block = data[source : source + stored]
    if block[:3] != b"EDL":
        return block
    return decode_edl2(block[12:], bool(block[3] & 0x80))


def export_edb(path, output_root):
    data = path.read_bytes()
    if data[:4] != b"GEOM":
        raise ValueError(f"{path.name}: not an EDB archive")
    archive_output = output_root / path.stem
    names = embedded_names(data)
    objects = payloads = 0
    for list_offset in LIST_OFFSETS:
        count = u32(data, list_offset)
        table = relative_target(data, list_offset + 4)
        for index in range(count):
            record = table + index * RECORD_SIZE
            _, uid, _, global_index, tagged = struct.unpack_from(">5I", data, record)
            if global_index >= len(names):
                continue
            descriptor = packed_target(record + 16, tagged)
            parts = tagged & 0xFF
            kind = rio_index.RESOURCE_TYPES.get(uid & rio_index.TYPE_MASK, f"type_{uid & rio_index.TYPE_MASK:08x}")
            base = asset_path(archive_output, names[global_index])
            for part in range(parts):
                source, stored, _, _ = struct.unpack_from(">4I", data, descriptor + part * 16)
                suffix = f".part{part}" if parts > 1 else ""
                target = base.parent / f"{base.name}.{kind}{suffix}"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(decode_part(data, source, stored))
                payloads += 1
            objects += 1
    return objects, payloads


def write_index(root, archives):
    index = {}
    existing = root / INDEX_NAME
    if existing.exists():
        index.update(rio_index.load(existing))
    for path in archives:
        try:
            for uid, kind, name in rio_index.index_archive(path):
                index[uid] = (kind, name, path.stem)
        except (ValueError, IndexError, struct.error, UnicodeError):
            continue
    rows = ["uid\ttype\tpath\tarchive"]
    for uid in sorted(index):
        kind, name, archive = index[uid]
        rows.append(f"{uid:#010x}\t{kind}\t{name}\t{archive}")
    existing.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return len(index)


def collect(values):
    found = []
    for value in values:
        path = Path(value)
        found.extend(sorted(path.rglob("*.edb")) if path.is_dir() else [path])
    return [path for path in found if path.suffix.lower() == ".edb"]


def main():
    files = collect(sys.argv[1:])
    if not files:
        print("Drop one or more .edb archives, or a folder holding them, onto this tool.")
        return 1
    root = files[0].parent / OUTPUT_NAME
    root.mkdir(parents=True, exist_ok=True)
    print(f"Extracting {len(files)} archive(s) to {root}\n")
    objects = payloads = failed = 0
    for number, path in enumerate(files, 1):
        try:
            found, written = export_edb(path, root)
        except (OSError, ValueError, IndexError, struct.error, UnicodeError) as error:
            print(f"[{number}/{len(files)}] {path.name}: FAILED - {error}")
            failed += 1
            continue
        objects += found
        payloads += written
        print(f"[{number}/{len(files)}] {path.name}: {found} resources")
    total = write_index(root, files)
    print(f"\n{objects} resources ({payloads} payloads) from {len(files) - failed} archive(s).")
    print(f"Index: {total} entries in {root / INDEX_NAME}")
    if failed:
        print(f"{failed} archive(s) could not be read.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
