#!/usr/bin/env python3
"""Parametric Rubik's cube display stand with an SVG logo on its front face.

The cube rests on one corner in a three-sided pocket; the logo is raised on a
slanted plaque in front of it. Run `python stand.py --help` for the parameters.
"""
from __future__ import annotations

import argparse
import math
import zipfile
from dataclasses import dataclass, fields
from pathlib import Path

import numpy as np
import svgelements as se
import trimesh
from manifold3d import CrossSection, FillRule, Manifold

ROOT = Path(__file__).resolve().parent


@dataclass
class Params:
    """All lengths in millimetres, angles in degrees."""

    cube_size: float = 57.0  # edge of the cube (standard 3x3 is 56-57 mm)
    pocket_depth: float = 17.0  # vertical depth of the corner pocket
    pocket_margin: float = 4.0  # flat rim left around the pocket
    logo_width: float = 66.0  # logo height follows from its aspect ratio
    logo_margin: float = 4.0  # clear border around the logo
    face_angle: float = 45.0  # tilt of the logo face from horizontal
    lip: float = 3.0  # vertical front edge below the logo face
    draft: float = 3.0  # how far the sides and back lean inward
    corner_radius: float = 4.0  # rounding of the vertical corners
    emboss: float = 0.6  # how far the logo stands proud of the face
    inlay: float = 0.8  # how deep the coloured logo parts sink into the body
    body_color: str = "#ECECEC"  # display colour only, used in the 3MF


@dataclass
class Stand:
    solid: Manifold  # body and logo fused, for single-colour printing
    parts: list[tuple[str, str, Manifold]]  # (name, hex colour, mesh), non-overlapping
    cube_frame: np.ndarray  # 3x4 transform placing a unit-axis cube corner in the pocket
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

    if not 0 < p.pocket_depth <= p.cube_size / math.sqrt(3):
        raise ValueError("pocket_depth must be positive and at most cube_size / sqrt(3)")
    if height - p.pocket_depth < 2:
        raise ValueError("pocket would cut through the floor; reduce pocket_depth or enlarge the logo")

    # Seen from above, a cube corner cut at `pocket_depth` is an equilateral triangle.
    rim_radius = p.pocket_depth * math.sqrt(2)
    top_half = p.logo_width / 2 + p.logo_margin + p.corner_radius
    top_half = max(top_half, rim_radius * math.sqrt(3) / 2 + p.pocket_margin)
    top_depth = 1.5 * rim_radius + 2 * p.pocket_margin
    depth = run + top_depth + p.draft
    bottom_half = top_half + p.draft

    # y = 0 is the front edge; the body is the convex hull of its outlines at three heights.
    bottom = (-bottom_half, bottom_half, 0, depth, p.corner_radius)
    top = (-top_half, top_half, run, run + top_depth, p.corner_radius)
    body = Manifold.hull_points(
        np.array(rounded_rect(*bottom, 0) + rounded_rect(*bottom, p.lip) + rounded_rect(*top, height))
    )

    # The pocket is the corner of a cube standing on its body diagonal, one flat side to the front.
    k = math.sqrt(2 / 3)
    edges = [(k * math.cos(math.radians(a)), k * math.sin(math.radians(a)), 1 / math.sqrt(3)) for a in (90, 210, 330)]
    apex = (0, run + p.pocket_margin + rim_radius / 2, height - p.pocket_depth)
    cube_frame = np.column_stack(edges + [apex])
    body -= Manifold.cube([2 * p.cube_size] * 3).transform(cube_frame)

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
    body = body.translate(centre_xy)
    cube_frame[:, 3] += centre_xy

    solid, parts, used = body, [], CrossSection()
    for color, outline in logo.items():
        outline -= used  # keep the colour parts from overlapping each other
        used += outline
        raised = outline.extrude(p.inlay + p.emboss).translate((0, 0, -p.inlay))
        raised = raised.transform(face).translate(centre_xy)
        solid += raised
        body -= raised
        parts.append((f"logo_{color.lstrip('#')}", color, raised))
    parts.insert(0, ("body", p.body_color, body))

    return Stand(solid, parts, cube_frame, (2 * bottom_half, depth, height))


def to_trimesh(solid: Manifold) -> trimesh.Trimesh:
    mesh = solid.to_mesh()
    return trimesh.Trimesh(np.asarray(mesh.vert_properties)[:, :3], np.asarray(mesh.tri_verts), process=False)


def write_3mf(path: Path, parts: list[tuple[str, str, Manifold]]) -> None:
    """Write the parts as one object with a named, coloured component per part."""
    materials, objects, components = [], [], []
    for index, (name, color, solid) in enumerate(parts):
        mesh = to_trimesh(solid)
        vertices = "".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in mesh.vertices)
        triangles = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh.faces)
        materials.append(f'<base name="{name}" displaycolor="{color.upper()}"/>')
        objects.append(
            f'<object id="{index + 2}" type="model" name="{name}" pid="1" pindex="{index}">'
            f"<mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles></mesh></object>"
        )
        components.append(f'<component objectid="{index + 2}"/>')
    assembly = len(parts) + 2
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'<resources><basematerials id="1">{"".join(materials)}</basematerials>{"".join(objects)}'
        f'<object id="{assembly}" type="model" name="cube_stand"><components>{"".join(components)}</components></object>'
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
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("3D/3dmodel.model", model)


def parse_args() -> tuple[Params, Path, Path]:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--logo", type=Path, default=ROOT / "logo" / "SYBIKO.svg", help="SVG logo")
    parser.add_argument("--out", type=Path, default=ROOT / "output", help="output directory")
    for field in fields(Params):
        kind = str if field.name == "body_color" else float
        parser.add_argument(f"--{field.name.replace('_', '-')}", type=kind, default=field.default, help=" ")
    args = vars(parser.parse_args())
    logo, out = args.pop("logo"), args.pop("out")
    return Params(**args), logo, out


def main() -> None:
    params, logo, out = parse_args()
    stand = build(params, logo)
    out.mkdir(parents=True, exist_ok=True)

    to_trimesh(stand.solid).export(out / "cube_stand.stl")
    for name, _, solid in stand.parts:
        to_trimesh(solid).export(out / f"cube_stand_{name}.stl")
    write_3mf(out / "cube_stand_multicolor.3mf", stand.parts)

    print("size (mm): %.1f x %.1f x %.1f" % stand.size)
    print("volume (cm3): %.1f" % (stand.solid.volume() / 1000))
    print(f"wrote {len(stand.parts) + 2} files to {out}")


if __name__ == "__main__":
    main()
