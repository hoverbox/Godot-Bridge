"""
export_tscn.py — TSCN scene assembly for Godot Bridge.

TRANSFORM STRATEGY
------------------
Mesh GLBs:
  apply_transforms=ON  → geometry baked at world position, node gets identity.
  apply_transforms=OFF → geometry at local origin, node gets world transform
                         read back from the GLB JSON.

CollisionShape3D siblings:
  Primitives: placed at the mesh bbox center (centroid_transform3d of bbox midpoint).
  Convex hull: placed at the vertex mean centroid (hull points are centroid-relative).
  Concave GLB: identity (GLB carries world-space geometry).

COLLISION ARCHITECTURE
----------------------
ALL collision shapes are written as inline sub_resource blocks in the TSCN.
No separate GLB files, no manual import steps — opens and works immediately.

Primitive (Box/Sphere/Capsule/Cylinder):
  Sized from the mesh bounding box.

Convex Hull:
  ConvexPolygonShape3D — points array contains all mesh vertices in Godot
  world space. Godot computes the actual convex hull at load time.

Concave / Trimesh:
  ConcavePolygonShape3D — faces array contains all triangulated faces in
  Godot world space. Exact mesh geometry. Static bodies only.

Custom Mesh:
  Same as Convex or Concave (user's choice), but the source mesh is the
  picked custom_obj instead of the host object. Use this to assign a
  separate low-poly collision mesh to a high-poly visible mesh.

WHY COLLISION SHAPE IS A SIBLING, NOT A CHILD
-----------------------------------------------
MeshInstance3D nodes are written as instanced PackedScenes. Child nodes
cannot be added to instanced scenes in plain .tscn text — they would
end up inside the sub-scene. The CollisionShape3D must be a sibling
(same parent) so physics bodies can see both the mesh and the shape.
"""

import os
import bpy
import mathutils

from .utils import (
    sanitize, get_node_name, fmtf, compute_res_path,
    trs_to_transform3d, centroid_transform3d,
    IDENTITY_TRANSFORM, PRIMITIVE_SHAPES, MESH_SHAPES,
)
from .export_glb import (
    read_glb_json, glb_node_transforms,
    export_glb_isolated, export_concave_glb, write_concave_import_file,
)
from .collision import (
    bbox_half_extents,
    write_sized_shape, write_convex_shape,
)


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

            # col_tr: CollisionShape3D node transform.
            # Hull points are in world/body space, so collision node stays at identity.
            # Only primitives need placement at bbox center.
            def _col_tr_for(o, ctype):
                if ctype in PRIMITIVE_SHAPES:
                    bc = obj_bbox_ctr.get(o.name)
                    if bc is not None:
                        return centroid_transform3d(bc.x, bc.y, bc.z)
                return IDENTITY_TRANSFORM

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
                        ctype = obj.godot_props.auto_collision_type
                        write_col_node(obj_collision[obj.name], col_nname, body_path,
                                       _col_tr_for(obj, ctype))

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
                        ctype = obj.godot_props.auto_collision_type
                        write_col_node(obj_collision[obj.name], nname + "_Col", parent_path,
                                       _col_tr_for(obj, ctype))

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
    import bmesh as _bm_exp

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
        # Primitives: CollisionShape3D sits at the bbox center.
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
