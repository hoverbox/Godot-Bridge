"""
panels.py — Blender UI panels for Godot Bridge.
"""

import bpy
from bpy.types import Panel

from .utils import (
    sanitize, compute_res_path, abs_dir,
    PRIMITIVE_SHAPES, MESH_SHAPES,
    validate_paths, validate_paths_no_scenes,
)


class GODOT_PT_ObjectPanel(Panel):
    bl_label       = "Godot Object Settings"
    bl_idname      = "GODOT_PT_object_panel"
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
