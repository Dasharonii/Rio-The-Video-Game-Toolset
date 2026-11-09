import argparse
import json
import struct
from pathlib import Path

import rio_index
import rio_material
import rio_skeleton

TYPE_OFFSET = 0x03
ENTITY_TYPE = 0x09
BONE_COUNT = 0x44
VERTEX_COUNT = 0x5C
INDEX_COUNT = 0x60

INDEX_BLOCK_HEADER = 8
INDEX_BLOCK_FLAGS = 4




RELOCATION_FIELD = 0x24
REVERSED_PAD_LIMIT = 128
REVERSED_STRIDES = range(128, 8, -4)
REVERSED_TOLERANCE = 0.02
REVERSED_EXACT = 1e-4
REVERSED_SAMPLE = 32
POSITION = 0x00
NORMAL = 0x0C
TANGENT = 0x10
BINORMAL = 0x14
UV = 0x18




EXTRA_UV = 0x1C
COLOUR_BYTES = 8
EXTRA_SAMPLE = 64
UV_LIMIT = 1.05
COLOUR_LIMIT = 1.01
HOT_COLOUR_LIMIT = 8.0




COLLISION_RECORD = 16
COLLISION_TERMINATORS = 2
COLLISION_MINIMUM = 4
COLLISION_LIMIT = 1e6





BONE_HASH_STRIDE = 8
BONE_HASH_BYTES = 7
BONE_HASH_SHARE = 0.9


FIELD_BYTES = 4
SKIN_MIN_STRIDE = 0x20
BONE_INDICES_FROM_END = 0x0C
BONE_WEIGHTS_FROM_END = 0x08
SKIN_SAMPLE = 64
MATERIAL_FIELD = 0xD8



PART_COUNT = 0x34


PART_TABLES = (0x80, 0x70)
PART_STRIDE = 64
PART_BOUNDS_MIN = 0x00
PART_BOUNDS_MAX = 0x10

SUBMESH_STRIDE = 32
SUBMESH_TRIS = 0x04
SUBMESH_BASE = 0x08
SUBMESH_VCOUNT = 0x10
SUBMESH_ISTART = 0x14
SUBMESH_MATERIAL = 0x18





DIRECTION_LANES = (1, 12, 22)



XBOX_DIRECTION_LANES = (0, 10, 20)
DIRECTION_BITS = 10
DIRECTION_SCALE = 511.0


def u32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def u16(data, offset):
    return struct.unpack_from(">H", data, offset)[0]


def half(bits):
    sign, exponent, mantissa = (bits >> 15) & 1, (bits >> 10) & 0x1F, bits & 0x3FF
    if exponent == 0x1F:
        return float("nan") if mantissa else float("-inf" if sign else "inf")
    if exponent:
        value = (1 + mantissa / 1024) * 2.0 ** (exponent - 15)
    else:
        value = (mantissa / 1024) * 2.0**-14
    return -value if sign else value


def read_half(data, offset):
    return half(struct.unpack_from(">H", data, offset)[0])


def unpack_direction(bits, lanes=DIRECTION_LANES):
    limit, sign_bit = 1 << DIRECTION_BITS, 1 << (DIRECTION_BITS - 1)
    axes = []
    for shift in lanes:
        lane = (bits >> shift) & (limit - 1)
        axes.append((lane - limit if lane >= sign_bit else lane) / DIRECTION_SCALE)
    length = sum(axis * axis for axis in axes) ** 0.5 or 1.0
    return tuple(axis / length for axis in axes)


def read_direction(data, offset, lanes=DIRECTION_LANES):
    return unpack_direction(u32(data, offset), lanes)


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def find_index_block(data, index_count, vertex_count):
    size = (index_count * 2 + INDEX_BLOCK_HEADER + 3) & ~3
    needle = struct.pack(">I", size)
    search = 0
    while (offset := data.find(needle, search)) >= 0:
        search = offset + 4
        if offset % 4 or u32(data, offset + 4) != INDEX_BLOCK_FLAGS:
            continue
        payload = offset + INDEX_BLOCK_HEADER
        if payload + index_count * 2 > len(data):
            continue
        indices = struct.unpack_from(f">{index_count}H", data, payload)
        if max(indices) >= vertex_count:
            continue
        span = len(data) - (offset + size)
        if span <= 0 or span % vertex_count:
            continue
        return offset + size, indices
    raise ValueError("no index block consistent with the header counts")


def signed32(value):
    return value - (1 << 32) if value & 0x80000000 else value


def relocation_table(data):
    return RELOCATION_FIELD + signed32(u32(data, RELOCATION_FIELD))


def positions_inside(data, base, stride, count, bounds, step):
    low, high = bounds
    slack = [(high[axis] - low[axis]) * 0.001 + 1e-6 for axis in range(3)]
    for number in range(0, count, step):
        point = struct.unpack_from(">3f", data, base + number * stride)
        for axis in range(3):
            value = point[axis]
            if value != value or not low[axis] - slack[axis] <= value <= high[axis] + slack[axis]:
                return False
    return True


def bounds_error(data, base, stride, count, bounds):
    low, high = bounds
    least, most = [float("inf")] * 3, [float("-inf")] * 3
    for number in range(count):
        point = struct.unpack_from(">3f", data, base + number * stride)
        for axis in range(3):
            value = point[axis]
            if value != value or value in (float("inf"), float("-inf")):
                return None
            least[axis] = min(least[axis], value)
            most[axis] = max(most[axis], value)
    worst = 0.0
    for axis in range(3):
        extent = high[axis] - low[axis] or 1e-6
        worst = max(worst, (abs(least[axis] - low[axis]) + abs(most[axis] - high[axis])) / extent)
    return worst


def find_reversed_geometry(data, index_count, vertex_count, floor, bounds):
    if not bounds:
        return None
    end = relocation_table(data)
    if not floor < end <= len(data):
        return None
    best = None
    for pad in range(0, REVERSED_PAD_LIMIT + 1, 2):
        index_base = end - index_count * 2 - pad
        if index_base < floor:
            break
        indices = struct.unpack_from(f">{index_count}H", data, index_base)
        if max(indices) >= vertex_count:
            continue
        for stride in REVERSED_STRIDES:
            vertex_base = index_base - vertex_count * stride
            if vertex_base < floor:
                continue
            step = max(1, vertex_count // REVERSED_SAMPLE)
            if not positions_inside(data, vertex_base, stride, vertex_count, bounds, step):
                continue
            error = bounds_error(data, vertex_base, stride, vertex_count, bounds)
            if error is None or error > REVERSED_TOLERANCE:
                continue
            if error < REVERSED_EXACT:
                return vertex_base, stride, indices
            if best is None or error < best[0]:
                best = (error, vertex_base, stride, indices)
    return best[1:] if best else None


def skinning_holds(data, base, stride, count, bones):
    if not bones or stride < SKIN_MIN_STRIDE:
        return False
    bone_field, weight_field = stride - BONE_INDICES_FROM_END, stride - BONE_WEIGHTS_FROM_END
    checked = min(count, SKIN_SAMPLE)
    for index in range(checked):
        offset = base + index * stride
        if max(data[offset + bone_field : offset + bone_field + 4]) >= bones:
            return False
        total = sum(read_half(data, offset + weight_field + n * 2) for n in range(4))
        if abs(total - 1.0) > 0.02:
            return False
    return bool(checked)


def uv_holds(data, base, stride, count, offset):
    checked = min(count, EXTRA_SAMPLE)
    for index in range(checked):
        at = base + index * stride + offset
        for half_offset in (0, 2):
            value = read_half(data, at + half_offset)
            if not -0.05 <= value <= UV_LIMIT:
                return False
    return bool(checked)


def colour_holds(data, base, stride, count, offset):
    checked = min(count, EXTRA_SAMPLE)
    if not checked:
        return False
    for index in range(checked):
        at = base + index * stride + offset
        values = [read_half(data, at + n * 2) for n in range(4)]
        if any(value != value for value in values):
            return False
        if not -0.01 <= values[3] <= COLOUR_LIMIT:
            return False
        if not all(-0.01 <= value <= HOT_COLOUR_LIMIT for value in values[:3]):
            return False
    return True


def extra_fields(data, mesh):
    stride, base, count = mesh["stride"], mesh["vertex_base"], mesh["vertex_count"]
    spare = stride - (BONE_INDICES_FROM_END if mesh["skinned"] else 0) - EXTRA_UV
    colour = None
    if spare >= COLOUR_BYTES:
        at = EXTRA_UV + spare - COLOUR_BYTES
        if colour_holds(data, base, stride, count, at):
            colour = at
    uv1 = None
    if spare == FIELD_BYTES or spare >= COLOUR_BYTES + FIELD_BYTES:
        if uv_holds(data, base, stride, count, EXTRA_UV):
            uv1 = EXTRA_UV
    return uv1, colour


def collision_face(record):
    return record[0:4] == b"\0\0\0\0" and record[10:14] == b"\xff\xff\xff\xff"


def collision_point(record):
    if record[12:16] != b"\0\0\0\0":
        return False
    axes = struct.unpack_from(">3f", record, 0)
    return all(axis == axis and abs(axis) < COLLISION_LIMIT for axis in axes)


def find_collision(data, mesh):
    start = mesh["vertex_base"] - COLLISION_TERMINATORS * COLLISION_RECORD
    if start < 0:
        return None
    faces, scan = [], start
    while scan - COLLISION_RECORD >= 0 and collision_face(data[scan - COLLISION_RECORD : scan]):
        scan -= COLLISION_RECORD
        faces.append(struct.unpack_from(">3H", data, scan + 4))
    if len(faces) < COLLISION_MINIMUM:
        return None
    faces.reverse()
    count = max(max(face) for face in faces) + 1
    base = scan - count * COLLISION_RECORD
    if base < 0:
        return None
    for index in range(count):
        if not collision_point(data[base + index * COLLISION_RECORD :][:COLLISION_RECORD]):
            return None
    points = [struct.unpack_from(">3f", data, base + index * COLLISION_RECORD) for index in range(count)]
    return {"points": points, "faces": faces, "base": base}


def write_collision_glb(path, name, collision):
    mesh = {
        "vertex_count": len(collision["points"]),
        "index_count": len(collision["faces"]) * 3,
        "has_normal": False,
        "has_tangent": False,
        "has_uv": False,
        "skinned": False,
    }
    points = [{"position": point} for point in collision["points"]]
    indices = [index for face in collision["faces"] for index in face]
    write_glb(path, name, mesh, points, [{"indices": indices, "maps": {}, "material": None}])


def part_box(data, record):
    if record + PART_STRIDE > len(data):
        return None
    low = struct.unpack_from(">3f", data, record + PART_BOUNDS_MIN)
    high = struct.unpack_from(">3f", data, record + PART_BOUNDS_MAX)
    if all(-1e30 < least <= most < 1e30 for least, most in zip(low, high)):
        return low, high
    return None


def part_table(data):
    for base in PART_TABLES:
        if part_box(data, base):
            return base
    return PART_TABLES[0]


def entity_bounds(data, base):
    boxes = [
        box
        for part in range(u16(data, PART_COUNT))
        if (box := part_box(data, base + part * PART_STRIDE))
    ]
    if not boxes:
        return None
    return (
        tuple(min(box[0][axis] for box in boxes) for axis in range(3)),
        tuple(max(box[1][axis] for box in boxes) for axis in range(3)),
    )


def parse(data):
    if data[TYPE_OFFSET] != ENTITY_TYPE:
        raise ValueError(f"not an entity resource (type {data[TYPE_OFFSET]:#04x})")
    vertex_count, index_count = u32(data, VERTEX_COUNT), u32(data, INDEX_COUNT)
    if not vertex_count or not index_count:
        raise ValueError("empty mesh")
    if index_count % 3:
        raise ValueError(f"{index_count} indices is not a whole number of triangles")
    base = part_table(data)
    bounds = entity_bounds(data, base)
    try:
        vertex_base, indices = find_index_block(data, index_count, vertex_count)
        stride = (len(data) - vertex_base) // vertex_count
        reversed_layout = False
    except ValueError:
        floor = base + u16(data, PART_COUNT) * PART_STRIDE
        found = find_reversed_geometry(data, index_count, vertex_count, floor, bounds)
        if not found:
            raise
        vertex_base, stride, indices = found
        reversed_layout = True
    bones = data[BONE_COUNT]
    skinned = skinning_holds(data, vertex_base, stride, vertex_count, bones)
    fields = stride - (BONE_INDICES_FROM_END if skinned else 0)
    mesh = {
        "vertex_count": vertex_count,
        "index_count": index_count,
        "stride": stride,
        "vertex_base": vertex_base,
        "indices": indices,
        "bones": bones,
        "has_normal": fields >= NORMAL + FIELD_BYTES,

        "has_tangent": fields >= BINORMAL + FIELD_BYTES,
        "has_uv": fields >= UV + FIELD_BYTES,
        "skinned": skinned,
        "bounds": bounds,
        "reversed": reversed_layout,
    }
    table = read_submeshes(data, mesh, base + u16(data, PART_COUNT) * PART_STRIDE)
    mesh["submeshes"] = table["submeshes"] if table else None
    mesh["table_end"] = table["end"] if table else None
    mesh["palettes"] = bone_palettes(data, mesh, bones) if skinned else None
    mesh["bone_spans"] = palette_spans(mesh)
    mesh["uv1"], mesh["colour"] = extra_fields(data, mesh)
    return mesh


def vertices(data, mesh):
    base, stride = mesh["vertex_base"], mesh["stride"]
    bone_field, weight_field = stride - BONE_INDICES_FROM_END, stride - BONE_WEIGHTS_FROM_END
    spans, cursor = mesh.get("bone_spans") or (), 0
    lanes = XBOX_DIRECTION_LANES if mesh.get("reversed") else DIRECTION_LANES
    for index in range(mesh["vertex_count"]):
        offset = base + index * stride
        point = {"position": struct.unpack_from(">3f", data, offset + POSITION)}
        if mesh["has_normal"]:
            point["normal"] = read_direction(data, offset + NORMAL, lanes)
        if mesh["has_tangent"]:
            tangent = read_direction(data, offset + TANGENT, lanes)
            binormal = read_direction(data, offset + BINORMAL, lanes)
            point["tangent"] = tangent

            point["handedness"] = (
                -1.0 if dot(cross(point["normal"], tangent), binormal) < 0 else 1.0
            )
        if mesh["has_uv"]:
            point["uv"] = (read_half(data, offset + UV), read_half(data, offset + UV + 2))
        if mesh.get("uv1") is not None:
            at = offset + mesh["uv1"]
            point["uv1"] = (read_half(data, at), read_half(data, at + 2))
        if mesh.get("colour") is not None:
            at = offset + mesh["colour"]
            point["colour"] = tuple(read_half(data, at + n * 2) for n in range(4))
        if mesh["skinned"]:
            slots = data[offset + bone_field : offset + bone_field + 4]
            while cursor + 1 < len(spans) and index >= spans[cursor][1]:
                cursor += 1
            table = spans[cursor][2] if spans and spans[cursor][0] <= index < spans[cursor][1] else None
            point["bones"] = (
                tuple(table[slot] if slot < len(table) else 0 for slot in slots)
                if table
                else tuple(slots)
            )
            point["weights"] = tuple(read_half(data, offset + weight_field + n * 2) for n in range(4))
        yield point


def bone_palette(data, mesh, bone_limit):
    palettes = mesh.get("palettes") or bone_palettes(data, mesh, bone_limit)
    if not palettes or max(max(palette) for palette in palettes) >= bone_limit:
        return None
    if len(palettes) == 1:
        return palettes[0]
    return sorted({bone for palette in palettes for bone in palette})


def bone_palettes(data, mesh, bone_limit):
    if mesh["table_end"] is None:
        return None
    groups = palette_groups(mesh)
    wanted = len(groups) if mesh.get("reversed") and groups else 1
    palettes, offset = [], mesh["table_end"]
    for _ in range(wanted):
        count = data[offset] if offset < len(data) else 0
        palette = list(data[offset + 1 : offset + 1 + count])
        if not count or len(palette) != count or len(set(palette)) != count:
            break
        if max(palette) >= bone_limit:
            break
        palettes.append(palette)
        offset += 1 + count
    return palettes or None


def palette_groups(mesh):
    if not mesh.get("submeshes"):
        return None
    bases = []
    for record in mesh["submeshes"]:
        if record["base_vertex"] not in bases:
            bases.append(record["base_vertex"])
    if any(bases[n] >= bases[n + 1] for n in range(len(bases) - 1)):
        return None
    return [
        (start, bases[number + 1] if number + 1 < len(bases) else mesh["vertex_count"])
        for number, start in enumerate(bases)
    ]


def palette_spans(mesh):
    palettes = mesh.get("palettes")
    groups = palette_groups(mesh)
    if not palettes or len(palettes) < 2 or not groups or len(palettes) != len(groups):
        return None
    combined = sorted({bone for palette in palettes for bone in palette})
    slot = {bone: number for number, bone in enumerate(combined)}
    return [
        (start, end, [slot[bone] for bone in palette])
        for (start, end), palette in zip(groups, palettes)
    ]


def bone_hash_table(data, mesh, count, wanted):
    if count < 1:
        return None
    span = count * BONE_HASH_STRIDE


    stop = min(mesh["vertex_base"], len(data)) - span
    for start in range(0, max(0, stop) + 1, 4):
        first = int.from_bytes(data[start : start + BONE_HASH_BYTES], "big")
        if not first:
            continue
        if count > 1:
            second = data[start + BONE_HASH_STRIDE :][:BONE_HASH_BYTES]
            if first >= int.from_bytes(second, "big"):
                continue
        keys, slots, previous = [], [], -1
        for index in range(count):
            record = data[start + index * BONE_HASH_STRIDE :][:BONE_HASH_STRIDE]
            key = int.from_bytes(record[:BONE_HASH_BYTES], "big")
            if key <= previous or key not in wanted:
                break
            previous = key
            keys.append(key)
            slots.append(record[BONE_HASH_BYTES])
        if len(keys) != count or len(set(slots)) != count or max(slots) != count - 1:
            continue
        return dict(zip(slots, keys))
    return None


def bone_map(data, mesh, skeleton):
    palette = bone_palette(data, mesh, len(skeleton))
    if not palette:
        return None
    lookup = {bone["name_hash"]: bone["index"] for bone in skeleton}
    table = bone_hash_table(data, mesh, len(palette), set(lookup))
    if not table:
        return palette



    resolved, named = [], 0
    for slot in range(len(palette)):
        bone = lookup.get(table.get(palette[slot]))
        if bone is None:
            bone = palette[slot] if palette[slot] < len(skeleton) else 0
        else:
            named += 1
        resolved.append(bone)
    return resolved if named >= len(palette) * BONE_HASH_SHARE else palette


def find_skeleton(path):
    for parent in (path.parent, path.parent.parent, path.parent.parent.parent):
        found = sorted(parent.glob("*.skeleton"))
        if found:
            return found[0]
    return None


def match_texture(stem, textures):
    best = None
    for texture in sorted(textures):
        shared = 0
        while shared < min(len(stem), len(texture.stem)) and stem[shared] == texture.stem[shared]:
            shared += 1
        if shared >= 4 and (best is None or shared > best[0]):
            best = (shared, texture)
    return best[1] if best else None


def match_maps(stem, directory):
    if not directory:
        return {}
    found = {}
    for role, pattern in (("base", "*_cm.png"), ("normal", "*_nm.png")):
        match = match_texture(stem, directory.glob(pattern))
        if match:
            found[role] = match
    return found


def read_submeshes(data, mesh, start):
    records, offset, used = [], start, 0
    while used < mesh["index_count"]:
        if offset + SUBMESH_STRIDE > mesh["vertex_base"]:
            return None
        material = u32(data, offset + SUBMESH_MATERIAL)
        triangles = u32(data, offset + SUBMESH_TRIS)
        if u32(data, offset + SUBMESH_ISTART) != used:
            return None
        if triangles:
            records.append(
                {
                    "triangles": triangles,
                    "index_start": used,
                    "base_vertex": u32(data, offset + SUBMESH_BASE),
                    "vertex_count": u32(data, offset + SUBMESH_VCOUNT),
                    "material": material,
                }
            )
            used += triangles * 3
            if used > mesh["index_count"]:
                return None
        offset += SUBMESH_STRIDE
    return {"submeshes": records, "end": offset}


def submesh_indices(mesh, record):
    start = record["index_start"]
    base = record["base_vertex"]
    return [index + base for index in mesh["indices"][start : start + record["triangles"] * 3]]


def entity_material_uid(data, index, limit):
    materials = {uid for uid, entry in index.items() if entry[0] == "material"}
    if u32(data, MATERIAL_FIELD) in materials:
        return u32(data, MATERIAL_FIELD)
    for offset in range(0x40, min(limit, len(data) - 4), 4):
        if u32(data, offset) in materials:
            return u32(data, offset)
    return None


def material_maps(uid, index, materials, textures, directory):
    entry = index.get(uid)
    source = rio_index.find_resource(materials, entry[1]) if entry else None
    if not source:
        return {}, entry[1].rsplit("/", 1)[-1] if entry else None
    try:
        material = rio_material.parse(source.read_bytes())
    except (ValueError, IndexError, struct.error):
        return {}, source.stem
    found = {}
    for texture in material["textures"]:
        target = index.get(texture["uid"])
        if not target:
            continue
        asset = rio_index.find_resource(textures, target[1])
        if not asset or not directory:
            continue
        png = directory / f"{asset.stem}.png"
        if png.exists():
            found[texture["role"]] = png
    return found, source.stem


def build_parts(data, mesh, index, materials, textures, directory, stem):
    if index is not None:
        if mesh["submeshes"]:
            parts = []
            for record in mesh["submeshes"]:
                maps, name = material_maps(record["material"], index, materials, textures, directory)
                parts.append({"indices": submesh_indices(mesh, record), "maps": maps, "material": name})
            return parts, len(mesh["submeshes"])
        uid = entity_material_uid(data, index, mesh["vertex_base"])
        if uid is not None:
            maps, name = material_maps(uid, index, materials, textures, directory)
            return [{"indices": list(mesh["indices"]), "maps": maps, "material": name}], 0
    maps = match_maps(stem, directory)
    return [{"indices": list(mesh["indices"]), "maps": maps, "material": stem if maps else None}], 0


def face_corner(mesh, slot):
    corner = f"{{{slot}}}"
    if mesh["has_uv"]:
        corner += f"/{{{slot}}}"
    elif mesh["has_normal"]:
        corner += "/"
    if mesh["has_normal"]:
        corner += f"/{{{slot}}}"
    return corner


def write_obj(path, name, mesh, points, parts):
    lines = [f"# {name}", f"# {mesh['vertex_count']} vertices, {mesh['index_count'] // 3} triangles"]
    if any(part["material"] for part in parts):
        lines.append(f"mtllib {name}.mtl")
    for point in points:
        lines.append("v %.6f %.6f %.6f" % point["position"])
    if mesh["has_uv"]:
        for point in points:
            lines.append("vt %.6f %.6f" % (point["uv"][0], 1.0 - point["uv"][1]))
    if mesh["has_normal"]:
        for point in points:
            lines.append("vn %.6f %.6f %.6f" % point["normal"])
    template = "f " + " ".join(face_corner(mesh, slot) for slot in range(3))
    for number, part in enumerate(parts):
        lines.append(f"g {name}_{number}" if len(parts) > 1 else f"g {name}")
        if part["material"]:
            lines.append(f"usemtl {part['material']}")
        indices = part["indices"]
        for triangle in range(0, len(indices), 3):
            corners = [index + 1 for index in indices[triangle : triangle + 3]]
            lines.append(template.format(*corners))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_mtl_library(path, parts):
    lines, written = [], set()
    for part in parts:
        if not part["material"] or part["material"] in written:
            continue
        written.add(part["material"])
        lines += [
            f"newmtl {part['material']}",
            "Ka 1.000 1.000 1.000",
            "Kd 1.000 1.000 1.000",
            "Ks 1.000 1.000 1.000",
            "d 1.0",
            "illum 2",
        ]
        for role, keyword in (("base", "map_Kd"), ("specular", "map_Ks"), ("normal", "map_Bump")):
            if part["maps"].get(role):
                lines.append(f"{keyword} {part['maps'][role].name}")
        lines.append("")
    if lines:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


GLB_MAGIC = 0x46546C67
GLB_JSON, GLB_BIN = 0x4E4F534A, 0x004E4942
FLOAT, UNSIGNED_SHORT, UNSIGNED_BYTE = 5126, 5123, 5121
ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER = 34962, 34963


def pad(blob, filler=b"\0"):
    return blob + filler * (-len(blob) % 4)


def skin_nodes(bones, palette):
    nodes = []
    for bone in bones:
        node = {"name": f"bone_{bone['index']:03d}_{bone['name_hash']:014x}"}
        rotation, translation = bone["local"]["rotation"], bone["local"]["translation"]
        if any(rotation[n] != (0.0, 0.0, 0.0, 1.0)[n] for n in range(4)):
            node["rotation"] = list(rotation)
        if any(translation):
            node["translation"] = list(translation)
        nodes.append(node)
    for bone in bones:
        if bone["parent"] is not None:
            nodes[bone["parent"]].setdefault("children", []).append(bone["index"])
    roots = [bone["index"] for bone in bones if bone["parent"] is None]
    return nodes, [palette[slot] for slot in range(len(palette))], roots


def write_glb(path, name, mesh, points, parts, skeleton=None, palette=None):
    chunks, views, accessors = [], [], []

    def add(blob, target=None):
        offset = sum(len(chunk) for chunk in chunks)
        chunks.append(pad(blob))
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(blob)}
        if target:
            view["target"] = target
        views.append(view)
        return len(views) - 1

    def attribute(blob, kind, component, count, target=ARRAY_BUFFER, bounds=None):
        accessor = {
            "bufferView": add(blob, target),
            "componentType": component,
            "count": count,
            "type": kind,
        }
        if bounds:
            accessor["min"], accessor["max"] = bounds
        accessors.append(accessor)
        return len(accessors) - 1

    count = mesh["vertex_count"]
    positions = [point["position"] for point in points]
    low = [min(axis[n] for axis in positions) for n in range(3)]
    high = [max(axis[n] for axis in positions) for n in range(3)]
    attributes = {
        "POSITION": attribute(
            b"".join(struct.pack("<3f", *p) for p in positions), "VEC3", FLOAT, count, bounds=(low, high)
        )
    }
    if mesh["has_normal"]:
        attributes["NORMAL"] = attribute(
            b"".join(struct.pack("<3f", *point["normal"]) for point in points), "VEC3", FLOAT, count
        )
    if mesh["has_tangent"]:
        attributes["TANGENT"] = attribute(
            b"".join(struct.pack("<4f", *point["tangent"], point["handedness"]) for point in points),
            "VEC4",
            FLOAT,
            count,
        )
    if mesh["has_uv"]:
        attributes["TEXCOORD_0"] = attribute(
            b"".join(struct.pack("<2f", point["uv"][0], point["uv"][1]) for point in points),
            "VEC2",
            FLOAT,
            count,
        )
    if points and "uv1" in points[0]:
        attributes["TEXCOORD_1"] = attribute(
            b"".join(struct.pack("<2f", point["uv1"][0], point["uv1"][1]) for point in points),
            "VEC2",
            FLOAT,
            count,
        )
    if points and "colour" in points[0]:
        attributes["COLOR_0"] = attribute(
            b"".join(
                struct.pack("<4f", *(min(1.0, max(0.0, value)) for value in point["colour"]))
                for point in points
            ),
            "VEC4",
            FLOAT,
            count,
        )

    rigged = bool(skeleton and mesh["skinned"] and palette)
    if rigged:
        joint_blob, weight_blob = bytearray(), bytearray()
        for point in points:
            weights = [max(0.0, value) for value in point["weights"]]
            total = sum(weights) or 1.0

            joints = [bone if weight > 0 else 0 for bone, weight in zip(point["bones"], weights)]
            joint_blob += struct.pack("<4B", *joints)
            weight_blob += struct.pack("<4f", *(value / total for value in weights))
        attributes["JOINTS_0"] = attribute(bytes(joint_blob), "VEC4", UNSIGNED_BYTE, count)
        attributes["WEIGHTS_0"] = attribute(bytes(weight_blob), "VEC4", FLOAT, count)

    gltf = {
        "asset": {"version": "2.0", "generator": "rio_entity.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": name}],
        "meshes": [{"name": name, "primitives": []}],
        "accessors": accessors,
        "bufferViews": views,
        "materials": [],
    }
    images, textures, sources = [], [], {}

    def texture_slot(source):
        if source not in sources:
            sources[source] = len(textures)
            images.append(
                {"bufferView": add(source.read_bytes()), "mimeType": "image/png", "name": source.stem}
            )
            textures.append({"sampler": 0, "source": len(images) - 1})
        return sources[source]

    material_slots = {}
    for part in parts:
        key = (part["material"], tuple(sorted((r, str(m)) for r, m in part["maps"].items())))
        if key in material_slots:
            gltf["meshes"][0]["primitives"].append(
                {
                    "attributes": attributes,
                    "indices": attribute(
                        struct.pack(f"<{len(part['indices'])}H", *part["indices"]),
                        "SCALAR",
                        UNSIGNED_SHORT,
                        len(part["indices"]),
                        target=ELEMENT_ARRAY_BUFFER,
                    ),
                    "material": material_slots[key],
                }
            )
            continue
        material = {
            "name": part["material"] or f"{name}_material",
            "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 0.85},
            "doubleSided": False,
        }
        maps = part["maps"] if mesh["has_uv"] else {}
        if maps.get("base"):
            material["pbrMetallicRoughness"]["baseColorTexture"] = {"index": texture_slot(maps["base"])}
        if maps.get("normal"):


            material["normalTexture"] = {"index": texture_slot(maps["normal"]), "scale": 1.0}
        if maps.get("specular"):


            material.setdefault("extensions", {})["KHR_materials_specular"] = {
                "specularColorTexture": {"index": texture_slot(maps["specular"])}
            }
            if "KHR_materials_specular" not in gltf.setdefault("extensionsUsed", []):
                gltf["extensionsUsed"].append("KHR_materials_specular")
        material_slots[key] = len(gltf["materials"])
        gltf["materials"].append(material)
        gltf["meshes"][0]["primitives"].append(
            {
                "attributes": attributes,
                "indices": attribute(
                    struct.pack(f"<{len(part['indices'])}H", *part["indices"]),
                    "SCALAR",
                    UNSIGNED_SHORT,
                    len(part["indices"]),
                    target=ELEMENT_ARRAY_BUFFER,
                ),
                "material": len(gltf["materials"]) - 1,
            }
        )
    if textures:
        gltf["images"] = images
        gltf["samplers"] = [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}]
        gltf["textures"] = textures

    if rigged:
        joints, joint_list, roots = skin_nodes(skeleton, palette)
        mesh_node = len(joints)
        gltf["nodes"] = joints + [{"mesh": 0, "name": name, "skin": 0}]
        gltf["scenes"] = [{"nodes": roots + [mesh_node]}]
        matrices = b"".join(
            struct.pack("<16f", *rio_skeleton.inverse_bind_matrix(skeleton[bone]["world"]))
            for bone in joint_list
        )
        gltf["skins"] = [
            {
                "name": f"{name}_skin",
                "joints": joint_list,
                "skeleton": roots[0],
                "inverseBindMatrices": attribute(matrices, "MAT4", FLOAT, len(joint_list), target=None),
            }
        ]

    binary = b"".join(chunks)
    gltf["buffers"] = [{"byteLength": len(binary)}]
    payload = pad(json.dumps(gltf, separators=(",", ":")).encode("utf-8"), b" ")
    total = 12 + 8 + len(payload) + 8 + len(binary)
    with path.open("wb") as sink:
        sink.write(struct.pack("<3I", GLB_MAGIC, 2, total))
        sink.write(struct.pack("<2I", len(payload), GLB_JSON))
        sink.write(payload)
        sink.write(struct.pack("<2I", len(binary), GLB_BIN))
        sink.write(binary)


def report(name, mesh):
    kind = "skinned" if mesh["skinned"] else "static"
    detail = f", {mesh['bones']} bones" if mesh["skinned"] else ""
    missing = "".join(
        label for label, held in ((", no UVs", mesh["has_uv"]), (", no tangents", mesh["has_tangent"])) if not held
    )
    print(f"{name}")
    print(
        f"    {mesh['vertex_count']} vertices, {mesh['index_count'] // 3} triangles, "
        f"stride {mesh['stride']}, {kind}{detail}{missing}"
    )
    if mesh["bounds"]:
        low, high = mesh["bounds"]
        print(
            f"    bounds ({low[0]:.3f}, {low[1]:.3f}, {low[2]:.3f})"
            f" .. ({high[0]:.3f}, {high[1]:.3f}, {high[2]:.3f})"
        )


def entity_files(values):
    found = []
    for value in values:
        path = Path(value)
        found.extend(sorted(path.rglob("*.entity")) if path.is_dir() else [path])
    return found


def main():
    parser = argparse.ArgumentParser(description="Convert Rio .entity meshes to OBJ or glTF")
    parser.add_argument("input", nargs="+", help=".entity files or directories to search")
    parser.add_argument("-o", "--output", type=Path, help="output directory (default: alongside input)")
    parser.add_argument("-f", "--format", choices=("obj", "glb", "both"), default="obj")
    parser.add_argument("-n", "--dry-run", action="store_true", help="report structure only")
    parser.add_argument("-q", "--quiet", action="store_true", help="totals only")
    parser.add_argument("--textures", type=Path, help="directory of converted PNGs, for materials")
    parser.add_argument("--no-rig", action="store_true", help="skip skeleton lookup, export unskinned")
    parser.add_argument("--index", type=Path, help="resource_index.tsv, to resolve materials by UID")
    parser.add_argument("--assets", type=Path, help="extracted assets root, holding the .material files")
    args = parser.parse_args()
    files = entity_files(args.input)
    if not files:
        parser.error("no .entity files found")
    index = materials = textures = None
    if args.index:
        index = rio_index.load(args.index)
        root = args.assets or Path("extracted_assets")
        materials = rio_index.resource_files(root, ".material")
        textures = rio_index.resource_files(root, ".texture")
        print(f"index: {len(index)} resources, {len(materials)} materials and {len(textures)} textures on disk")
    converted = failed = 0
    reasons = {}
    for path in files:
        try:
            data = path.read_bytes()
            mesh = parse(data)
            points = list(vertices(data, mesh))
        except (ValueError, IndexError, struct.error) as error:
            reasons[str(error)] = reasons.get(str(error), 0) + 1
            if not args.quiet:
                print(f"{path.name}: SKIPPED - {error}")
            failed += 1
            continue
        skeleton = palette = None
        if mesh["skinned"] and not args.no_rig:
            found = find_skeleton(path)
            if found:
                try:
                    skeleton = rio_skeleton.parse(found.read_bytes())
                    palette = bone_map(data, mesh, skeleton)
                    if palette is None:
                        skeleton = None
                except (ValueError, IndexError, struct.error):
                    skeleton = None
        if not args.quiet:
            report(path.name, mesh)
            if skeleton:
                error = rio_skeleton.composition_error(skeleton)
                print(
                    f"    rig: {len(skeleton)} bones from {found.name}, "
                    f"{len(palette)} joints bound, pose composition error {error:.2e}"
                )
            elif mesh["skinned"] and not args.no_rig:
                print("    rig: no usable skeleton found, exporting unskinned")
        if not args.dry_run:
            destination = args.output or path.parent
            destination.mkdir(parents=True, exist_ok=True)
            parts, submeshes = build_parts(
                data, mesh, index, materials, textures, args.textures, path.stem
            )
            if not args.quiet:
                names = sorted({part["material"] for part in parts if part["material"]})
                shape = f"{submeshes} submeshes" if submeshes else "one part (no submesh table)"
                print(f"    {shape}, {len(names)} material(s): {', '.join(names) or 'none resolved'}")
                for part in parts[:1]:
                    detail = ", ".join(f"{r}={m.name}" for r, m in sorted(part["maps"].items()))
                    if detail:
                        print(f"      first material maps: {detail}")
            if args.format in ("obj", "both"):
                write_obj(destination / f"{path.stem}.obj", path.stem, mesh, points, parts)
                write_mtl_library(destination / f"{path.stem}.mtl", parts)
            if args.format in ("glb", "both"):
                write_glb(destination / f"{path.stem}.glb", path.stem, mesh, points, parts, skeleton, palette)
        converted += 1
    print(f"\n{converted} converted, {failed} skipped")
    for reason, occurrences in sorted(reasons.items(), key=lambda item: -item[1]):
        print(f"    {occurrences:5} x {reason}")


if __name__ == "__main__":
    main()
