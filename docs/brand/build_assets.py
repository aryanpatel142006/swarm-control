"""Generate the swarm icon and banner set. Writes SVGs here and PNGs into swarm/assets/ (shipped with the package).

Run: python docs/brand/build_assets.py   (needs Google Chrome for rasterizing)
"""
from __future__ import annotations

import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent.parent / "swarm" / "assets"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

NAVY, NAVY2 = "#1b2140", "#2a3a7a"
PALETTE = {  # accent, accent-light, label
    "board": ("#f0a22b", "#ffd76a", "Swarm"),
    "tasks": ("#f5b942", "#ffe9a8", "Tasks"),
    "questions": ("#ff7a6b", "#ffb3a8", "Questions"),
    "agents": ("#3ecfb2", "#a8f0e2", "Agents"),
    "status": ("#9b8cff", "#d2c9ff", "Status"),
}


def defs(accent: str, light: str) -> str:
    return f"""<defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{NAVY}"/><stop offset="1" stop-color="{NAVY2}"/></linearGradient>
    <radialGradient id="node" cx="0.4" cy="0.35" r="0.7"><stop offset="0" stop-color="{light}"/><stop offset="1" stop-color="{accent}"/></radialGradient>
  </defs>"""


def swarm_mark(cx: float, cy: float, s: float, accent: str) -> str:
    """Hub with six nodes, scaled by s (1.0 = 512px design)."""
    pts = [(0, -138), (120, -68), (120, 70), (0, 138), (-120, 70), (-120, -68)]
    radii = [30, 24, 32, 22, 28, 34]
    out = [f'<g stroke="{accent}" stroke-opacity="0.45" stroke-width="{10 * s}" stroke-linecap="round">']
    out += [f'<line x1="{cx}" y1="{cy}" x2="{cx + x * s}" y2="{cy + y * s}"/>' for x, y in pts]
    out.append("</g>")
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{138 * s}" fill="none" stroke="{accent}" stroke-opacity="0.18" stroke-width="{6 * s}"/>')
    out += [f'<circle cx="{cx + x * s}" cy="{cy + y * s}" r="{r * s}" fill="url(#node)"/>' for (x, y), r in zip(pts, radii)]
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{58 * s}" fill="url(#node)"/>')
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{58 * s}" fill="none" stroke="{NAVY}" stroke-width="{8 * s}"/>')
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{22 * s}" fill="{NAVY}"/>')
    return "\n".join(out)


def glyph(kind: str, accent: str) -> str:
    """Centered 512-space glyph for each database icon."""
    if kind == "board":
        return swarm_mark(256, 256, 1.0, accent)
    if kind == "tasks":  # three cards, each with a node dot, like a kanban column
        cards = ""
        for i, w in enumerate((300, 240, 270)):
            y = 118 + i * 100
            cards += (f'<rect x="{106}" y="{y}" width="{w}" height="72" rx="18" fill="{accent}" fill-opacity="0.16" stroke="{accent}" stroke-width="8"/>'
                      f'<circle cx="{146}" cy="{y + 36}" r="18" fill="url(#node)"/>'
                      f'<rect x="{182}" y="{y + 26}" width="{w - 110}" height="20" rx="10" fill="{accent}" fill-opacity="0.55"/>')
        return cards
    if kind == "questions":  # a node with a question mark, and two small nodes waiting
        return (f'<circle cx="256" cy="232" r="132" fill="url(#node)"/>'
                f'<circle cx="256" cy="232" r="132" fill="none" stroke="{NAVY}" stroke-width="10"/>'
                f'<text x="256" y="292" text-anchor="middle" font-family="Helvetica Neue, Helvetica, Arial, sans-serif" '
                f'font-size="190" font-weight="700" fill="{NAVY}">?</text>'
                f'<circle cx="150" cy="400" r="26" fill="url(#node)"/><circle cx="362" cy="400" r="26" fill="url(#node)"/>'
                f'<line x1="176" y1="400" x2="336" y2="400" stroke="{accent}" stroke-opacity="0.5" stroke-width="10" stroke-linecap="round"/>')
    if kind == "agents":  # three agents in a triangle, all linked
        pts = [(256, 130), (140, 330), (372, 330)]
        lines = "".join(f'<line x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" stroke="{accent}" stroke-opacity="0.5" stroke-width="12" stroke-linecap="round"/>'
                        for a, b in ((pts[0], pts[1]), (pts[1], pts[2]), (pts[2], pts[0])))
        nodes = "".join(f'<circle cx="{x}" cy="{y}" r="58" fill="url(#node)"/><circle cx="{x}" cy="{y}" r="58" fill="none" stroke="{NAVY}" stroke-width="8"/>'
                        f'<circle cx="{x - 16}" cy="{y - 6}" r="9" fill="{NAVY}"/><circle cx="{x + 16}" cy="{y - 6}" r="9" fill="{NAVY}"/>'
                        f'<rect x="{x - 18}" y="{y + 14}" width="36" height="8" rx="4" fill="{NAVY}"/>' for x, y in pts)
        return lines + nodes
    if kind == "status":  # rising bars with a node on the tallest
        bars = ""
        for i, h in enumerate((110, 180, 250)):
            x = 122 + i * 96
            bars += f'<rect x="{x}" y="{400 - h}" width="76" height="{h}" rx="18" fill="{accent}" fill-opacity="{0.35 + i * 0.3}"/>'
        return bars + '<circle cx="352" cy="118" r="34" fill="url(#node)"/>'
    raise ValueError(kind)


def icon_svg(kind: str) -> str:
    accent, light, _ = PALETTE[kind]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">{defs(accent, light)}'
            f'<rect width="512" height="512" rx="112" fill="url(#bg)"/>{glyph(kind, accent)}</svg>')


def banner_svg(kind: str) -> str:
    accent, light, label = PALETTE[kind]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1500 500" width="1500" height="500">{defs(accent, light)}'
            f'<rect width="1500" height="500" fill="url(#bg)"/>'
            f'<circle cx="1320" cy="80" r="360" fill="{accent}" fill-opacity="0.07"/>'
            f'<circle cx="180" cy="520" r="300" fill="{accent}" fill-opacity="0.06"/>'
            f'<g transform="translate(300 250) scale(0.62) translate(-256 -256)">{glyph(kind, accent)}</g>'
            f'<text x="560" y="228" font-family="Helvetica Neue, Helvetica, Arial, sans-serif" font-size="44" font-weight="600" '
            f'letter-spacing="14" fill="{accent}">SWARM CONTROL</text>'
            f'<text x="556" y="330" font-family="Helvetica Neue, Helvetica, Arial, sans-serif" font-size="112" font-weight="700" '
            f'fill="#ffffff">{label}</text></svg>')


def render(svg_path: Path, png_path: Path, w: int, h: int) -> None:
    html = svg_path.with_suffix(".html")
    html.write_text(f'<!doctype html><html><head><meta charset="utf-8"><style>html,body{{margin:0;background:transparent}}'
                    f'img{{display:block}}</style></head><body><img src="{svg_path.name}" width="{w}" height="{h}"></body></html>')
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--default-background-color=00000000",
                    f"--window-size={w},{h}", f"--screenshot={png_path}", f"file://{html}"],
                   check=True, capture_output=True)
    html.unlink()


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    for kind in PALETTE:
        isvg, bsvg = HERE / f"icon-{kind}.svg", HERE / f"banner-{kind}.svg"
        isvg.write_text(icon_svg(kind))
        bsvg.write_text(banner_svg(kind))
        render(isvg, ASSETS / f"icon-{kind}.png", 512, 512)
        render(bsvg, ASSETS / f"banner-{kind}.png", 1500, 500)
        print("built", kind)


if __name__ == "__main__":
    main()
