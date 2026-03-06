# Godot Bridge

A Blender add-on for exporting scenes and assets directly to a Godot 4 project.

![Godot Bridge](screenshots/preview.png)

## Features

- Export Blender scenes directly to your Godot project folder
- Designed for a smooth Blender → Godot 4 pipeline

## Requirements

- Blender 4.2 or newer
- Godot 4.x

## Installation

### Via Blender Extensions (recommended — Blender 4.2+)

1. Open Blender and go to **Edit → Preferences → Get Extensions**.
2. Search for **Godot Bridge** and click **Install**.

### Manual

1. Download the latest `.zip` from the [Releases](https://github.com/hoverbox/Godot-Bridge/releases) page.
2. In Blender, go to **Edit → Preferences → Add-ons → Install from Disk**.
3. Select the downloaded `.zip` and enable the add-on.

## Usage

1. After enabling the add-on, find the **Godot Bridge** panel in the sidebar (`N` key) or the relevant properties panel.
2. Set your Godot project path.
3. Select the objects or collections you want to export and click **Export**.

## Building from Source

Requires Blender 4.2+ installed and available on your PATH.

```bash
git clone https://github.com/hoverbox/Godot-Bridge.git
cd Godot-Bridge
blender --command extension build --source-dir blender_godot_bridge/
```

This produces `godot_bridge-1.0.0.zip`, which you can install via **Install from Disk** to test locally.

To validate the manifest before building:

```bash
blender --command extension validate --source-dir blender_godot_bridge/
```

## License

GNU General Public License v3.0 or later — see [LICENSE](LICENSE) for details.

This add-on is free software. You are free to use, modify, and distribute it
under the terms of the GPL-3.0-or-later license.
