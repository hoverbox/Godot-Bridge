"""
collision.py — Collision shape sub-resource writers and mesh helpers for Godot Bridge.
"""

import bpy
import mathutils

from .utils import fmtf


# ---------------------------------------------------------------------------
# Mesh analysis helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Collision shape sub-resource writers
# ---------------------------------------------------------------------------

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
