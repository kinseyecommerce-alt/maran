#!/usr/bin/env python3
"""Generate PWA icon PNGs for AlgoTrader Pro.

Run once from the algotrader_v4 directory:
    python static/generate_icons.py

Requires matplotlib (already in requirements.txt).
Outputs: static/icon-192.png  static/icon-512.png
"""
import math
import sys
from pathlib import Path

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.patches import FancyBboxPatch
except ImportError:
    sys.exit("matplotlib is required: pip install matplotlib")

OUT = Path(__file__).parent


def _draw_icon(ax, size):
    """Draw the AlgoTrader icon on the given axes."""
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.set_aspect('equal')
    ax.axis('off')

    # Dark background
    bg = mpatches.Rectangle((0, 0), 100, 100, color='#111827', zorder=0)
    ax.add_patch(bg)

    # Purple rounded-square card (safe zone = inner 80%)
    card = FancyBboxPatch(
        (10, 10), 80, 80,
        boxstyle="round,pad=0,rounding_size=18",
        facecolor='#7C3AED', edgecolor='none', zorder=1,
    )
    ax.add_patch(card)

    # Subtle inner glow (lighter purple strip at top)
    glow = FancyBboxPatch(
        (10, 72), 80, 18,
        boxstyle="round,pad=0,rounding_size=12",
        facecolor='#8B5CF6', edgecolor='none', zorder=2, alpha=0.6,
    )
    ax.add_patch(glow)

    # Candlestick chart (5 bars) — white
    candles = [
        (22, 38, 48, 44),   # x, body_lo, body_hi, wick_hi
        (34, 44, 56, 60),
        (46, 50, 42, 62),   # red candle (body_lo > body_hi)
        (58, 42, 58, 63),
        (70, 56, 68, 72),
    ]
    bar_w = 7
    for (x, b_lo, b_hi, wick_hi) in candles:
        body_lo = min(b_lo, b_hi)
        body_hi = max(b_lo, b_hi)
        is_red = b_lo > b_hi
        color = '#F87171' if is_red else '#FFFFFF'
        # Wick
        ax.plot([x + bar_w / 2, x + bar_w / 2], [body_lo, wick_hi],
                color=color, linewidth=1.5, zorder=3)
        # Body
        body = mpatches.Rectangle(
            (x, body_lo), bar_w, body_hi - body_lo,
            facecolor=color, edgecolor='none', zorder=4,
        )
        ax.add_patch(body)

    # White lightning bolt (top-right corner)
    bolt_x = [75, 68, 72, 62, 76, 68, 72]
    bolt_y = [78, 68, 68, 55, 55, 65, 65]
    ax.fill(bolt_x, bolt_y, color='white', zorder=5, alpha=0.9)

    # "AT" monogram text at bottom
    ax.text(50, 22, 'AlgoTrader', ha='center', va='center',
            fontsize=7 if size >= 512 else 6,
            color='white', alpha=0.75, fontweight='bold',
            fontfamily='DejaVu Sans', zorder=5)


def generate(size: int):
    dpi = 96
    figsize = size / dpi
    fig, ax = plt.subplots(figsize=(figsize, figsize), dpi=dpi)
    fig.patch.set_facecolor('#111827')
    plt.subplots_adjust(left=0, right=1, top=1, bottom=0)
    _draw_icon(ax, size)
    out_path = OUT / f'icon-{size}.png'
    fig.savefig(str(out_path), dpi=dpi, bbox_inches='tight', pad_inches=0,
                facecolor='#111827')
    plt.close(fig)
    print(f'[OK] {out_path} ({size}×{size})')


if __name__ == '__main__':
    generate(192)
    generate(512)
    print('Icons generated successfully.')
