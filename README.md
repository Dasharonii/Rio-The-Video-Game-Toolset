Rio: The Video Game - Noesis plugin
===================================

Imports the assets the Rio tools extract from the Eurocom EDB archives:

    .entity     meshes, static and skinned
    .skeleton   skeletons, inspectable on their own
    .texture    DXT1/DXT5/BC5 and the other block formats the game uses

Written against the PS3 and Xbox 360 releases, and the 2010 and 2011
prototype builds made during the game's development. All four share one
resource format; the plugin parses it without per-platform branches.

Install
-------

Copy both files into Noesis' Python plugin folder:

    fmt_RioTheVideoGame.py
    inc_rio.py

    Noesis\plugins\python\

Restart Noesis. Entity, skeleton and texture files are then recognised. Tested
with Noesis 4.472.

Resource index
--------------

Meshes carry material and texture numbers, not names. Those numbers are only
explained by `resource_index.tsv`, which the extractor writes beside the
`extracted_assets*` folder. Keep the index and the extracted trees together:
a mesh in one archive routinely uses materials and textures from another.

With an index, the plugin resolves each submesh's material and its base, normal
and specular maps by UID, and names the Noesis materials after the material
resource.

Without an index, it falls back to filenames near the mesh - `<name>_cm`,
`<name>_nm`, `<name>_sm` - which covers simple rips but not colour variants or
shared libraries.

Reading a model
---------------

    * Every submesh becomes a NoeMesh named `<entity>` or `<entity>_000`, and
      keeps its own material.
    * A tabled submesh always uses its own material field. Only a whole-mesh
      entity (one with no submesh table) takes the entity header's material.
      This matches the reference `rio_entity.py` and keeps auxiliary geometry
      (zero UVs, flat helper triangles) on `default` instead of lending it a
      neighbour's material.
    * A part whose UID is zero, or has no catalogue entry, appears as
      `default` or `rio_material_<uid>` respectively, with no maps.
    * Meshes with a second UV set expose it as UV1 - the lightmap channel. The
      extra per-vertex field some rig meshes carry is a baked shading mask
      centred on a neutral 0.5, not albedo. It is parsed but not imported as a
      colour: Noesis can only multiply such a field into the diffuse, which
      halves the model's brightness and made characters preview metallic.
    * Materials are previewed with one subdued, neutral highlight (0.4 grey,
      exponent 24). The `_sm` maps are not mounted as a highlight colour: their
      energy sits in a single channel that differs per part - flat blue body,
      yellow eye, green beak on Bluu - so Noesis would paint green gloss onto
      some surfaces and blue onto others. Noesis' white, exponent-8 default was
      broad and hot enough to read as chrome.
    * `_nm` and `_sm` textures are flagged linear, since they carry data rather
      than colour; Noesis otherwise converts them to linear a second time.
    * Materials preview with a 0.4 grey ambient term. Noesis lights with one
      directional light and no ambient, which drops the unlit side to
      near-black on top of shading the textures already carry, and that reads
      as a second layer of AO.
    * Skinned meshes look for their skeleton beside the entity and map the
      vertex slots onto skeleton bones. Weights are normalised for Noesis.
    * Truncated small textures are padded so their payloads still decode; the
      plugin also accepts textures whose payload is shorter than the header
      promises.

Testing
-------

Both harnesses run under the system Python, outside Noesis:

    python test_rio_core.py                 # sampled
    python test_rio_core.py --full          # every byte, slow
    python test_plugin_entity.py            # a sample of entities per set
    python test_plugin_entity.py --all      # every entity, slow

They expect the `Rio Tools Final` folder (with `tools` and `exports`) beside
this folder. `test_rio_core.py` compares `inc_rio.py` against the reference
`rio_*` tools. `test_plugin_entity.py` stubs the Noesis classes and compares
every NoeMesh, material, map, bone and weight the plugin builds against
`rio_entity.py`.

Known behaviour
---------------

    * Parts whose material UID has no catalogue entry stay unresolved and keep
      that UID in their name. This is expected for a handful of HUD materials
      that were not extracted with the rest.
    * Some materials define no maps at all; their parts still show the correct
      material name.
    * A few tiny auxiliary parts have no material of their own and no UVs. They
      are intended to be untextured and use `default`.
    * Noesis logs "DXT-compressed files must be divisible by 4" while loading
      mip levels smaller than 4x4. The texture still decodes correctly; the
      message comes from Noesis, not the plugin, and the log can be closed.
    * The skeleton handler is for inspection; animation data is not imported.
