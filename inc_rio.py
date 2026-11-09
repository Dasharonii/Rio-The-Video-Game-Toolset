"""Pure-Python Rio resource core.

No Noesis imports live here, so the format work can be tested outside the
editor. Every parser mirrors the rio_* command line tools in
`Rio Tools Final/tools`; `test_rio_core.py` asserts that parity.

Resources are big-endian and identified by the type byte at 0x03.
"""

import glob
import os
import re
import struct

# Noesis embeds Python 3.2, so this module deliberately avoids pathlib, f
# strings and other features newer than that interpreter.

# ---------------------------------------------------------------- headers

TYPE_OFFSET = 0x03
MATERIAL_TYPE = 0x00
TEXTURE_TYPE = 0x08
ENTITY_TYPE = 0x09
SKELETON_TYPE = 0x0A

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

RESOURCE_TYPE_MASK = 0xFFF00000
RESOURCE_PAYLOAD_MASK = 0x000FFFFF


def u32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def u16(data, offset):
    return struct.unpack_from(">H", data, offset)[0]


def signed32(value):
    return value - (1 << 32) if value & 0x80000000 else value


def relative_target(data, field):
    return field + signed32(u32(data, field))


def resource_type(data):
    """Human-facing resource kind for the type byte at 0x03."""
    if len(data) <= TYPE_OFFSET:
        return None
    return {
        MATERIAL_TYPE: "material",
        TEXTURE_TYPE: "texture",
        ENTITY_TYPE: "entity",
        SKELETON_TYPE: "skeleton",
    }.get(data[TYPE_OFFSET], "unknown")


# ---------------------------------------------------------------- texture

WIDTH = 0x28
HEIGHT = 0x2A
PIXEL_FORMAT = 0x2F
MIP_COUNT = 0x32
HEADER_EXTRA = 0x24
PAYLOAD_BASE = 0x50

PIXEL_FORMATS = {1: "DXT5", 3: "DXT1", 4: "DXT1", 12: "DXT5"}
BLOCK_BYTES = {"DXT1": 8, "DXT5": 16, "BC5": 16}
DDS_FOURCC = {"DXT1": b"DXT1", "DXT5": b"DXT5", "BC5": b"ATI2"}

TILED_NORMAL_FORMAT = 12
TILED_PAGE = 0x1000
TILE_BLOCKS = 32
TILED_BASE_MINIMUM = 0x400
MIN_TILED_BLOCKS = 8
MAX_TILED_LEVELS = 16


def mip_size(fourcc, width, height):
    blocks = max(1, (width + 3) // 4) * max(1, (height + 3) // 4)
    return blocks * BLOCK_BYTES[fourcc]


def block_extent(width, height):
    wide, high = max(1, (width + 3) // 4), max(1, (height + 3) // 4)
    align = TILE_BLOCKS - 1
    return wide, high, (wide + align) & ~align, (high + align) & ~align


def tiled_span(fourcc, width, height):
    _, _, aligned_wide, aligned_high = block_extent(width, height)
    return aligned_wide * aligned_high * BLOCK_BYTES[fourcc]


def tiled_levels(texture):
    pitch = BLOCK_BYTES[texture["fourcc"]]
    levels, offset, used = [], texture["payload"], 0
    for level in range(MAX_TILED_LEVELS):
        width = max(1, texture["width"] >> level)
        height = max(1, texture["height"] >> level)
        wide, high, aligned_wide, aligned_high = block_extent(width, height)
        span = aligned_wide * aligned_high * pitch
        if used + span > texture["available"]:
            break
        if levels and (wide < MIN_TILED_BLOCKS or high < MIN_TILED_BLOCKS):
            break
        levels.append((offset, width, height))
        offset += span
        used += span
    return levels


def linear_levels(texture):
    fourcc = texture["fourcc"]
    levels, total = [], 0
    for level in range(texture["mips"]):
        width = max(1, texture["width"] >> level)
        height = max(1, texture["height"] >> level)
        step = mip_size(fourcc, width, height)
        if total + step > texture["available"]:
            break
        levels.append((texture["payload"] + total, width, height))
        total += step
    return levels


def levels(texture):
    """Mip slices actually backed by the payload, in decode order."""
    return tiled_levels(texture) if texture["tiled"] else linear_levels(texture)


def tiled_offset(x, y, aligned_wide, log_bpp):
    macro = ((x >> 5) + (y >> 5) * (aligned_wide >> 5)) << (log_bpp + 7)
    micro = ((x & 7) + ((y & 6) << 2)) << log_bpp
    offset = macro + ((micro & ~0xF) << 1) + (micro & 0xF) + ((y & 8) << (3 + log_bpp)) + ((y & 1) << 4)
    return (
        ((offset & ~0x1FF) << 3)
        + ((offset & 0x1C0) << 2)
        + (offset & 0x3F)
        + ((y & 16) << 7)
        + (((((y & 8) >> 2) + (x >> 3)) & 3) << 6)
    ) >> log_bpp


def detile(data, fourcc, offset, width, height):
    """Software Xenos detile. The adapter uses rapi.imageUntile360DXT instead."""
    pitch = BLOCK_BYTES[fourcc]
    log_bpp = (pitch >> 2) + ((pitch >> 1) >> (pitch >> 2))
    wide, high, aligned_wide, aligned_high = block_extent(width, height)
    span = aligned_wide * aligned_high * pitch
    source = bytearray(data[offset : offset + span])
    source.extend(bytes(span - len(source)))
    source[0::2], source[1::2] = source[1::2], source[0::2]
    flat = bytearray(wide * high * pitch)
    for y in range(high):
        row = y * wide
        for x in range(wide):
            start = tiled_offset(x, y, aligned_wide, log_bpp) * pitch
            target = (row + x) * pitch
            flat[target : target + pitch] = source[start : start + pitch]
    return bytes(flat)


def parse_texture(data, tolerant=False):
    if len(data) < PAYLOAD_BASE + 4:
        raise ValueError("truncated texture resource")
    if data[TYPE_OFFSET] != TEXTURE_TYPE:
        raise ValueError("not a texture resource (type %#04x)" % data[TYPE_OFFSET])
    raw_format = data[PIXEL_FORMAT]
    if raw_format not in PIXEL_FORMATS:
        raise ValueError("unknown pixel format %d" % raw_format)
    fourcc = PIXEL_FORMATS[raw_format]
    width, height = u16(data, WIDTH), u16(data, HEIGHT)
    payload = u32(data, PAYLOAD_BASE) + u32(data, HEADER_EXTRA)
    tiled = u32(data, PAYLOAD_BASE) >= TILED_BASE_MINIMUM
    if tiled:
        payload = (payload + TILED_PAGE - 1) & ~(TILED_PAGE - 1)
        if raw_format == TILED_NORMAL_FORMAT:
            fourcc = "BC5"
    mips = max(1, data[MIP_COUNT])
    available = len(data) - payload
    needed = tiled_span(fourcc, width, height) if tiled else mip_size(fourcc, width, height)
    truncated = available < needed
    if truncated and not tolerant:
        raise ValueError(
            "payload holds %d bytes, too small for %dx%d %s" % (available, width, height, fourcc)
        )
    return {
        "fourcc": fourcc,
        "raw_format": raw_format,
        "width": width,
        "height": height,
        "mips": mips,
        "payload": payload,
        "available": available,
        "tiled": tiled,
        "truncated": truncated,
    }


def normal_kind(texture):
    """'bc5', 'dxt5nm', or None, following the tools' classification."""
    if texture["raw_format"] != TILED_NORMAL_FORMAT:
        return None
    return "bc5" if texture["tiled"] else "dxt5nm"


def looks_like_dxt5nm(rgba):
    """True when decoded RGBA follows the DXT5nm convention (x in A, y in G)."""
    if len(rgba) < 4:
        return False
    if any(rgba[0::4]) or any(rgba[2::4]):
        return False
    alpha = rgba[3::4]
    return min(alpha) != max(alpha)


def texture_payloads(data, texture):
    """Raw (still tiled where applicable) bytes for each backed mip."""
    payloads = []
    for offset, width, height in levels(texture):
        if texture["tiled"]:
            span = tiled_span(texture["fourcc"], width, height)
        else:
            span = mip_size(texture["fourcc"], width, height)
        payloads.append((width, height, data[offset : offset + span]))
    return payloads


def padded_data(data, texture):
    """First mip, zero-padded to its nominal span (damaged/short files)."""
    width, height = texture["width"], texture["height"]
    if texture["tiled"]:
        span = tiled_span(texture["fourcc"], width, height)
    else:
        span = mip_size(texture["fourcc"], width, height)
    payload = data[texture["payload"] : texture["payload"] + span]
    return width, height, payload + bytes(span - len(payload))


def detiled_levels(data, texture):
    """Software-decoded mip payloads; slow, for tests and fallbacks."""
    result = []
    for offset, width, height in levels(texture):
        if texture["tiled"]:
            result.append((width, height, detile(data, texture["fourcc"], offset, width, height)))
        else:
            span = mip_size(texture["fourcc"], width, height)
            result.append((width, height, data[offset : offset + span]))
    return result


# ---------------------------------------------------------------- skeleton

BONE_COUNT = 0x28
POSE_STRIDE = 32
HIERARCHY_STRIDE = 16
HASH_STRIDE = 8
HASH_BYTES = 7
NO_PARENT = 0xFFFF

SKELETON_LAYOUTS = (
    {"name": "standard", "hashes": 0x38, "world": 0x3C, "local": 0x40, "hierarchy": 0x44},
    {"name": "staff", "hashes": 0x3C, "world": 0x30, "local": 0x34, "hierarchy": 0x38},
)


def read_pose(data, offset):
    values = struct.unpack_from(">8f", data, offset)
    return {"rotation": values[0:4], "translation": values[4:7]}


def _pose_fits(data, target, count):
    return 0 < target and target + count * POSE_STRIDE <= len(data)


def _skeleton_with(data, layout, count):
    hierarchy = relative_target(data, layout["hierarchy"])
    world = relative_target(data, layout["world"])
    local = relative_target(data, layout["local"])
    hashes = relative_target(data, layout["hashes"]) + HASH_STRIDE
    if hierarchy <= 0 or hierarchy + count * HIERARCHY_STRIDE > len(data):
        raise ValueError("bone hierarchy table falls outside the resource")
    if not _pose_fits(data, world, count) or not _pose_fits(data, local, count):
        raise ValueError("pose arrays fall outside the resource")
    if not 0 < hashes < len(data):
        raise ValueError("name hash table falls outside the resource")
    bones = []
    for index in range(count):
        record = hierarchy + index * HIERARCHY_STRIDE
        parent = u16(data, record)
        slot = u16(data, record + 4)
        if slot >= count or hashes + slot * HASH_STRIDE + HASH_BYTES > len(data):
            raise ValueError("bone %d names slot %d outside the hash table" % (index, slot))
        bones.append(
            {
                "index": index,
                "parent": None if parent == NO_PARENT else parent,
                "subtree_end": u16(data, record + 2),
                "name_hash": int.from_bytes(data[hashes + slot * HASH_STRIDE :][:HASH_BYTES], "big"),
                "local": read_pose(data, local + index * POSE_STRIDE),
                "world": read_pose(data, world + index * POSE_STRIDE),
            }
        )
    roots = 0
    for bone in bones:
        if bone["parent"] is None:
            roots += 1
        elif bone["parent"] >= bone["index"]:
            raise ValueError("bone %d has forward parent %d" % (bone["index"], bone["parent"]))
    if not roots:
        raise ValueError("skeleton has no root bone")
    return bones


def parse_skeleton_layout(data):
    """Return (bones, layout_name), trying the standard header first."""
    if len(data) <= BONE_COUNT + 1:
        raise ValueError("truncated skeleton resource")
    if data[TYPE_OFFSET] != SKELETON_TYPE:
        raise ValueError("not a skeleton resource (type %#04x)" % data[TYPE_OFFSET])
    count = u16(data, BONE_COUNT)
    if not count:
        raise ValueError("skeleton has no bones")
    errors = []
    for layout in SKELETON_LAYOUTS:
        try:
            return _skeleton_with(data, layout, count), layout["name"]
        except (ValueError, IndexError, struct.error) as error:
            errors.append("%s: %s" % (layout["name"], error))
    raise ValueError("no skeleton header layout fits (%s)" % "; ".join(errors))


def parse_skeleton(data):
    return parse_skeleton_layout(data)[0]


def quaternion_multiply(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quaternion_rotate(q, v):
    x, y, z, w = q
    ux, uy, uz = x, y, z
    cx, cy, cz = uy * v[2] - uz * v[1], uz * v[0] - ux * v[2], ux * v[1] - uy * v[0]
    cx, cy, cz = cx + w * v[0], cy + w * v[1], cz + w * v[2]
    return (
        v[0] + 2 * (uy * cz - uz * cy),
        v[1] + 2 * (uz * cx - ux * cz),
        v[2] + 2 * (ux * cy - uy * cx),
    )


def rotation_matrix(q):
    x, y, z, w = q
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )


def inverse_bind_matrix(pose):
    r = rotation_matrix(pose["rotation"])
    t = pose["translation"]
    offset = [-sum(r[row][column] * t[row] for row in range(3)) for column in range(3)]
    return [
        r[0][0], r[0][1], r[0][2], 0.0,
        r[1][0], r[1][1], r[1][2], 0.0,
        r[2][0], r[2][1], r[2][2], 0.0,
        offset[0], offset[1], offset[2], 1.0,
    ]


def composition_error(bones):
    worst = 0.0
    composed = []
    for bone in bones:
        rotation, translation = bone["local"]["rotation"], bone["local"]["translation"]
        if bone["parent"] is not None:
            parent = composed[bone["parent"]]
            rotation = quaternion_multiply(parent["rotation"], rotation)
            moved = quaternion_rotate(parent["rotation"], translation)
            translation = tuple(parent["translation"][n] + moved[n] for n in range(3))
        composed.append({"rotation": rotation, "translation": translation})
        stored = bone["world"]["translation"]
        worst = max(worst, max(abs(translation[n] - stored[n]) for n in range(3)))
    return worst


# ---------------------------------------------------------------- entity

ENTITY_BONE_COUNT = 0x44
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
    while True:
        offset = data.find(needle, search)
        if offset < 0:
            break
        search = offset + 4
        if offset % 4 or u32(data, offset + 4) != INDEX_BLOCK_FLAGS:
            continue
        payload = offset + INDEX_BLOCK_HEADER
        if payload + index_count * 2 > len(data):
            continue
        indices = struct.unpack_from(">%dH" % index_count, data, payload)
        if max(indices) >= vertex_count:
            continue
        span = len(data) - (offset + size)
        if span <= 0 or span % vertex_count:
            continue
        return offset + size, indices
    raise ValueError("no index block consistent with the header counts")


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
        indices = struct.unpack_from(">%dH" % index_count, data, index_base)
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
    boxes = []
    for part in range(u16(data, PART_COUNT)):
        box = part_box(data, base + part * PART_STRIDE)
        if box:
            boxes.append(box)
    if not boxes:
        return None
    return (
        tuple(min(box[0][axis] for box in boxes) for axis in range(3)),
        tuple(max(box[1][axis] for box in boxes) for axis in range(3)),
    )


def parse_entity(data):
    if len(data) <= INDEX_COUNT + 4:
        raise ValueError("truncated entity resource")
    if data[TYPE_OFFSET] != ENTITY_TYPE:
        raise ValueError("not an entity resource (type %#04x)" % data[TYPE_OFFSET])
    vertex_count, index_count = u32(data, VERTEX_COUNT), u32(data, INDEX_COUNT)
    if not vertex_count or not index_count:
        raise ValueError("empty mesh")
    if index_count % 3:
        raise ValueError("%d indices is not a whole number of triangles" % index_count)
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
    bones = data[ENTITY_BONE_COUNT]
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


def entity_vertices(data, mesh):
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


def bone_palette(data, mesh, bone_limit):
    palettes = mesh.get("palettes") or bone_palettes(data, mesh, bone_limit)
    if not palettes or max(max(palette) for palette in palettes) >= bone_limit:
        return None
    if len(palettes) == 1:
        return palettes[0]
    return sorted({bone for palette in palettes for bone in palette})


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


def submesh_indices(mesh, record):
    start = record["index_start"]
    base = record["base_vertex"]
    return [index + base for index in mesh["indices"][start : start + record["triangles"] * 3]]


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


def whole_mesh(mesh):
    return {
        "triangles": mesh["index_count"] // 3,
        "index_start": 0,
        "base_vertex": 0,
        "vertex_count": mesh["vertex_count"],
        "material": 0,
    }


def find_skeleton(path):
    """Nearest sibling skeleton, matching rio_entity's search."""
    folder = os.path.dirname(os.path.abspath(str(path)))
    for _ in range(3):
        found = sorted(glob.glob(os.path.join(folder, "*.skeleton")))
        if found:
            return found[0]
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return None


def skeleton_candidates(path):
    """Every skeleton in the entity's directory, parents first."""
    seen, found = set(), []
    folder = os.path.dirname(os.path.abspath(str(path)))
    for _ in range(3):
        for candidate in sorted(glob.glob(os.path.join(folder, "*.skeleton"))):
            if candidate not in seen:
                seen.add(candidate)
                found.append(candidate)
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return found


def entity_material_uid(data, index, limit, materials=None):
    if materials is None:
        materials = {uid for uid, entry in index.items() if entry[0] == "material"}
    if u32(data, MATERIAL_FIELD) in materials:
        return u32(data, MATERIAL_FIELD)
    for offset in range(0x40, min(limit, len(data) - 4), 4):
        if u32(data, offset) in materials:
            return u32(data, offset)
    return None


# ---------------------------------------------------------------- material

SHADER_UID = 0x28
TEXTURE_COUNT = 0x30
TEXTURE_TABLE = 0x50
TEXTURE_ENTRY = 8
MAX_TEXTURES = 32

SLOT_ROLES = {0x00000000: "base", 0x10000001: "normal", 0x10000002: "specular"}


def parse_material(data):
    if len(data) <= TEXTURE_TABLE + 4:
        raise ValueError("truncated material resource")
    if data[TYPE_OFFSET] != MATERIAL_TYPE:
        raise ValueError("not a material resource (type %#04x)" % data[TYPE_OFFSET])
    count = u32(data, TEXTURE_COUNT)
    if count > MAX_TEXTURES:
        raise ValueError("implausible texture count %d" % count)
    table = relative_target(data, TEXTURE_TABLE)
    if count and not 0 < table <= len(data) - count * TEXTURE_ENTRY:
        raise ValueError("texture table falls outside the resource")
    textures = []
    for entry in range(count):
        uid, slot = struct.unpack_from(">2I", data, table + entry * TEXTURE_ENTRY)
        textures.append({"uid": uid, "slot": slot, "role": SLOT_ROLES.get(slot, "slot_%#010x" % slot)})
    return {"shader": u32(data, SHADER_UID), "textures": textures}


def resolve_material(material, index):
    resolved = {}
    for texture in material["textures"]:
        entry = index.get(texture["uid"])
        if entry:
            resolved[texture["role"]] = entry[1]
    return resolved


# ---------------------------------------------------------------- index

LIST_OFFSETS = range(0x48, 0xB0, 8)
RECORD_SIZE = 20
INDEX_NAME = "resource_index.tsv"
SEARCH_DEPTH = 8


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


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def path_stem(path):
    return os.path.splitext(os.path.basename(str(path)))[0]


def index_archive(path):
    data = read_bytes(path)
    if data[:4] != b"GEOM":
        raise ValueError("%s: not an EDB" % os.path.basename(str(path)))
    names = embedded_names(data)
    for list_offset in LIST_OFFSETS:
        count = u32(data, list_offset)
        table = relative_target(data, list_offset + 4)
        for entry in range(count):
            record = table + entry * RECORD_SIZE
            _, uid, _, name_index, _ = struct.unpack_from(">5I", data, record)
            if name_index >= len(names):
                continue
            kind = RESOURCE_TYPES.get(uid & RESOURCE_TYPE_MASK, "type_%08x" % (uid & RESOURCE_TYPE_MASK))
            yield uid, kind, normalise(names[name_index])


def build_index(archives):
    index, collisions = {}, 0
    for path in archives:
        for uid, kind, name in index_archive(path):
            existing = index.get(uid)
            if existing and existing[1] != name:
                collisions += 1
            index[uid] = (kind, name, path_stem(path))
    return index, collisions


def load_index(path):
    index = {}
    with open(str(path), "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    for line in lines[1:]:
        uid, kind, name, archive = line.split("\t")
        index[int(uid, 16)] = (kind, name, archive)
    return index


def find_index(path):
    current = os.path.dirname(os.path.abspath(str(path)))
    for _ in range(SEARCH_DEPTH):
        candidate = os.path.join(current, INDEX_NAME)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def resource_files(root, suffix):
    catalogue = {}
    for base, _, files in os.walk(str(root)):
        for filename in files:
            if not filename.endswith(suffix):
                continue
            path = os.path.join(base, filename)
            parts = os.path.splitext(path)[0].replace("\\", "/").split("/")
            for start in range(len(parts)):
                catalogue.setdefault("/".join(parts[start:]).lower(), path)
    return catalogue


def find_resource(catalogue, name):
    return catalogue.get(name.lower())


# ---------------------------------------------------------------- catalogue

COLOUR_VARIANT = re.compile(r"^(.*?)(\d+)(_cm)$")
COMPANION_ROLES = {"normal": "nm", "specular": "sm"}
WIRED_ROLES = ("base", "normal", "specular")


class Catalogue:
    """UID index plus the material/texture files it points at."""

    def __init__(self, index_path):
        self.index_path = str(index_path)
        self.index = load_index(self.index_path)
        self.material_uids = {
            uid for uid, entry in self.index.items() if entry[0] == "material"
        }
        root = os.path.dirname(os.path.abspath(self.index_path))
        self.materials = resource_files(root, ".material")
        self.textures = resource_files(root, ".texture")
        self.sizes = {}
        self.folders = {}
        for kind, name, _ in self.index.values():
            if kind == "texture" and "/" in name:
                folder, leaf = name.rsplit("/", 1)
                self.folders.setdefault(folder, set()).add(leaf)

    def texture_file(self, name):
        return find_resource(self.textures, name)

    def dimensions(self, name):
        if name not in self.sizes:
            source = self.texture_file(name)
            try:
                texture = parse_texture(read_bytes(source)) if source else None
            except (ValueError, IndexError, struct.error, OSError):
                texture = None
            self.sizes[name] = (texture["width"], texture["height"]) if texture else None
        return self.sizes[name]

    def maps(self, uid):
        entry = self.index.get(uid)
        source = find_resource(self.materials, entry[1]) if entry else None
        if not source:
            return {}, None
        try:
            material = parse_material(read_bytes(source))
        except (ValueError, IndexError, struct.error):
            return {}, path_stem(source)
        found = {}
        for texture in material["textures"]:
            target = self.index.get(texture["uid"])
            if target:
                found[texture["role"]] = target[1]
        found.update(self.companions(found))
        return found, path_stem(source)

    def companions(self, found):
        colour = found.get("base")
        if not colour or "/" not in colour:
            return {}
        folder, leaf = colour.rsplit("/", 1)
        stem = leaf[: -len("_cm")] if leaf.endswith("_cm") else leaf
        extra = {}
        for role, suffix in COMPANION_ROLES.items():
            if role in found:
                continue
            for candidate in ("%s_%s" % (stem, suffix), "%s_%s" % (re.sub(r"[0-9]+$", "", stem), suffix)):
                name = "%s/%s" % (folder, candidate)
                if candidate in self.folders.get(folder, ()) and self.dimensions(name) == self.dimensions(colour):
                    extra[role] = name
                    break
        return extra

    def variants(self, colour):
        if not colour or "/" not in colour:
            return {}
        folder, leaf = colour.rsplit("/", 1)
        found = COLOUR_VARIANT.match(leaf)
        if not found:
            return {}
        family = found.group(1), found.group(3)
        siblings = {}
        for other in sorted(self.folders.get(folder, ())):
            match = COLOUR_VARIANT.match(other)
            if other != leaf and match and (match.group(1), match.group(3)) == family:
                token = variant_token(leaf, other)
                siblings[token] = "%s/%s" % (folder, other)
        return siblings


def variant_token(default, variant):
    cut = 0
    for position, (left, right) in enumerate(zip(default, variant)):
        if left != right:
            break
        if left == "_":
            cut = position + 1
    name = variant[cut:]
    return name[: -len("_cm")] if name.endswith("_cm") else name


# ---------------------------------------------------------------- helpers

def open_index_for(path):
    """Nearest resource_index.tsv above a resource file, like rio_convert."""
    index_path = find_index(path)
    return Catalogue(index_path) if index_path else None
