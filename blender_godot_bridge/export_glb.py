"""
export_glb.py — GLB reader and export helpers for Godot Bridge.
"""

import os
import json
import struct
import bpy

from .utils import sanitize


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
