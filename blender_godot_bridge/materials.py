"""
materials.py — Blender → Godot 4 material conversion for Godot Bridge.

OVERVIEW
--------
Converts Blender Principled BSDF materials to Godot 4 StandardMaterial3D
.tres files.  Each unique material is written once; objects that share the
same Blender material reuse the same .tres.

PROPERTY MAPPING
----------------
Blender Principled BSDF          Godot StandardMaterial3D
─────────────────────────────    ────────────────────────────────────────
Base Color (solid)           →   albedo_color = Color(r, g, b, a)
Base Color Texture           →   albedo_texture  + texture_filter
Metallic (value / texture)   →   metallic  / metallic_texture
Roughness (value / texture)  →   roughness / roughness_texture
Normal Map texture           →   normal_enabled + normal_texture
Emission Color / texture     →   emission_enabled + emission + emission_texture
Alpha (blend mode)           →   transparency flag

TEXTURE HANDLING
----------------
Image textures are copied to <export_dir>/textures/ and referenced via
res:// paths.  Packed images are unpacked on-the-fly to a temp file, copied,
then the temp file is removed.  Already-on-disk images are copied as-is.

If a texture copy fails for any reason the property falls back to the
corresponding scalar/color value so the material is still valid.

USAGE
-----
    from .materials import export_materials_for_objects

    mat_res_paths = export_materials_for_objects(
        objects,
        project_root_abs,
        export_dir_abs,
    )
    # mat_res_paths: dict  material_name → res:// path of the .tres file

The returned dict is used by the TSCN builder to attach
    surface_material_override/0 = ExtResource("...")
to each MeshInstance3D node.
"""

import os
import re
import math
import shutil
import struct
import tempfile

import bpy


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sanitize(name: str) -> str:
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if name and name[0].isdigit():
        name = "_" + name
    return name or "Material"


def _fmtf(v: float) -> str:
    """Format a float for Godot .tres — no sci notation, no nan/inf."""
    if not math.isfinite(v):
        return "0.0"
    if abs(v) < 1e-6:
        return "0.0"
    s = f"{v:.7f}".rstrip('0')
    if s.endswith('.'):
        s += '0'
    return s


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


# ---------------------------------------------------------------------------
# Texture export
# ---------------------------------------------------------------------------

def _ensure_tex_dir(export_dir_abs: str) -> str:
    tex_dir = os.path.join(export_dir_abs, "textures")
    os.makedirs(tex_dir, exist_ok=True)
    return tex_dir


def _copy_texture(image: bpy.types.Image, tex_dir: str) -> str | None:
    """
    Copy a Blender image to tex_dir.
    Returns the destination absolute path, or None on failure.

    Handles three cases:
      1. Image saved on disk and not dirty → copy directly.
      2. Image packed (or dirty) → save to a temp PNG, copy, remove temp.
      3. Image has no pixels at all → return None.
    """
    if image is None:
        return None

    dest_name = _sanitize(os.path.splitext(image.name)[0])
    # Prefer the original file extension; fall back to .png.
    src_ext = os.path.splitext(image.filepath_raw)[-1].lower() if image.filepath_raw else ""
    ext = src_ext if src_ext in (".png", ".jpg", ".jpeg", ".webp", ".exr", ".hdr") else ".png"
    dest_name += ext
    dest_path = os.path.join(tex_dir, dest_name)

    # Case 1 — file on disk and clean
    if image.filepath_raw and not image.is_dirty and not image.packed_file:
        src = bpy.path.abspath(image.filepath_raw)
        if os.path.isfile(src):
            if src != dest_path:
                shutil.copy2(src, dest_path)
            return dest_path

    # Case 2 — packed or dirty: save to temp PNG then copy
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        old_path = image.filepath_raw
        old_fmt  = image.file_format
        try:
            image.filepath_raw = tmp.name
            image.file_format  = "PNG"
            image.save()
            shutil.copy2(tmp.name, dest_path.replace(ext, ".png"))
            dest_path = dest_path.replace(ext, ".png")
        finally:
            image.filepath_raw = old_path
            image.file_format  = old_fmt
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
        return dest_path
    except Exception:
        return None


def _res_path_for_texture(project_root_abs: str, tex_abs: str) -> str:
    try:
        rel = os.path.relpath(tex_abs, project_root_abs)
    except ValueError:
        rel = os.path.basename(tex_abs)
    return "res://" + rel.replace("\\", "/")


# ---------------------------------------------------------------------------
# Godot .import sidecar writer
# ---------------------------------------------------------------------------

def _write_texture_import(tex_abs: str, is_normal: bool = False) -> None:
    """
    Write a Godot 4 .import sidecar next to tex_abs so the editor never
    auto-discovers and re-imports it mid-session, preventing the reimport
    task collision error.

    Normal maps get compress/normal_map=1 (RG compression, discard blue).
    All other 3D textures get VRAM compression with mipmaps enabled.
    Overwrites any existing sidecar to keep settings in sync.
    """
    import_path = tex_abs + ".import"
    fname = os.path.basename(tex_abs)
    normal_map_val = "1" if is_normal else "0"

    lines = [
        "[remap]",
        "",
        'importer="texture"',
        'importer_version=1',
        'type="CompressedTexture2D"',
        "",
        "[deps]",
        "",
        f'source_file="res://PLACEHOLDER/{fname}"',
        "",
        "[params]",
        "",
        "compress/mode=1",
        "compress/high_quality=false",
        "compress/lossy_quality=0.7",
        f"compress/normal_map={normal_map_val}",
        "compress/channel_pack=0",
        "mipmaps/generate=true",
        "mipmaps/limit=-1",
        "roughness/mode=0",
        'roughness/src_normal=""',
        "process/fix_alpha_border=true",
        "process/premult_alpha=false",
        "process/normal_map_invert_y=false",
        "process/hdr_as_srgb=false",
        "process/hdr_clamp_exposure=false",
        "process/size_limit=0",
        "detect_3d/compress_to=1",
        "svg/scale=1.0",
        "editor/scale_with_editor_scale=false",
        "editor/convert_colors_with_editor_theme=false",
    ]
    try:
        with open(import_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Principled BSDF input extraction
# ---------------------------------------------------------------------------

def _get_principled(mat: bpy.types.Material):
    """Return the first Principled BSDF node in the material, or None."""
    if not mat.use_nodes:
        return None
    for node in mat.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            return node
    return None


def _input_value(node, name: str, default=0.0):
    """Return the scalar default value of a Principled BSDF socket."""
    inp = node.inputs.get(name)
    if inp is None:
        return default
    return inp.default_value


def _input_color(node, name: str):
    """Return (r, g, b, a) from a color socket."""
    inp = node.inputs.get(name)
    if inp is None:
        return (1.0, 1.0, 1.0, 1.0)
    c = inp.default_value
    # Socket may be RGB (len 3) or RGBA (len 4)
    if hasattr(c, '__len__'):
        r, g, b = float(c[0]), float(c[1]), float(c[2])
        a = float(c[3]) if len(c) > 3 else 1.0
    else:
        r = g = b = float(c); a = 1.0
    return (r, g, b, a)


def _input_texture(node, name: str):
    """
    Return the bpy.types.Image connected to a socket, or None.
    Follows the standard Image Texture → socket link.
    """
    inp = node.inputs.get(name)
    if inp is None or not inp.links:
        return None
    link = inp.links[0]
    src  = link.from_node
    if src.type == 'TEX_IMAGE':
        return src.image
    # Normal Map node in between (for normal socket)
    if src.type == 'NORMAL_MAP':
        color_inp = src.inputs.get('Color')
        if color_inp and color_inp.links:
            nm_src = color_inp.links[0].from_node
            if nm_src.type == 'TEX_IMAGE':
                return nm_src.image
    return None


# ---------------------------------------------------------------------------
# .tres writer
# ---------------------------------------------------------------------------

# Godot 4 transparency enum values
_TRANSPARENCY_DISABLED = 0
_TRANSPARENCY_ALPHA    = 1


def _write_tres(
    mat_name:          str,
    albedo_color:      tuple,          # (r, g, b, a)
    albedo_tex_res:    str | None,
    metallic_val:      float,
    metallic_tex_res:  str | None,
    roughness_val:     float,
    roughness_tex_res: str | None,
    normal_tex_res:    str | None,
    emission_enabled:  bool,
    emission_color:    tuple,          # (r, g, b, a)  linear
    emission_tex_res:  str | None,
    use_transparency:  bool,
) -> str:
    """
    Produce the text of a Godot 4 StandardMaterial3D .tres file.

    External resource IDs are assigned in declaration order.
    The sub_resource count is always 0 (StandardMaterial3D needs none).
    """
    lines      = []
    ext_res    = []   # list of (id_str, type_str, path_str)
    next_id    = [1]

    def add_ext(rtype: str, path: str) -> str:
        rid = str(next_id[0]); next_id[0] += 1
        ext_res.append((rid, rtype, path))
        return rid

    # Pre-register textures so IDs are declared before [resource]
    alb_rid  = add_ext("Texture2D", albedo_tex_res)   if albedo_tex_res  else None
    met_rid  = add_ext("Texture2D", metallic_tex_res) if metallic_tex_res else None
    rou_rid  = add_ext("Texture2D", roughness_tex_res) if roughness_tex_res else None
    nrm_rid  = add_ext("Texture2D", normal_tex_res)   if normal_tex_res  else None
    emi_rid  = add_ext("Texture2D", emission_tex_res) if emission_tex_res else None

    load_steps = 1 + len(ext_res)   # 1 for the [resource] itself

    lines.append(f'[gd_resource type="StandardMaterial3D" load_steps={load_steps} format=3]')
    lines.append("")

    for rid, rtype, path in ext_res:
        lines.append(f'[ext_resource type="{rtype}" path="{path}" id="{rid}"]')
    if ext_res:
        lines.append("")

    lines.append('[resource]')

    # ── Transparency ──────────────────────────────────────────────────────
    if use_transparency:
        lines.append(f'transparency = {_TRANSPARENCY_ALPHA}')

    # ── Albedo ────────────────────────────────────────────────────────────
    r, g, b, a = (_clamp01(x) for x in albedo_color)
    lines.append(f'albedo_color = Color({_fmtf(r)}, {_fmtf(g)}, {_fmtf(b)}, {_fmtf(a)})')
    if alb_rid:
        lines.append(f'albedo_texture = ExtResource("{alb_rid}")')

    # ── Metallic ──────────────────────────────────────────────────────────
    lines.append(f'metallic = {_fmtf(_clamp01(metallic_val))}')
    if met_rid:
        lines.append(f'metallic_texture = ExtResource("{met_rid}")')
        # Channel R in Blender's metallic texture
        lines.append('metallic_texture_channel = 0')

    # ── Roughness ─────────────────────────────────────────────────────────
    lines.append(f'roughness = {_fmtf(_clamp01(roughness_val))}')
    if rou_rid:
        lines.append(f'roughness_texture = ExtResource("{rou_rid}")')
        # Channel G in Blender's roughness texture
        lines.append('roughness_texture_channel = 1')

    # ── Normal Map ────────────────────────────────────────────────────────
    if nrm_rid:
        lines.append('normal_enabled = true')
        lines.append(f'normal_texture = ExtResource("{nrm_rid}")')

    # ── Emission ──────────────────────────────────────────────────────────
    if emission_enabled:
        lines.append('emission_enabled = true')
        er, eg, eb, _ = (_clamp01(x) for x in emission_color)
        lines.append(f'emission = Color({_fmtf(er)}, {_fmtf(eg)}, {_fmtf(eb)}, 1.0)')
        if emi_rid:
            lines.append(f'emission_texture = ExtResource("{emi_rid}")')

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_materials_for_objects(
    objects,
    project_root_abs: str,
    export_dir_abs:   str,
) -> dict:
    """
    Convert and write .tres files for all Blender materials used by *objects*.

    Parameters
    ----------
    objects          : iterable of bpy.types.Object  (mesh objects)
    project_root_abs : absolute path to the Godot project root
    export_dir_abs   : absolute path of the export directory

    Returns
    -------
    dict  { blender_material_name: str  →  res_path: str }
        Only materials that were successfully exported are included.
        Caller uses this to write  surface_material_override/0  on mesh nodes.
    """
    tex_dir     = _ensure_tex_dir(export_dir_abs)
    mat_dir     = export_dir_abs          # .tres files sit alongside .glb files
    result      = {}                      # mat.name → res:// path
    seen        = set()                   # avoid re-exporting the same material

    mesh_objects = [o for o in objects if o.type == 'MESH']

    for obj in mesh_objects:
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or mat.name in seen:
                continue
            seen.add(mat.name)

            principled = _get_principled(mat)

            # ── Collect property values ───────────────────────────────────

            # Albedo
            if principled:
                albedo_color   = _input_color(principled, 'Base Color')
                albedo_img     = _input_texture(principled, 'Base Color')
            else:
                # Fallback: use diffuse_color if no node tree
                dc = mat.diffuse_color
                albedo_color = (float(dc[0]), float(dc[1]), float(dc[2]),
                                float(dc[3]) if len(dc) > 3 else 1.0)
                albedo_img   = None

            # Metallic
            metallic_val = _clamp01(float(_input_value(principled, 'Metallic', 0.0))) \
                           if principled else 0.0
            metallic_img = _input_texture(principled, 'Metallic') if principled else None

            # Roughness
            roughness_val = _clamp01(float(_input_value(principled, 'Roughness', 0.5))) \
                            if principled else 0.5
            roughness_img = _input_texture(principled, 'Roughness') if principled else None

            # Normal
            normal_img = _input_texture(principled, 'Normal') if principled else None

            # Emission
            if principled:
                # Blender 4.x: 'Emission Color' socket; older: 'Emission'
                emi_socket   = 'Emission Color' if 'Emission Color' in principled.inputs \
                               else 'Emission'
                emission_col = _input_color(principled, emi_socket)
                emission_str = float(_input_value(principled, 'Emission Strength', 0.0))
                emission_img = _input_texture(principled, emi_socket)
                # Emission is "enabled" if strength > 0 or a texture is connected
                emission_enabled = (emission_str > 1e-4) or (emission_img is not None)
                # Scale color by strength so Godot sees the right intensity
                if emission_enabled and emission_str > 0:
                    emission_col = tuple(_clamp01(c * emission_str) for c in emission_col)
            else:
                emission_enabled = False
                emission_col     = (0.0, 0.0, 0.0, 1.0)
                emission_img     = None

            # Transparency: use alpha blend if material blend mode is not OPAQUE
            use_transparency = (mat.blend_method != 'OPAQUE') \
                               if hasattr(mat, 'blend_method') else False
            # Also enable if albedo alpha < 1 and no separate texture drives it
            if not use_transparency and albedo_img is None:
                if albedo_color[3] < 0.999:
                    use_transparency = True

            # ── Copy textures ─────────────────────────────────────────────

            def tex_res(img, is_normal=False):
                if img is None:
                    return None
                dest = _copy_texture(img, tex_dir)
                if dest is None:
                    return None
                _write_texture_import(dest, is_normal=is_normal)
                return _res_path_for_texture(project_root_abs, dest)

            albedo_tex_res    = tex_res(albedo_img)
            metallic_tex_res  = tex_res(metallic_img)
            roughness_tex_res = tex_res(roughness_img)
            normal_tex_res    = tex_res(normal_img, is_normal=True)
            emission_tex_res  = tex_res(emission_img)

            # ── Write .tres ───────────────────────────────────────────────

            tres_filename = _sanitize(mat.name) + ".tres"
            tres_path     = os.path.join(mat_dir, tres_filename)

            tres_text = _write_tres(
                mat_name          = mat.name,
                albedo_color      = albedo_color,
                albedo_tex_res    = albedo_tex_res,
                metallic_val      = metallic_val,
                metallic_tex_res  = metallic_tex_res,
                roughness_val     = roughness_val,
                roughness_tex_res = roughness_tex_res,
                normal_tex_res    = normal_tex_res,
                emission_enabled  = emission_enabled,
                emission_color    = emission_col,
                emission_tex_res  = emission_tex_res,
                use_transparency  = use_transparency,
            )

            try:
                with open(tres_path, 'w', encoding='utf-8') as f:
                    f.write(tres_text)
            except OSError:
                continue   # skip silently; no entry added to result

            from .utils import compute_res_path
            result[mat.name] = compute_res_path(
                project_root_abs, export_dir_abs, tres_filename
            )

    return result
