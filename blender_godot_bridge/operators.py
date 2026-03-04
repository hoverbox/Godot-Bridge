"""
operators.py — Blender operator classes for Godot Bridge.
"""

import os
import bpy
import mathutils
from bpy.props import BoolProperty, IntProperty, StringProperty
from bpy.types import Operator

from .utils import (
    abs_dir, sanitize, validate_paths, validate_paths_no_scenes,
)
from .export_tscn import build_tscn, build_single_object_tscn


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
