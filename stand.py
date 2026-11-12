#!/usr/bin/env python3
"""Parametric Rubik's puzzle display stand with an SVG logo on its front face.

The puzzle is either a cube balanced on one corner in a three-sided pocket or a
sphere resting in a dish; the logo is raised on a slanted plaque in front of it.
Run `python stand.py --help` for the parameters.
"""
from __future__ import annotations

import argparse
import math
import zipfile
from dataclasses import dataclass, fields
from functools import reduce
from operator import add
from pathlib import Path

import numpy as np
import svgelements as se
import trimesh
from manifold3d import CrossSection, FillRule, JoinType, Manifold

ROOT = Path(__file__).resolve().parent
# Offsetting outlines leaves features far smaller than STL's precision; anything below this (mm) is removed.
TOLERANCE = 0.004
BASIC_COLORS = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "grey": (128, 128, 128),
    "red": (255, 0, 0),
    "orange": (255, 128, 0),
    "yellow": (255, 255, 0),
    "green": (0, 160, 0),
    "blue": (0, 0, 255),
    "purple": (128, 0, 160),
    "pink": (255, 105, 180),
    "brown": (120, 70, 20),
}


def color_labels(colors: list[str]) -> list[str]:
    """Give each hex colour a distinct name: the nearest basic colour, plus the hex or position if names clash."""
    names = []
    for color in colors:
        rgb = [int(color.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
        names.append(min(BASIC_COLORS, key=lambda n: sum((a - b) ** 2 for a, b in zip(BASIC_COLORS[n], rgb))))
    labels = [n if names.count(n) == 1 else f"{n}_{c.lstrip('#').lower()}" for n, c in zip(names, colors)]
    return [label if labels.count(label) == 1 else f"{label}_{i + 1}" for i, label in enumerate(labels)]


@dataclass
class Params:
    """All lengths in millimetres, angles in degrees."""

    shape: str = "cube"  # "cube" (balanced on a corner) or "sphere" (resting in a dish)
    cube_size: float = 57.0  # edge of the cube (standard 3x3 is 56-57 mm)
    sphere_diameter: float = 40.0  # diameter of the sphere
    sphere_clearance: float = 0.3  # extra dish radius so the sphere seats freely
    pocket_depth: float = 17.0  # vertical depth of the pocket
    pocket_margin: float = 4.0  # flat rim left around the pocket
    logo_width: float = 66.0  # logo height follows from its aspect ratio
    logo_margin: float = 4.0  # clear border around the logo
    face_angle: float = 45.0  # tilt of the logo face from horizontal
    lip: float = 3.0  # vertical front edge below the logo face
    draft: float = 3.0  # how far the sides and back lean inward
    corner_radius: float = 4.0  # rounding of the vertical corners
    emboss: float = 0.6  # how far the logo stands proud of the face
    inlay: float = 1.4  # how deep the logo sits in the body
    inlay_clearance: float = 0.15  # gap around each separately printed logo piece in its recess
    min_wall: float = 0.5  # thinnest printable wall; closer logo pieces share a recess
    body_color: str = "#ECECEC"  # display colour only, used in the 3MF


# Default dimensions for each puzzle shape.
PRESETS = {
    "cube": Params(),
    "sphere": Params(shape="sphere", pocket_depth=8.0, logo_width=60.0),
}


@dataclass
class Stand:
    solid: Manifold  # body and logo fused, for single-colour printing
    parts: list[tuple[str, str, Manifold]]  # (name, hex colour, mesh), non-overlapping
    inlay_body: Manifold  # body with recesses for the separately printed logo pieces
    inlay_pieces: list[tuple[str, str, Manifold]]  # (name, hex colour, that colour's pieces lying flat on z = 0)
    seat: np.ndarray  # 3x4 transform moving the flat logo pieces into their recesses
    puzzle: Manifold  # the cube or sphere resting in its pocket, for previews and fit checks
    puzzle_frame: np.ndarray  # 3x4 transform from the puzzle's own centred axes to the stand
    size: tuple[float, float, float]


def load_logo(svg_path: Path, width: float) -> tuple[dict[str, CrossSection], float]:
    """Return the logo as one 2D outline per fill colour, centred, `width` wide, y up."""
    shapes = []
    for element in se.SVG.parse(str(svg_path)).elements():
        if not isinstance(element, se.Shape) or element.fill is None or element.fill.value is None:
            continue
        contours = []
        for subpath in se.Path(element).as_subpaths():
            points = []
            for segment in se.Path(subpath):
                if isinstance(segment, se.Move):
                    continue
                if isinstance(segment, (se.Line, se.Close)):
                    points.append((segment.end.x, segment.end.y))
                else:
                    points.extend(segment.npoint(np.linspace(0, 1, 25))[1:])
            if len(points) >= 3:
                contours.append(np.asarray(points, dtype=float))
        rule = FillRule.EvenOdd if element.values.get("fill-rule") == "evenodd" else FillRule.NonZero
        shapes.append((element.fill.hex.lower(), rule, contours))
    if not shapes:
        raise ValueError(f"no filled shapes found in {svg_path}")

    every_point = np.vstack([c for _, _, contours in shapes for c in contours])
    low, high = every_point.min(axis=0), every_point.max(axis=0)
    scale = width / (high[0] - low[0])
    centre = (low + high) / 2

    by_color: dict[str, CrossSection] = {}
    for color, rule, contours in shapes:
        # SVG y points down; flipping it keeps the logo readable from the front.
        outline = CrossSection([(c - centre) * (scale, -scale) for c in contours], rule)
        by_color[color] = by_color[color] + outline if color in by_color else outline
    return by_color, (high[1] - low[1]) * scale


def grow(outline: CrossSection, distance: float) -> CrossSection:
    return outline.offset(distance, JoinType.Round, 2.0, 32).simplify(TOLERANCE)


def split_logo(logo: dict[str, CrossSection], p: Params) -> list[tuple[str, CrossSection, CrossSection]]:
    """Split the logo into separately printable pieces: (colour, footprint, part of it left flush with the face)."""
    bridge = p.min_wall / 2 + p.inlay_clearance
    pieces = []
    for color, outline in logo.items():
        others = reduce(add, (o for c, o in logo.items() if c != color), CrossSection())
        islands = outline.decompose()
        for group in grow(grow(outline, bridge), -bridge).decompose():
            members = [island for island in islands if (island ^ group).area() > island.area() / 2]
            footprint, flush = reduce(add, members), CrossSection()
            if len(members) > 1:
                # Shapes of one colour with no room for a wall between them become one piece on a shared base,
                # rounded off so that its recess has no teeth too fine to print.
                raised = footprint
                footprint = grow(grow(raised, 3 * bridge), -3 * bridge) - grow(others, 2 * p.inlay_clearance)
                # The base shows between the shapes; slivers along their edges are dropped.
                flush = grow(grow(footprint - raised, -0.01), 0.02)
            pieces.append((color, footprint, flush))
    return pieces


def rounded_rect(x0: float, x1: float, y0: float, y1: float, r: float, z: float) -> list[tuple]:
    points = []
    for cx, cy, start in ((x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180), (x1 - r, y0 + r, 270)):
        for a in np.radians(np.linspace(start, start + 90, 25)):
            points.append((cx + r * math.cos(a), cy + r * math.sin(a), z))
    return points


def build(p: Params, svg_path: Path) -> Stand:
    logo, logo_height = load_logo(svg_path, p.logo_width)

    angle = math.radians(p.face_angle)
    slope = logo_height + 2 * p.logo_margin
    run, rise = slope * math.cos(angle), slope * math.sin(angle)
    height = p.lip + rise

    # Footprint of the pocket on the top face: half width, and reach to the front and back of its lowest point.
    if p.shape == "cube":
        if not 0 < p.pocket_depth <= p.cube_size / math.sqrt(3):
            raise ValueError("pocket_depth must be positive and at most cube_size / sqrt(3)")
        # Seen from above, a cube corner cut at `pocket_depth` is an equilateral triangle.
        rim_radius = p.pocket_depth * math.sqrt(2)
        pocket_half, pocket_front, pocket_back = rim_radius * math.sqrt(3) / 2, rim_radius / 2, rim_radius
    elif p.shape == "sphere":
        if not 0 < p.pocket_depth <= p.sphere_diameter / 2:
            raise ValueError("pocket_depth must be positive and at most half of sphere_diameter")
        dish_radius = p.sphere_diameter / 2 + p.sphere_clearance
        pocket_half = pocket_front = pocket_back = math.sqrt(p.pocket_depth * (2 * dish_radius - p.pocket_depth))
    else:
        raise ValueError(f"unknown shape {p.shape!r}; use one of {sorted(PRESETS)}")
    if height - p.pocket_depth < 2:
        raise ValueError("pocket would cut through the floor; reduce pocket_depth or enlarge the logo")

    top_half = max(p.logo_width / 2 + p.logo_margin + p.corner_radius, pocket_half + p.pocket_margin)
    top_depth = pocket_front + pocket_back + 2 * p.pocket_margin
    depth = run + top_depth + p.draft
    bottom_half = top_half + p.draft

    # y = 0 is the front edge; the body is the convex hull of its outlines at three heights.
    bottom = (-bottom_half, bottom_half, 0, depth, p.corner_radius)
    top = (-top_half, top_half, run, run + top_depth, p.corner_radius)
    body = Manifold.hull_points(
        np.array(rounded_rect(*bottom, 0) + rounded_rect(*bottom, p.lip) + rounded_rect(*top, height))
    )

    # The puzzle's axes stand on a body diagonal, so a cube rests on one corner with a flat side to the front.
    k = math.sqrt(2 / 3)
    axes = [(k * math.cos(math.radians(a)), k * math.sin(math.radians(a)), 1 / math.sqrt(3)) for a in (90, 210, 330)]
    lowest = np.array((0, run + p.pocket_margin + pocket_front, height - p.pocket_depth))
    if p.shape == "cube":
        centre = lowest + (0, 0, p.cube_size * math.sqrt(3) / 2)
        puzzle = Manifold.cube([p.cube_size] * 3, True)
        body -= Manifold.cube([2 * p.cube_size] * 3).transform(np.column_stack(axes + [lowest]))
    else:
        centre = lowest + (0, 0, p.sphere_diameter / 2)
        puzzle = Manifold.sphere(p.sphere_diameter / 2, 128)
        body -= Manifold.sphere(dish_radius, 256).translate(lowest + (0, 0, dish_radius))

    # Logo frame: x across the face, y up the slope, z out of the face.
    face = np.column_stack(
        [
            (1, 0, 0),
            (0, math.cos(angle), math.sin(angle)),
            (0, -math.sin(angle), math.cos(angle)),
            (0, run / 2, p.lip + rise / 2),
        ]
    )
    centre_xy = (0, -depth / 2, 0)
    blank = body.translate(centre_xy)
    puzzle_frame = np.column_stack(axes + [centre + centre_xy])
    # A logo outline extruded up from z = 0 lands in the face, sunk `inlay` deep.
    seat = face.copy()
    seat[:, 3] += centre_xy - p.inlay * face[:, 2]
    thickness = p.inlay + p.emboss

    used = CrossSection()
    for color in logo:
        logo[color] -= used  # keep the colours from overlapping each other
        used += logo[color]

    solid, body, parts = blank, blank, []
    for color, outline in logo.items():
        raised = outline.extrude(thickness).transform(seat)
        solid += raised
        body -= raised
        parts.append((f"logo_{color.lstrip('#')}", color, raised))
    parts.insert(0, ("body", p.body_color, body))

    # Separately printed logo: every piece gets a recess with clearance all round.
    pieces = split_logo(logo, p)
    recess = reduce(add, (grow(footprint, p.inlay_clearance) for _, footprint, _ in pieces))
    recess = grow(grow(recess, p.min_wall / 2), -p.min_wall / 2)  # walls too thin to print are left out
    inlay_body = (blank - recess.extrude(2 * thickness).transform(seat)).simplify(TOLERANCE)
    inlay_pieces = []
    for color in logo:
        flat = Manifold()
        for _, footprint, flush in (piece for piece in pieces if piece[0] == color):
            flat += footprint.extrude(thickness)
            if not flush.is_empty():  # cut down to face level where the shared base shows
                flat -= flush.extrude(thickness).translate((0, 0, p.inlay))
        inlay_pieces.append((f"logo_{color.lstrip('#')}", color, flat.simplify(TOLERANCE)))

    size = (2 * bottom_half, depth, height)
    return Stand(solid, parts, inlay_body, inlay_pieces, seat, puzzle.transform(puzzle_frame), puzzle_frame, size)


def to_trimesh(solid: Manifold) -> trimesh.Trimesh:
    mesh = solid.to_mesh()
    return trimesh.Trimesh(np.asarray(mesh.vert_properties)[:, :3], np.asarray(mesh.tri_verts), process=False)


def write_3mf(path: Path, name: str, parts: list[tuple[str, str, Manifold]]) -> None:
    """Write the parts as one object called `name` with a named, coloured component per part."""
    materials, objects, components = [], [], []
    for index, (part, color, solid) in enumerate(parts):
        mesh = to_trimesh(solid)
        vertices = "".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in mesh.vertices)
        triangles = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh.faces)
        materials.append(f'<base name="{part}" displaycolor="{color.upper()}"/>')
        objects.append(
            f'<object id="{index + 2}" type="model" name="{part}" pid="1" pindex="{index}">'
            f"<mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles></mesh></object>"
        )
        components.append(f'<component objectid="{index + 2}"/>')
    assembly = len(parts) + 2
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'<resources><basematerials id="1">{"".join(materials)}</basematerials>{"".join(objects)}'
        f'<object id="{assembly}" type="model" name="{name}"><components>{"".join(components)}</components></object>'
        f'</resources><build><item objectid="{assembly}"/></build></model>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        for entry, text in (("[Content_Types].xml", content_types), ("_rels/.rels", rels), ("3D/3dmodel.model", model)):
            # A fixed timestamp keeps regenerated files byte-identical.
            info = zipfile.ZipInfo(entry, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, text, zipfile.ZIP_DEFLATED)


def parse_args() -> tuple[Params, Path, Path]:
    # The shape is read first because it decides the defaults of the other options.
    shape_option = dict(choices=sorted(PRESETS), default="cube", help="puzzle to hold; sets the defaults below")
    shape_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    shape_parser.add_argument("--shape", **shape_option)
    preset = PRESETS[shape_parser.parse_known_args()[0].shape]

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--logo", type=Path, default=ROOT / "logo" / "SYBIKO.svg", help="SVG logo")
    parser.add_argument("--out", type=Path, default=ROOT / "output", help="output directory")
    parser.add_argument("--shape", **shape_option)
    for field in fields(Params):
        if field.name == "shape":
            continue
        kind = str if field.type == "str" else float
        option = f"--{field.name.replace('_', '-')}"
        parser.add_argument(option, type=kind, default=getattr(preset, field.name), help="default: %(default)s")
    args = vars(parser.parse_args())
    logo, out = args.pop("logo"), args.pop("out")
    return Params(**args), logo, out


def main() -> None:
    params, logo, out = parse_args()
    stand = build(params, logo)
    out.mkdir(parents=True, exist_ok=True)

    name = f"{params.shape}_stand"
    to_trimesh(stand.solid).export(out / f"{name}_single_color.stl")
    write_3mf(out / f"{name}_multicolor.3mf", name, stand.parts)
    # One file per filament colour: the stand with its recesses, then each colour's logo pieces.
    by_color = [(params.body_color, stand.inlay_body)] + [(color, flat) for _, color, flat in stand.inlay_pieces]
    labels = color_labels([color for color, _ in by_color])
    for label, (_, mesh) in zip(labels, by_color):
        to_trimesh(mesh).export(out / f"{name}_{label}.stl")
        print(f"{name}_{label}.stl: {len(mesh.decompose())} piece(s)")

    print("size (mm): %.1f x %.1f x %.1f" % stand.size)
    print("volume (cm3): %.1f" % (stand.solid.volume() / 1000))
    print(f"wrote {len(by_color) + 2} files to {out}")


if __name__ == "__main__":
    main()
