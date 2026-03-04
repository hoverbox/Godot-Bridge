bl_info = {
    "name": "Godot Bridge Exporter",
    "author": "Godot Bridge",
    "version": (2, 9, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Godot Bridge",
    "description": "Export Blender scenes to Godot 4 .tscn + GLB with correct res:// paths",
    "category": "Import-Export",
}

# =============================================================================
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
import os
import re
import json
import struct
import mathutils
from bpy.props import (
    StringProperty, EnumProperty, BoolProperty, PointerProperty,
    IntProperty, CollectionProperty
)
from bpy.types import Panel, Operator, PropertyGroup


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

GODOT_NODE_TYPES = [
    ("MeshInstance3D",   "MeshInstance3D",       "Instanced GLB mesh scene"),
    ("StaticBody3D",     "StaticBody3D",          "Static physics body"),
    ("RigidBody3D",      "RigidBody3D",           "Rigid physics body"),
    ("CharacterBody3D",  "CharacterBody3D",       "Character controller body"),
    ("Area3D",           "Area3D",                "Trigger / detection area"),
    ("CollisionShape3D", "CollisionShape3D",      "Standalone collision shape node"),
    ("AnimatableBody3D", "AnimatableBody3D",      "Animatable body"),
    ("VehicleBody3D",    "VehicleBody3D",         "Vehicle body"),
    ("VehicleWheel3D",   "VehicleWheel3D",        "Vehicle wheel"),
    ("Path3D",           "Path3D",                "Spline path"),
    ("Marker3D",         "Marker3D",              "Spawn point / marker"),
    ("Node3D",           "Node3D",                "Generic 3D node"),
    ("NONE",             "(Skip / Don't Export)", "Exclude from export"),
]

ROOT_NODE_TYPES = [
    ("Node3D",           "Node3D",           "Generic 3D root"),
    ("CharacterBody3D",  "CharacterBody3D",  "Character controller"),
    ("RigidBody3D",      "RigidBody3D",      "Rigid body"),
    ("StaticBody3D",     "StaticBody3D",     "Static body"),
    ("Area3D",           "Area3D",           "Area / trigger"),
    ("AnimatableBody3D", "AnimatableBody3D", "Animatable body"),
    ("VehicleBody3D",    "VehicleBody3D",    "Vehicle"),
    ("Node",             "Node",             "Non-spatial root"),
]

GLB_EXPORT_MODES = [
    ("INDIVIDUAL", "Individual GLB", "One .glb per object"),
    ("GROUP",      "Group GLB",      "Merge objects with the same Group Name into one GLB"),
    ("NONE",       "No GLB",         "No mesh file — node has no mesh"),
]

COLLISION_SHAPE_TYPES = [
    ("BOX",      "Box",               "BoxShape3D sized to bounding box"),
    ("SPHERE",   "Sphere",            "SphereShape3D sized to bounding box"),
    ("CAPSULE",  "Capsule",           "CapsuleShape3D sized to bounding box"),
    ("CYLINDER", "Cylinder",          "CylinderShape3D sized to bounding box"),
    ("CONVEX",   "Convex Hull",       "ConvexPolygonShape3D — exports this mesh as a GLB for import"),
    ("CONCAVE",  "Concave / Trimesh", "ConcavePolygonShape3D — exact mesh (static only)"),
    ("CUSTOM",   "Custom Mesh",       "Use a different mesh object as the collision source"),
]

CUSTOM_COLLISION_SUBTYPES = [
    ("CONVEX",  "Convex Hull",       "ConvexPolygonShape3D from the picked mesh"),
    ("CONCAVE", "Concave / Trimesh", "ConcavePolygonShape3D from the picked mesh (static only)"),
]

PRIMITIVE_SHAPES = {"BOX", "SPHERE", "CAPSULE", "CYLINDER"}
MESH_SHAPES      = {"CONVEX", "CONCAVE"}

EXPORT_MODES = [
    ("OBJECT",     "Object Mode",     "Per-object settings drive the hierarchy (classic behaviour)"),
    ("COLLECTION", "Collection Mode", "Collections drive the hierarchy — meshes group under body nodes"),
]

BODY_NODE_TYPES = [
    ("NONE",           "No Body",          "No physics body wrapper — mesh exports as plain MeshInstance3D"),
    ("StaticBody3D",   "StaticBody3D",     "Wrap in a StaticBody3D parent"),
    ("RigidBody3D",    "RigidBody3D",      "Wrap in a RigidBody3D parent"),
    ("Area3D",         "Area3D",           "Wrap in an Area3D parent"),
    ("AnimatableBody3D","AnimatableBody3D","Wrap in an AnimatableBody3D parent"),
]

COLLECTION_COLLISION_MODES = [
    ("PER_OBJECT", "Per Object",       "Each object generates its own CollisionShape3D child"),
    ("COMBINED",   "Combined Proxy",   "One CollisionShape3D from the assigned proxy mesh"),
]


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


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def abs_dir(path_str: str) -> str:
    return os.path.normpath(bpy.path.abspath(path_str))


def compute_res_path(project_root_abs: str, export_dir_abs: str, filename: str) -> str:
    try:
        rel = os.path.relpath(export_dir_abs, project_root_abs)
    except ValueError:
        rel = ""
    rel = rel.replace("\\", "/").strip("/")
    return f"res://{rel}/{filename}" if (rel and rel != ".") else f"res://{filename}"


def validate_paths(context) -> str:
    sp = context.scene.godot_scene_props
    proj_raw = sp.project_root.strip()
    exp_raw  = sp.export_path.strip()
    if not proj_raw:
        return "Godot Project Root is not set."
    proj_abs = abs_dir(proj_raw)
    if not os.path.isdir(proj_abs):
        return f"Project Root does not exist:\n{proj_abs}"
    if not os.path.isfile(os.path.join(proj_abs, "project.godot")):
        return f"No project.godot found in:\n{proj_abs}"
    if not exp_raw:
        return "Export Folder is not set."
    exp_abs = abs_dir(exp_raw)
    try:
        rel = os.path.relpath(exp_abs, proj_abs)
    except ValueError:
        return "Export Folder must be on the same drive as the Project Root."
    if rel.startswith(".."):
        return "Export Folder must be inside the Godot Project Root."
    if len(sp.scenes) == 0:
        return "No scenes defined. Add at least one scene."
    return ""


def validate_paths_no_scenes(context) -> str:
    """Path validation that does NOT require any scene definitions (used for batch export)."""
    sp = context.scene.godot_scene_props
    proj_raw = sp.project_root.strip()
    exp_raw  = sp.export_path.strip()
    if not proj_raw:
        return "Godot Project Root is not set."
    proj_abs = abs_dir(proj_raw)
    if not os.path.isdir(proj_abs):
        return f"Project Root does not exist:\n{proj_abs}"
    if not os.path.isfile(os.path.join(proj_abs, "project.godot")):
        return f"No project.godot found in:\n{proj_abs}"
    if not exp_raw:
        return "Export Folder is not set."
    exp_abs = abs_dir(exp_raw)
    try:
        rel = os.path.relpath(exp_abs, proj_abs)
    except ValueError:
        return "Export Folder must be on the same drive as the Project Root."
    if rel.startswith(".."):
        return "Export Folder must be inside the Godot Project Root."
    return ""


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def sanitize(name: str) -> str:
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if name and name[0].isdigit():
        name = "_" + name
    return name or "Node"


def get_node_name(obj) -> str:
    override = obj.godot_props.node_name.strip()
    return sanitize(override if override else obj.name)


def bbox_half_extents(obj, depsgraph=None):
    """
    Return (hx, hy, hz) half-extents of obj's bounding box in Godot Y-up space.
    Uses the *evaluated* mesh (respects modifiers like Bevel) in world space.
    """
    import bmesh as _bm
    if depsgraph is None:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    bm = _bm.new()
    obj_eval = obj.evaluated_get(depsgraph)
    bm.from_mesh(obj_eval.to_mesh())
    mw = obj.matrix_world
    xs, ys, zs = [], [], []
    for v in bm.verts:
        wp = mw @ v.co
        xs.append(wp.x)
        ys.append(wp.z)   # Blender Z → Godot Y
        zs.append(-wp.y)  # -Blender Y → Godot Z
    bm.free()
    obj_eval.to_mesh_clear()
    if not xs:
        return (0.5, 0.5, 0.5)
    hx = max((max(xs) - min(xs)) * 0.5, 0.001)
    hy = max((max(ys) - min(ys)) * 0.5, 0.001)
    hz = max((max(zs) - min(zs)) * 0.5, 0.001)
    return (hx, hy, hz)


# ---------------------------------------------------------------------------
# Transform helpers
# ---------------------------------------------------------------------------

IDENTITY_TRANSFORM = "Transform3D(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)"


def fmtf(v: float) -> str:
    """
    Format a float for Godot TSCN.  Must be a plain decimal — Godot's parser
    rejects scientific notation (1e-7), nan, and inf.
    We use fixed-point with 7 significant digits of precision.
    """
    import math
    if not math.isfinite(v):
        return "0.0"
    # Clamp sub-micron values to zero.  Torus/cone/icosphere meshes have
    # many vertices where sin/cos floating-point error produces values like
    # -1.19e-7.  These format as '-0.0000001' which Godot's
    # PackedVector3Array constructor parser rejects as an invalid float token.
    if abs(v) < 1e-6:
        return "0.0"
    # :.7g would use sci-notation for very small/large values.
    # Instead, always write fixed-point.  Strip trailing zeros but keep
    # at least one decimal place so Godot sees it as a float, not an int.
    s = f"{v:.7f}".rstrip('0')
    if s.endswith('.'):
        s += '0'
    return s


def trs_to_transform3d(t, r, s) -> str:
    """TRS tuples (already in Godot Y-up space from GLB) → Transform3D literal."""
    tx, ty, tz      = t
    qx, qy, qz, qw  = r
    sx, sy, sz       = s
    x2, y2, z2 = qx*2, qy*2, qz*2
    xx, yy, zz = qx*x2, qy*y2, qz*z2
    xy, xz, yz = qx*y2, qx*z2, qy*z2
    wx, wy, wz = qw*x2, qw*y2, qw*z2
    Xx=(1-(yy+zz))*sx; Xy=(xy+wz)*sx;  Xz=(xz-wy)*sx
    Yx=(xy-wz)*sy;     Yy=(1-(xx+zz))*sy; Yz=(yz+wx)*sy
    Zx=(xz+wy)*sz;     Zy=(yz-wx)*sz;  Zz=(1-(xx+yy))*sz
    f = fmtf
    return (f"Transform3D("
            f"{f(Xx)}, {f(Xy)}, {f(Xz)}, "
            f"{f(Yx)}, {f(Yy)}, {f(Yz)}, "
            f"{f(Zx)}, {f(Zy)}, {f(Zz)}, "
            f"{f(tx)}, {f(ty)}, {f(tz)})")


def matrix_world_to_transform3d(matrix_world) -> str:
    """
    Convert a Blender world matrix directly to a Godot Transform3D string.
    Used for CollisionShape3D nodes which always need their world-space position
    regardless of whether apply_transforms is on or off.

    Blender is Z-up / Y-forward.  Godot is Y-up / -Z-forward.
    The correction is: swap Y and Z axes, negate new Z.
      Godot X = Blender X
      Godot Y = Blender Z
      Godot Z = -Blender Y
    """
    m = matrix_world
    bX = (m[0][0], m[1][0], m[2][0])
    bY = (m[0][1], m[1][1], m[2][1])
    bZ = (m[0][2], m[1][2], m[2][2])
    bT = (m[0][3], m[1][3], m[2][3])

    def bl_to_gd(v):
        return (v[0], v[2], -v[1])

    gX = bl_to_gd(bX)
    gY = bl_to_gd(bY)
    gZ = bl_to_gd(bZ)
    gT = bl_to_gd(bT)

    f = fmtf
    return (f"Transform3D("
            f"{f(gX[0])}, {f(gX[1])}, {f(gX[2])}, "
            f"{f(gY[0])}, {f(gY[1])}, {f(gY[2])}, "
            f"{f(gZ[0])}, {f(gZ[1])}, {f(gZ[2])}, "
            f"{f(gT[0])}, {f(gT[1])}, {f(gT[2])})")


# ---------------------------------------------------------------------------
# GLB reader  (pure Python stdlib — no external deps)
# ---------------------------------------------------------------------------

def read_glb_json(filepath: str) -> dict:
    try:
        with open(filepath, 'rb') as f:
            if f.read(4) != b'glTF':
                return {}
            f.read(8)
            chunk_len  = struct.unpack_from('<I', f.read(4))[0]
            chunk_type = struct.unpack_from('<I', f.read(4))[0]
            if chunk_type != 0x4E4F534A:   # "JSON"
                return {}
            return json.loads(f.read(chunk_len))
    except Exception:
        return {}


def glb_node_transforms(glb_json: dict) -> dict:
    result = {}
    for node in glb_json.get("nodes", []):
        name = node.get("name", "")
        t = tuple(node.get("translation", [0.0, 0.0, 0.0]))
        r = tuple(node.get("rotation",    [0.0, 0.0, 0.0, 1.0]))
        s = tuple(node.get("scale",       [1.0, 1.0, 1.0]))
        result[name] = (t, r, s)
    return result


# ---------------------------------------------------------------------------
# GLB export  — completely isolated, cannot corrupt other exports
# ---------------------------------------------------------------------------

def export_glb_isolated(context, objects, filepath: str, apply_transforms: bool) -> bool:
    """
    Export a list of mesh objects to a GLB file.

    Saves and restores hide flags so hidden helper meshes (e.g. custom collision
    objects) can be exported even when the user has them hidden in the viewport.

    Does NOT attempt to restore selection or active object — every call starts
    with a full DESELECT so there is nothing to restore and nothing that can
    accidentally interfere with a subsequent call.

    Returns True on success, False if there were no mesh objects to export.
    """
    mesh_objects = [o for o in objects if o.type == 'MESH']
    if not mesh_objects:
        return False

    prev_hide_vp  = {o.name: o.hide_viewport for o in mesh_objects}
    prev_hide_rnd = {o.name: o.hide_render   for o in mesh_objects}

    try:
        for obj in mesh_objects:
            obj.hide_viewport = False
            obj.hide_render   = False

        bpy.ops.object.select_all(action='DESELECT')
        for obj in mesh_objects:
            obj.select_set(True)
        context.view_layer.objects.active = mesh_objects[0]

        bpy.ops.export_scene.gltf(
            filepath=filepath,
            export_format='GLB',
            use_selection=True,
            export_yup=True,
            export_apply=apply_transforms,
            export_cameras=False,
            export_lights=False,
        )
        return True

    finally:
        bpy.ops.object.select_all(action='DESELECT')
        for obj in mesh_objects:
            obj.hide_viewport = prev_hide_vp[obj.name]
            obj.hide_render   = prev_hide_rnd[obj.name]


# ---------------------------------------------------------------------------
# Collision shape sub-resource writers
# ---------------------------------------------------------------------------

def vertex_mean_godot(obj, depsgraph=None):
    """
    Return the mean (centroid) of all world-space mesh vertices in Godot coordinates.
    Returns a mathutils.Vector (gx, gy, gz).
    depsgraph: pass the operator context depsgraph explicitly to avoid stale
               bpy.context issues when the object is not the active object.
    """
    import bmesh as _bm
    if depsgraph is None:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    bm = _bm.new()
    obj_eval  = obj.evaluated_get(depsgraph)
    bm.from_mesh(obj_eval.to_mesh())
    mw = obj.matrix_world
    sx, sy, sz = 0.0, 0.0, 0.0
    n = len(bm.verts)
    if n:
        for v in bm.verts:
            wp = mw @ v.co
            sx += wp.x; sy += wp.z; sz += -wp.y   # Blender→Godot
        sx /= n; sy /= n; sz /= n
    bm.free()
    obj_eval.to_mesh_clear()
    return mathutils.Vector((sx, sy, sz))


def centroid_transform3d(gx: float, gy: float, gz: float) -> str:
    """Identity rotation/scale, centroid as translation."""
    f = fmtf
    return (f"Transform3D(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, "
            f"{f(gx)}, {f(gy)}, {f(gz)})")


def write_sized_shape(lines, sid, ctype, half_extents):
    """Write a primitive collision shape sub_resource block (Box/Sphere/Capsule/Cylinder)."""
    hx, hy, hz = half_extents
    if ctype == "BOX":
        lines.append(f'[sub_resource type="BoxShape3D" id="{sid}"]')
        lines.append(f"size = Vector3({fmtf(hx*2)}, {fmtf(hy*2)}, {fmtf(hz*2)})")
        lines.append("")
    elif ctype == "SPHERE":
        r = max(hx, hy, hz)
        lines.append(f'[sub_resource type="SphereShape3D" id="{sid}"]')
        lines.append(f"radius = {fmtf(r)}")
        lines.append("")
    elif ctype == "CAPSULE":
        r = max(hx, hz)
        h = max(hy * 2 - r * 2, 0.001)
        lines.append(f'[sub_resource type="CapsuleShape3D" id="{sid}"]')
        lines.append(f"radius = {fmtf(r)}")
        lines.append(f"height = {fmtf(h + r * 2)}")
        lines.append("")
    elif ctype == "CYLINDER":
        lines.append(f'[sub_resource type="CylinderShape3D" id="{sid}"]')
        lines.append(f"radius = {fmtf(max(hx, hz))}")
        lines.append(f"height = {fmtf(hy * 2)}")
        lines.append("")


def write_convex_shape(lines, sid, obj, depsgraph, centroid=None):
    """Write a ConvexPolygonShape3D sub_resource from a mesh object's vertices.
    PackedVector3Array takes flat floats (x, y, z, x, y, z, ...) — NOT Vector3(...)
    constructors. Deduplicate vertices to keep the hull clean.
    Vertices are written in Godot world space. The CollisionShape3D node sits at
    identity so Godot evaluates the hull in the same space as the body.
    """
    obj_eval = obj.evaluated_get(depsgraph)
    mesh = obj_eval.to_mesh()
    mesh.transform(obj.matrix_world)  # bake world transform into coords
    seen = set()
    for v in mesh.vertices:
        # Blender→Godot: x=x, y=z, z=-y
        seen.add((fmtf(v.co.x), fmtf(v.co.z), fmtf(-v.co.y)))
    obj_eval.to_mesh_clear()
    flat = []
    for gx, gy, gz in seen:
        flat.extend([gx, gy, gz])
    lines.append(f'[sub_resource type="ConvexPolygonShape3D" id="{sid}"]')
    lines.append(f"points = PackedVector3Array({', '.join(flat)})")
    lines.append("")


def export_concave_glb(context, obj, glb_path: str) -> bool:
    """Export a single mesh as a -colonly GLB for ConcavePolygonShape3D import."""
    original_name  = obj.name
    prev_hide_vp   = obj.hide_viewport
    prev_hide_rnd  = obj.hide_render

    try:
        obj.name          = sanitize(obj.name) + "-colonly"
        obj.hide_viewport = False
        obj.hide_render   = False

        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj

        bpy.ops.export_scene.gltf(
            filepath=glb_path,
            export_format='GLB',
            use_selection=True,
            export_yup=True,
            export_apply=True,
            export_cameras=False,
            export_lights=False,
        )
        return True

    finally:
        obj.name          = original_name
        obj.hide_viewport = prev_hide_vp
        obj.hide_render   = prev_hide_rnd
        bpy.ops.object.select_all(action='DESELECT')


def write_concave_import_file(glb_path: str, res_path: str) -> None:
    """
    Write a Godot .import sidecar file next to glb_path that enables
    node name suffix processing so Godot recognises the -colonly suffix
    and generates a ConcavePolygonShape3D on import.
    res_path is the res:// path of the GLB (e.g. res://Assets/Level_1/foo-colonly.glb).
    """
    import_path = glb_path + ".import"

    content = (
        "[remap]\n\n"
        "importer=\"scene\"\n"
        "importer_version=1\n"
        "type=\"PackedScene\"\n\n"
        "[deps]\n\n"
        f"source_file=\"{res_path}\"\n\n"
        "[params]\n\n"
        "nodes/root_type=\"\"\n"
        "nodes/root_name=\"\"\n"
        "nodes/apply_root_scale=true\n"
        "nodes/root_scale=1.0\n"
        "nodes/import_as_skeleton_bones=false\n"
        "nodes/use_name_suffixes=true\n"
        "nodes/use_node_type_suffixes=true\n"
        "meshes/ensure_tangents=true\n"
        "meshes/generate_lods=false\n"
        "meshes/create_shadow_meshes=false\n"
        "meshes/light_baking=0\n"
        "meshes/force_disable_compression=false\n"
        "skins/use_named_skins=true\n"
        "animation/import=false\n"
        "_subresources={}\n"
    )

    with open(import_path, 'w') as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Core TSCN builder
# ---------------------------------------------------------------------------

def build_tscn(context, project_root_abs: str, export_dir_abs: str,
               scene_def) -> str:
    """
    Build the TSCN text for one scene definition.

    OBJECT MODE  (scene_def.export_mode == "OBJECT")
    ------------------------------------------------
    Classic per-object behaviour.  Each object's godot_node_type, body_type,
    parent_node, and collision settings drive the output.

    Body type NONE  (current / legacy behaviour):
        Root
          └── MyMesh (MeshInstance3D)
          └── MyMesh_Col (CollisionShape3D)   # only if add_collision

    Body type set (e.g. StaticBody3D):
        Root
          └── MyMesh (StaticBody3D)
                └── MyMesh_Mesh (MeshInstance3D)
                └── MyMesh_Col (CollisionShape3D)   # only if add_collision

    COLLECTION MODE  (scene_def.export_mode == "COLLECTION")
    ---------------------------------------------------------
    Collections configured in scene_def.collections drive the hierarchy.
    Objects not in any configured collection are warned and skipped.

    Body type set:
        Root
          └── MyCollection (StaticBody3D)
                └── Obj_A_Mesh (MeshInstance3D)
                └── Obj_A_Col (CollisionShape3D)   # per-object mode
                └── Obj_B_Mesh (MeshInstance3D)
                └── Obj_B_Col (CollisionShape3D)
          OR
                └── Obj_A_Mesh (MeshInstance3D)
                └── Obj_B_Mesh (MeshInstance3D)
                └── MyCollection_Col (CollisionShape3D)   # combined mode

    Body type NONE:
        Root
          └── Obj_A_Mesh (MeshInstance3D)
          └── Obj_A_Col (CollisionShape3D)
          └── Obj_B_Mesh (MeshInstance3D)
          └── Obj_B_Col (CollisionShape3D)

    GODOT GROUPS  (both modes)
    --------------------------
    Every Blender collection an object belongs to is written as:
        metadata/_groups = ["CollA", "CollB"]
    on the outermost exported node for that object (body node, or mesh node
    if no body).  Groups metadata goes on body/collection nodes, not on
    instanced MeshInstance3D nodes (instanced PackedScenes can't have metadata
    in plain TSCN).
    """
    sp        = context.scene.godot_scene_props
    root_type = scene_def.root_node_type
    root_name = sanitize(scene_def.root_node_name) or "Root"
    apply_tr  = sp.apply_transforms
    mode      = scene_def.export_mode   # "OBJECT" or "COLLECTION"
    sname     = scene_def.scene_name.strip()

    # ------------------------------------------------------------------
    # Helpers: scene membership
    # ------------------------------------------------------------------
    def obj_in_scene(obj):
        if not obj.godot_props.export:
            return False
        if obj.godot_props.godot_node_type == "NONE":
            return False
        if obj.godot_props.is_collision_proxy:
            return False
        memberships = obj.godot_props.scene_memberships.strip()
        if not memberships:
            return True
        return sname in [m.strip() for m in memberships.split(",")]

    export_objs = [obj for obj in context.scene.objects if obj_in_scene(obj)]
    if not export_objs:
        raise ValueError("No objects are marked for export.")

    # ------------------------------------------------------------------
    # Shared state
    # ------------------------------------------------------------------
    id_counter     = [1]
    glb_to_res_id  = {}
    ext_res_list   = []
    glb_json_cache = {}
    sub_res_blocks = []
    obj_collision  = {}   # obj.name → {sid}   (auto-collision, object mode)
    standalone_col = {}   # obj.name → {sid}   (standalone CollisionShape3D)
    obj_centroid   = {}   # obj.name → Vector(gx,gy,gz)  vertex mean in Godot space
    obj_bbox_ctr   = {}   # obj.name → Vector(gx,gy,gz)  bbox center in Godot space
    warnings       = []   # non-fatal warnings collected during build
    depsgraph      = context.evaluated_depsgraph_get()

    def new_id() -> str:
        v = str(id_counter[0]); id_counter[0] += 1; return v

    def register_mesh_glb(glb_name: str, glb_path: str) -> str:
        if glb_name not in glb_to_res_id:
            rid = new_id()
            glb_to_res_id[glb_name] = rid
            ext_res_list.append((rid, compute_res_path(project_root_abs, export_dir_abs, glb_name)))
        if glb_name not in glb_json_cache:
            glb_json_cache[glb_name] = read_glb_json(glb_path)
        return glb_to_res_id[glb_name]

    def get_mesh_transform_str(obj) -> str:
        if apply_tr:
            c = obj_centroid.get(obj.name)
            if c is not None:
                return centroid_transform3d(c.x, c.y, c.z)
            return IDENTITY_TRANSFORM
        props = obj.godot_props
        glb_mode = props.glb_export_mode
        if glb_mode == "INDIVIDUAL":
            glb_name = sanitize(obj.name) + ".glb"
        elif glb_mode == "GROUP":
            glb_name = (sanitize(props.glb_group_name) or "mesh_group") + ".glb"
        else:
            glb_name = "_tmp_xform_" + sanitize(obj.name) + ".glb"
            glb_path = os.path.join(export_dir_abs, glb_name)
            if glb_name not in glb_json_cache:
                export_glb_isolated(context, [obj], glb_path, False)
                glb_json_cache[glb_name] = read_glb_json(glb_path)
                try: os.remove(glb_path)
                except OSError: pass
        transforms = glb_node_transforms(glb_json_cache.get(glb_name, {}))
        if obj.name in transforms:
            return trs_to_transform3d(*transforms[obj.name])
        san = sanitize(obj.name)
        for k, trs in transforms.items():
            if sanitize(k) == san:
                return trs_to_transform3d(*trs)
        return IDENTITY_TRANSFORM

    def export_centered(context, objects, glb_path):
        if not apply_tr:
            export_glb_isolated(context, objects, glb_path, apply_tr)
            return
        centroids_blender = {}
        for obj in objects:
            if obj.type != 'MESH':
                continue
            # Compute vertex mean AND bbox center in one evaluated-mesh pass.
            import bmesh as _bm
            bm = _bm.new()
            obj_eval = obj.evaluated_get(depsgraph)
            bm.from_mesh(obj_eval.to_mesh())
            mw = obj.matrix_world
            xs, ys, zs = [], [], []
            sx, sy, sz = 0.0, 0.0, 0.0
            n_v = len(bm.verts)
            for v in bm.verts:
                wp = mw @ v.co
                gx, gy, gz = wp.x, wp.z, -wp.y
                sx += gx; sy += gy; sz += gz
                xs.append(gx); ys.append(gy); zs.append(gz)
            bm.free(); obj_eval.to_mesh_clear()
            if n_v:
                sx /= n_v; sy /= n_v; sz /= n_v
            c = mathutils.Vector((sx, sy, sz))
            obj_centroid[obj.name] = c
            if xs:
                obj_bbox_ctr[obj.name] = mathutils.Vector((
                    (max(xs) + min(xs)) * 0.5,
                    (max(ys) + min(ys)) * 0.5,
                    (max(zs) + min(zs)) * 0.5,
                ))
            centroids_blender[obj.name] = mathutils.Vector((c.x, -c.z, c.y))
        saved_loc = {obj.name: obj.location.copy() for obj in objects}
        try:
            for obj in objects:
                if obj.name in centroids_blender:
                    obj.location = obj.location - centroids_blender[obj.name]
            export_glb_isolated(context, objects, glb_path, True)
        finally:
            for obj in objects:
                obj.location = saved_loc[obj.name]

    def build_collision_info(host_obj, ctype, subtype, custom_obj):
        """
        For CONVEX and CONCAVE: if custom_obj is assigned it is used as the
        mesh source instead of host_obj, so the Custom Collision Mesh field
        works with all shape types without needing to switch to CUSTOM mode.

        Returns {"sid": ...} for inline sub_resource shapes (primitives, convex).
        Returns {"ext_rid": ...} for concave shapes exported as a -colonly GLB.
        """
        mesh_source = custom_obj if (custom_obj is not None and custom_obj.type == 'MESH') else host_obj

        if ctype in PRIMITIVE_SHAPES:
            sid = new_id()
            he    = bbox_half_extents(host_obj, depsgraph) if host_obj.type == 'MESH' else (0.5, 0.5, 0.5)
            block = []
            write_sized_shape(block, sid, ctype, he)
            sub_res_blocks.append("\n".join(block))
            return {"sid": sid}
        elif ctype == "CONVEX":
            if mesh_source.type != 'MESH': return None
            sid = new_id()
            block = []
            write_convex_shape(block, sid, mesh_source, depsgraph)
            sub_res_blocks.append("\n".join(block))
            return {"sid": sid}
        elif ctype == "CONCAVE":
            if mesh_source.type != 'MESH': return None
            glb_name = sanitize(mesh_source.name) + "-colonly.glb"
            glb_path = os.path.join(export_dir_abs, glb_name)
            ok = export_concave_glb(context, mesh_source, glb_path)
            if not ok: return None
            res_path = compute_res_path(project_root_abs, export_dir_abs, glb_name)
            write_concave_import_file(glb_path, res_path)
            rid = register_mesh_glb(glb_name, glb_path)
            return {"ext_rid": rid}
        elif ctype == "CUSTOM":
            if mesh_source.type != 'MESH': return None
            sub = subtype if subtype in MESH_SHAPES else "CONVEX"
            if sub == "CONVEX":
                block = []
                sid = new_id()
                write_convex_shape(block, sid, mesh_source, depsgraph)
                sub_res_blocks.append("\n".join(block))
                return {"sid": sid}
            else:
                glb_name = sanitize(mesh_source.name) + "-colonly.glb"
                glb_path = os.path.join(export_dir_abs, glb_name)
                ok = export_concave_glb(context, mesh_source, glb_path)
                if not ok: return None
                res_path = compute_res_path(project_root_abs, export_dir_abs, glb_name)
                write_concave_import_file(glb_path, res_path)
                rid = register_mesh_glb(glb_name, glb_path)
                return {"ext_rid": rid}
        return None

    def groups_for_obj(obj):
        """Return list of collection names this object belongs to (sanitized)."""
        return [sanitize(c.name) for c in bpy.data.collections if obj.name in c.objects]

    def write_groups_metadata(groups):
        """Return a TSCN metadata line for Godot groups, or empty string."""
        if not groups:
            return ""
        quoted = ", ".join(f'"{g}"' for g in groups)
        return f'metadata/_groups = [{quoted}]'

    # ------------------------------------------------------------------
    # Phase 1: Export GLBs
    # ------------------------------------------------------------------
    if mode == "OBJECT":
        group_buckets = {}
        for obj in export_objs:
            if obj.type == 'MESH' and obj.godot_props.glb_export_mode == "GROUP":
                gname = sanitize(obj.godot_props.glb_group_name) or "mesh_group"
                group_buckets.setdefault(gname, []).append(obj)

        for obj in export_objs:
            if obj.type != 'MESH' or obj.godot_props.glb_export_mode != "INDIVIDUAL":
                continue
            glb_name = sanitize(obj.name) + ".glb"
            glb_path = os.path.join(export_dir_abs, glb_name)
            export_centered(context, [obj], glb_path)
            register_mesh_glb(glb_name, glb_path)

        for gname, objs in group_buckets.items():
            glb_name = gname + ".glb"
            glb_path = os.path.join(export_dir_abs, glb_name)
            export_centered(context, objs, glb_path)
            for obj in objs:
                register_mesh_glb(glb_name, glb_path)

    else:  # COLLECTION mode — one GLB per object (always INDIVIDUAL)
        for obj in export_objs:
            if obj.type != 'MESH':
                continue
            glb_name = sanitize(obj.name) + ".glb"
            glb_path = os.path.join(export_dir_abs, glb_name)
            export_centered(context, [obj], glb_path)
            register_mesh_glb(glb_name, glb_path)

    # ------------------------------------------------------------------
    # Phase 2: Build collision data
    # ------------------------------------------------------------------
    if mode == "OBJECT":
        # Auto-collision for MeshInstance3D nodes
        for obj in export_objs:
            if obj.godot_props.godot_node_type != "MeshInstance3D":
                continue
            if not obj.godot_props.add_collision or obj.type != 'MESH':
                continue
            info = build_collision_info(
                obj,
                obj.godot_props.auto_collision_type,
                obj.godot_props.custom_collision_subtype,
                obj.godot_props.collision_custom_mesh,
            )
            if info:
                obj_collision[obj.name] = info

        # Standalone CollisionShape3D nodes
        for obj in export_objs:
            if obj.godot_props.godot_node_type != "CollisionShape3D":
                continue
            info = build_collision_info(
                obj,
                obj.godot_props.collision_shape,
                obj.godot_props.custom_collision_subtype,
                obj.godot_props.collision_custom_mesh,
            )
            if info:
                standalone_col[obj.name] = info

    else:  # COLLECTION mode — collision built per-collection below

        # Build a lookup: collection_name → GodotCollectionDef
        col_defs = {cd.collection_name: cd for cd in scene_def.collections
                    if cd.collection_name.strip()}

        # Determine primary collection for each object
        def primary_col_for(obj):
            pc = obj.godot_props.primary_collection.strip()
            obj_colls = [c.name for c in bpy.data.collections if obj.name in c.objects]
            if pc and pc in col_defs and pc in obj_colls:
                return pc
            for cname in obj_colls:
                if cname in col_defs:
                    return cname
            return None

        # Warn about objects with no configured collection
        for obj in export_objs:
            if primary_col_for(obj) is None:
                warnings.append(
                    f"'{obj.name}' is not in any configured collection — skipped."
                )

        # Build per-collection combined collision if needed
        col_combined_info = {}   # collection_name → {sid}
        for cname, cd in col_defs.items():
            if cd.collision_mode != "COMBINED":
                continue
            proxy = cd.collision_proxy
            if proxy is None or proxy.type != 'MESH':
                warnings.append(
                    f"Collection '{cname}': Combined mode needs a proxy mesh — skipped collision."
                )
                continue
            info = build_collision_info(
                proxy,
                cd.collision_shape_type,
                "CONVEX",
                None,
            )
            if info:
                col_combined_info[cname] = info

        # Per-object collision in collection mode (PER_OBJECT mode)
        col_obj_collision = {}   # obj.name → {sid}
        for obj in export_objs:
            cname = primary_col_for(obj)
            if cname is None:
                continue
            cd = col_defs.get(cname)
            if cd is None or cd.collision_mode != "PER_OBJECT":
                continue
            if not obj.godot_props.add_collision or obj.type != 'MESH':
                continue
            info = build_collision_info(
                obj,
                obj.godot_props.auto_collision_type,
                obj.godot_props.custom_collision_subtype,
                obj.godot_props.collision_custom_mesh,
            )
            if info:
                col_obj_collision[obj.name] = info

    # ------------------------------------------------------------------
    # Phase 3: Assemble TSCN text
    # ------------------------------------------------------------------
    lines = []

    load_steps = 1 + len(ext_res_list) + len(sub_res_blocks)
    lines.append(f'[gd_scene load_steps={load_steps} format=3]')
    lines.append("")

    for rid, res_path in ext_res_list:
        lines.append(f'[ext_resource type="PackedScene" path="{res_path}" id="{rid}"]')
    if ext_res_list:
        lines.append("")

    for block in sub_res_blocks:
        lines.append(block)

    lines.append(f'[node name="{root_name}" type="{root_type}"]')
    lines.append("")

    def write_mesh_node(obj, parent_path, node_name=None):
        """Write a MeshInstance3D node for obj under parent_path."""
        nname    = node_name or (sanitize(obj.name) + "_Mesh")
        glb_name = sanitize(obj.name) + ".glb"
        rid      = glb_to_res_id.get(glb_name)
        tr       = get_mesh_transform_str(obj)
        if rid:
            lines.append(
                f'[node name="{nname}" parent="{parent_path}" '
                f'instance=ExtResource("{rid}")]'
            )
        else:
            lines.append(f'[node name="{nname}" type="MeshInstance3D" parent="{parent_path}"]')
        lines.append(f"transform = {tr}")
        lines.append("")

    def write_col_node(col_info, node_name, parent_path, col_transform_str):
        if "ext_rid" in col_info:
            lines.append(
                f'[node name="{node_name}" parent="{parent_path}" '
                f'instance=ExtResource("{col_info["ext_rid"]}")]'
            )
            lines.append(f"transform = {col_transform_str}")
            lines.append("")
        else:
            lines.append(f'[node name="{node_name}" type="CollisionShape3D" parent="{parent_path}"]')
            lines.append(f"transform = {col_transform_str}")
            lines.append(f'shape = SubResource("{col_info["sid"]}")')
            lines.append("")

    # ── OBJECT MODE ──────────────────────────────────────────────────────
    if mode == "OBJECT":
        for obj in export_objs:
            ntype      = obj.godot_props.godot_node_type
            nname      = get_node_name(obj)
            glb_mode   = obj.godot_props.glb_export_mode
            body       = obj.godot_props.body_type
            parent_raw = obj.godot_props.parent_node.strip()
            parent_path = sanitize(parent_raw) if parent_raw else "."
            groups     = groups_for_obj(obj)
            grp_line   = write_groups_metadata(groups)

            mesh_tr = get_mesh_transform_str(obj)

            if ntype == "MeshInstance3D" and obj.type == 'MESH' and glb_mode in ("INDIVIDUAL", "GROUP"):
                glb_name = (
                    sanitize(obj.name) + ".glb" if glb_mode == "INDIVIDUAL"
                    else (sanitize(obj.godot_props.glb_group_name) or "mesh_group") + ".glb"
                )
                rid = glb_to_res_id.get(glb_name)

                if body != "NONE":
                    # ── Body wrapper ─────────────────────────────────────────
                    body_name  = nname
                    mesh_nname = nname + "_Mesh"
                    col_nname  = nname + "_Col"
                    body_path  = parent_path + "/" + body_name if parent_path != "." else body_name

                    lines.append(f'[node name="{body_name}" type="{body}" parent="{parent_path}"]')
                    lines.append(f"transform = {IDENTITY_TRANSFORM}")
                    if grp_line:
                        lines.append(grp_line)
                    lines.append("")

                    # Mesh child
                    if rid:
                        lines.append(
                            f'[node name="{mesh_nname}" parent="{body_path}" '
                            f'instance=ExtResource("{rid}")]'
                        )
                    else:
                        lines.append(f'[node name="{mesh_nname}" type="MeshInstance3D" parent="{body_path}"]')
                    lines.append(f"transform = {mesh_tr}")
                    lines.append("")

                    # Collision child
                    if obj.name in obj_collision:
                        ctype  = obj.godot_props.auto_collision_type
                        if ctype in PRIMITIVE_SHAPES:
                            bc = obj_bbox_ctr.get(obj.name)
                            col_tr = centroid_transform3d(bc.x, bc.y, bc.z) if bc else IDENTITY_TRANSFORM
                        else:
                            col_tr = IDENTITY_TRANSFORM
                        write_col_node(obj_collision[obj.name], col_nname, body_path, col_tr)

                else:
                    # ── No body wrapper (classic) ─────────────────────────
                    if rid:
                        lines.append(
                            f'[node name="{nname}" parent="{parent_path}" '
                            f'instance=ExtResource("{rid}")]'
                        )
                    else:
                        lines.append(f'[node name="{nname}" type="MeshInstance3D" parent="{parent_path}"]')
                    lines.append(f"transform = {mesh_tr}")
                    if grp_line:
                        lines.append(grp_line)
                    lines.append("")

                    if obj.name in obj_collision:
                        ctype  = obj.godot_props.auto_collision_type
                        if ctype in PRIMITIVE_SHAPES:
                            bc = obj_bbox_ctr.get(obj.name)
                            col_tr = centroid_transform3d(bc.x, bc.y, bc.z) if bc else IDENTITY_TRANSFORM
                        else:
                            col_tr = IDENTITY_TRANSFORM
                        write_col_node(obj_collision[obj.name], nname + "_Col", parent_path, col_tr)

            elif ntype == "CollisionShape3D":
                if obj.name in standalone_col:
                    info = standalone_col[obj.name]
                    if "ext_rid" in info:
                        lines.append(
                            f'[node name="{nname}" parent="{parent_path}" '
                            f'instance=ExtResource("{info["ext_rid"]}")]'
                        )
                        lines.append(f"transform = {mesh_tr}")
                    else:
                        lines.append(f'[node name="{nname}" type="CollisionShape3D" parent="{parent_path}"]')
                        lines.append(f"transform = {mesh_tr}")
                        lines.append(f'shape = SubResource("{info["sid"]}")')
                else:
                    lines.append(f'[node name="{nname}" type="CollisionShape3D" parent="{parent_path}"]')
                    lines.append(f"transform = {mesh_tr}")
                if grp_line:
                    lines.append(grp_line)
                lines.append("")

            else:
                lines.append(f'[node name="{ntype}" type="{ntype}" parent="{parent_path}"]'
                             .replace(f'[node name="{ntype}"', f'[node name="{nname}"'))
                lines.append(f"transform = {mesh_tr}")
                if grp_line:
                    lines.append(grp_line)
                lines.append("")

    # ── COLLECTION MODE ───────────────────────────────────────────────────
    else:
        col_defs = {cd.collection_name: cd for cd in scene_def.collections
                    if cd.collection_name.strip()}

        def primary_col_for(obj):
            pc       = obj.godot_props.primary_collection.strip()
            obj_cols = [c.name for c in bpy.data.collections if obj.name in c.objects]
            if pc and pc in col_defs and pc in obj_cols:
                return pc
            for cn in obj_cols:
                if cn in col_defs:
                    return cn
            return None

        # Group export objects by their primary collection
        col_buckets = {}   # collection_name → [obj, ...]
        for obj in export_objs:
            cname = primary_col_for(obj)
            if cname:
                col_buckets.setdefault(cname, []).append(obj)

        for cname, objs in col_buckets.items():
            cd         = col_defs[cname]
            body       = cd.body_type
            col_nname  = sanitize(cname) + "_Col"
            body_nname = sanitize(cname)
            all_groups = sorted({g for obj in objs for g in groups_for_obj(obj)})
            grp_line   = write_groups_metadata(all_groups)

            if body != "NONE":
                body_path = body_nname
                lines.append(f'[node name="{body_nname}" type="{body}" parent="."]')
                lines.append(f"transform = {IDENTITY_TRANSFORM}")
                if grp_line:
                    lines.append(grp_line)
                lines.append("")
            else:
                body_path = "."

            # Write each mesh child
            for obj in objs:
                mesh_nname = sanitize(obj.name) + "_Mesh"
                glb_name   = sanitize(obj.name) + ".glb"
                rid        = glb_to_res_id.get(glb_name)
                mesh_tr    = get_mesh_transform_str(obj)

                if rid:
                    lines.append(
                        f'[node name="{mesh_nname}" parent="{body_path}" '
                        f'instance=ExtResource("{rid}")]'
                    )
                else:
                    lines.append(f'[node name="{mesh_nname}" type="MeshInstance3D" parent="{body_path}"]')
                lines.append(f"transform = {mesh_tr}")
                lines.append("")

                # Per-object collision
                if cd.collision_mode == "PER_OBJECT" and obj.name in col_obj_collision:
                    obj_col_nname = sanitize(obj.name) + "_Col"
                    ctype = obj.godot_props.auto_collision_type
                    if ctype in PRIMITIVE_SHAPES:
                        bc = obj_bbox_ctr.get(obj.name)
                        col_tr = centroid_transform3d(bc.x, bc.y, bc.z) if bc else IDENTITY_TRANSFORM
                    else:
                        col_tr = IDENTITY_TRANSFORM
                    write_col_node(col_obj_collision[obj.name], obj_col_nname, body_path, col_tr)

            # Combined collision
            if cd.collision_mode == "COMBINED" and cname in col_combined_info:
                info = col_combined_info[cname]
                if "ext_rid" in info:
                    lines.append(
                        f'[node name="{col_nname}" parent="{body_path}" '
                        f'instance=ExtResource("{info["ext_rid"]}")]'
                    )
                    lines.append(f"transform = {IDENTITY_TRANSFORM}")
                else:
                    lines.append(f'[node name="{col_nname}" type="CollisionShape3D" parent="{body_path}"]')
                    lines.append(f"transform = {IDENTITY_TRANSFORM}")
                    lines.append(f'shape = SubResource("{info["sid"]}")')
                lines.append("")

    # ------------------------------------------------------------------
    # Append any warnings as comments at the end of the file
    # ------------------------------------------------------------------
    if warnings:
        lines.append("# ── Export warnings ──────────────────────────────────")
        for w in warnings:
            lines.append(f"# WARNING: {w}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Batch TSCN builder — one .tscn per object
# ---------------------------------------------------------------------------

def build_single_object_tscn(context, project_root_abs: str, export_dir_abs: str,
                               obj, root_node_type: str, body_type: str,
                               add_collision: bool, collision_type: str) -> str:
    """
    Build a standalone .tscn for a single mesh object.
    The scene contains just this one object (and optional collision).
    """
    sp        = context.scene.godot_scene_props
    apply_tr  = sp.apply_transforms
    depsgraph = context.evaluated_depsgraph_get()

    id_counter     = [1]
    glb_to_res_id  = {}
    ext_res_list   = []
    glb_json_cache = {}
    sub_res_blocks = []
    obj_centroid   = {}   # obj.name -> vertex mean Vector (Godot space)
    obj_bbox_ctr   = {}   # obj.name -> bbox center Vector (Godot space)

    def new_id() -> str:
        v = str(id_counter[0]); id_counter[0] += 1; return v

    def register_mesh_glb(glb_name: str, glb_path: str) -> str:
        if glb_name not in glb_to_res_id:
            rid = new_id()
            glb_to_res_id[glb_name] = rid
            ext_res_list.append((rid, compute_res_path(project_root_abs, export_dir_abs, glb_name)))
        if glb_name not in glb_json_cache:
            glb_json_cache[glb_name] = read_glb_json(glb_path)
        return glb_to_res_id[glb_name]

    # Export the GLB, centering if apply_transforms is on.
    # Exception: if collision is CONVEX, skip centering so mesh_tr stays identity
    # and the world-space hull points align without any offset arithmetic.
    glb_name = sanitize(obj.name) + ".glb"
    glb_path = os.path.join(export_dir_abs, glb_name)
    skip_centering = (add_collision and collision_type == "CONVEX")

    if apply_tr and not skip_centering:
        # Compute vertex mean AND bbox center in one evaluated-mesh pass,
        # before any location shift, so matrix_world is in its original state.
        import bmesh as _bm_exp
        _bm2 = _bm_exp.new()
        _ev  = obj.evaluated_get(depsgraph)
        _bm2.from_mesh(_ev.to_mesh())
        _mw  = obj.matrix_world
        _xs, _ys, _zs = [], [], []
        _sx, _sy, _sz = 0.0, 0.0, 0.0
        _n = len(_bm2.verts)
        for _v in _bm2.verts:
            _wp = _mw @ _v.co
            _gx, _gy, _gz = _wp.x, _wp.z, -_wp.y  # Blender->Godot
            _sx += _gx; _sy += _gy; _sz += _gz
            _xs.append(_gx); _ys.append(_gy); _zs.append(_gz)
        _bm2.free(); _ev.to_mesh_clear()
        if _n:
            _sx /= _n; _sy /= _n; _sz /= _n
        c = mathutils.Vector((_sx, _sy, _sz))
        obj_centroid[obj.name] = c
        if _xs:
            obj_bbox_ctr[obj.name] = mathutils.Vector((
                (max(_xs) + min(_xs)) * 0.5,
                (max(_ys) + min(_ys)) * 0.5,
                (max(_zs) + min(_zs)) * 0.5,
            ))
        centroid_bl = mathutils.Vector((c.x, -c.z, c.y))
        saved_loc   = obj.location.copy()
        obj.location = obj.location - centroid_bl
        export_glb_isolated(context, [obj], glb_path, True)
        obj.location = saved_loc
    else:
        export_glb_isolated(context, [obj], glb_path, apply_tr)

    rid = register_mesh_glb(glb_name, glb_path)

    # Mesh transform
    if apply_tr and not skip_centering:
        c = obj_centroid.get(obj.name)
        mesh_tr = centroid_transform3d(c.x, c.y, c.z) if c else IDENTITY_TRANSFORM
    elif not apply_tr:
        glb_json = glb_json_cache.get(glb_name, {})
        transforms = glb_node_transforms(glb_json)
        if obj.name in transforms:
            mesh_tr = trs_to_transform3d(*transforms[obj.name])
        else:
            san = sanitize(obj.name)
            mesh_tr = IDENTITY_TRANSFORM
            for k, trs in transforms.items():
                if sanitize(k) == san:
                    mesh_tr = trs_to_transform3d(*trs)
                    break
    else:
        # skip_centering + apply_tr: GLB baked at world origin, mesh node at identity
        mesh_tr = IDENTITY_TRANSFORM

    # Build collision shape if requested.
    # For primitive shapes, col_tr is set to the world-space bounding-box
    # center so the shape is placed on the mesh geometry rather than at the
    # object origin.  Convex/concave shapes embed world-space geometry already
    # so identity is correct for those.
    def _bbox_center_tr(mesh_obj) -> str:
        # The mesh node and collision node are siblings under the same parent.
        # Both transforms are in the same coordinate space (parent-relative).
        # mesh_tr = vertex_mean (world-space Godot coords)
        # col_tr  = bbox_center (world-space Godot coords) — no subtraction needed.
        bc = obj_bbox_ctr.get(mesh_obj.name)
        if bc is None:
            return IDENTITY_TRANSFORM
        return centroid_transform3d(bc.x, bc.y, bc.z)

    col_info = None
    col_tr   = IDENTITY_TRANSFORM
    if add_collision and obj.type == 'MESH':
        if collision_type in PRIMITIVE_SHAPES:
            sid = new_id()
            he  = bbox_half_extents(obj, depsgraph)
            block = []
            write_sized_shape(block, sid, collision_type, he)
            sub_res_blocks.append("\n".join(block))
            col_info = {"sid": sid}
            col_tr   = _bbox_center_tr(obj)  # place at bbox center, not origin
        elif collision_type == "CONVEX":
            sid = new_id()
            block = []
            # Hull points are in world/body space. CollisionShape3D stays at identity.
            write_convex_shape(block, sid, obj, depsgraph)
            sub_res_blocks.append("\n".join(block))
            col_info = {"sid": sid}
            # col_tr stays IDENTITY_TRANSFORM
        elif collision_type == "CONCAVE":
            col_glb_name = sanitize(obj.name) + "-colonly.glb"
            col_glb_path = os.path.join(export_dir_abs, col_glb_name)
            ok = export_concave_glb(context, obj, col_glb_path)
            if ok:
                col_res = compute_res_path(project_root_abs, export_dir_abs, col_glb_name)
                write_concave_import_file(col_glb_path, col_res)
                col_rid = new_id()
                glb_to_res_id[col_glb_name] = col_rid
                ext_res_list.append((col_rid, col_res))
                col_info = {"ext_rid": col_rid}
                # GLB already carries geometry in world space -- identity is correct

    # Assemble TSCN
    lines = []
    load_steps = 1 + len(ext_res_list) + len(sub_res_blocks)
    lines.append(f'[gd_scene load_steps={load_steps} format=3]')
    lines.append("")

    for r_id, res_path in ext_res_list:
        lines.append(f'[ext_resource type="PackedScene" path="{res_path}" id="{r_id}"]')
    if ext_res_list:
        lines.append("")

    for block in sub_res_blocks:
        lines.append(block)

    root_name = sanitize(obj.name)
    lines.append(f'[node name="{root_name}" type="{root_node_type}"]')
    lines.append("")

    mesh_nname = root_name + "_Mesh"
    col_nname  = root_name + "_Col"

    if body_type != "NONE":
        # Body wrapper → mesh child → collision child
        body_path = root_name + "/" + body_type + "Body" if root_node_type != body_type else "."
        # If root IS the body, write mesh directly under root
        if root_node_type == body_type:
            body_path = "."
        else:
            body_nname = root_name + "_Body"
            lines.append(f'[node name="{body_nname}" type="{body_type}" parent="."]')
            lines.append(f"transform = {IDENTITY_TRANSFORM}")
            lines.append("")
            body_path = body_nname

        if rid:
            lines.append(f'[node name="{mesh_nname}" parent="{body_path}" instance=ExtResource("{rid}")]')
        else:
            lines.append(f'[node name="{mesh_nname}" type="MeshInstance3D" parent="{body_path}"]')
        lines.append(f"transform = {mesh_tr}")
        lines.append("")

        if col_info:
            if "ext_rid" in col_info:
                lines.append(f'[node name="{col_nname}" parent="{body_path}" instance=ExtResource("{col_info["ext_rid"]}")]')
                lines.append(f"transform = {col_tr}")
            else:
                lines.append(f'[node name="{col_nname}" type="CollisionShape3D" parent="{body_path}"]')
                lines.append(f"transform = {col_tr}")
                lines.append(f'shape = SubResource("{col_info["sid"]}")')
            lines.append("")

    else:
        # No body — mesh directly under root
        if rid:
            lines.append(f'[node name="{mesh_nname}" parent="." instance=ExtResource("{rid}")]')
        else:
            lines.append(f'[node name="{mesh_nname}" type="MeshInstance3D" parent="."]')
        lines.append(f"transform = {mesh_tr}")
        lines.append("")

        if col_info:
            if "ext_rid" in col_info:
                lines.append(f'[node name="{col_nname}" parent="." instance=ExtResource("{col_info["ext_rid"]}")]')
                lines.append(f"transform = {col_tr}")
            else:
                lines.append(f'[node name="{col_nname}" type="CollisionShape3D" parent="."]')
                lines.append(f"transform = {col_tr}")
                lines.append(f'shape = SubResource("{col_info["sid"]}")')
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class GODOT_OT_ApplyToSelected(Operator):
    bl_idname  = "godot_bridge.apply_to_selected"
    bl_label   = "Apply Settings to All Selected"
    bl_description = (
        "Copy the active object's Godot Bridge settings to all other selected objects. "
        "Node Name and Parent Node are not copied (they must be unique per object)."
    )

    def execute(self, context):
        active = context.active_object
        if not active:
            self.report({'WARNING'}, "No active object.")
            return {'CANCELLED'}
        src     = active.godot_props
        targets = [o for o in context.selected_objects if o != active]
        if not targets:
            self.report({'WARNING'}, "No other objects selected.")
            return {'CANCELLED'}
        for obj in targets:
            dst = obj.godot_props
            dst.export                   = src.export
            dst.godot_node_type          = src.godot_node_type
            dst.glb_export_mode          = src.glb_export_mode
            dst.glb_group_name           = src.glb_group_name
            dst.add_collision            = src.add_collision
            dst.auto_collision_type      = src.auto_collision_type
            dst.custom_collision_subtype = src.custom_collision_subtype
            dst.collision_shape          = src.collision_shape
        self.report({'INFO'}, f"Applied to {len(targets)} object(s).")
        return {'FINISHED'}


class GODOT_OT_DetectProjectRoot(Operator):
    bl_idname  = "godot_bridge.detect_project_root"
    bl_label   = "Auto-detect Root from Export Folder"
    bl_description = "Walk up from the Export Folder looking for project.godot"

    def execute(self, context):
        sp = context.scene.godot_scene_props
        if not sp.export_path.strip():
            self.report({'WARNING'}, "Set Export Folder first.")
            return {'CANCELLED'}
        path = abs_dir(sp.export_path)
        for _ in range(20):
            if os.path.isfile(os.path.join(path, "project.godot")):
                sp.project_root = path + os.sep
                self.report({'INFO'}, f"Found: {path}")
                return {'FINISHED'}
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
        self.report({'WARNING'}, "Could not find project.godot. Set manually.")
        return {'CANCELLED'}


class GODOT_OT_ExportScene(Operator):
    bl_idname  = "godot_bridge.export_scene"
    bl_label   = "Export All Scenes"
    bl_description = "Export all scene definitions to .tscn + GLB files"

    def execute(self, context):
        sp  = context.scene.godot_scene_props
        err = validate_paths(context)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        project_root_abs = abs_dir(sp.project_root)
        export_dir_abs   = abs_dir(sp.export_path)
        os.makedirs(export_dir_abs, exist_ok=True)

        exported = []
        errors   = []
        for scene_def in sp.scenes:
            sname = scene_def.scene_name.strip() or "scene"
            try:
                tscn_content = build_tscn(context, project_root_abs,
                                          export_dir_abs, scene_def)
            except Exception as e:
                errors.append(f"{sname}: {e}")
                import traceback; traceback.print_exc()
                continue
            tscn_path = os.path.join(export_dir_abs, sname + ".tscn")
            with open(tscn_path, 'w', encoding='utf-8') as f:
                f.write(tscn_content)
            exported.append(sname)

        if exported:
            self.report({'INFO'}, f"Exported: {', '.join(exported)}")
        if errors:
            self.report({'ERROR'}, "Errors: " + "; ".join(errors))
        return {'FINISHED'} if exported else {'CANCELLED'}


class GODOT_OT_ExportActiveScene(Operator):
    bl_idname  = "godot_bridge.export_active_scene"
    bl_label   = "Export This Scene"
    bl_description = "Export only the currently selected scene definition"

    def execute(self, context):
        sp  = context.scene.godot_scene_props
        err = validate_paths(context)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        if not sp.scenes or sp.active_scene_index >= len(sp.scenes):
            self.report({'ERROR'}, "No scene selected.")
            return {'CANCELLED'}
        scene_def        = sp.scenes[sp.active_scene_index]
        project_root_abs = abs_dir(sp.project_root)
        export_dir_abs   = abs_dir(sp.export_path)
        os.makedirs(export_dir_abs, exist_ok=True)
        sname = scene_def.scene_name.strip() or "scene"
        try:
            tscn_content = build_tscn(context, project_root_abs,
                                      export_dir_abs, scene_def)
        except Exception as e:
            self.report({'ERROR'}, f"Export failed: {e}")
            import traceback; traceback.print_exc()
            return {'CANCELLED'}
        tscn_path = os.path.join(export_dir_abs, sname + ".tscn")
        with open(tscn_path, 'w', encoding='utf-8') as f:
            f.write(tscn_content)
        self.report({'INFO'}, f"Exported: {tscn_path}")
        return {'FINISHED'}


class GODOT_OT_AddScene(Operator):
    bl_idname  = "godot_bridge.add_scene"
    bl_label   = "Add Scene"
    bl_description = "Add a new scene definition"

    def execute(self, context):
        sp  = context.scene.godot_scene_props
        new = sp.scenes.add()
        # Auto-name: scene, scene_1, scene_2, ...
        existing = {s.scene_name for s in sp.scenes}
        base = "scene"
        name = base
        i = 1
        while name in existing:
            name = f"{base}_{i}"; i += 1
        new.scene_name = name
        sp.active_scene_index = len(sp.scenes) - 1
        return {'FINISHED'}


class GODOT_OT_RemoveScene(Operator):
    bl_idname  = "godot_bridge.remove_scene"
    bl_label   = "Remove Scene"
    bl_description = "Remove the selected scene definition"

    def execute(self, context):
        sp = context.scene.godot_scene_props
        if not sp.scenes:
            return {'CANCELLED'}
        sp.scenes.remove(sp.active_scene_index)
        sp.active_scene_index = max(0, sp.active_scene_index - 1)
        return {'FINISHED'}


class GODOT_OT_AssignToScene(Operator):
    """Add or remove the active scene name from selected objects' memberships."""
    bl_idname  = "godot_bridge.assign_to_scene"
    bl_label   = "Assign/Remove Selected from Scene"

    assign: BoolProperty(default=True)

    def execute(self, context):
        sp = context.scene.godot_scene_props
        if not sp.scenes or sp.active_scene_index >= len(sp.scenes):
            self.report({'WARNING'}, "No scene selected.")
            return {'CANCELLED'}
        sname = sp.scenes[sp.active_scene_index].scene_name.strip()
        if not sname:
            return {'CANCELLED'}
        for obj in context.selected_objects:
            p = obj.godot_props
            members = [m.strip() for m in p.scene_memberships.split(",") if m.strip()]
            if self.assign:
                if sname not in members:
                    members.append(sname)
                p.export = True
            else:
                members = [m for m in members if m != sname]
            p.scene_memberships = ", ".join(members)
        return {'FINISHED'}


class GODOT_OT_MarkSelected(Operator):
    bl_idname = "godot_bridge.mark_selected"
    bl_label  = "Mark/Unmark Selected"
    mark: BoolProperty(default=True)

    def execute(self, context):
        for obj in context.selected_objects:
            obj.godot_props.export = self.mark
        return {'FINISHED'}


class GODOT_OT_AddCollection(Operator):
    bl_idname  = "godot_bridge.add_collection"
    bl_label   = "Add Collection"
    bl_description = "Add a collection to this scene's Collection Mode list"

    def execute(self, context):
        sp = context.scene.godot_scene_props
        if not sp.scenes or sp.active_scene_index >= len(sp.scenes):
            return {'CANCELLED'}
        sd  = sp.scenes[sp.active_scene_index]
        new = sd.collections.add()
        new.collection_name = ""
        sd.active_collection_index = len(sd.collections) - 1
        return {'FINISHED'}


class GODOT_OT_RemoveCollection(Operator):
    bl_idname  = "godot_bridge.remove_collection"
    bl_label   = "Remove Collection"
    bl_description = "Remove the selected collection from this scene's list"

    def execute(self, context):
        sp = context.scene.godot_scene_props
        if not sp.scenes or sp.active_scene_index >= len(sp.scenes):
            return {'CANCELLED'}
        sd = sp.scenes[sp.active_scene_index]
        if not sd.collections:
            return {'CANCELLED'}
        sd.collections.remove(sd.active_collection_index)
        sd.active_collection_index = max(0, sd.active_collection_index - 1)
        return {'FINISHED'}


class GODOT_OT_PickCollection(Operator):
    """Apply the dropdown selection to collection_name."""
    bl_idname  = "godot_bridge.pick_collection"
    bl_label   = "Apply Collection Pick"
    bl_description = "Set collection name from the dropdown selection"

    scene_index:      IntProperty(default=0)
    collection_index: IntProperty(default=0)

    def execute(self, context):
        sp = context.scene.godot_scene_props
        if self.scene_index >= len(sp.scenes):
            return {'CANCELLED'}
        sd = sp.scenes[self.scene_index]
        if self.collection_index >= len(sd.collections):
            return {'CANCELLED'}
        cd = sd.collections[self.collection_index]
        picked = cd.collection_picker
        if picked and picked in bpy.data.collections:
            cd.collection_name = picked
        return {'FINISHED'}


class GODOT_OT_AddCollisionProxy(Operator):
    bl_idname  = "godot_bridge.add_collision_proxy"
    bl_label   = "Add Collision Proxy Mesh"
    bl_description = (
        "Create a box mesh scaled to this collection's bounding box and "
        "assign it as the combined collision proxy"
    )

    collection_name: StringProperty(default="")

    def execute(self, context):
        sp = context.scene.godot_scene_props
        if not sp.scenes or sp.active_scene_index >= len(sp.scenes):
            return {'CANCELLED'}
        sd    = sp.scenes[sp.active_scene_index]
        cname = self.collection_name.strip()
        if not cname:
            self.report({'WARNING'}, "No collection name set.")
            return {'CANCELLED'}

        bl_col = bpy.data.collections.get(cname)
        if bl_col is None:
            self.report({'WARNING'}, f"Collection '{cname}' not found.")
            return {'CANCELLED'}

        cd = next((c for c in sd.collections if c.collection_name == cname), None)
        if cd is None:
            self.report({'WARNING'}, "Collection def not found.")
            return {'CANCELLED'}

        # Compute bounding box of all mesh objects in the collection
        mesh_objs = [o for o in bl_col.objects if o.type == 'MESH'
                     and not o.godot_props.is_collision_proxy]
        if not mesh_objs:
            self.report({'WARNING'}, "No mesh objects in this collection.")
            return {'CANCELLED'}

        import bmesh as _bm
        all_pts = []
        for obj in mesh_objs:
            mw = obj.matrix_world
            for corner in obj.bound_box:
                all_pts.append(mw @ mathutils.Vector(corner))

        xs = [p.x for p in all_pts]
        ys = [p.y for p in all_pts]
        zs = [p.z for p in all_pts]
        cx = (max(xs) + min(xs)) * 0.5
        cy = (max(ys) + min(ys)) * 0.5
        cz = (max(zs) + min(zs)) * 0.5
        sx = (max(xs) - min(xs)) * 0.5
        sy = (max(ys) - min(ys)) * 0.5
        sz = (max(zs) - min(zs)) * 0.5

        proxy_name = f"_proxy_{sanitize(cname)}"
        # Remove existing proxy with same name
        if proxy_name in bpy.data.objects:
            bpy.data.objects.remove(bpy.data.objects[proxy_name], do_unlink=True)

        mesh_data  = bpy.data.meshes.new(proxy_name)
        proxy_obj  = bpy.data.objects.new(proxy_name, mesh_data)

        bm = _bm.new()
        _bm.ops.create_cube(bm, size=2.0)
        bm.to_mesh(mesh_data)
        bm.free()

        proxy_obj.location = (cx, cy, cz)
        proxy_obj.scale    = (sx, sy, sz)
        proxy_obj.display_type = 'WIRE'
        proxy_obj.godot_props.is_collision_proxy = True

        # Link to collection
        bl_col.objects.link(proxy_obj)

        # Also link to scene so it's visible
        if proxy_obj.name not in context.scene.collection.objects:
            context.scene.collection.objects.link(proxy_obj)

        # Assign as the collision proxy for this collection def
        cd.collision_proxy = proxy_obj

        self.report({'INFO'}, f"Created proxy: {proxy_name}")
        return {'FINISHED'}


class GODOT_OT_SelectSceneObjects(Operator):
    bl_idname  = "godot_bridge.select_scene_objects"
    bl_label   = "Select Scene Objects"
    bl_description = "Select all objects assigned to the active scene in the viewport"

    def execute(self, context):
        sp = context.scene.godot_scene_props
        if not sp.scenes or sp.active_scene_index >= len(sp.scenes):
            self.report({'WARNING'}, "No scene selected.")
            return {'CANCELLED'}
        sname = sp.scenes[sp.active_scene_index].scene_name.strip()
        bpy.ops.object.select_all(action='DESELECT')
        count = 0
        for obj in context.scene.objects:
            if not obj.godot_props.export:
                continue
            memberships = obj.godot_props.scene_memberships.strip()
            if not memberships or sname in [m.strip() for m in memberships.split(",")]:
                obj.select_set(True)
                count += 1
        self.report({'INFO'}, f"Selected {count} object(s) in scene '{sname}'.")
        return {'FINISHED'}


class GODOT_OT_BatchExportObjects(Operator):
    bl_idname  = "godot_bridge.batch_export_objects"
    bl_label   = "Batch Export Objects"
    bl_description = (
        "Export each marked mesh object as its own individual .tscn + GLB file. "
        "Uses the Root Node, Body, and Collision settings in the Batch Export panel."
    )

    def execute(self, context):
        sp  = context.scene.godot_scene_props
        err = validate_paths_no_scenes(context)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}

        project_root_abs = abs_dir(sp.project_root)
        export_dir_abs   = abs_dir(sp.export_path)
        os.makedirs(export_dir_abs, exist_ok=True)

        # Collect objects to export
        if sp.batch_only_selected:
            candidates = [o for o in context.selected_objects if o.type == 'MESH']
        else:
            candidates = [
                o for o in context.scene.objects
                if o.type == 'MESH'
                and o.godot_props.export
                and not o.godot_props.is_collision_proxy
                and o.godot_props.godot_node_type != "NONE"
            ]

        if not candidates:
            self.report({'WARNING'}, "No mesh objects found to batch export.")
            return {'CANCELLED'}

        exported = []
        errors   = []

        for obj in candidates:
            try:
                tscn_content = build_single_object_tscn(
                    context,
                    project_root_abs,
                    export_dir_abs,
                    obj,
                    root_node_type = sp.batch_root_node_type,
                    body_type      = sp.batch_body_type,
                    add_collision  = sp.batch_add_collision,
                    collision_type = sp.batch_collision_type,
                )
            except Exception as e:
                errors.append(f"{obj.name}: {e}")
                import traceback; traceback.print_exc()
                continue

            tscn_path = os.path.join(export_dir_abs, sanitize(obj.name) + ".tscn")
            with open(tscn_path, 'w', encoding='utf-8') as f:
                f.write(tscn_content)
            exported.append(obj.name)

        if exported:
            self.report({'INFO'}, f"Batch exported {len(exported)} object(s): {', '.join(exported)}")
        if errors:
            self.report({'ERROR'}, "Errors: " + "; ".join(errors))
        return {'FINISHED'} if exported else {'CANCELLED'}


# ---------------------------------------------------------------------------
# UI Panels
# ---------------------------------------------------------------------------

class GODOT_PT_ObjectPanel(Panel):
    bl_label       = "Godot Object Settings"
    bl_idname      = "GODOT_PT_object_panel"
    bl_space_space  = 'VIEW_3D'
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "Godot Bridge"

    def draw(self, context):
        layout = self.layout
        obj    = context.active_object

        if obj is None:
            layout.label(text="No active object", icon='INFO')
            return

        props = obj.godot_props
        box   = layout.box()
        box.label(text=obj.name, icon='OBJECT_DATA')
        box.prop(props, "export", text="Export this object")

        if not props.export:
            return

        # Scene membership summary
        sp = context.scene.godot_scene_props
        if sp.scenes:
            member_box = layout.box()
            member_box.label(text="Scene Memberships", icon='SCENE_DATA')
            memberships = [m.strip() for m in props.scene_memberships.split(",") if m.strip()]
            for scene_def in sp.scenes:
                sname = scene_def.scene_name.strip()
                row = member_box.row()
                is_member = not props.scene_memberships.strip() or sname in memberships
                row.label(
                    text=sname,
                    icon='CHECKMARK' if is_member else 'BLANK1',
                )
            if not props.scene_memberships.strip():
                member_box.label(text="(all scenes — no filter set)", icon='INFO')

        box.prop(props, "godot_node_type")
        box.prop(props, "node_name",   text="Override Name")
        box.prop(props, "parent_node", text="Parent Node")

        # --- MeshInstance3D ---
        if obj.type == 'MESH' and props.godot_node_type == "MeshInstance3D":
            mesh_box = layout.box()
            mesh_box.label(text="Mesh / GLB Export", icon='MESH_DATA')
            mesh_box.prop(props, "glb_export_mode")
            if props.glb_export_mode == "GROUP":
                mesh_box.prop(props, "glb_group_name")

            # Body type (Object Mode)
            body_box = layout.box()
            body_box.label(text="Physics Body (Object Mode)", icon='PHYSICS')
            body_box.prop(props, "body_type")

            col_box = layout.box()
            col_box.prop(props, "add_collision", text="Add Collision Shape")
            if props.add_collision:
                self._draw_collision(col_box, props, obj, "auto_collision_type")

        # --- Standalone CollisionShape3D ---
        elif props.godot_node_type == "CollisionShape3D":
            col_box = layout.box()
            col_box.label(text="Collision Shape", icon='MESH_ICOSPHERE')
            self._draw_collision(col_box, props, obj, "collision_shape")

        # Collection Mode settings
        coll_box = layout.box()
        coll_box.label(text="Collection Mode", icon='OUTLINER_COLLECTION')
        coll_box.prop(props, "primary_collection", text="Primary Collection")
        if props.is_collision_proxy:
            coll_box.label(text="⚠ This is a collision proxy mesh", icon='ERROR')

        # --- Bulk actions ---
        layout.separator()
        sel_count = len(context.selected_objects)
        row = layout.row(align=True)
        op  = row.operator("godot_bridge.mark_selected", text="Mark Selected", icon='CHECKMARK')
        op.mark = True
        op2 = row.operator("godot_bridge.mark_selected", text="Unmark", icon='X')
        op2.mark = False

        if sel_count > 1:
            layout.operator(
                "godot_bridge.apply_to_selected",
                text=f"Apply Settings to {sel_count - 1} Other Selected",
                icon='COPYDOWN',
            )
        else:
            r = layout.row()
            r.enabled = False
            r.label(text="Select multiple objects to bulk-apply", icon='INFO')

    def _draw_collision(self, layout, props, obj, prop_name):
        """Draw the collision shape selector and context hints."""
        layout.prop(props, prop_name, text="Shape")
        ctype = getattr(props, prop_name)

        layout.prop(props, "collision_custom_mesh", text="Custom Mesh (optional)")
        has_custom = props.collision_custom_mesh is not None

        if ctype in PRIMITIVE_SHAPES:
            if has_custom:
                layout.label(text="Primitive ignores custom mesh — uses bbox.", icon='INFO')
            elif obj.type == 'MESH':
                layout.label(text="Auto-sized from bounding box.", icon='INFO')
            else:
                layout.label(text="No mesh — 0.5 m default size.", icon='ERROR')

        elif ctype == "CONVEX":
            if has_custom:
                layout.label(text=f"Source: {props.collision_custom_mesh.name}", icon='INFO')
            elif obj.type != 'MESH':
                layout.label(text="Object must be a mesh for Convex.", icon='ERROR')
            else:
                layout.label(text="ConvexPolygonShape3D from this mesh.", icon='INFO')

        elif ctype == "CONCAVE":
            if has_custom:
                layout.label(text=f"Source: {props.collision_custom_mesh.name}", icon='INFO')
            elif obj.type != 'MESH':
                layout.label(text="Object must be a mesh for Concave.", icon='ERROR')
            else:
                layout.label(text="ConcavePolygonShape3D — static bodies only!", icon='INFO')

        elif ctype == "CUSTOM":
            layout.prop(props, "custom_collision_subtype", text="Shape Type")
            if not props.collision_custom_mesh:
                layout.label(text="No mesh selected — will use this object.", icon='ERROR')
            else:
                layout.label(
                    text=f"Source: {props.collision_custom_mesh.name}",
                    icon='INFO',
                )


class GODOT_UL_SceneList(bpy.types.UIList):
    bl_idname = "GODOT_UL_scene_list"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "scene_name", text="", emboss=False, icon='SCENE')
            row.prop(item, "root_node_type", text="", emboss=False)
        elif self.layout_type == 'GRID':
            layout.label(text=item.scene_name, icon='SCENE')


class GODOT_PT_ScenePanel(Panel):
    bl_label       = "Godot Scene Export"
    bl_idname      = "GODOT_PT_scene_panel"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "Godot Bridge"
    bl_order       = 1

    def draw(self, context):
        layout = self.layout
        sp     = context.scene.godot_scene_props

        # ── Project paths ───────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Godot Project", icon='FILE_FOLDER')
        box.prop(sp, "project_root", text="Project Root")
        box.prop(sp, "export_path",  text="Export Folder")
        box.operator("godot_bridge.detect_project_root",
                     text="Auto-detect Root from Export Folder", icon='VIEWZOOM')
        if sp.export_path.strip() and sp.project_root.strip():
            try:
                sample = compute_res_path(
                    abs_dir(sp.project_root), abs_dir(sp.export_path), "example.glb"
                )
                box.label(text=f"→ {sample}", icon='INFO')
            except Exception:
                pass

        box.prop(sp, "apply_transforms")

        # ── Scene list ──────────────────────────────────────────────────────
        layout.separator()
        layout.label(text="Scene Definitions", icon='SCENE_DATA')

        row = layout.row()
        row.template_list(
            "GODOT_UL_scene_list", "",
            sp, "scenes",
            sp, "active_scene_index",
            rows=3,
        )
        col = row.column(align=True)
        col.operator("godot_bridge.add_scene",    text="", icon='ADD')
        col.operator("godot_bridge.remove_scene", text="", icon='REMOVE')

        # ── Active scene settings ────────────────────────────────────────────
        if sp.scenes and sp.active_scene_index < len(sp.scenes):
            active = sp.scenes[sp.active_scene_index]
            sbox = layout.box()
            sbox.label(text=f"Scene: {active.scene_name}", icon='SCENE')
            sbox.prop(active, "scene_name",    text="Filename")
            sbox.prop(active, "root_node_type")
            sbox.prop(active, "root_node_name")
            sbox.prop(active, "export_mode",   text="Export Mode")

            # Object assignment shortcuts
            sbox.separator()
            sbox.label(text="Selected Objects:", icon='OBJECT_DATA')
            row2 = sbox.row(align=True)
            op  = row2.operator("godot_bridge.assign_to_scene",
                                text="Add to Scene", icon='CHECKMARK')
            op.assign = True
            op2 = row2.operator("godot_bridge.assign_to_scene",
                                text="Remove from Scene", icon='X')
            op2.assign = False
            sbox.operator("godot_bridge.select_scene_objects",
                          text="Select Scene Objects in Viewport", icon='RESTRICT_SELECT_OFF')

            # Count objects assigned to this scene
            sname = active.scene_name.strip()
            count = 0
            for o in context.scene.objects:
                if not o.godot_props.export:
                    continue
                if o.godot_props.godot_node_type == "NONE":
                    continue
                memberships = o.godot_props.scene_memberships.strip()
                if not memberships or sname in [m.strip() for m in memberships.split(",")]:
                    count += 1
            sbox.label(text=f"{count} object(s) in this scene", icon='INFO')

            # ── Collection Mode settings ──────────────────────────────────
            if active.export_mode == "COLLECTION":
                layout.separator()
                layout.label(text="Collection Definitions", icon='OUTLINER_COLLECTION')

                scene_idx = sp.active_scene_index

                # List all configured collections
                for i, cd in enumerate(active.collections):
                    cbox = layout.box()
                    header = cbox.row()
                    header.label(
                        text=cd.collection_name if cd.collection_name else "(none selected)",
                        icon='COLLECTION_COLOR_01',
                    )
                    rem = header.operator("godot_bridge.remove_collection",
                                         text="", icon='X')

                    # ── Collection picker dropdown ────────────────────────
                    pick_row = cbox.row(align=True)
                    pick_row.prop(cd, "collection_picker", text="Collection")
                    op_pick = pick_row.operator("godot_bridge.pick_collection",
                                               text="", icon='CHECKMARK')
                    op_pick.scene_index      = scene_idx
                    op_pick.collection_index = i

                    if cd.collection_name:
                        cbox.label(text=f"Active: {cd.collection_name}", icon='INFO')
                    else:
                        cbox.label(text="Select a collection above and click ✓", icon='ERROR')

                    cbox.prop(cd, "body_type")
                    cbox.prop(cd, "collision_mode")

                    if cd.collision_mode == "COMBINED":
                        cbox.prop(cd, "collision_shape_type", text="Shape")
                        cbox.prop(cd, "collision_proxy", text="Proxy Mesh")
                        if cd.collision_proxy is None:
                            cbox.label(text="⚠ No proxy — collision will be skipped!", icon='ERROR')
                        else:
                            cbox.label(
                                text=f"Proxy: {cd.collision_proxy.name}  |  Shape: {cd.collision_shape_type}",
                                icon='INFO',
                            )
                        op3 = cbox.operator("godot_bridge.add_collision_proxy",
                                            text="Generate Proxy from Bounding Box",
                                            icon='MESH_CUBE')
                        op3.collection_name = cd.collection_name

                layout.operator("godot_bridge.add_collection",
                                text="Add Collection", icon='ADD')

        # ── Export buttons ───────────────────────────────────────────────────
        layout.separator()
        err = validate_paths(context)
        if err:
            for line in err.split("\n"):
                layout.label(text=line, icon='ERROR')

        row = layout.row(align=True)
        row.enabled = not bool(err)
        row.operator("godot_bridge.export_active_scene",
                     text="Export This Scene", icon='SCENE')
        row.operator("godot_bridge.export_scene",
                     text="Export All", icon='FILE_TICK')


class GODOT_PT_BatchExportPanel(Panel):
    """Panel for one-click batch export of all objects as individual .tscn files."""
    bl_label       = "Batch Object Export"
    bl_idname      = "GODOT_PT_batch_export_panel"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "Godot Bridge"
    bl_order       = 2

    def draw(self, context):
        layout = self.layout
        sp     = context.scene.godot_scene_props

        layout.label(text="Export Each Object as its Own .tscn", icon='OBJECT_DATA')

        box = layout.box()
        box.label(text="Scene Settings", icon='SCENE_DATA')
        box.prop(sp, "batch_root_node_type", text="Root Node")
        box.prop(sp, "batch_body_type",      text="Body Type")

        col_box = layout.box()
        col_box.label(text="Collision", icon='MESH_ICOSPHERE')
        col_box.prop(sp, "batch_add_collision", text="Add Collision Shape")
        if sp.batch_add_collision:
            col_box.prop(sp, "batch_collision_type", text="Shape Type")

        layout.prop(sp, "batch_only_selected", text="Selected Objects Only")

        # Count candidates
        if sp.batch_only_selected:
            n = sum(1 for o in context.selected_objects if o.type == 'MESH')
            layout.label(text=f"{n} selected mesh object(s) will be exported", icon='INFO')
        else:
            n = sum(
                1 for o in context.scene.objects
                if o.type == 'MESH'
                and o.godot_props.export
                and not o.godot_props.is_collision_proxy
                and o.godot_props.godot_node_type != "NONE"
            )
            layout.label(text=f"{n} marked mesh object(s) will be exported", icon='INFO')

        layout.separator()
        err = validate_paths_no_scenes(context)
        if err:
            for line in err.split("\n"):
                layout.label(text=line, icon='ERROR')

        row = layout.row()
        row.enabled = not bool(err) and n > 0
        row.scale_y = 1.4
        row.operator("godot_bridge.batch_export_objects",
                     text=f"Batch Export {n} Object(s)", icon='EXPORT')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

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
