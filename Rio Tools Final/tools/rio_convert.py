import re
import struct
import sys
from pathlib import Path

import rio_entity
import rio_index
import rio_material
import rio_skeleton
import rio_texture

INDEX_NAME = "resource_index.tsv"
SEARCH_DEPTH = 8
COLOUR_VARIANT = re.compile(r"^(.*?)(\d+)(_cm)$")
COMPANION_ROLES = {"normal": "nm", "specular": "sm"}
WIRED_ROLES = ("base", "normal", "specular")


def find_index(path):
    for parent in list(path.resolve().parents)[:SEARCH_DEPTH]:
        candidate = parent / INDEX_NAME
        if candidate.exists():
            return candidate
    return None


def variant_token(default, variant):
    cut = 0
    for position, (left, right) in enumerate(zip(default, variant)):
        if left != right:
            break
        if left == "_":
            cut = position + 1
    name = variant[cut:]
    return name[: -len("_cm")] if name.endswith("_cm") else name


class Assets:

    def __init__(self, index_path):
        self.index = rio_index.load(index_path)
        root = index_path.parent
        self.materials = rio_index.resource_files(root, ".material")
        self.textures = rio_index.resource_files(root, ".texture")
        self.sizes = {}
        self.folders = {}
        for kind, name, _ in self.index.values():
            if kind == "texture":
                folder, leaf = name.rsplit("/", 1)
                self.folders.setdefault(folder, set()).add(leaf)

    def texture_file(self, name):
        return rio_index.find_resource(self.textures, name)

    def dimensions(self, name):
        if name not in self.sizes:
            source = self.texture_file(name)
            try:
                texture = rio_texture.parse(source.read_bytes()) if source else None
            except (ValueError, IndexError, struct.error, OSError):
                texture = None
            self.sizes[name] = (texture["width"], texture["height"]) if texture else None
        return self.sizes[name]

    def maps(self, uid):
        entry = self.index.get(uid)
        source = rio_index.find_resource(self.materials, entry[1]) if entry else None
        if not source:
            return {}, None
        try:
            material = rio_material.parse(source.read_bytes())
        except (ValueError, IndexError, struct.error):
            return {}, source.stem
        found = {}
        for texture in material["textures"]:
            target = self.index.get(texture["uid"])
            if target:
                found[texture["role"]] = target[1]
        found.update(self.companions(found))
        return found, source.stem

    def companions(self, found):
        colour = found.get("base")
        if not colour:
            return {}
        folder, leaf = colour.rsplit("/", 1)
        stem = leaf[: -len("_cm")] if leaf.endswith("_cm") else leaf
        extra = {}
        for role, suffix in COMPANION_ROLES.items():
            if role in found:
                continue
            for candidate in (f"{stem}_{suffix}", f"{re.sub(r'[0-9]+$', '', stem)}_{suffix}"):
                name = f"{folder}/{candidate}"
                if candidate in self.folders.get(folder, ()) and self.dimensions(name) == self.dimensions(colour):
                    extra[role] = name
                    break
        return extra

    def variants(self, colour):
        if not colour:
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
                siblings[variant_token(leaf, other)] = f"{folder}/{other}"
        return siblings


def whole_mesh(mesh):
    return {
        "triangles": mesh["index_count"] // 3,
        "index_start": 0,
        "base_vertex": 0,
        "vertex_count": mesh["vertex_count"],
        "material": 0,
    }


def plan_materials(mesh, assets):
    plan, wanted, variants = [], set(), {}
    for record in mesh["submeshes"] or [whole_mesh(mesh)]:
        maps, name = assets.maps(record["material"]) if assets else ({}, None)
        plan.append((record, maps, name))
        wanted.update(maps.values())
        for token, resource in (assets.variants(maps.get("base")) if assets else {}).items():
            variants.setdefault(token, {})[record["material"]] = resource
            wanted.add(resource)
    return plan, wanted, variants


def convert_textures(assets, wanted, destination):
    written, failed = {}, 0
    for name in sorted(wanted):
        source = assets.texture_file(name)
        if not source:
            failed += 1
            continue
        try:
            _, files = rio_texture.convert(source, destination, False)
        except (ValueError, IndexError, struct.error, OSError):
            failed += 1
            continue
        page = next((file for file in files if file.suffix == ".png"), None)
        if page:
            written[name] = page
        else:
            failed += 1
    return written, failed


def build_parts(mesh, plan, pages, swap=None):
    parts = []
    for record, maps, name in plan:
        chosen = dict(maps)
        if swap and record["material"] in swap:
            chosen["base"] = swap[record["material"]]
        parts.append(
            {
                "indices": rio_entity.submesh_indices(mesh, record),
                "maps": {role: pages[value] for role, value in chosen.items() if value in pages},
                "material": name,
            }
        )
    return parts


def find_rig(path, data, mesh):
    if not mesh["skinned"]:
        return None, None, None
    found = rio_entity.find_skeleton(path)
    if not found:
        return None, None, None
    try:
        skeleton = rio_skeleton.parse(found.read_bytes())
    except (ValueError, IndexError, struct.error):
        return None, None, None
    palette = rio_entity.bone_map(data, mesh, skeleton)
    return (skeleton, palette, found.name) if palette else (None, None, None)


def convert(path, assets):
    data = path.read_bytes()
    mesh = rio_entity.parse(data)
    points = list(rio_entity.vertices(data, mesh))
    destination = path.parent / path.stem
    destination.mkdir(parents=True, exist_ok=True)

    plan, wanted, variants = plan_materials(mesh, assets)
    pages, missing = convert_textures(assets, wanted, destination / "textures") if wanted else ({}, 0)
    skeleton, palette, rig_name = find_rig(path, data, mesh)

    written = []
    for token, swap in [(None, None)] + sorted(variants.items()):
        name = path.stem if token is None else f"{path.stem}_{token}"
        target = destination / f"{name}.glb"
        rio_entity.write_glb(target, name, mesh, points, build_parts(mesh, plan, pages, swap), skeleton, palette)
        written.append(target)

    collision = rio_entity.find_collision(data, mesh)
    if collision:
        target = destination / f"{path.stem}_collision.glb"
        rio_entity.write_collision_glb(target, f"{path.stem}_collision", collision)
        written.append(target)

    kind = "skinned" if mesh["skinned"] else "static"
    print(f"{path.name}")
    print(f"    {mesh['vertex_count']} vertices, {mesh['index_count'] // 3} triangles, {len(plan)} submesh(es), {kind}")
    if rig_name:
        print(f"    rig: {len(skeleton)} bones from {rig_name}, {len(palette)} joints bound")
    elif mesh["skinned"]:
        print("    rig: no matching skeleton found, exported unskinned")
    names = sorted({name for _, _, name in plan if name})
    print(f"    materials: {', '.join(names) if names else 'none resolved'}")
    print(f"    textures: {len(pages)} converted{f', {missing} missing' if missing else ''}")
    if collision:
        print(f"    collision: {len(collision['points'])} vertices, {len(collision['faces'])} triangles")

    spare = sorted({value for _, maps, _ in plan for role, value in maps.items() if role not in WIRED_ROLES})
    if spare:
        print(f"    {len(spare)} texture(s) sit in material slots nothing has decoded yet:")
        print(f"        in textures/, not attached to the GLB: {', '.join(name.rsplit('/', 1)[-1] for name in spare)}")
    print(f"    wrote {len(written)} GLB(s) to {destination}")
    for target in written:
        print(f"        {target.name}")
    return True


def collect(values):
    found = []
    for value in values:
        path = Path(value)
        found.extend(sorted(path.rglob("*.entity")) if path.is_dir() else [path])
    return [path for path in found if path.suffix.lower() == ".entity"]


def main():
    files = collect(sys.argv[1:])
    if not files:
        print("Drop one or more .entity resources, or a folder holding them, onto this tool.")
        return 1
    index_path = find_index(files[0])
    assets = None
    if index_path:
        print(f"Using {index_path}\n")
        assets = Assets(index_path)
    else:
        print("No resource_index.tsv found above these files: exporting geometry only.")
        print("Run the EDB extractor first - it writes the index the materials need.\n")
    done = failed = 0
    for path in files:
        try:
            convert(path, assets)
            done += 1
        except (ValueError, IndexError, struct.error, OSError) as error:
            print(f"{path.name}: SKIPPED - {error}")
            failed += 1
    print(f"\n{done} converted, {failed} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
