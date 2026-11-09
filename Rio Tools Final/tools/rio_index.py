import argparse
import struct
from pathlib import Path

LIST_OFFSETS = range(0x48, 0xB0, 8)
RECORD_SIZE = 20
TYPE_MASK = 0xFFF00000

RESOURCE_TYPES = {
    0x40200000: "texture",
    0x40300000: "material",
    0x40400000: "entity",
    0x40500000: "skeleton",
    0x40600000: "animsequence",
    0x40700000: "timeline",
    0x40800000: "particlesystem",
    0x40900000: "map",
    0x40A00000: "zone",
    0x40D00000: "swoosh",
    0x40E00000: "rigidbody",
    0x40F00000: "shader",
    0x41200000: "customattributetemplate",
    0x41300000: "animdatabase",
    0x41400000: "scheme",
    0x41600000: "navmesh",
    0x41900000: "script",
    0x41C00000: "textstore",
    0x41E00000: "animhierarchy",
    0x41F00000: "font",
    0x42000000: "gibundle",
}


def u32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def signed32(value):
    return value - (1 << 32) if value & 0x80000000 else value


def relative_target(data, pointer_field):
    return pointer_field + signed32(u32(data, pointer_field))


def embedded_names(data):
    footer = u32(data, len(data) - 4)
    count = u32(data, footer)
    names = []
    for index in range(count):
        record = relative_target(data, footer + 0x10 + index * 4)
        address = relative_target(data, record)
        names.append(data[address : data.index(0, address)].decode("utf-8"))
    return names


def normalise(name):
    return name.lstrip("*").replace("\\", "/").lower()


def index_archive(path):
    data = path.read_bytes()
    if data[:4] != b"GEOM":
        raise ValueError(f"{path.name}: not an EDB")
    names = embedded_names(data)
    for list_offset in LIST_OFFSETS:
        count = u32(data, list_offset)
        table = relative_target(data, list_offset + 4)
        for entry in range(count):
            record = table + entry * RECORD_SIZE
            _, uid, _, name_index, _ = struct.unpack_from(">5I", data, record)
            if name_index >= len(names):
                continue
            kind = RESOURCE_TYPES.get(uid & TYPE_MASK, f"type_{uid & TYPE_MASK:08x}")
            yield uid, kind, normalise(names[name_index])


def build(archives):
    index, collisions = {}, 0
    for path in archives:
        for uid, kind, name in index_archive(path):
            existing = index.get(uid)
            if existing and existing[1] != name:
                collisions += 1
            index[uid] = (kind, name, path.stem)
    return index, collisions


def load(path):
    index = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines()[1:]:
        uid, kind, name, archive = line.split("\t")
        index[int(uid, 16)] = (kind, name, archive)
    return index


def resource_files(root, suffix):
    catalogue = {}
    for path in Path(root).rglob(f"*{suffix}"):
        parts = path.with_suffix("").parts
        for start in range(len(parts)):
            catalogue.setdefault("/".join(parts[start:]).lower(), path)
    return catalogue


def find_resource(catalogue, name):
    return catalogue.get(name.lower())


def archive_files(values):
    found = []
    for value in values:
        path = Path(value)
        found.extend(sorted(path.rglob("*.edb")) if path.is_dir() else [path])
    return found


def main():
    parser = argparse.ArgumentParser(description="Build a UID -> resource path index")
    parser.add_argument("input", nargs="+", help=".edb files or directories to search")
    parser.add_argument("-o", "--output", type=Path, default=Path("resource_index.tsv"))
    args = parser.parse_args()
    archives = archive_files(args.input)
    if not archives:
        parser.error("no .edb files found")
    index, collisions = build(archives)
    rows = ["uid\ttype\tpath\tarchive"]
    for uid in sorted(index):
        kind, name, archive = index[uid]
        rows.append(f"{uid:#010x}\t{kind}\t{name}\t{archive}")
    args.output.write_text("\n".join(rows) + "\n", encoding="utf-8")
    counts = {}
    for kind, _, _ in index.values():
        counts[kind] = counts.get(kind, 0) + 1
    print(f"{len(index)} resources from {len(archives)} archives -> {args.output}")
    for kind, total in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"    {total:6} {kind}")
    if collisions:
        print(f"    note: {collisions} UIDs appeared with more than one path")


if __name__ == "__main__":
    main()
