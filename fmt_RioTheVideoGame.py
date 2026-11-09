"""Noesis importer for Rio: The Video Game resources.

Format and geometry work lives in inc_rio.py, which has no Noesis dependencies
so it can be exercised by test_rio_core.py under the system Python.
"""

import os
import re
import struct

import inc_rio
import noesis
import rapi
from inc_noesis import *


PLUGIN_NAME = "Rio: The Video Game"
MODEL_PLUGIN_NAME = "Rio: The Video Game Model"

CATALOGUES = {}
MATERIAL_MAPS = {}
TEXTURES = {}
TEXTURE_NAMES = {}


def registerNoesisTypes():
    handle = noesis.register(PLUGIN_NAME, ".texture")
    noesis.setHandlerTypeCheck(handle, texture_check_type)
    noesis.setHandlerLoadRGBA(handle, texture_load_rgba)

    handle = noesis.register(MODEL_PLUGIN_NAME, ".entity")
    noesis.setHandlerTypeCheck(handle, entity_check_type)
    noesis.setHandlerLoadModel(handle, entity_load_model)

    handle = noesis.register("Rio: The Video Game Skeleton", ".skeleton")
    noesis.setHandlerTypeCheck(handle, skeleton_check_type)
    noesis.setHandlerLoadModel(handle, skeleton_load_model)
    return 1


# ---------------------------------------------------------------- textures


def texture_check_type(data):
    if len(data) < inc_rio.PAYLOAD_BASE + 4:
        return 0
    return 1 if data[inc_rio.TYPE_OFFSET] == inc_rio.TEXTURE_TYPE else 0


def texture_load_rgba(data, texList):
    try:
        texture = inc_rio.parse_texture(data, tolerant=True)
    except ValueError as error:
        print("Rio texture: %s" % error)
        return 0

    name = rapi.getExtensionlessName(rapi.getLocalFileName(rapi.getLastCheckedName()))
    levels = inc_rio.texture_payloads(data, texture)
    if not levels and texture["truncated"]:
        padded = inc_rio.padded_data(data, texture)
        print(
            "WARNING: %s holds %d of %d bytes; decoding the first page only."
            % (name, texture["available"], len(padded[2]))
        )
        levels = [padded]
    if not levels:
        print("Rio texture: %s has no complete mip levels" % name)
        return 0

    kind = inc_rio.normal_kind(texture)
    pitch = inc_rio.BLOCK_BYTES[texture["fourcc"]]
    chunks = []
    try:
        if kind == "dxt5nm":
            # Some linear fmt-12 files are plain DXT5; only swizzle DXT5nm data.
            width, height, payload = levels[0]
            if texture["tiled"]:
                payload = rapi.imageUntile360DXT(
                    rapi.swapEndianArray(payload, 2), width, height, pitch
                )
            first = rapi.imageDecodeDXT(payload, width, height, noesis.FOURCC_DXT5)
            if not inc_rio.looks_like_dxt5nm(first):
                kind = None
        if kind:
            for width, height, payload in levels:
                if texture["tiled"]:
                    payload = rapi.imageUntile360DXT(
                        rapi.swapEndianArray(payload, 2), width, height, pitch
                    )
                if kind == "bc5":
                    payload = rapi.imageDecodeDXT(payload, width, height, noesis.FOURCC_ATI2)
                    payload = rapi.imageNormalSwizzle(payload, width, height, 0, 1, 0)
                else:
                    payload = rapi.imageDecodeDXT(payload, width, height, noesis.FOURCC_DXT5)
                    payload = rapi.imageNormalSwizzle(payload, width, height, 1, 1, 0)
                chunks.append(payload)
            pixel_type = noesis.NOESISTEX_RGBA32
        else:
            for width, height, payload in levels:
                if texture["tiled"]:
                    payload = rapi.imageUntile360DXT(
                        rapi.swapEndianArray(payload, 2), width, height, pitch
                    )
                chunks.append(payload)
            pixel_type = (
                noesis.NOESISTEX_DXT1
                if texture["fourcc"] == "DXT1"
                else noesis.NOESISTEX_DXT5
            )
    except Exception as error:
        print("Rio texture: %s failed to decode (%s)" % (name, error))
        return 0

    texture_object = NoeTexture(
        name, texture["width"], texture["height"], b"".join(chunks), pixel_type
    )
    flags = noesis.NTEXFLAG_ISNORMALMAP if kind else 0
    if name.lower().endswith(LINEAR_SUFFIXES):
        flags |= getattr(noesis, "NTEXFLAG_ISLINEAR", 0)
    texture_object.setFlags(flags)
    if len(chunks) > 1:
        texture_object.setMipCount(len(chunks))
    texList.append(texture_object)
    return 1


# ---------------------------------------------------------------- skeletons


def skeleton_check_type(data):
    if len(data) <= inc_rio.BONE_COUNT + 1:
        return 0
    return 1 if data[inc_rio.TYPE_OFFSET] == inc_rio.SKELETON_TYPE else 0


def skeleton_bone_name(bone):
    return "bone_%03d_%014x" % (bone["index"], bone["name_hash"])


def skeleton_bone_matrix(pose):
    """Noesis bone matrices are model space, as stored in the world pose."""
    matrix = NoeQuat(pose["rotation"]).toMat43(1)
    matrix[3] = NoeVec3(pose["translation"])
    return matrix


def skeleton_bones(skeleton):
    return [
        NoeBone(
            bone["index"],
            skeleton_bone_name(bone),
            skeleton_bone_matrix(bone["world"]),
            None,
            bone["parent"] if bone["parent"] is not None else -1,
        )
        for bone in skeleton
    ]


def skeleton_load_model(data, mdlList):
    name = rapi.getExtensionlessName(rapi.getLocalFileName(rapi.getLastCheckedName()))
    try:
        skeleton = inc_rio.parse_skeleton(data)
    except (ValueError, IndexError, struct.error) as error:
        print("Rio skeleton: %s: %s" % (name, error))
        return 0
    mdlList.append(NoeModel([], skeleton_bones(skeleton)))
    return 1


# ---------------------------------------------------------------- entities


def entity_check_type(data):
    if len(data) <= inc_rio.INDEX_COUNT + 4:
        return 0
    return 1 if data[inc_rio.TYPE_OFFSET] == inc_rio.ENTITY_TYPE else 0


def entity_rig(path, data, mesh):
    """Nearest sibling skeleton and its palette in skeleton bone indices."""
    if not mesh["skinned"]:
        return None, None
    for candidate in inc_rio.skeleton_candidates(path):
        try:
            skeleton = inc_rio.parse_skeleton(rapi.loadIntoByteArray(candidate))
        except (ValueError, IndexError, struct.error, OSError) as error:
            print("Rio entity: skipping skeleton %s (%s)" % (candidate, error))
            continue
        palette = inc_rio.bone_map(data, mesh, skeleton)
        if palette:
            return skeleton, palette
    return None, None


def entity_weights(points, palette):
    """Per-vertex NoeVertWeight using skeleton bone indices, normalized."""
    weights = []
    for point in points:
        values = [max(0.0, value) for value in point["weights"]]
        total = sum(values) or 1.0
        indices, normalised = [], []
        for slot, weight in zip(point["bones"], values):
            bone = palette[slot] if slot < len(palette) else 0
            indices.append(bone if weight > 0.0 else 0)
            normalised.append(weight / total)
        weights.append(NoeVertWeight(indices, normalised))
    return weights


def entity_parts(mesh):
    """Submesh records, or one whole-mesh record when the table is absent."""
    if mesh.get("submeshes"):
        return mesh["submeshes"], True
    return [inc_rio.whole_mesh(mesh)], False


# ---------------------------------------------------------------- materials


FALLBACK_SUFFIXES = (("normal", "_nm"), ("specular", "_sm"))
FALLBACK_DEPTH = 3

# Blocks that hold data rather than colour: normals, and the _sm maps, whose
# energy sits in a single per-part channel. Noesis converts colour textures to
# linear for lighting but must not do that to these.
LINEAR_SUFFIXES = ("_nm", "_sm")

# The _sm maps are packed per-part parameter masks, not painted highlight
# colours - mounting one paints green or blue gloss onto parts that should not
# have it. Preview one subdued neutral highlight instead of Noesis' white,
# exponent-8 default, which was broad and hot enough to read as chrome.
SPECULAR_COLOUR = [0.4, 0.4, 0.4, 24.0]

# Noesis lights a preview with one directional light and a black ambient, so
# the side turned away falls to near-black. The art already carries its own
# painted shading, so that reads as a second, wrong-looking layer of AO. A
# small ambient lifts the shadowed side without flattening the model.
AMBIENT_COLOUR = [0.4, 0.4, 0.4, 1.0]


def entity_catalogue(path):
    """Cached Catalogue for the nearest resource_index.tsv, or (None, None)."""
    index_path = inc_rio.find_index(path)
    if not index_path:
        return None, None
    key = os.path.normcase(os.path.abspath(index_path))
    catalogue = CATALOGUES.get(key)
    if catalogue is None:
        catalogue = inc_rio.Catalogue(index_path)
        CATALOGUES[key] = catalogue
    return catalogue, key


def entity_fallback_folders(path):
    """Nearby folders that could hold textures or materials for a bare rip."""
    folder = os.path.dirname(os.path.abspath(path))
    folders = []
    for _ in range(FALLBACK_DEPTH):
        for name in ("", "textures", "materials", "material"):
            candidate = os.path.join(folder, name) if name else folder
            if os.path.isdir(candidate) and candidate not in folders:
                folders.append(candidate)
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return folders


def entity_fallback_library(path):
    textures, materials = {}, []
    for folder in entity_fallback_folders(path):
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            continue
        for filename in entries:
            lower = filename.lower()
            full = os.path.join(folder, filename)
            if lower.endswith(".texture"):
                textures.setdefault(filename[: -len(".texture")].lower(), full)
            elif lower.endswith(".material"):
                materials.append(os.path.splitext(filename)[0])
    return textures, materials


def entity_fallback_stems(path, materials):
    stems = [os.path.splitext(os.path.basename(path))[0]]
    for stem in materials:
        stems.append(stem)
        if stem.lower().endswith("_mat"):
            stems.append(stem[:-4])
    seen, ordered = set(), []
    for stem in stems:
        key = stem.lower()
        if key and key not in seen:
            seen.add(key)
            ordered.append(stem)
    return ordered


def entity_fallback_maps(path):
    """`<stem>_cm`/`_nm`/`_sm` lookup for rips without a resource_index.tsv."""
    textures, materials = entity_fallback_library(path)
    for stem in entity_fallback_stems(path, materials):
        base = textures.get((stem + "_cm").lower())
        if not base:
            continue
        maps = {"base": base}
        for role, suffix in FALLBACK_SUFFIXES:
            companion = textures.get((stem + suffix).lower())
            if companion is None:
                trimmed = re.sub(r"[0-9]+$", "", stem)
                if trimmed != stem:
                    companion = textures.get((trimmed + suffix).lower())
            if companion:
                maps[role] = companion
        return maps, stem
    return {}, None


def entity_texture_name(path, key):
    """A stable, unique Noesis texture name for one resource file."""
    stem = os.path.splitext(os.path.basename(path))[0]
    name = stem
    suffix = 2
    while TEXTURE_NAMES.get(name.lower(), key) != key:
        name = "%s_%d" % (stem, suffix)
        suffix += 1
    TEXTURE_NAMES[name.lower()] = key
    return name


def entity_texture(path):
    """Load a .texture through the registered handler, once per resource file."""
    key = os.path.normcase(os.path.abspath(path))
    if key in TEXTURES:
        return TEXTURES[key]
    texture = None
    try:
        texture = rapi.loadTexByHandler(rapi.loadIntoByteArray(path), ".texture")
    except Exception as error:
        print("Rio material: %s failed to load (%s)" % (os.path.basename(path), error))
    if texture is not None:
        texture.name = entity_texture_name(path, key)
        stem = os.path.splitext(os.path.basename(path))[0]
        if stem.lower().endswith(LINEAR_SUFFIXES):
            texture.setFlags(
                getattr(texture, "flags", 0) | getattr(noesis, "NTEXFLAG_ISLINEAR", 0)
            )
    TEXTURES[key] = texture
    return texture


class EntityMaterials(object):
    """Resolves submesh materials, loads their maps, and owns the model lists."""

    def __init__(self, path, catalogue, catalogue_key):
        self.path = path
        self.catalogue = catalogue
        self.catalogue_key = catalogue_key or ""
        self.material_uids = catalogue.material_uids if catalogue else set()
        self.fallback = None
        self.fallback_ready = False
        self.texList = []
        self.texPaths = set()
        self.matList = []
        self.materials = {}
        self.signatures = {}
        self.names = {}
        self.name_owners = {}

    def part_uid(self, record, tabled, data, mesh):
        """Submesh material UID, or the header UID for whole-mesh entities."""
        if tabled:
            uid = record["material"] or 0
        else:
            uid = 0
            if self.catalogue:
                uid = inc_rio.entity_material_uid(
                    data, self.catalogue.index, mesh["vertex_base"], self.material_uids
                )
        return uid if uid & inc_rio.RESOURCE_PAYLOAD_MASK else 0

    def maps_for(self, uid):
        if self.catalogue:
            key = (self.catalogue_key, uid)
            if key not in MATERIAL_MAPS:
                try:
                    maps, stem = self.catalogue.maps(uid) if uid else ({}, None)
                except (OSError, ValueError, IndexError, struct.error) as error:
                    print("Rio material: %#010x failed to parse (%s)" % (uid, error))
                    maps, stem = {}, None
                MATERIAL_MAPS[key] = (maps, stem)
            return MATERIAL_MAPS[key]
        if not self.fallback_ready:
            self.fallback = entity_fallback_maps(self.path)
            self.fallback_ready = True
        return self.fallback

    def material_for(self, uid):
        maps, stem = self.maps_for(uid)
        if self.catalogue:
            signature = (stem, tuple(sorted(maps.items())))
            name_key = uid
        else:
            signature = ("fallback", tuple(sorted(maps.items())))
            name_key = 0
        name = self.signatures.get(signature)
        if name is None:
            name = self._name_for(name_key, stem)
            material = NoeMaterial(name, "")
            for role, setter in (
                ("base", material.setTexture),
                ("normal", material.setNormalTexture),
            ):
                target = self._texture_name(maps.get(role))
                if target:
                    setter(target)
            # Loaded for the texture list, but never mounted: see SPECULAR_COLOUR.
            self._texture_name(maps.get("specular"))
            material.setSpecularColor(NoeVec4(SPECULAR_COLOUR))
            material.setAmbientColor(NoeVec4(AMBIENT_COLOUR))
            self.materials[name] = material
            self.matList.append(material)
            self.signatures[signature] = name
        self.names[name_key] = name
        return name

    def _name_for(self, key, stem):
        cached = self.names.get(key)
        if cached:
            return cached
        if stem:
            name = stem
        elif key:
            name = "rio_material_%08x" % key
        else:
            name = "default"
        owner = self.name_owners.get(name)
        if owner is not None and owner != key:
            name = "%s_%08x" % (name, key) if key else "%s_default" % name
        base, suffix = name, 2
        while name in self.name_owners and self.name_owners[name] != key:
            name = "%s_%d" % (base, suffix)
            suffix += 1
        self.name_owners[name] = key
        self.names[key] = name
        return name

    def _texture_path(self, source):
        if os.path.isabs(source):
            return source if os.path.isfile(source) else None
        if self.catalogue:
            return self.catalogue.texture_file(source)
        return None

    def _texture_name(self, source):
        if not source:
            return None
        path = self._texture_path(source)
        if not path:
            return None
        texture = entity_texture(path)
        if texture is None:
            return None
        key = os.path.normcase(os.path.abspath(path))
        if key not in self.texPaths:
            self.texPaths.add(key)
            self.texList.append(texture)
        return texture.name


def entity_vertex_arrays(mesh, points, weights=None):
    """Full vertex arrays for the entity; submeshes are compacted from these."""
    positions = [NoeVec3(point["position"]) for point in points]
    normals = [NoeVec3(point["normal"]) for point in points] if mesh["has_normal"] else None
    uvs = (
        [NoeVec3((point["uv"][0], point["uv"][1], 0.0)) for point in points]
        if mesh["has_uv"]
        else None
    )
    uv1s = (
        [NoeVec3((point["uv1"][0], point["uv1"][1], 0.0)) for point in points]
        if mesh.get("uv1") is not None
        else None
    )
    # inc_rio also finds the 8-byte per-vertex field that most rig items carry,
    # but it is baked shading centred on a neutral 0.5 - not albedo. Noesis can
    # only apply such a field by multiplying it into the diffuse, which halves
    # the brightness and turns every character metallic-dark, so it is parsed
    # and then deliberately left out of the preview.
    tangents = None
    if mesh["has_tangent"]:
        tangents = []
        for index, point in enumerate(points):
            normal = normals[index] if normals is not None else NoeVec3(point["normal"])
            tangent = NoeVec3(point["tangent"])
            bitangent = normal.cross(tangent) * point["handedness"]
            tangents.append(NoeMat43((normal, tangent, bitangent, NoeVec3())))
    return positions, normals, uvs, uv1s, tangents, weights


def entity_compact(indices, arrays):
    """Remap one submesh onto only the vertices its indices reference."""
    remap = {}
    order = []
    local_indices = []
    for index in indices:
        local = remap.get(index)
        if local is None:
            local = len(order)
            remap[index] = local
            order.append(index)
        local_indices.append(local)
    compacted = tuple(
        [values[index] for index in order] if values is not None else None
        for values in arrays
    )
    return local_indices, compacted


def entity_load_model(data, mdlList):
    path = rapi.getLastCheckedName()
    name = rapi.getExtensionlessName(rapi.getLocalFileName(path))
    try:
        mesh = inc_rio.parse_entity(data)
        points = list(inc_rio.entity_vertices(data, mesh))
    except (ValueError, IndexError, struct.error) as error:
        print("Rio entity: %s: %s" % (name, error))
        return 0
    if not points:
        print("Rio entity: %s has no vertices" % name)
        return 0

    skeleton, palette = entity_rig(path, data, mesh)
    bones = skeleton_bones(skeleton) if skeleton else []
    weights = entity_weights(points, palette) if palette else None
    full = entity_vertex_arrays(mesh, points, weights)
    records, tabled = entity_parts(mesh)
    try:
        catalogue, catalogue_key = entity_catalogue(path)
    except (OSError, ValueError, UnicodeDecodeError) as error:
        print("Rio entity: %s index unavailable (%s)" % (name, error))
        catalogue, catalogue_key = None, None
    supplies = EntityMaterials(path, catalogue, catalogue_key)
    meshes = []
    for number, record in enumerate(records):
        indices = inc_rio.submesh_indices(mesh, record) if tabled else list(mesh["indices"])
        if not indices:
            continue
        if max(indices) >= len(full[0]):
            print("Rio entity: %s submesh %d indexes past the vertex pool" % (name, number))
            continue
        local_indices, compacted = entity_compact(indices, full)
        positions, normals, uvs, uv1s, tangents, vertex_weights = compacted
        label = name if len(records) == 1 else "%s_%03d" % (name, number)
        uid = supplies.part_uid(record, tabled, data, mesh)
        piece = NoeMesh(local_indices, positions, label, supplies.material_for(uid))
        if normals is not None:
            piece.setNormals(normals)
        if uvs is not None:
            piece.setUVs(uvs)
        if uv1s is not None:
            piece.setUVs(uv1s, 1)
        if tangents is not None:
            piece.setTangents(tangents)
        if vertex_weights is not None:
            piece.setWeights(vertex_weights)
        meshes.append(piece)

    if not meshes:
        print("Rio entity: %s has no usable submeshes" % name)
        return 0
    if palette:
        print("Rio entity: %s rigged with %d bones from %d joints" % (name, len(bones), len(palette)))
    model = NoeModel(meshes, bones)
    if supplies.matList:
        model.setModelMaterials(NoeModelMaterials(supplies.texList, supplies.matList))
    mdlList.append(model)
    return 1
