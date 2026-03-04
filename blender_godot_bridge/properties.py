"""
properties.py — Blender PropertyGroup classes for Godot Bridge.
"""

import bpy
from bpy.props import (
    StringProperty, EnumProperty, BoolProperty, PointerProperty,
    IntProperty, CollectionProperty,
)
from bpy.types import PropertyGroup

from .utils import (
    GODOT_NODE_TYPES, ROOT_NODE_TYPES, GLB_EXPORT_MODES,
    COLLISION_SHAPE_TYPES, CUSTOM_COLLISION_SUBTYPES,
    EXPORT_MODES, BODY_NODE_TYPES, COLLECTION_COLLISION_MODES,
)


# ---------------------------------------------------------------------------
# Dynamic enum callback for collections
# ---------------------------------------------------------------------------

def get_collection_items(self, context):
    """Return all Blender collections as enum items for a dropdown."""
    items = [("", "(None)", "No collection selected")]
    for col in bpy.data.collections:
        items.append((col.name, col.name, f"Collection: {col.name}"))
    return items


# ---------------------------------------------------------------------------
# Collection export definition  (one per collection configured in a scene)
# ---------------------------------------------------------------------------

class GodotCollectionDef(PropertyGroup):
    """Settings for one Blender collection in Collection Mode."""
    collection_name: StringProperty(
        name="Collection", default="",
        description="Name of the Blender collection this entry controls",
    )
    # Dynamic enum for picking from existing collections via dropdown
    collection_picker: EnumProperty(
        name="Collection",
        description="Pick a Blender collection from the dropdown",
        items=get_collection_items,
    )
    body_type: EnumProperty(
        name="Body Type", items=BODY_NODE_TYPES, default="StaticBody3D",
    )
    collision_mode: EnumProperty(
        name="Collision Mode", items=COLLECTION_COLLISION_MODES, default="PER_OBJECT",
    )
    collision_shape_type: EnumProperty(
        name="Proxy Shape", items=COLLISION_SHAPE_TYPES, default="CONVEX",
        description="Shape type for the combined proxy collision mesh",
    )
    # PointerProperty to a mesh object marked as the collision proxy for this collection.
    # Set automatically by the "Add Collision Proxy" operator, or manually by the user.
    collision_proxy: PointerProperty(
        name="Collision Proxy Mesh",
        type=bpy.types.Object,
        description="Mesh object to use as the combined collision shape for this collection",
        poll=lambda self, obj: obj.type == 'MESH',
    )


# ---------------------------------------------------------------------------
# Scene export definition  (one entry per scene the user wants to export)
# ---------------------------------------------------------------------------

class GodotSceneDef(PropertyGroup):
    """One entry in the scene list — defines a single .tscn output."""
    scene_name: StringProperty(
        name="Scene Name", default="scene",
        description="Output filename without extension",
    )
    root_node_type: EnumProperty(
        name="Root Node Type", items=ROOT_NODE_TYPES, default="Node3D",
    )
    root_node_name: StringProperty(name="Root Node Name", default="Root")
    export_mode: EnumProperty(
        name="Export Mode", items=EXPORT_MODES, default="OBJECT",
        description="Object Mode: per-object settings. Collection Mode: collections drive hierarchy.",
    )
    # Collection Mode: list of collection definitions
    collections: CollectionProperty(type=GodotCollectionDef)
    active_collection_index: IntProperty(name="Active Collection", default=0)


# ---------------------------------------------------------------------------
# Per-object properties
# ---------------------------------------------------------------------------

class GodotObjectProps(PropertyGroup):
    export: BoolProperty(name="Export to Godot", default=False)

    # Comma-separated list of scene_name values this object belongs to.
    # Empty string = not assigned to any scene (will not be exported).
    # Objects are exported in every scene whose name appears in this list.
    scene_memberships: StringProperty(
        name="Scene Memberships",
        default="",
        description="Comma-separated scene names this object is included in",
    )

    godot_node_type: EnumProperty(
        name="Node Type", items=GODOT_NODE_TYPES, default="MeshInstance3D",
    )
    node_name: StringProperty(
        name="Node Name", default="",
        description="Override the node name in Godot (blank = use object name)",
    )
    parent_node: StringProperty(
        name="Parent Node", default="",
        description=(
            "Name of the parent node in this scene. "
            "Leave blank to parent directly under the root."
        ),
    )
    glb_export_mode: EnumProperty(
        name="GLB Export", items=GLB_EXPORT_MODES, default="INDIVIDUAL",
    )
    glb_group_name: StringProperty(
        name="Group Name", default="mesh_group",
        description="Objects sharing this group name are merged into one GLB",
    )

    # Auto-collision — shown when godot_node_type == MeshInstance3D
    add_collision: BoolProperty(
        name="Add Collision Shape", default=False,
        description="Auto-generate a CollisionShape3D sibling node for this mesh",
    )
    auto_collision_type: EnumProperty(
        name="Collision Shape", items=COLLISION_SHAPE_TYPES, default="BOX",
    )
    collision_custom_mesh: PointerProperty(
        name="Custom Collision Mesh",
        type=bpy.types.Object,
        description="Mesh object to use as the collision shape source",
        poll=lambda self, obj: obj.type == 'MESH',
    )
    custom_collision_subtype: EnumProperty(
        name="Custom Shape Type",
        items=CUSTOM_COLLISION_SUBTYPES,
        default="CONVEX",
        description="Convex or concave shape to generate from the custom mesh",
    )

    # Standalone CollisionShape3D node
    collision_shape: EnumProperty(
        name="Shape Type", items=COLLISION_SHAPE_TYPES, default="BOX",
    )

    # Object Mode: physics body wrapper
    body_type: EnumProperty(
        name="Body Type", items=BODY_NODE_TYPES, default="NONE",
        description="Wrap this mesh in a physics body parent node",
    )

    # Collection Mode: which collection drives this object's hierarchy placement
    # when the object belongs to multiple collections.
    primary_collection: StringProperty(
        name="Primary Collection", default="",
        description=(
            "In Collection Mode, the collection whose body/collision settings "
            "determine this object's parent node. Leave blank to use the first "
            "matching configured collection."
        ),
    )

    # Flag: this object is a collision proxy mesh (created by Add Collision Proxy).
    # Proxy meshes are excluded from GLB export and node writing.
    is_collision_proxy: BoolProperty(
        name="Is Collision Proxy", default=False,
        description="This object is a collision proxy — excluded from normal export",
    )


# ---------------------------------------------------------------------------
# Scene-level settings
# ---------------------------------------------------------------------------

class GodotSceneProps(PropertyGroup):
    project_root: StringProperty(
        name="Godot Project Root", subtype="DIR_PATH", default="",
        description="Folder containing project.godot",
    )
    export_path: StringProperty(
        name="Export Folder", subtype="DIR_PATH", default="",
        description="Where to write .tscn and .glb files (must be inside project root)",
    )
    apply_transforms: BoolProperty(
        name="Apply Transforms on GLB", default=True,
        description=(
            "Bake object transforms into mesh geometry when exporting GLB. "
            "ON = identity in TSCN (recommended).  "
            "OFF = transform read from GLB JSON."
        ),
    )
    # List of scene definitions
    scenes: CollectionProperty(type=GodotSceneDef)
    # Index of the scene currently selected in the UI list
    active_scene_index: IntProperty(name="Active Scene", default=0)

    # ── Batch Export settings ─────────────────────────────────────────────
    batch_root_node_type: EnumProperty(
        name="Root Node Type", items=ROOT_NODE_TYPES, default="Node3D",
        description="Root node type for each individually exported .tscn",
    )
    batch_add_collision: BoolProperty(
        name="Add Collision Shape", default=False,
        description="Add a CollisionShape3D to each exported object's scene",
    )
    batch_collision_type: EnumProperty(
        name="Collision Shape", items=COLLISION_SHAPE_TYPES, default="BOX",
        description="Shape type for the auto-generated collision",
    )
    batch_body_type: EnumProperty(
        name="Body Type", items=BODY_NODE_TYPES, default="NONE",
        description="Wrap the mesh in a physics body node",
    )
    batch_only_selected: BoolProperty(
        name="Selected Objects Only", default=False,
        description="Export only currently selected objects instead of all marked objects",
    )
