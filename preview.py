#!/usr/bin/env python3
"""Render preview images of the stands into docs/ (needs pillow; embreex makes it fast)."""
from __future__ import annotations

import math

import numpy as np
import trimesh
from PIL import Image

import stand as S

DOCS = S.ROOT / "docs"
STICKERS = np.array(
    [[[0, 155, 72], [0, 70, 173]], [[255, 213, 0], [255, 255, 255]], [[255, 88, 0], [183, 18, 52]]], dtype=float
)  # [axis][low side, high side]
LIGHT = np.array([-0.45, -0.55, 0.7]) / np.linalg.norm([-0.45, -0.55, 0.7])
BACKGROUND = np.array([247, 247, 249], dtype=float)
# Camera (azimuth, elevation, distance, target) per shape: with the puzzle, then the empty stand.
VIEWS = {
    "cube": ((-28, 22, 330, (0, 2, 52)), (-24, 38, 250, (0, -4, 12))),
    "sphere": ((-26, 24, 250, (0, 0, 27)), (-24, 38, 235, (0, -4, 11))),
}


def hex_rgb(color: str) -> np.ndarray:
    return np.array([int(color.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)], dtype=float)


def puzzle_colors(points: np.ndarray, frame: np.ndarray, shape: str, size: float) -> np.ndarray:
    """Colour hits on the puzzle as a solved 3x3 with dark gaps between the pieces."""
    local = (points - frame[:, 3]) @ frame[:, :3]
    rows = np.arange(len(points))
    axis = np.abs(local).argmax(axis=1)
    out = np.abs(local[rows, axis])
    colors = STICKERS[axis, (local[rows, axis] > 0).astype(int)]
    line = 0.022 * size
    for other in (1, 2):
        across = np.abs(local[rows, (axis + other) % 3])
        gap = np.abs(across - size / 6) < line
        # Faces meet at the edge of a cube, and where two axes are equally far out on a sphere.
        gap |= (size / 2 - across < line) if shape == "cube" else (out - across < line)
        colors[gap] = (22, 22, 24)
    return colors


def render(parts, puzzle, azimuth, elevation, distance, target, size=(1200, 900), fov=28, ss=2):
    """Ray trace `parts` [(mesh, rgb)] on a ground plane; `puzzle` is None or (mesh, frame, shape, size)."""
    meshes, face_colors = [], []
    for mesh, rgb in parts:
        meshes.append(mesh)
        face_colors.append(np.tile(rgb, (len(mesh.faces), 1)))
    ground = trimesh.Trimesh([[-900, -900, 0], [900, -900, 0], [900, 900, 0], [-900, 900, 0]], [[0, 1, 2], [0, 2, 3]])
    meshes.append(ground)
    face_colors.append(np.tile(BACKGROUND, (2, 1)))
    puzzle_start = sum(len(m.faces) for m in meshes)
    if puzzle is not None:
        meshes.append(puzzle[0])
        face_colors.append(np.zeros((len(puzzle[0].faces), 3)))
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
    on_puzzle = tri >= puzzle_start
    if on_puzzle.any():
        _, frame, shape, puzzle_size = puzzle
        colors[on_puzzle] = puzzle_colors(hit[on_puzzle], frame, shape, puzzle_size)
        if shape == "sphere":  # exact normals hide the facets of the mesh
            normals[on_puzzle] = (hit[on_puzzle] - frame[:, 3]) / (puzzle_size / 2)

    lit = np.clip(normals @ LIGHT, 0, None)
    shadowed = scene.ray.intersects_any(hit + normals * 1e-3, np.tile(LIGHT, (len(hit), 1)))
    lit[shadowed] *= 0.25
    shade = 0.55 + 0.5 * lit
    # The ground only shows shadows, so it blends into the background elsewhere.
    on_ground = (tri >= puzzle_start - 2) & (tri < puzzle_start)
    shade[on_ground] = np.where(shadowed[on_ground], 0.88, 1.0)
    image = np.tile(BACKGROUND, (len(rays), 1))
    image[ray] = np.clip(colors * shade[:, None], 0, 255)
    picture = Image.fromarray(image.reshape(h, w, 3).astype(np.uint8))
    return picture.resize(size, Image.LANCZOS)


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    for shape, (in_use, empty) in VIEWS.items():
        params = S.PRESETS[shape]
        stand = S.build(params, S.ROOT / "logo" / "SYBIKO.svg")
        colored = [(S.to_trimesh(solid), hex_rgb(color)) for _, color, solid in stand.parts]
        plain = [(S.to_trimesh(stand.solid), hex_rgb(params.body_color))]
        puzzle_size = params.cube_size if shape == "cube" else params.sphere_diameter
        puzzle = (S.to_trimesh(stand.puzzle), stand.puzzle_frame, shape, puzzle_size)
        logo_bounds = np.array([mesh.bounds for mesh, _ in colored[1:]])
        logo_centre = (logo_bounds[:, 0].min(axis=0) + logo_bounds[:, 1].max(axis=0)) / 2
        closeup = (0, params.face_angle, 2.3 * params.logo_width, logo_centre)

        render(colored, puzzle, *in_use).save(DOCS / f"{shape}_stand_in_use.png")
        render(colored, None, *empty).save(DOCS / f"{shape}_stand.png")
        render(plain, None, *empty).save(DOCS / f"{shape}_stand_single_color.png")
        render(colored, None, *closeup, fov=24).save(DOCS / f"{shape}_stand_logo.png")
    print(f"wrote previews to {DOCS}")


if __name__ == "__main__":
    main()
