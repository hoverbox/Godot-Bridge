# =============================================================================
#  Blender to Godot Export — __init__.py
#
#  DESIGN NOTES  (v2.7)
#  --------------------
#  TRANSFORM STRATEGY
#  ------------------
#  Mesh GLBs:
#    apply_transforms=ON  → geometry baked at world position, node gets identity.
#    apply_transforms=OFF → geometry at local origin, node gets world transform
#                           read back from the GLB JSON.
#
#  CollisionShape3D siblings:
#    Always use matrix_world_to_transform3d(obj.matrix_world) — computed directly
#    from Blender's matrix, independent of apply_transforms.
#
#  COLLISION ARCHITECTURE
#  ----------------------
#  ALL collision shapes are written as inline sub_resource blocks in the TSCN.
#  No separate GLB files, no manual import steps — opens and works immediately.
#
#  Primitive (Box/Sphere/Capsule/Cylinder):
#    Sized from the mesh bounding box.
#
#  Convex Hull:
#    ConvexPolygonShape3D — points array contains all mesh vertices in Godot
#    world space. Godot computes the actual convex hull at load time.
#
#  Concave / Trimesh:
#    ConcavePolygonShape3D — faces array contains all triangulated faces in
#    Godot world space. Exact mesh geometry. Static bodies only.
#
#  Custom Mesh:
#    Same as Convex or Concave (user's choice), but the source mesh is the
#    picked custom_obj instead of the host object. Use this to assign a
#    separate low-poly collision mesh to a high-poly visible mesh.
#
#  WHY COLLISION SHAPE IS A SIBLING, NOT A CHILD
#  -----------------------------------------------
#  MeshInstance3D nodes are written as instanced PackedScenes. Child nodes
#  cannot be added to instanced scenes in plain .tscn text — they would
#  end up inside the sub-scene. The CollisionShape3D must be a sibling
#  (same parent) so physics bodies can see both the mesh and the shape.
# =============================================================================

import bpy
from bpy.props import PointerProperty

from . import materials  # noqa: F401 — registers the module for use by exporters

from .properties import (
    GodotCollectionDef,
    GodotSceneDef,
    GodotObjectProps,
    GodotSceneProps,
)
from .operators import (
    GODOT_OT_ApplyToSelected,
    GODOT_OT_DetectProjectRoot,
    GODOT_OT_ExportScene,
    GODOT_OT_ExportActiveScene,
    GODOT_OT_AddScene,
    GODOT_OT_RemoveScene,
    GODOT_OT_AssignToScene,
    GODOT_OT_MarkSelected,
    GODOT_OT_AddCollection,
    GODOT_OT_RemoveCollection,
    GODOT_OT_PickCollection,
    GODOT_OT_AddCollisionProxy,
    GODOT_OT_SelectSceneObjects,
    GODOT_OT_BatchExportObjects,
)
from .panels import (
    GODOT_UL_SceneList,
    GODOT_PT_ObjectPanel,
    GODOT_PT_ScenePanel,
    GODOT_PT_BatchExportPanel,
)


classes = (
    GodotCollectionDef,
    GodotSceneDef,
    GodotObjectProps,
    GodotSceneProps,
    GODOT_OT_ApplyToSelected,
    GODOT_OT_DetectProjectRoot,
    GODOT_OT_ExportScene,
    GODOT_OT_ExportActiveScene,
    GODOT_OT_AddScene,
    GODOT_OT_RemoveScene,
    GODOT_OT_AssignToScene,
    GODOT_OT_MarkSelected,
    GODOT_OT_AddCollection,
    GODOT_OT_RemoveCollection,
    GODOT_OT_PickCollection,
    GODOT_OT_AddCollisionProxy,
    GODOT_OT_SelectSceneObjects,
    GODOT_OT_BatchExportObjects,
    GODOT_UL_SceneList,
    GODOT_PT_ObjectPanel,
    GODOT_PT_ScenePanel,
    GODOT_PT_BatchExportPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.godot_props      = PointerProperty(type=GodotObjectProps)
    bpy.types.Scene.godot_scene_props = PointerProperty(type=GodotSceneProps)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Object.godot_props
    del bpy.types.Scene.godot_scene_props


if __name__ == "__main__":
    register()
