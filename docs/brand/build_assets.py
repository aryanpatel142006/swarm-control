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


def render_banner(kind: str, png_path: Path) -> None:
    """Banners are generative canvas art (docs/brand/banner.html) drawn at 2400×800 for retina covers."""
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--window-size=2400,800", "--virtual-time-budget=15000", f"--screenshot={png_path}",
                    f"file://{HERE / 'banner.html'}?kind={kind}"], check=True, capture_output=True)


# ---------- small row icons: one per task type, question kind, and agent ----------
GOLD, VIOLET, CORAL, TEAL = "#f5b942", "#a893ff", "#ff8570", "#4fdcc4"
ROW_ICONS = {  # name: (accent, glyph builder)
    "type-frontend":    (GOLD,   lambda a: f'<rect x="120" y="140" width="272" height="232" rx="26" fill="none" stroke="{a}" stroke-width="22"/><rect x="120" y="140" width="272" height="60" rx="26" fill="{a}"/><rect x="150" y="230" width="90" height="112" rx="14" fill="{a}" fill-opacity="0.5"/>'),
    "type-backend":     (GOLD,   lambda a: f'<rect x="120" y="120" width="272" height="80" rx="20" fill="{a}"/><rect x="120" y="216" width="272" height="80" rx="20" fill="{a}" fill-opacity="0.7"/><rect x="120" y="312" width="272" height="80" rx="20" fill="{a}" fill-opacity="0.45"/><circle cx="160" cy="160" r="14" fill="#1b2140"/><circle cx="160" cy="256" r="14" fill="#1b2140"/><circle cx="160" cy="352" r="14" fill="#1b2140"/>'),
    "type-realtime":    (GOLD,   lambda a: f'<polygon points="292,96 150,292 250,292 220,416 362,220 262,220" fill="{a}"/>'),
    "type-ml_audio":    (VIOLET, lambda a: "".join(f'<rect x="{112+i*40}" y="{256-h/2}" width="22" height="{h}" rx="11" fill="{a}" fill-opacity="{0.55+0.45*(i%2)}"/>' for i, h in enumerate([60, 140, 220, 300, 200, 120, 240, 80]))),
    "type-ml_vision":   (VIOLET, lambda a: f'<path d="M80 256 C160 130, 352 130, 432 256 C352 382, 160 382, 80 256 Z" fill="none" stroke="{a}" stroke-width="24"/><circle cx="256" cy="256" r="70" fill="{a}"/><circle cx="256" cy="256" r="30" fill="#1b2140"/>'),
    "type-ml_fusion":   (VIOLET, lambda a: f'<circle cx="150" cy="180" r="46" fill="{a}"/><circle cx="362" cy="180" r="46" fill="{a}"/><circle cx="256" cy="352" r="46" fill="{a}"/><path d="M150 180 L362 180 L256 352 Z" fill="none" stroke="{a}" stroke-width="18" stroke-opacity="0.6"/>'),
    "type-eval":        (VIOLET, lambda a: f'<circle cx="256" cy="256" r="150" fill="none" stroke="{a}" stroke-width="20"/><circle cx="256" cy="256" r="92" fill="none" stroke="{a}" stroke-width="20" stroke-opacity="0.6"/><circle cx="256" cy="256" r="36" fill="{a}"/>'),
    "type-tests":       (CORAL,  lambda a: f'<path d="M206 96 L306 96 L306 200 L392 372 C410 408, 386 430, 350 430 L162 430 C126 430, 102 408, 120 372 L206 200 Z" fill="none" stroke="{a}" stroke-width="22" stroke-linejoin="round"/><path d="M150 330 L362 330 L392 372 C410 408, 386 430, 350 430 L162 430 C126 430, 102 408, 120 372 Z" fill="{a}"/>'),
    "type-bugfix":      (CORAL,  lambda a: f'<ellipse cx="256" cy="286" rx="110" ry="130" fill="{a}"/><circle cx="256" cy="150" r="56" fill="{a}"/><path d="M146 240 L86 200 M146 300 L80 310 M146 360 L96 410 M366 240 L426 200 M366 300 L432 310 M366 360 L416 410" stroke="{a}" stroke-width="20" stroke-linecap="round"/><path d="M256 180 L256 400" stroke="#1b2140" stroke-width="16"/>'),
    "type-docs":        (TEAL,   lambda a: f'<rect x="136" y="96" width="240" height="320" rx="24" fill="none" stroke="{a}" stroke-width="22"/>' + "".join(f'<rect x="176" y="{160+i*54}" width="{160 if i<3 else 90}" height="20" rx="10" fill="{a}" fill-opacity="0.8"/>' for i in range(4))),
    "type-research":    (TEAL,   lambda a: f'<circle cx="222" cy="222" r="120" fill="none" stroke="{a}" stroke-width="26"/><path d="M310 310 L420 420" stroke="{a}" stroke-width="34" stroke-linecap="round"/>'),
    "type-integration": (GOLD,   lambda a: f'<path d="M110 200 L200 200 L200 150 C200 110, 260 110, 260 150 L260 200 L360 200 L360 290 C400 290, 400 350, 360 350 L360 420 L260 420 L260 380 C260 340, 200 340, 200 380 L200 420 L110 420 Z" fill="{a}"/>'),
    "type-infra":       (GOLD,   lambda a: f'<rect x="110" y="110" width="292" height="92" rx="18" fill="{a}"/><rect x="110" y="210" width="292" height="92" rx="18" fill="{a}" fill-opacity="0.75"/><rect x="110" y="310" width="292" height="92" rx="18" fill="{a}" fill-opacity="0.5"/>' + "".join(f'<circle cx="{362}" cy="{156+i*100}" r="16" fill="#1b2140"/>' for i in range(3))),
    "q-blocking":       (CORAL,  lambda a: f'<circle cx="256" cy="256" r="160" fill="{a}"/><text x="256" y="318" text-anchor="middle" font-family="Helvetica Neue, Helvetica, Arial, sans-serif" font-size="210" font-weight="700" fill="#1b2140">?</text>'),
    "q-fyi":            (TEAL,   lambda a: f'<circle cx="256" cy="256" r="160" fill="{a}"/><circle cx="256" cy="176" r="24" fill="#1b2140"/><rect x="232" y="224" width="48" height="140" rx="16" fill="#1b2140"/>'),
    "agent":            (TEAL,   lambda a: f'<circle cx="256" cy="256" r="150" fill="none" stroke="{a}" stroke-width="10" stroke-opacity="0.35"/>' + "".join(f'<line x1="256" y1="256" x2="{256+int(150*__import__("math").cos(t))}" y2="{256+int(150*__import__("math").sin(t))}" stroke="{a}" stroke-width="10" stroke-opacity="0.5"/><circle cx="{256+int(150*__import__("math").cos(t))}" cy="{256+int(150*__import__("math").sin(t))}" r="26" fill="{a}"/>' for t in [i*1.0472 for i in range(6)]) + f'<circle cx="256" cy="256" r="60" fill="{a}"/><circle cx="256" cy="256" r="24" fill="#1b2140"/>'),
    "serve":            (VIOLET, lambda a: f'<path d="M120 300 A150 150 0 0 1 390 190" fill="none" stroke="{a}" stroke-width="22" stroke-linecap="round"/><path d="M160 340 A100 100 0 0 1 350 240" fill="none" stroke="{a}" stroke-width="22" stroke-linecap="round" stroke-opacity="0.7"/><circle cx="256" cy="330" r="40" fill="{a}"/><rect x="236" y="330" width="40" height="110" rx="14" fill="{a}"/>'),
}


def row_icon_svg(name: str) -> str:
    """Row icons are the bare glyph on a transparent background (no tile), enlarged to read at list size."""
    accent, glyph = ROW_ICONS[name]
    body = glyph(accent).replace("#1b2140", "#0e1330")   # the cut-outs stay dark on light and dark themes
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">'
            f'<g transform="translate(256 256) scale(1.3) translate(-256 -256)">{body}</g></svg>')


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    for kind in PALETTE:
        isvg = HERE / f"icon-{kind}.svg"
        isvg.write_text(icon_svg(kind))
        render(isvg, ASSETS / f"icon-{kind}.png", 512, 512)
        render_banner(kind, ASSETS / f"banner-{kind}.png")
        print("built", kind)
    for name in ROW_ICONS:
        svg = HERE / f"icon-{name}.svg"
        svg.write_text(row_icon_svg(name))
        render(svg, ASSETS / f"icon-{name}.png", 256, 256)
    print("built", len(ROW_ICONS), "row icons")


if __name__ == "__main__":
    main()
