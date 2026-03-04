"""
utils.py — Enums, constants, and shared helper functions for Godot Bridge.
"""

import os
import re
import math
import mathutils
import bpy


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
    ("NONE",            "No Body",           "No physics body wrapper — mesh exports as plain MeshInstance3D"),
    ("StaticBody3D",    "StaticBody3D",      "Wrap in a StaticBody3D parent"),
    ("RigidBody3D",     "RigidBody3D",       "Wrap in a RigidBody3D parent"),
    ("Area3D",          "Area3D",            "Wrap in an Area3D parent"),
    ("AnimatableBody3D","AnimatableBody3D",  "Wrap in an AnimatableBody3D parent"),
]

COLLECTION_COLLISION_MODES = [
    ("PER_OBJECT", "Per Object",     "Each object generates its own CollisionShape3D child"),
    ("COMBINED",   "Combined Proxy", "One CollisionShape3D from the assigned proxy mesh"),
]


# ---------------------------------------------------------------------------
# Transform constants
# ---------------------------------------------------------------------------

IDENTITY_TRANSFORM = "Transform3D(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)"


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
# String helpers
# ---------------------------------------------------------------------------

def sanitize(name: str) -> str:
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if name and name[0].isdigit():
        name = "_" + name
    return name or "Node"


def get_node_name(obj) -> str:
    override = obj.godot_props.node_name.strip()
    return sanitize(override if override else obj.name)


# ---------------------------------------------------------------------------
# Float formatting
# ---------------------------------------------------------------------------

def fmtf(v: float) -> str:
    """
    Format a float for Godot TSCN.  Must be a plain decimal — Godot's parser
    rejects scientific notation (1e-7), nan, and inf.
    We use fixed-point with 7 significant digits of precision.
    """
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


# ---------------------------------------------------------------------------
# Transform helpers
# ---------------------------------------------------------------------------

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


def centroid_transform3d(gx: float, gy: float, gz: float) -> str:
    """Identity rotation/scale, centroid as translation."""
    f = fmtf
    return (f"Transform3D(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, "
            f"{f(gx)}, {f(gy)}, {f(gz)})")
