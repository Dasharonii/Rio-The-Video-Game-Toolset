Rio: The Video Game Toolset
===================================

Toolset for ripping from the 2011 multiplayer party game developed by 
Eurocom, Rio: The Video Game. Imports assets extracted from the .EDB archives:

    .entity     meshes; static and skinned.
    .skeleton   skeletons; inspectable on their own.
    .texture    DXT1/DXT5/BC5 and the other block formats the game uses.

Written against the PS3 and Xbox 360 releases, and the 2010 and 2011
prototype builds made during the game's development. All four share one
resource format; the plugin parses it without per-platform branches.

Install (Noesis Plugin)
-------

Copy both files into Noesis' Python plugin folder:

    fmt_RioTheVideoGame.py
    inc_rio.py

    Noesis\plugins\python\

Restart Noesis. Entity, skeleton and texture files are then recognized. Tested
with Noesis 4.472.

Resource index
--------------

Meshes carry material and texture numbers, not names. Those numbers are only
explained by the `resource_index.tsv` index database found in the prototype builds, 
which the extractor writes beside a folder containing extracted assets. Keep the 
contents of the level/character folders as they are. Reason being is that the meshes 
routinely uses materials and textures from their own respective designated folders.

Without the index, the plugin wouldn't have been able to resolve each submesh's 
material and its base, normal and specular maps by UID, and name the Noesis materials 
after the material resource. Not without some manual labor that is.

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
      than color; Noesis otherwise converts them to linear a second time.
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

Both harnesses run under Python outside of Noesis:

    python test_rio_core.py                 # sampled
    python test_rio_core.py --full          # every byte, slow
    python test_plugin_entity.py            # a sample of entities per set
    python test_plugin_entity.py --all      # every entity, slow

They expect the `Rio Tools Final` folder (with `tools` and at least one .EDB archive export inside) beside
this folder. `test_rio_core.py` compares `inc_rio.py` against the reference
`rio_*` tools. `test_plugin_entity.py` stubs the Noesis classes and compares
every NoeMesh, material, map, bone and weight the plugin builds against
`rio_entity.py`.

Known behavior
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

Credits
---------------

    * Hazza12555: Developed the Rio Tools, of which have been uploaded here out 
      of convenience and preservation. 
    * h3x3r: Drafted up and provided the [blueprint](https://reshax.com/topic/19286-xbox-360-playstation-3-rio-the-video-game-goldeneye-007-reloaded-edb-geo-dat/?&_rid=2465#findComment-106442) used for the Noesis plugin.
    * Falkrian: Assisted with this whole endeavor, especially in cracking the .EDB 
      archives.
    * Obscure Gamers: Responsible for having provided both the [2010](https://hiddenpalace.org/Rio_(Apr_13,_2011_prototype)) and [2011](https://hiddenpalace.org/Rio_(Apr_13,_2011_prototype)) 
      prototype builds. 
    * LittleBigBug (Luigi Auriemma): Cracked the .EDB compression algo. Codes that
      were referenced from are [this](https://github.com/LittleBigBug/QuickBMS/blob/5315ffe664b88dc09ae783ad17d9dfd252b1c927/src/unz.c#L5406-L5538), [this](https://github.com/LittleBigBug/QuickBMS/blob/5315ffe664b88dc09ae783ad17d9dfd252b1c927/src/unz.c#L5090-L5118), and [this](https://github.com/LittleBigBug/QuickBMS/blob/5315ffe664b88dc09ae783ad17d9dfd252b1c927/src/perform.c#L2358-L2365).
    * Sphinx Modding Community: Documented their research on the inner workings of
      Eurocom's games, specifically on their [proprietary in-house game engine](https://sphinxandthecursedmummy.fandom.com/wiki/EngineX) and
      [the EDB archival format.](https://sphinxandthecursedmummy.fandom.com/wiki/EDB)
    * Eurocom: Developers of Rio: The Video Game.
