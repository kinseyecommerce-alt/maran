#!/usr/bin/env python3
"""Generate launcher icons for the Android project.

Run from algotrader_v4/android/:
    python generate_android_icons.py

Requires: matplotlib (in requirements.txt), Pillow optional.
Produces mipmap-*/ic_launcher.png at correct densities.
"""
import sys
from pathlib import Path

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.patches import FancyBboxPatch
except ImportError:
    sys.exit("matplotlib required: pip install matplotlib")

DENSITIES = {
    'mipmap-mdpi':    48,
    'mipmap-hdpi':    72,
    'mipmap-xhdpi':   96,
    'mipmap-xxhdpi':  144,
    'mipmap-xxxhdpi': 192,
}

BASE = Path(__file__).parent / 'app' / 'src' / 'main' / 'res'


def _draw(ax):
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.set_aspect('equal')
    ax.axis('off')
    ax.add_patch(mpatches.Rectangle((0, 0), 100, 100, color='#111827', zorder=0))
    ax.add_patch(FancyBboxPatch((10, 10), 80, 80,
                                boxstyle="round,pad=0,rounding_size=20",
                                facecolor='#7C3AED', edgecolor='none', zorder=1))
    # Candlestick bars
    for x, lo, hi in [(22,38,50),(36,44,58),(50,40,52),(64,50,64),(78,58,70)]:
        ax.add_patch(mpatches.Rectangle((x, lo), 7, hi - lo,
                                        facecolor='white', edgecolor='none', zorder=3))
        ax.plot([x+3.5, x+3.5], [hi, hi+6], color='white', lw=1.5, zorder=3)
        ax.plot([x+3.5, x+3.5], [lo-4, lo], color='white', lw=1.5, zorder=3)
    # "A" letterform suggestion — just label
    ax.text(50, 18, 'AT', ha='center', va='center',
            fontsize=9, color='white', alpha=0.7, fontweight='bold',
            fontfamily='DejaVu Sans', zorder=4)


def generate(folder: str, size: int):
    dpi = 96
    fs = size / dpi
    fig, ax = plt.subplots(figsize=(fs, fs), dpi=dpi)
    fig.patch.set_facecolor('#111827')
    plt.subplots_adjust(left=0, right=1, top=1, bottom=0)
    _draw(ax)
    out = BASE / folder / 'ic_launcher.png'
    fig.savefig(str(out), dpi=dpi, bbox_inches='tight', pad_inches=0, facecolor='#111827')
    plt.close(fig)
    # Also save round variant
    out_r = BASE / folder / 'ic_launcher_round.png'
    import shutil
    shutil.copy(out, out_r)
    print(f'[OK] {folder}/ic_launcher.png ({size}px)')


if __name__ == '__main__':
    for folder, size in DENSITIES.items():
        generate(folder, size)
    print('Android launcher icons generated.')
