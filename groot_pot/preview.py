#!/usr/bin/env python3
"""Render preview images of the pot into docs/ (needs pillow; embreex makes it fast)."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

ROOT = Path(__file__).resolve().parent
TAU = 2 * math.pi
WOOD = np.array([172.0, 122.0, 74.0])
BACKGROUND = np.array([244.0, 244.0, 246.0])
KEY = np.array([-0.5, -0.6, 0.62]) / np.linalg.norm([-0.5, -0.6, 0.62])
FILL = np.array([0.75, -0.35, 0.3]) / np.linalg.norm([0.75, -0.35, 0.3])
BACK = np.array([0.3, 0.8, 0.45]) / np.linalg.norm([0.3, 0.8, 0.45])
# name: (azimuth, elevation, distance, look-at point); azimuth 0 looks at the face.
VIEWS = {
    "front": (0, 8, 370, (0, 0, 64)),
    "three_quarter": (-34, 16, 375, (0, 0, 64)),
    "back": (150, 16, 375, (0, 0, 64)),
    "top": (-18, 58, 370, (0, 0, 55)),
    "face": (10, 6, 215, (0, -25, 50)),
    "bark": (76, 10, 110, (48, -10, 60)),
}


def render(mesh, azimuth, elevation, distance, target, size=(1200, 900), fov=26, ss=2, ao=8, color=WOOD):
    """Ray trace the mesh standing on the ground, with a key light, a fill light and ambient occlusion."""
    ground = trimesh.Trimesh([[-3000, -3000, 0], [3000, -3000, 0], [3000, 3000, 0], [-3000, 3000, 0]], [[0, 1, 2], [0, 2, 3]])
    scene = trimesh.util.concatenate([mesh, ground])

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

    tri, ray, hit = scene.ray.intersects_id(np.tile(eye, (len(rays), 1)), rays, multiple_hits=False, return_locations=True)
    on_mesh = tri < len(mesh.faces)
    normals = np.tile([0.0, 0.0, 1.0], (len(tri), 1))
    # Normals are blended across each triangle so that the facets do not show.
    corners = mesh.faces[tri[on_mesh]]
    weights = trimesh.triangles.points_to_barycentric(mesh.vertices[corners], hit[on_mesh])
    blended = (mesh.vertex_normals[corners] * weights[:, :, None]).sum(axis=1)
    normals[on_mesh] = blended / np.maximum(np.linalg.norm(blended, axis=1, keepdims=True), 1e-9)
    normals[np.einsum("ij,ij->i", normals, rays[ray]) > 0] *= -1

    start = hit + normals * 0.02
    shadowed = scene.ray.intersects_any(start, np.tile(KEY, (len(hit), 1)))
    helper = np.where(np.abs(normals[:, 2:3]) < 0.9, [[0.0, 0.0, 1.0]], [[1.0, 0.0, 0.0]])
    tx = np.cross(helper, normals)
    tx /= np.linalg.norm(tx, axis=1, keepdims=True)
    ty = np.cross(normals, tx)
    rng = np.random.default_rng(1)
    occluded = np.zeros(len(hit))
    for _ in range(ao):
        u, phi = rng.random(len(hit)), TAU * rng.random(len(hit))
        spread = np.sqrt(u)[:, None]
        direction = (tx * np.cos(phi)[:, None] + ty * np.sin(phi)[:, None]) * spread + normals * np.sqrt(1 - u)[:, None]
        occluded += scene.ray.intersects_any(start, direction)
    occluded /= max(ao, 1)

    light = 0.6 * (1 - 0.9 * occluded) + 0.5 * np.clip(normals @ KEY, 0, None) * ~shadowed
    light += 0.12 * np.clip(normals @ FILL, 0, None) + 0.22 * np.clip(normals @ BACK, 0, None)
    # The ground only shows the shadow and where the pot stands.
    light[~on_mesh] = 1 - 0.3 * occluded[~on_mesh] - 0.1 * shadowed[~on_mesh]
    colors = np.where(on_mesh[:, None], color, BACKGROUND)
    image = np.tile(BACKGROUND, (len(rays), 1))
    image[ray] = np.clip(colors * light[:, None], 0, 255)
    picture = Image.fromarray(image.reshape(h, w, 3).astype(np.uint8))
    return picture.resize(size, Image.LANCZOS)


def main() -> None:
    mesh = trimesh.load(ROOT / "output" / "groot_pot.stl")
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    for name, view in VIEWS.items():
        render(mesh, *view, ao=12).save(docs / f"groot_pot_{name}.jpg", quality=90)
    print(f"wrote {len(VIEWS)} previews to {docs}")


if __name__ == "__main__":
    main()
