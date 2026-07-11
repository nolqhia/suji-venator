#!/usr/bin/env python3
"""
crop-adf.py - DS-571W (ADF) スキャン用 余白除去ツール

crop/ に処理対象ファイル (1冊分) を配置して python crop-adf.py を実行。
処理結果は output/ に PNG で出力。
"""

from crop_core import ScannerProfile, run

PROFILE = ScannerProfile(
    name="DS-571W (ADF)",
    shadow_range_low=50,
    shadow_range_high=12,
    shadow_dip_threshold=30,
    shadow_dip_search=30,
    enable_tilt=False,
    min_tilt_deg=0.1,
    enable_streak=True,
    streak_threshold=4,
    streak_strip_width=60,
    streak_min_length_ratio=0.15,
    adf_margin_px=8,
    enable_ratio_check=True,
    ratio_sigma=3.0,
    color_dist_threshold=25.0,
)

if __name__ == "__main__":
    run(PROFILE, "crop-adf.py - DS-571W 余白除去ツール")
