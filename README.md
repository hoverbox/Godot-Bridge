# Godot Bridge

**Blender to Godot 4 Scene Exporter**

Tag your Blender objects with Godot node types, set up your scene hierarchy using normal Blender parenting, then export a ready-to-use `.tscn` with a single click. No Godot plugin required.

---

## Requirements

- Blender 3.6+ (tested on Blender 5.0)
- Godot 4.x
- Blender's built-in **glTF 2.0** exporter enabled *(Edit → Preferences → Add-ons → Import-Export: glTF 2.0)*

---

## Installation

1. Download `godot_bridge.zip`
2. In Blender open **Edit → Preferences → Add-ons**
3. Click **Install** and select `godot_bridge.zip`
4. Enable the addon by checking the box next to **Godot Bridge**

> To update, disable the existing version first, then install the new zip.

---

## Quick Start

1. Build your scene in Blender as normal — meshes, collision shapes, empties, etc.
2. Select each object → **Properties → Object → Godot Bridge** → check the toggle → pick a node type
3. Use Blender's normal parenting (`Ctrl+P`) to build the hierarchy you want in Godot
4. Go to **Properties → Scene → Godot Bridge** → set your Godot project path, export subfolder, and scene name
5. Set the **Scene Root Node Type**
6. Click **Export Scene .tscn**
7. Open the exported `.tscn` in Godot

---

## How It Works

The exporter writes two types of files into your Godot project:

- **`SceneName.tscn`** — the main scene file with your full node hierarchy and all collision shape data baked in as Godot shape resources. Open this in Godot.
- **`ObjectName.glb`** — one `.glb` per `MeshInstance3D`-tagged object containing only the visible mesh geometry.

Collision meshes are **never** included in the `.glb` — their geometry is read by the exporter to generate shape data, then written directly into the `.tscn` as `ConcavePolygonShape3D`, `ConvexPolygonShape3D`, `BoxShape3D`, etc.

---

## Object Panel

**Properties → Object → Godot Bridge**

Check the toggle in the panel header to include an object in the export. Each object gets two settings:

### Node Type

| Type | Description |
|---|---|
| `MeshInstance3D` | Visible mesh. Exports to its own `.glb`. |
| `StaticBody3D` | Immovable physics body. |
| `CharacterBody3D` | Kinematic character body. |
| `Area3D` | Trigger / detection volume. |
| `RigidBody3D` | Physics-simulated body. |
| `AnimatableBody3D` | Animated physics body. |
| `CollisionShape3D` | Collision shape derived from this mesh. Invisible in Godot. |
| `Node3D` | Plain transform node. |
| `Marker3D` | Empty transform marker. |
| *(+ many more)* | Full dropdown includes lights, cameras, audio, particles, navigation, etc. |

### Collision Shape Type

Shown when an object is tagged as `CollisionShape3D`.

| Shape | Description |
|---|---|
| `Trimesh` | `ConcavePolygonShape3D` — exact triangle mesh. Best for static terrain and walls. **StaticBody3D / Area3D only.** |
| `Convex Hull` | `ConvexPolygonShape3D` — convex hull. Works with all body types including `RigidBody3D`. |
| `Box` | `BoxShape3D` from bounding box. |
| `Sphere` | `SphereShape3D` from bounding box. |
| `Capsule` | `CapsuleShape3D` from bounding box. |
| `Cylinder` | `CylinderShape3D` from bounding box. |

---

## Scene Panel

**Properties → Scene → Godot Bridge**

| Setting | Description |
|---|---|
| Godot Project Path | Path to the folder containing `project.godot` |
| Export Path | Subfolder inside the project, e.g. `scenes` or `assets/props` |
| Scene File Name | Name for the `.tscn` file, without extension |
| Scene Root Node Type | Godot node type for the scene root — `Node3D`, `StaticBody3D`, `CharacterBody3D`, `Area3D`, etc. |

The **Tagged Objects** list shows a live summary of everything that will be exported before you click the button.

---

## Example Setups

### Static Physics Object
```
WoodCrate              ← scene root: StaticBody3D
  CrateMesh            ← MeshInstance3D
  CrateCollision       ← CollisionShape3D [Convex Hull]
```

### Trigger Volume
```
DoorTrigger            ← scene root: Node3D
  TriggerArea          ← Area3D
    TriggerShape       ← CollisionShape3D [Box]
```

### Character
```
Player                 ← scene root: CharacterBody3D
  PlayerMesh           ← MeshInstance3D
  PlayerCollision      ← CollisionShape3D [Capsule]
```

### Level / Environment
```
Level1                 ← scene root: Node3D
  FloorMesh            ← MeshInstance3D
  FloorCol             ← CollisionShape3D [Trimesh]
  Wall_A               ← StaticBody3D
    WallMesh_A         ← MeshInstance3D
    WallCol_A          ← CollisionShape3D [Trimesh]
```

---

## Tips

- **Keep collision meshes simple.** Use Box or Convex Hull for most objects. Reserve Trimesh for large static geometry like terrain.
- **Trimesh is StaticBody3D / Area3D only.** It won't work correctly with `RigidBody3D` — use Convex Hull instead.
- **Object names in Blender become node names in Godot.** Name things clearly.
- **Re-export any time you change geometry or hierarchy.** All files are fully overwritten.
- **Untagged objects are ignored completely.** You can have other objects in the scene without affecting the export.
- **Check the Blender console** (`Window → Toggle System Console` on Windows) for a per-object export log and any warnings.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| *GLB export failed* | Enable the glTF 2.0 addon in Edit → Preferences → Add-ons → Import-Export: glTF 2.0 |
| *No objects tagged for export* | Check the toggle in the Godot Bridge header on each object |
| *Godot Project Path error* | The path must point to the **folder** containing `project.godot`, not the file itself |
| *Wrong collision shape in Godot* | Change the Shape Type in the object panel and re-export |
| *Mesh has no collision in Godot* | Make sure the collision mesh is tagged `CollisionShape3D` and parented to the correct physics body in Blender |
| *`.tscn` parse error in Godot* | Try applying transforms in Blender (`Ctrl+A → All Transforms`) before exporting |
| *Wrong scene root type* | Set the correct type in Properties → Scene → Godot Bridge → Scene Root Node Type |

---

## Changelog

### v2.0
- Rebuilt around a single-scene export workflow
- Collision shape geometry baked directly into `.tscn` as Godot shape resources
- Each `MeshInstance3D` exports to its own `.glb` — collision meshes are never included
- Configurable scene root node type
- Full support for Trimesh, Convex Hull, Box, Sphere, Capsule, and Cylinder shapes
- Console logging for all exported objects and warnings

### v1.0
- Initial release

---

*Godot Bridge is an independent project and is not affiliated with the Godot Engine or the Blender Foundation.*
