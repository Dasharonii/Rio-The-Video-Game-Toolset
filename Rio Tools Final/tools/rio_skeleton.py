import struct

TYPE_OFFSET = 0x03
SKELETON_TYPE = 0x0A
BONE_COUNT = 0x28
NAME_HASHES = 0x38
GLOBAL_POSE = 0x3C
LOCAL_POSE = 0x40
HIERARCHY = 0x44

POSE_STRIDE = 32
HIERARCHY_STRIDE = 16
HASH_STRIDE = 8
HASH_BYTES = 7
NO_PARENT = 0xFFFF


def u32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def u16(data, offset):
    return struct.unpack_from(">H", data, offset)[0]


def signed32(value):
    return value - (1 << 32) if value & 0x80000000 else value


def relative_target(data, field):
    return field + signed32(u32(data, field))


def read_pose(data, offset):
    values = struct.unpack_from(">8f", data, offset)
    return {"rotation": values[0:4], "translation": values[4:7]}


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


def parse(data):
    if data[TYPE_OFFSET] != SKELETON_TYPE:
        raise ValueError(f"not a skeleton resource (type {data[TYPE_OFFSET]:#04x})")
    count = u16(data, BONE_COUNT)
    if not count:
        raise ValueError("skeleton has no bones")
    hierarchy = relative_target(data, HIERARCHY)
    world = relative_target(data, GLOBAL_POSE)
    local = relative_target(data, LOCAL_POSE)
    hashes = relative_target(data, NAME_HASHES) + HASH_STRIDE
    bones = []
    for index in range(count):
        record = hierarchy + index * HIERARCHY_STRIDE
        parent = u16(data, record)
        slot = u16(data, record + 4)
        name_hash = int.from_bytes(data[hashes + slot * HASH_STRIDE :][:HASH_BYTES], "big")
        bones.append(
            {
                "index": index,
                "parent": None if parent == NO_PARENT else parent,
                "subtree_end": u16(data, record + 2),
                "name_hash": name_hash,
                "local": read_pose(data, local + index * POSE_STRIDE),
                "world": read_pose(data, world + index * POSE_STRIDE),
            }
        )
    for bone in bones:
        if bone["parent"] is not None and bone["parent"] >= bone["index"]:
            raise ValueError(f"bone {bone['index']} has forward parent {bone['parent']}")
    return bones


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
