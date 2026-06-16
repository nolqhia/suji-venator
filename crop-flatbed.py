#!/usr/bin/env python3
"""
crop-flatbed.py - GT-X830 (フラットベッド) スキャン用 余白除去ツール

crop/ に処理対象ファイルを配置して python crop-flatbed.py を実行。
処理結果は output/ に PNG で出力。
"""

from crop_core import ScannerProfile, run

PROFILE = ScannerProfile(
    name="GT-X830 (フラットベッド)",
    shadow_range_low=50,
    shadow_range_high=3,
    shadow_dip_threshold=20,
    shadow_dip_search=30,
    enable_tilt=True,
    min_tilt_deg=0.1,
    enable_streak=False,
    streak_threshold=10,
    streak_strip_width=60,
    streak_min_length_ratio=0.15,
    adf_margin_px=0,
    enable_ratio_check=False,
    ratio_sigma=2.0,
)

if __name__ == "__main__":
    run(PROFILE, "crop-flatbed.py - GT-X830 余白除去ツール")
