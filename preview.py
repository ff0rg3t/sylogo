#!/usr/bin/env python3
"""Render preview images of the stand into docs/ (needs pillow; embreex makes it fast)."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import trimesh
from manifold3d import Manifold
from PIL import Image

import stand as S

DOCS = S.ROOT / "docs"
STICKERS = np.array(
    [[[0, 155, 72], [0, 70, 173]], [[255, 213, 0], [255, 255, 255]], [[255, 88, 0], [183, 18, 52]]], dtype=float
)  # [axis][low side, high side]
LIGHT = np.array([-0.45, -0.55, 0.7]) / np.linalg.norm([-0.45, -0.55, 0.7])
BACKGROUND = np.array([247, 247, 249], dtype=float)


def hex_rgb(color: str) -> np.ndarray:
    return np.array([int(color.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)], dtype=float)


def cube_colors(points: np.ndarray, frame: np.ndarray, size: float) -> np.ndarray:
    """Colour hits on the cube as a solved 3x3 with dark gaps between the stickers."""
    local = (points - frame[:, 3]) @ frame[:, :3]
    axis = np.abs(local / size - 0.5).argmax(axis=1)
    rows = np.arange(len(points))
    side = (local[rows, axis] > size / 2).astype(int)
    colors = STICKERS[axis, side]
    cell = size / 3
    for other in (1, 2):
        offset = local[rows, (axis + other) % 3] % cell
        colors[(offset < 1.3) | (offset > cell - 1.3)] = (22, 22, 24)
    return colors


def render(parts, cube_frame, cube_size, azimuth, elevation, distance, target, size=(1200, 900), fov=28, ss=2):
    """Ray trace `parts` [(mesh, rgb)] plus an optional cube on a ground plane."""
    meshes, face_colors = [], []
    for mesh, rgb in parts:
        meshes.append(mesh)
        face_colors.append(np.tile(rgb, (len(mesh.faces), 1)))
    ground = trimesh.Trimesh([[-900, -900, 0], [900, -900, 0], [900, 900, 0], [-900, 900, 0]], [[0, 1, 2], [0, 2, 3]])
    meshes.append(ground)
    face_colors.append(np.tile(BACKGROUND, (2, 1)))
    cube_start = sum(len(m.faces) for m in meshes)
    if cube_frame is not None:
        meshes.append(S.to_trimesh(Manifold.cube([cube_size] * 3).transform(cube_frame)))
        face_colors.append(np.zeros((12, 3)))
    scene = trimesh.util.concatenate(meshes)
    face_colors = np.vstack(face_colors)

    az, el = math.radians(azimuth), math.radians(elevation)
    target = np.asarray(target, dtype=float)
    eye = target + distance * np.array([math.cos(el) * math.sin(az), -math.cos(el) * math.cos(az), math.sin(el)])
    forward = (target - eye) / distance
    right = np.cross(forward, [0, 0, 1])
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    w, h = size[0] * ss, size[1] * ss
    half = math.tan(math.radians(fov) / 2)
    px, py = np.meshgrid((np.arange(w) + 0.5) / w * 2 - 1, 1 - (np.arange(h) + 0.5) / h * 2)
    rays = forward + px[..., None] * half * w / h * right + py[..., None] * half * up
    rays = (rays / np.linalg.norm(rays, axis=-1, keepdims=True)).reshape(-1, 3)

    origins = np.tile(eye, (len(rays), 1))
    tri, ray, hit = scene.ray.intersects_id(origins, rays, multiple_hits=False, return_locations=True)
    normals = scene.face_normals[tri]
    normals[np.einsum("ij,ij->i", normals, rays[ray]) > 0] *= -1
    colors = face_colors[tri].copy()
    on_cube = tri >= cube_start
    if on_cube.any():
        colors[on_cube] = cube_colors(hit[on_cube], cube_frame, cube_size)

    lit = np.clip(normals @ LIGHT, 0, None)
    shadowed = scene.ray.intersects_any(hit + normals * 1e-3, np.tile(LIGHT, (len(hit), 1)))
    lit[shadowed] *= 0.25
    shade = 0.55 + 0.5 * lit
    # The ground only shows shadows, so it blends into the background elsewhere.
    on_ground = (tri >= cube_start - 2) & (tri < cube_start)
    shade[on_ground] = np.where(shadowed[on_ground], 0.88, 1.0)
    image = np.tile(BACKGROUND, (len(rays), 1))
    image[ray] = np.clip(colors * shade[:, None], 0, 255)
    picture = Image.fromarray(image.reshape(h, w, 3).astype(np.uint8))
    return picture.resize(size, Image.LANCZOS)


def main() -> None:
    params = S.Params()
    stand = S.build(params, S.ROOT / "logo" / "SYBIKO.svg")
    colored = [(S.to_trimesh(solid), hex_rgb(color)) for _, color, solid in stand.parts]
    plain = [(S.to_trimesh(stand.solid), hex_rgb(params.body_color))]
    DOCS.mkdir(exist_ok=True)

    render(colored, stand.cube_frame, params.cube_size, -28, 22, 330, (0, 2, 52)).save(DOCS / "stand_with_cube.png")
    render(colored, None, params.cube_size, -24, 38, 250, (0, -4, 12)).save(DOCS / "stand.png")
    render(plain, None, params.cube_size, -24, 38, 250, (0, -4, 12)).save(DOCS / "stand_single_color.png")
    render(colored, None, params.cube_size, 0, 45, 150, (0, -24, 15), fov=24).save(DOCS / "logo_closeup.png")
    print(f"wrote previews to {DOCS}")


if __name__ == "__main__":
    main()
