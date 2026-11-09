import argparse
import struct
from pathlib import Path

TYPE_OFFSET = 0x03
TEXTURE_TYPE = 0x08
HEADER_EXTRA = 0x24
WIDTH = 0x28
HEIGHT = 0x2A
PIXEL_FORMAT = 0x2F
MIP_COUNT = 0x32
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

DDSD_CAPS_HEIGHT_WIDTH_PIXELFORMAT_LINEARSIZE_MIPMAPCOUNT = 0x0A1007
DDPF_FOURCC = 0x04
DDSCAPS_TEXTURE_MIPMAP_COMPLEX = 0x401008


def u32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def u16(data, offset):
    return struct.unpack_from(">H", data, offset)[0]


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


def detile(data, texture, offset=None, width=None, height=None):
    fourcc = texture["fourcc"]
    pitch = BLOCK_BYTES[fourcc]
    log_bpp = (pitch >> 2) + ((pitch >> 1) >> (pitch >> 2))
    width = texture["width"] if width is None else width
    height = texture["height"] if height is None else height
    offset = texture["payload"] if offset is None else offset
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


def parse(data):
    if data[TYPE_OFFSET] != TEXTURE_TYPE:
        raise ValueError(f"not a texture resource (type {data[TYPE_OFFSET]:#04x})")
    raw_format = data[PIXEL_FORMAT]
    if raw_format not in PIXEL_FORMATS:
        raise ValueError(f"unknown pixel format {raw_format}")
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
    if available < needed:
        raise ValueError(f"payload holds {available} bytes, too small for {width}x{height} {fourcc}")
    return {
        "fourcc": fourcc,
        "raw_format": raw_format,
        "width": width,
        "height": height,
        "mips": mips,
        "payload": payload,
        "available": available,
        "tiled": tiled,
    }


def dds(data, texture):
    width, height, fourcc = texture["width"], texture["height"], texture["fourcc"]
    if texture.get("tiled"):
        levels = tiled_levels(texture)
        payload = b"".join(detile(data, texture, *level) for level in levels)
        kept = len(levels)
    else:
        total = kept = 0
        for level in range(texture["mips"]):
            step = mip_size(fourcc, max(1, width >> level), max(1, height >> level))
            if total + step > texture["available"]:
                break
            total += step
            kept += 1
        payload = data[texture["payload"] : texture["payload"] + total]
    header = struct.pack(
        "<4s7I44s13I",
        b"DDS ",
        124,
        DDSD_CAPS_HEIGHT_WIDTH_PIXELFORMAT_LINEARSIZE_MIPMAPCOUNT,
        height,
        width,
        mip_size(fourcc, width, height),
        0,
        kept,
        b"\0" * 44,
        32,
        DDPF_FOURCC,
        int.from_bytes(DDS_FOURCC[fourcc], "little"),
        0,
        0,
        0,
        0,
        0,
        DDSCAPS_TEXTURE_MIPMAP_COMPLEX,
        0,
        0,
        0,
        0,
    )
    return header + payload


def bc5_planes(payload, wide, high):
    try:
        import numpy
    except ImportError:
        return None
    blocks = numpy.frombuffer(payload, dtype=numpy.uint8)[: wide * high * 16]
    blocks = blocks.reshape(-1, 2, 8)
    first, second = blocks[:, :, 0].astype(numpy.int32), blocks[:, :, 1].astype(numpy.int32)
    bits = numpy.zeros(blocks.shape[:2], dtype=numpy.uint64)
    for step in range(6):
        bits |= blocks[:, :, 2 + step].astype(numpy.uint64) << numpy.uint64(8 * step)
    ramp = numpy.zeros(blocks.shape[:2] + (8,), dtype=numpy.float64)
    ramp[:, :, 0], ramp[:, :, 1] = first, second
    wide_ramp = first > second
    for step in range(1, 7):
        ramp[:, :, step + 1] = ((7 - step) * first + step * second) / 7
    for step in range(1, 5):
        narrow = ((5 - step) * first + step * second) / 5
        ramp[:, :, step + 1] = numpy.where(wide_ramp, ramp[:, :, step + 1], narrow)
    ramp[:, :, 6] = numpy.where(wide_ramp, ramp[:, :, 6], 0)
    ramp[:, :, 7] = numpy.where(wide_ramp, ramp[:, :, 7], 255)
    picks = numpy.zeros(blocks.shape[:2] + (16,), dtype=numpy.int64)
    for pixel in range(16):
        picks[:, :, pixel] = (bits >> numpy.uint64(3 * pixel)) & numpy.uint64(7)
    values = numpy.take_along_axis(ramp, picks, axis=2)
    grid = values.reshape(high, wide, 2, 4, 4).transpose(2, 0, 3, 1, 4)
    return grid.reshape(2, high * 4, wide * 4)


def bc5_normal_map(payload, texture):
    try:
        import numpy
        from PIL import Image
    except ImportError:
        return None
    wide, high, _, _ = block_extent(texture["width"], texture["height"])
    planes = bc5_planes(payload, wide, high)
    if planes is None:
        return None
    x, y = planes[0] / 255.0 * 2 - 1, planes[1] / 255.0 * 2 - 1
    z = numpy.sqrt(numpy.clip(1 - x * x - y * y, 0.0, 1.0))
    rebuilt = numpy.stack([x, y, z], axis=-1) * 0.5 + 0.5
    image = Image.fromarray((rebuilt * 255).round().astype(numpy.uint8), "RGB")
    return image.crop((0, 0, texture["width"], texture["height"]))


def is_dxt5nm(image):
    red, green, blue, alpha = image.split()
    low, high = alpha.getextrema()
    return red.getextrema() == (0, 0) and blue.getextrema() == (0, 0) and low != high


def rebuild_normal_map(image):
    try:
        import numpy
        from PIL import Image
    except ImportError:
        return None
    data = numpy.asarray(image, dtype=numpy.float32) / 255.0
    x, y = data[..., 3] * 2 - 1, data[..., 1] * 2 - 1
    z = numpy.sqrt(numpy.clip(1 - x * x - y * y, 0.0, 1.0))
    rebuilt = numpy.stack([x, y, z], axis=-1) * 0.5 + 0.5
    return Image.fromarray((rebuilt * 255).round().astype(numpy.uint8), "RGB")


def convert(path, destination, keep_dds, raw_normals=False):
    data = path.read_bytes()
    texture = parse(data)
    container = dds(data, texture)
    destination.mkdir(parents=True, exist_ok=True)
    written = []
    kind = "colour"
    try:
        import io

        from PIL import Image

        if texture["fourcc"] == "BC5":


            image = None if raw_normals else bc5_normal_map(detile(data, texture), texture)
            if image is None:
                raise ImportError("BC5 needs numpy")
            kind = "normal"
        else:
            image = Image.open(io.BytesIO(container))
            image.load()
            image = image.convert("RGBA")
            if not raw_normals and is_dxt5nm(image):
                rebuilt = rebuild_normal_map(image)
                if rebuilt is not None:
                    image, kind = rebuilt, "normal"
                else:
                    kind = "normal (unconverted: needs numpy)"
        target = destination / f"{path.stem}.png"
        image.save(target)
        written.append(target)
    except ImportError:
        keep_dds = True
    if keep_dds:
        target = destination / f"{path.stem}.dds"
        target.write_bytes(container)
        written.append(target)
    texture["kind"] = kind
    return texture, written


def texture_files(values):
    found = []
    for value in values:
        path = Path(value)
        found.extend(sorted(path.rglob("*.texture")) if path.is_dir() else [path])
    return found


def main():
    parser = argparse.ArgumentParser(description="Convert Rio .texture resources to PNG")
    parser.add_argument("input", nargs="+", help=".texture files or directories to search")
    parser.add_argument("-o", "--output", type=Path, help="output directory (default: alongside input)")
    parser.add_argument("--dds", action="store_true", help="also keep the intermediate DDS")
    parser.add_argument("--raw-normals", action="store_true", help="leave DXT5nm maps as stored")
    args = parser.parse_args()
    files = texture_files(args.input)
    if not files:
        parser.error("no .texture files found")
    converted = failed = 0
    for path in files:
        try:
            texture, written = convert(path, args.output or path.parent, args.dds, args.raw_normals)
        except (ValueError, IndexError, struct.error, OSError) as error:
            print(f"{path.name}: SKIPPED - {error}")
            failed += 1
            continue
        names = ", ".join(target.name for target in written)
        print(
            f"{path.name}: {texture['width']}x{texture['height']} {texture['fourcc']} "
            f"(format {texture['raw_format']}), {texture['mips']} mips, {texture.get('kind','colour')} -> {names}"
        )
        converted += 1
    print(f"\n{converted} converted, {failed} skipped")


if __name__ == "__main__":
    main()
