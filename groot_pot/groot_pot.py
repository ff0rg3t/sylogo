#!/usr/bin/env python3
"""Flower pot in the shape of Groot's head, as a single printable solid.

The head is sculpted as a signed distance field (bark strands, face, a crown of
broken sticks, twigs and leaves), meshed with marching cubes and written as one
STL. Run `python groot_pot.py --help` for the options.
"""
from __future__ import annotations

import argparse
import math
import time
from functools import reduce
from pathlib import Path

import fast_simplification
import numpy as np
import trimesh
from manifold3d import Error, Manifold, Mesh
from scipy.interpolate import PchipInterpolator
from skimage.measure import marching_cubes

ROOT = Path(__file__).resolve().parent
TAU = 2 * math.pi
UP = np.array([0.0, 0.0, 1.0])

# All lengths in millimetres. The face looks along -y and the base is cut flat at z = 0.
# Outline of the head as (height, radius); the lower face is pushed forward by CHIN.
PROFILE = np.array(
    [(-6, 28), (0, 31), (10, 39), (22, 46), (38, 50.5), (55, 53), (72, 52.5), (88, 50), (100, 47.5), (112, 46), (140, 44)]
)
CHIN = np.array([(-6, 3), (0, 3), (12, 5), (24, 5.5), (40, 2.5), (56, 0), (140, 0)])
radius = PchipInterpolator(PROFILE[:, 0], PROFILE[:, 1])
chin = PchipInterpolator(CHIN[:, 0], CHIN[:, 1])
radius_slope, chin_slope = radius.derivative(), chin.derivative()
DEPTH = 0.9  # front-to-back size relative to the width
WALL = 4.5  # wall thickness, measured from the smooth head under the bark
FLOOR = 4.5
DRAIN = 8.0  # diameter of the drainage hole
STRANDS = 34  # bark strands around the head
PLATE = 34.0  # typical length of one bark plate
RIM = 97.0  # height of the lowest notches in the crown
EYE_HEIGHT = 55.0
BOUNDS = ((-64.0, 64.0), (-62.0, 62.0), (-2.0, 138.0))


def smoothstep(edge0, edge1, value):
    t = np.clip((value - edge0) / (edge1 - edge0), 0, 1)
    return t * t * (3 - 2 * t)


def smin(a, b, k):
    """Union of two distance fields with a fillet of size k."""
    h = np.clip(0.5 + 0.5 * (b - a) / k, 0, 1)
    return b + (a - b) * h - k * h * (1 - h)


def smax(a, b, k):
    return -smin(-a, -b, k)


def wrap(angle):
    return (angle + math.pi) % TAU - math.pi


def unit(vector):
    return np.asarray(vector, dtype=float) / np.linalg.norm(vector)


class Noise:
    """Smooth 3D value noise in -1..1, repeating every 64 lattice cells."""

    def __init__(self, rng):
        self.table = rng.uniform(-1, 1, (64, 64, 64)).astype(np.float32)

    def __call__(self, x, y, z):
        cells = [np.floor(a) for a in (x, y, z)]
        fades = [(a - c) ** 2 * (3 - 2 * (a - c)) for a, c in zip((x, y, z), cells)]
        index = [c.astype(np.int64) & 63 for c in cells]
        total = np.zeros(np.shape(x), dtype=np.float64)
        for dx in (0, 1):
            wx = fades[0] if dx else 1 - fades[0]
            for dy in (0, 1):
                wxy = wx * (fades[1] if dy else 1 - fades[1])
                for dz in (0, 1):
                    weight = wxy * (fades[2] if dz else 1 - fades[2])
                    total += weight * self.table[(index[0] + dx) & 63, (index[1] + dy) & 63, (index[2] + dz) & 63]
        return total


def outline(theta, z):
    """Radius of the smooth head and its slope along z, at angle theta around the head."""
    front = 1 - smoothstep(0.25, 1.25, np.abs(wrap(theta + math.pi / 2)))
    return radius(z) + front * chin(z), radius_slope(z) + front * chin_slope(z)


def skull(x, y, z):
    """Distance to the smooth head without any detail, negative inside."""
    rho = np.sqrt(x * x + (y / DEPTH) ** 2)
    r, dr = outline(np.arctan2(y / DEPTH, x), z)
    safe = np.maximum(rho, 1e-6)
    gradient = np.sqrt((x / safe) ** 2 + (y / (DEPTH**2 * safe)) ** 2 + dr**2)
    return (rho - r) / np.maximum(gradient, 0.5)


def surface(theta, z):
    """Frame on the smooth head: point, outward normal, sideways tangent, upward tangent."""
    r, dr = (float(v) for v in outline(theta, z))
    c, s = math.cos(theta), math.sin(theta)
    normal = unit((c, s / DEPTH, -dr))
    side = unit((-s, DEPTH * c, 0))
    return np.array((r * c, DEPTH * r * s, z)), normal, side, np.cross(normal, side)


def front(x, z):
    """Frame on the face, x mm to the side of its centre line at height z."""
    theta = -math.pi / 2
    for _ in range(5):
        theta = -math.acos(x / float(outline(theta, z)[0]))
    return surface(theta, z)


def local(x, y, z, frame):
    """Coordinates in a surface frame: sideways, upward, outward."""
    point, normal, side, up = frame
    px, py, pz = x - point[0], y - point[1], z - point[2]
    return (
        px * side[0] + py * side[1] + pz * side[2],
        px * up[0] + py * up[1] + pz * up[2],
        px * normal[0] + py * normal[1] + pz * normal[2],
    )


def ellipsoid(a, b, c, radii):
    k0 = np.sqrt((a / radii[0]) ** 2 + (b / radii[1]) ** 2 + (c / radii[2]) ** 2)
    k1 = np.sqrt((a / radii[0] ** 2) ** 2 + (b / radii[1] ** 2) ** 2 + (c / radii[2] ** 2) ** 2)
    return k0 * (k0 - 1) / np.maximum(k1, 1e-9)


def capsule(x, y, z, start, end, r0, r1):
    axis = end - start
    px, py, pz = x - start[0], y - start[1], z - start[2]
    t = np.clip((px * axis[0] + py * axis[1] + pz * axis[2]) / axis.dot(axis), 0, 1)
    return np.sqrt((px - axis[0] * t) ** 2 + (py - axis[1] * t) ** 2 + (pz - axis[2] * t) ** 2) - (r0 + (r1 - r0) * t)


def chain(points, radii):
    """A tube through `points` with the given radius at each."""
    links = list(zip(points[:-1], points[1:], radii[:-1], radii[1:]))
    return lambda x, y, z: reduce(np.minimum, (capsule(x, y, z, *link) for link in links))


def blob(frame, sink, radii):
    """An ellipsoid lying in a surface frame, its centre `sink` below the surface."""

    def sdf(x, y, z):
        a, b, c = local(x, y, z, frame)
        return ellipsoid(a, b, c + sink, radii)

    return sdf


def leaf(centre, along, across, size):
    """A pointed, slightly curled leaf of size (length, width, thickness)."""
    along, across = unit(along), unit(across - np.dot(across, unit(along)) * unit(along))
    through = np.cross(along, across)

    def sdf(x, y, z):
        px, py, pz = x - centre[0], y - centre[1], z - centre[2]
        u = px * along[0] + py * along[1] + pz * along[2]
        v = px * across[0] + py * across[1] + pz * across[2]
        w = px * through[0] + py * through[1] + pz * through[2] + 0.01 * u * u
        w -= 0.3 * np.exp(-((v / 0.9) ** 2))  # midrib
        # Widest a little below the middle, with a pointed tip.
        width = np.sin(math.pi * np.clip(u / size[0] + 0.5, 0, 1) ** 0.9)
        return (np.sqrt((2 * v / size[1]) ** 2 + (2 * w / size[2]) ** 2) - width) * size[2] / 2

    return sdf


def twig(theta, z, tilt, length, r0, r1, leaves=(), bud=False):
    """A twig growing up and out of the head; returns (centre, reach, distance function).

    Each leaf is ("tip" or "middle" of the twig, direction it points, direction its face looks, size).
    """
    point, normal, side, _ = surface(theta, z)
    out = unit((normal[0], normal[1], 0))
    grow = unit(math.cos(tilt) * UP + math.sin(tilt) * out)
    start = point - 1.2 * out
    middle = start + grow * length * 0.5
    grow = unit(grow + 0.3 * UP - 0.12 * side)
    tip = middle + grow * length * 0.5
    r_mid = (r0 + r1) / 2
    parts = [chain([start, middle, tip], [r0, r_mid, r1])]
    if bud:
        parts.append(chain([tip, tip + grow * 3.2], [r1 * 1.35, 0.9]))
    for anchor, along, facing, size in leaves:
        along = unit(along)
        base = tip if anchor == "tip" else middle
        parts.append(leaf(base + along * (size[0] / 2 - 2.5), along, np.cross(facing, along), size))
    reach = length + max([size[0] for *_, size in leaves], default=4) + 4

    def sdf(x, y, z):
        return reduce(lambda a, b: smin(a, b, 0.8), (part(x, y, z) for part in parts))

    return middle, reach, sdf


class Model:
    def __init__(self, drain=True, seed=4):
        rng = np.random.default_rng(seed)
        self.noise = [Noise(rng) for _ in range(6)]
        self.phase = rng.random(STRANDS)
        self.heights = rng.uniform(-1, 1, (STRANDS, 64))
        self.tops = RIM + 6 + 14 * rng.random(STRANDS)
        self.slants = rng.uniform(-1, 1, STRANDS)
        self.points = rng.uniform(1.6, 3.4, STRANDS)
        # Some of the grooves between strands are deeper cracks.
        self.cracks = np.where(rng.random(STRANDS) < 0.45, rng.uniform(0.3, 0.9, STRANDS), 0.0)
        self.drain = drain

        # Brows, nose and cheeks are added to the smooth head so that the bark runs over them.
        self.bumps = []  # (centre, reach, fillet, distance function)
        for s in (-1, 1):
            spine = [front(s * bx, bz) for bx, bz in ((9, 72), (17, 73.5), (27, 71), (33.5, 66.5))]
            points = [frame[0] - 0.6 * frame[1] for frame in spine]
            self.bumps.append((points[1], 24, 2.0, chain(points, (1.2, 1.7, 1.6, 1.1))))
            cheek = front(s * 26, 33)
            self.bumps.append((cheek[0], 18, 6.0, blob(cheek, 2.4, (12, 10, 4.2))))
        nose = front(0, 42)
        self.bumps.append((nose[0], 10, 2.5, blob(nose, 1.4, (2.6, 4.5, 2.3))))

        self.eyes = [front(s * 21, EYE_HEIGHT) for s in (-1, 1)]

        across_mouth = np.linspace(-11, 11, 41)
        smile = [front(mx, 26.5 + 4.2 * (mx / 11) ** 2) for mx in across_mouth]
        points = [frame[0] - 0.3 * frame[1] for frame in smile]
        widths = [1.25 + 0.75 * (1 - (mx / 11) ** 2) for mx in across_mouth]
        groove = chain(points, widths)
        dimples = [smile[i][0] + 0.7 * smile[i][1] for i in (0, -1)]
        self.mouth = lambda x, y, z: reduce(
            np.minimum, [groove(x, y, z)] + [capsule(x, y, z, d, d + 1e-3, 2.1, 2.1) for d in dimples]
        )
        self.mouth_centre = front(0, 29)[0]

        # Knots in the bark as (angle around the head, height, size).
        self.knots = [(math.radians(a), z, size) for a, z, size in ((165, 48, 7.5), (75, 68, 6.0), (-8, 36, 6.5), (-160, 82, 5.0))]
        # The bark strands flow around the eyes and knots: (angle, height, extent, strength).
        self.pushers = [(math.atan2(eye[0][1] / DEPTH, eye[0][0]), EYE_HEIGHT, 16, 1.0) for eye in self.eyes]
        self.pushers += [(angle, z, 1.5 * size, 0.8) for angle, z, size in self.knots]

        self.twigs = [
            twig(math.radians(-60), 96, math.radians(30), 20, 2.8, 1.9, leaves=(
                ("tip", (0.3, -0.1, 0.95), (0.25, -1, 0.15), (24, 12, 2.4)),
                ("middle", (-0.38, -0.15, 0.9), (-0.1, -1, 0.2), (15, 8, 2.2)),
            )),
            twig(math.radians(-125), 97, math.radians(28), 13, 2.5, 1.8, leaves=(
                ("tip", (-0.3, -0.1, 0.95), (-0.2, -1, 0.15), (17, 9, 2.3)),
            )),
            twig(math.radians(152), 98, math.radians(28), 14, 2.6, 2.0, bud=True),
            twig(math.radians(20), 92, math.radians(40), 8, 3.0, 2.7),
        ]

    def bark(self, x, y, z, depth):
        """Height of the bark above the smooth head, and the cut that leaves the crown of broken sticks.

        `depth` is the distance of each point from the smooth head.
        """
        n = self.noise
        theta = np.arctan2(y / DEPTH, x)
        r_here = radius(z)
        # The bark is looked up on the smooth head itself, so it is the same at any depth below or
        # above that point and the displaced surface cannot fold over on itself.
        onto = outline(theta, z)[0] / np.maximum(np.sqrt(x * x + (y / DEPTH) ** 2), 1e-6)
        x, y = x * onto, y * onto

        # Position across the strands; whole numbers are the grooves between them.
        s = STRANDS * theta / TAU
        s += 1.0 * n[0](x / 45, y / 45, z / 60) + 0.3 * n[1](x / 13, y / 13, z / 24)
        for angle, height, extent, strength in self.pushers:
            u, v = r_here * wrap(theta - angle), z - height
            s -= strength * u / extent * np.exp(-(u * u + v * v) / extent**2)
        strand = np.floor(s)
        e = 2 * (s - strand) - 1  # -1..1 across one strand
        strand = strand.astype(np.int64) % STRANDS
        # Rounded strands. The grooves between them have a 0.6 mm flat floor and sides that are steep
        # but not vertical: anything sharper is finer than the grid can mesh cleanly.
        half_width = math.pi * r_here / STRANDS  # of one strand, in mm
        to_groove = (1 - np.abs(e)) * half_width  # distance to the nearest groove, in mm
        across = np.sin(math.pi / 2 * (1 - np.clip(1 - (to_groove - 0.3) / (half_width - 0.3), 0, 1) ** 2.6))

        # Each strand is a stack of plates with pointed upper ends, thicker towards the top.
        t = (z + 5 * n[2](x / 28, y / 28, z / 28)) / PLATE + self.phase[strand] + 0.2 * e * e
        plate = np.floor(t)
        ft = t - plate
        lift = self.heights[strand, plate.astype(np.int64) % 64]
        # Both ends ramp down to the groove level, so that the relief has no jumps to mesh badly.
        along = (0.6 + 0.4 * ft) * smoothstep(0.0, 0.05, ft) * (1 - smoothstep(0.97, 1.0, ft))
        relief = (1.2 + 0.7 * lift) * across * along - 0.55
        relief -= self.cracks[(strand + (e > 0)) % STRANDS] * (1 - smoothstep(0.3, 1.4, to_groove))

        # Finer ridges on each strand, then the wood grain.
        e2 = 2 * ((3 * s + 0.5 * n[3](x / 9, y / 9, z / 45)) % 1.0) - 1
        relief += 0.3 * (0.4 - np.minimum(e2 * e2 / 0.85, 1)) * across * along
        relief += 0.2 * (1 - 2 * np.abs(n[4](x * 0.55, y * 0.55, z * 0.045)))

        # The face is smoother so that its features stay readable.
        face = np.exp(-((wrap(theta + math.pi / 2) / 0.8) ** 2)) * smoothstep(10, 24, z) * (1 - smoothstep(72, 86, z))
        relief *= 1 - 0.45 * face
        # The bark fades out at the very bottom, which gives the print a clean first layer.
        relief *= smoothstep(0.0, 7.0, z)

        for angle, height, size in self.knots:
            u, v = float(radius(height)) * wrap(theta - angle), z - height
            near = (np.abs(u) < 1.4 * size) & (np.abs(v) < 1.4 * size)
            r = np.sqrt(u[near] ** 2 + v[near] ** 2) * (1 + 0.12 * n[5](x[near] / 7, y[near] / 7, z[near] / 7))
            rings = 0.42 * np.cos(TAU * r / 2.3) * smoothstep(1.2, 2.5, r)
            knot = 1.0 * (1 - smoothstep(0.5 * size, 1.2 * size, r)) + rings - 1.7 * np.exp(-((r / 1.4) ** 2))
            weight = 1 - smoothstep(0.8 * size, 1.25 * size, r)
            relief[near] = relief[near] * (1 - weight) + knot * weight

        notch = RIM + 2.5 + 2.5 * np.sin(3 * theta + 1.0)
        # Every stick ends at its own height and with its own point, broken off at a slant.
        power, slant = self.points[strand], 3.5 * self.slants[strand]
        tip = 1 - np.abs(e) ** power
        height = self.tops[strand] - notch + slant * e
        # The tips are also rounded off towards the inside and outside of the wall.
        middle = (depth + 1.8) / 2.8
        top = notch + height * tip - 2.5 * middle * middle
        # Dividing by the steepness of the cut keeps the field a distance, which meshes cleanly.
        steep = (slant * tip - height * power * np.sign(e) * np.abs(e) ** (power - 1)) * 2 * STRANDS / (TAU * r_here)
        return relief, (z - top) / np.sqrt(1 + steep * steep + (5 * middle / 2.8) ** 2)

    def evaluate(self, xs, ys, zs):
        """Signed distance to the pot (negative inside) on the grid xs * ys * zs."""
        x, y, z = np.meshgrid(xs, ys, zs, indexing="ij")

        def near(centre, reach):
            if zs[-1] < centre[2] - reach or zs[0] > centre[2] + reach:
                return None
            mask = (x - centre[0]) ** 2 + (y - centre[1]) ** 2 + (z - centre[2]) ** 2 < reach * reach
            return mask if mask.any() else None

        smooth = skull(x, y, z)
        base = smooth.copy()
        for centre, reach, fillet, sdf in self.bumps:
            if (m := near(centre, reach)) is not None:
                base[m] = smin(base[m], sdf(x[m], y[m], z[m]), fillet)

        # The bark only matters next to the surface, and across the whole wall where the crown is cut.
        field = base.copy()
        band = (base < 2.6) & ((base > -2.6) | ((z > RIM - 5) & (base > -WALL - 2)))
        relief, cut = self.bark(x[band], y[band], z[band], base[band])
        field[band] = np.maximum(base[band] - relief, cut)

        field = np.maximum(field, -np.maximum(smooth + WALL, FLOOR - z))  # hollow

        for eye in self.eyes:
            if (m := near(eye[0], 33)) is None:
                continue
            a, b, c = local(x[m], y[m], z[m], eye)
            # The socket is deeper than the wall, so the wall is thickened behind it. The thickening
            # runs out gently downwards to stay printable.
            backing = ellipsoid(a, b, c - 1, (15.5, np.where(b < 0, 28.0, 17.0), 10.5))
            value = smin(field[m], np.maximum(backing, smooth[m] + 1.8), 2.0)
            value = smax(value, -ellipsoid(a, b, c - 1, (12.0, 13.5, 6.5)), 1.0)
            # The eyeball is a dome; its back half is left out so that it stays inside the thickened wall.
            field[m] = np.minimum(value, np.maximum(ellipsoid(a, b, c + 6.3, (10.7, 12.1, 6.0)), -c - 9.0))

        if (m := near(self.mouth_centre, 18)) is not None:
            field[m] = smax(field[m], -self.mouth(x[m], y[m], z[m]), 0.6)

        for centre, reach, sdf in self.twigs:
            if (m := near(centre, reach)) is not None:
                field[m] = smin(field[m], sdf(x[m], y[m], z[m]), 1.2)

        if self.drain and zs[0] < FLOOR + 3:
            low = z < FLOOR + 3
            field[low] = np.maximum(field[low], DRAIN / 2 - np.sqrt(x[low] ** 2 + y[low] ** 2))
        return field


def capacity() -> float:
    """Volume of the hollow up to the lowest notch of the crown, in millilitres."""
    grid = np.arange(-60.0, 60.0, 1.0)
    x, y, z = np.meshgrid(grid, grid, np.arange(FLOOR + 0.5, RIM, 1.0), indexing="ij")
    return float((skull(x, y, z) < -WALL).sum()) / 1000


def to_trimesh(solid: Manifold) -> trimesh.Trimesh:
    data = solid.to_mesh()
    mesh = trimesh.Trimesh(np.asarray(data.vert_properties)[:, :3], np.asarray(data.tri_verts), process=False)
    # Where two surfaces touch, their vertices share a position and STL readers would weld them into
    # a mesh that is no longer a solid. Such vertices are moved 0.004 mm into the material.
    _, inverse, counts = np.unique(mesh.vertices.astype(np.float32), axis=0, return_inverse=True, return_counts=True)
    touching = counts[inverse.ravel()] > 1
    if touching.any():
        mesh.vertices[touching] -= mesh.vertex_normals[touching] * 0.004
    return mesh


def build(resolution: float = 0.3, triangles: int = 600_000, drain: bool = True, log=print) -> Manifold:
    """Sculpt the pot on a grid of `resolution` mm and return it with about `triangles` triangles."""
    started = time.time()
    model = Model(drain)
    xs, ys, zs = (lo + resolution * np.arange(int(round((hi - lo) / resolution)) + 1) for lo, hi in BOUNDS)
    field = np.empty((len(xs), len(ys), len(zs)), dtype=np.float32)
    for k in range(0, len(zs), 16):
        field[:, :, k : k + 16] = model.evaluate(xs, ys, zs[k : k + 16])
    log(f"sculpted {field.size / 1e6:.0f} million grid points in {time.time() - started:.0f} s")

    # The bottom is closed below z = 0 here and cut flat afterwards; nothing may touch the other sides.
    field[:, :, 0] = 1.0
    edges = [field[0], field[-1], field[:, 0], field[:, -1], field[:, :, -1]]
    if min(float(edge.min()) for edge in edges) <= 0:
        raise ValueError("the model reaches the edge of the grid; enlarge BOUNDS")

    vertices, faces, _, _ = marching_cubes(field, 0.0, spacing=(resolution,) * 3)
    del field
    vertices += [lo for lo, _ in BOUNDS]
    log(f"meshed {len(faces):,} triangles")
    if triangles and len(faces) > triangles:
        vertices, faces = fast_simplification.simplify(vertices, faces, target_count=triangles)

    solid = Manifold(Mesh(np.ascontiguousarray(vertices, np.float32), np.ascontiguousarray(faces, np.uint32)))
    if solid.status() != Error.NoError:
        raise ValueError(f"mesh is not a closed solid: {solid.status()}")
    if solid.volume() < 0:
        raise ValueError("mesh is inside out")
    solid = solid.trim_by_plane((0, 0, 1), 0.0)
    # Specks that the bark detail may leave detached are dropped.
    return max(solid.decompose(), key=lambda part: part.volume())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--out", type=Path, default=ROOT / "output" / "groot_pot.stl", help="STL file to write")
    parser.add_argument("--resolution", type=float, default=0.3, help="sculpting grid in mm; smaller is finer and slower")
    parser.add_argument("--triangles", type=int, default=600_000, help="triangle budget of the STL; 0 keeps them all")
    parser.add_argument("--scale", type=float, default=1.0, help="resize the finished pot (1.0 is 107 mm wide)")
    parser.add_argument("--no-drain", action="store_true", help="leave out the drainage hole")
    args = parser.parse_args()

    solid = build(args.resolution, args.triangles, not args.no_drain).scale((args.scale,) * 3)
    mesh = to_trimesh(solid)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(args.out)

    size = mesh.bounds[1] - mesh.bounds[0]
    # Faces that lean out more than 50 degrees from vertical, apart from the base, may need support.
    steep = (mesh.face_normals[:, 2] < -math.sin(math.radians(50))) & (mesh.triangles_center[:, 2] > 0.3 * args.scale)
    print("size (mm): %.1f x %.1f x %.1f" % tuple(size))
    print(f"triangles: {len(mesh.faces):,}, watertight when read back: {trimesh.load(args.out).is_watertight}")
    print(f"material (cm3): {solid.volume() / 1000:.1f}, holds about {capacity() * args.scale**3:.0f} ml")
    print(f"steep overhangs (cm2): {mesh.area_faces[steep].sum() / 100:.1f} of {mesh.area / 100:.0f}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
