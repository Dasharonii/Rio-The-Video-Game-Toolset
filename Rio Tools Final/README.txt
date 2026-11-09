Rio asset tools
===============

Two drag-and-drop tools for the Eurocom EDB archives in Rio, for both the
PlayStation 3 and the Xbox 360 builds of the game.

  1 - Extract EDB.bat
      Drag .edb archives onto this - or better, drag the whole folder of them.
      Everything inside is written to an "extracted_assets" folder beside them,
      along with resource_index.tsv.

      Do this first, and do it for every archive at once. Resources reference
      each other by number, and that numbering is only explained by the index
      built here. A mesh in one archive routinely uses textures kept in another,
      so extracting a few archives on their own gives a mesh missing maps and
      missing colour variants.

  2 - Convert Entity to GLB.bat
      Drag .entity files (or a folder holding them) onto this.
      Each one gets a folder beside it, named after the mesh, containing:
          <name>.glb           the mesh, with its materials, maps and rig
          <name>_a2.glb ...    one more for every colour variant of the mesh
          <name>_collision.glb the collision mesh, on the rare mesh that has one
          textures\            every texture it uses, as PNG

      Meshes that carry them also get a second UV set as TEXCOORD_1 - the lightmap
      channel - and a vertex colour as COLOR_0.

      The GLBs embed their textures, so they open anywhere on their own. The
      textures folder is there to work from.

Requirements
------------
  Python 3.9 or newer          https://www.python.org/downloads/
      Tick "Add python.exe to PATH" in the installer.
  Pillow, for PNG output       pip install pillow
  NumPy, for normal maps       pip install numpy

  Without Pillow the textures are written as .dds instead of .png.
  Without NumPy the normal maps are left in the packed form the game stores.

Notes
-----
  Dropping hundreds of files at once can exceed the Windows command line
  limit - drop the folder instead.

  Colour variants are found by name: a colour map ending in a number, such as
  brdh_body_a1_cm, is treated as one of a family with a2, a3 and so on. The
  format itself does not record which colours belong together.

  Meshes with no UVs (some effect meshes) export as geometry only.
