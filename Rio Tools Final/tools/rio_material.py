import struct

TYPE_OFFSET = 0x03
MATERIAL_TYPE = 0x00
SHADER_UID = 0x28
TEXTURE_COUNT = 0x30
TEXTURE_TABLE = 0x50
TEXTURE_ENTRY = 8
MAX_TEXTURES = 32

SLOT_ROLES = {0x00000000: "base", 0x10000001: "normal", 0x10000002: "specular"}


def u32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def signed32(value):
    return value - (1 << 32) if value & 0x80000000 else value


def relative_target(data, field):
    return field + signed32(u32(data, field))


def parse(data):
    if data[TYPE_OFFSET] != MATERIAL_TYPE:
        raise ValueError(f"not a material resource (type {data[TYPE_OFFSET]:#04x})")
    count = u32(data, TEXTURE_COUNT)
    if count > MAX_TEXTURES:
        raise ValueError(f"implausible texture count {count}")
    table = relative_target(data, TEXTURE_TABLE)
    if count and not 0 < table <= len(data) - count * TEXTURE_ENTRY:
        raise ValueError("texture table falls outside the resource")
    textures = []
    for entry in range(count):
        uid, slot = struct.unpack_from(">2I", data, table + entry * TEXTURE_ENTRY)
        textures.append({"uid": uid, "slot": slot, "role": SLOT_ROLES.get(slot, f"slot_{slot:#010x}")})
    return {"shader": u32(data, SHADER_UID), "textures": textures}


def resolve(material, index):
    resolved = {}
    for texture in material["textures"]:
        entry = index.get(texture["uid"])
        if entry:
            resolved[texture["role"]] = entry[1]
    return resolved
