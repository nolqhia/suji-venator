"""
crop_core.py - スキャン余白除去 共通ロジック

crop-adf.py / crop-flatbed.py から呼び出される共通モジュール。
"""

import sys
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing

import cv2
import numpy as np

HAS_SCIPY = False
try:
    from scipy.ndimage import median_filter
    HAS_SCIPY = True
except ImportError:
    pass

SUPPORTED_EXTENSIONS = {".png", ".tif", ".tiff", ".bmp"}

# ============================================================
# データ構造
# ============================================================

@dataclass
class ScannerProfile:
    name: str
    shadow_range_low: float
    shadow_range_high: float
    shadow_dip_threshold: float
    shadow_dip_search: int
    enable_tilt: bool
    min_tilt_deg: float
    enable_streak: bool
    streak_threshold: float
    streak_strip_width: int
    streak_min_length_ratio: float
    adf_margin_px: int
    enable_ratio_check: bool
    ratio_sigma: float

@dataclass
class StreakInfo:
    """検出したスジの構造化情報"""
    orientation: str   # "horizontal" (横スジ) / "vertical" (縦スジ)
    side: str          # "left"/"right" (横スジ) または "top"/"bottom" (縦スジ)
    start: int         # 横スジ: 開始row / 縦スジ: 開始col
    end: int           # 横スジ: 終了row / 縦スジ: 終了col
    length: int        # スジの長さ (px)
    max_dev: float     # 最大輝度偏差

    def describe(self) -> str:
        if self.orientation == "horizontal":
            return (f"  横スジ: rows {self.start}-{self.end} "
                    f"(幅{self.end - self.start + 1}px, 長さ~{self.length}px, "
                    f"偏差{self.max_dev:.1f})")
        else:
            return (f"  縦スジ: cols {self.start}-{self.end} "
                    f"(幅{self.end - self.start + 1}px, 長さ~{self.length}px, "
                    f"偏差{self.max_dev:.1f})")


@dataclass
class EdgeResult:
    position: int
    detected: bool
    samples: list = field(default_factory=list)  # (走査位置, エッジ位置) のペア

@dataclass
class Edges:
    left: EdgeResult
    right: EdgeResult
    top: EdgeResult
    bottom: EdgeResult

@dataclass
class ProcessResult:
    input_name: str
    output_name: str
    output_path: str
    original_size: tuple
    cropped_size: tuple
    tilt_deg: float
    tilt_corrected: bool
    edges_detected: dict
    streaks: list
    aspect_ratio: float
    success: bool
    error_msg: str = ""
    log_lines: list = field(default_factory=list)

CORNER_SIZE = 40
OUTER_SKIP_PX = 15
SCAN_DEPTH = 500
N_SAMPLES = 16

# ============================================================
# グレースケール変換
# ============================================================

def to_gray(img: np.ndarray) -> np.ndarray:
    """検出用の 8-bit グレースケールに変換。
    アルファ付き (BGRA) と 16-bit 入力にも対応する。
    クロップ自体は元の img に対して行うため、bit深度・チャンネルは保持される。
    """
    if img.ndim == 3 and img.shape[2] == 4:
        gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    elif img.ndim == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img.copy()
    if gray.dtype == np.uint16:
        gray = (gray >> 8).astype(np.uint8)
    return gray

# ============================================================
# 背景色推定
# ============================================================

def estimate_background(gray: np.ndarray) -> float:
    h, w = gray.shape
    cs = min(CORNER_SIZE, h // 10, w // 10)
    corners = np.concatenate([
        gray[:cs, :cs].ravel(), gray[:cs, -cs:].ravel(),
        gray[-cs:, :cs].ravel(), gray[-cs:, -cs:].ravel(),
    ])
    return float(np.median(corners))

# ============================================================
# エッジ検出
# ============================================================

def _detect_edge_one_side(gray: np.ndarray, side: str,
                          bg_med: float, prof: ScannerProfile) -> EdgeResult:
    """1辺のエッジを検出。
    Stage 1: 外側から走査し影範囲外(=紙面)を見つける
    Stage 2: first_paper から外側方向にディップ(影の最深部)を探す
             ディップがあればそこでクロップ(影を完全除去)
    """
    h, w = gray.shape
    s_low = bg_med - prof.shadow_range_low
    s_high = bg_med + prof.shadow_range_high
    dip_thresh = bg_med - prof.shadow_dip_threshold
    dip_search = prof.shadow_dip_search
    depth = min(SCAN_DEPTH, (w if side in ("left", "right") else h) // 2)

    is_horiz = side in ("left", "right")
    sample_positions = np.linspace(
        (h if is_horiz else w) // 5,
        4 * (h if is_horiz else w) // 5,
        N_SAMPLES, dtype=int
    )

    samples = []
    for idx in sample_positions:
        line = gray[idx, :].astype(float) if is_horiz else gray[:, idx].astype(float)
        length = len(line)

        # Stage 1: 外側から走査し、影範囲外の最初のピクセルを見つける
        if side == "left":
            scan_range = range(OUTER_SKIP_PX, OUTER_SKIP_PX + depth)
        elif side == "right":
            scan_range = range(length - OUTER_SKIP_PX - 1,
                               length - OUTER_SKIP_PX - depth - 1, -1)
        elif side == "top":
            scan_range = range(OUTER_SKIP_PX, OUTER_SKIP_PX + depth)
        else:
            scan_range = range(length - OUTER_SKIP_PX - 1,
                               length - OUTER_SKIP_PX - depth - 1, -1)

        first_paper = None
        for i in scan_range:
            val = line[i]
            if val < s_low or val > s_high:
                first_paper = i
                break

        if first_paper is None:
            continue

        # Stage 2: 影ディップ補正 (RIGHT辺のみ)
        # DS-571W の CIS センサは右辺に深い影を生成する:
        #   bg(228) → recovery(200→170=first_paper) → dip(70) → paper(255)
        # first_paper が回復ゾーンに着地するため、ディップを経由して
        # 実際の紙面端を見つける必要がある。
        # 他の辺 (LEFT/TOP/BOTTOM) は影が浅く first_paper が紙面端なので不要。

        if side == "right":
            in_start = max(first_paper - dip_search, 0)
            in_window = line[in_start:first_paper]
            if len(in_window) > 0 and in_window.min() < dip_thresh:
                dip_pos = in_start + int(np.argmin(in_window))
                # ディップから紙面方向 (左) へ走査して紙面端を探す
                found = False
                for j in range(dip_pos - 1, max(dip_pos - dip_search, 0), -1):
                    if line[j] < s_low or line[j] > s_high:
                        samples.append((int(idx), j))
                        found = True
                        break
                if not found:
                    samples.append((int(idx), first_paper))
            else:
                samples.append((int(idx), first_paper))
        else:
            # LEFT/TOP/BOTTOM: first_paper をそのまま使用
            samples.append((int(idx), first_paper))

    if len(samples) >= N_SAMPLES * 0.4:
        positions = [p for _, p in samples]
        return EdgeResult(int(np.median(positions)), True, samples)

    fallback = {"left": 0, "right": w - 1, "top": 0, "bottom": h - 1}[side]
    return EdgeResult(fallback, False)


def detect_all_edges(gray: np.ndarray, bg_med: float,
                     prof: ScannerProfile) -> Edges:
    return Edges(
        left=_detect_edge_one_side(gray, "left", bg_med, prof),
        right=_detect_edge_one_side(gray, "right", bg_med, prof),
        top=_detect_edge_one_side(gray, "top", bg_med, prof),
        bottom=_detect_edge_one_side(gray, "bottom", bg_med, prof),
    )

# ============================================================
# 傾き推定
# ============================================================

def estimate_tilt(edges: Edges) -> float:
    angles = []
    for edge, is_vert in [
        (edges.left, True), (edges.right, True),
        (edges.top, False), (edges.bottom, False),
    ]:
        if not edge.detected or len(edge.samples) < 4:
            continue
        pts = np.array(edge.samples, dtype=float)
        indices, values = pts[:, 0], pts[:, 1]
        q1, q3 = np.percentile(values, [25, 75])
        iqr = q3 - q1
        mask = (values >= q1 - 1.5 * iqr) & (values <= q3 + 1.5 * iqr)
        if mask.sum() < 4:
            continue
        fit = np.polyfit(indices[mask], values[mask], 1)
        ang = np.degrees(np.arctan(fit[0]))
        # 紙が θ 回転すると縦エッジの dx/dy は -tanθ、横エッジの dy/dx は
        # +tanθ になる。符号を揃えないと median で打ち消し合う。
        angles.append(ang if is_vert else -ang)
    return float(np.median(angles)) if angles else 0.0

# ============================================================
# 回転
# ============================================================

def rotate_image(img: np.ndarray, angle_deg: float) -> np.ndarray:
    """TODO: 安定したら BORDER_REPLICATE に切り替え。"""
    h, w = img.shape[:2]
    center = (w / 2, h / 2)
    mat = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    cos_a, sin_a = abs(mat[0, 0]), abs(mat[0, 1])
    new_w = int(h * sin_a + w * cos_a)
    new_h = int(h * cos_a + w * sin_a)
    mat[0, 2] += (new_w - w) / 2
    mat[1, 2] += (new_h - h) / 2
    return cv2.warpAffine(
        img, mat, (new_w, new_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0) if len(img.shape) == 3 else 0,
    )

# ============================================================
# スジ検出
# ============================================================

def detect_streaks(gray: np.ndarray, prof: ScannerProfile) -> list[StreakInfo]:
    if not prof.enable_streak or not HAS_SCIPY:
        return []
    h, w = gray.shape
    found = []
    sw = prof.streak_strip_width
    th = prof.streak_threshold

    # 横スジ: 左右ストリップで検出
    for side, region in [
        ("left", gray[:, OUTER_SKIP_PX:OUTER_SKIP_PX + sw]),
        ("right", gray[:, w - OUTER_SKIP_PX - sw:w - OUTER_SKIP_PX]),
    ]:
        if region.size == 0 or region.shape[1] < 5:
            continue
        means = region.mean(axis=1).astype(np.float64)
        fs = min(101, max(3, len(means) // 4 * 2 + 1))
        baseline = median_filter(means, size=fs)
        dev = np.abs(baseline - means)
        rows = np.where(dev > th)[0]
        if len(rows) == 0:
            continue
        for g in np.split(rows, np.where(np.diff(rows) > 5)[0] + 1):
            if len(g) < 2:
                continue
            r_slice = gray[g[0]:g[-1] + 1, :]
            c_means = r_slice.mean(axis=0)
            ref_a = gray[max(0, g[0]-10):g[0], :].mean(axis=0) if g[0] > 10 \
                else np.full(w, means.mean())
            ref_b = gray[g[-1]+1:min(h, g[-1]+11), :].mean(axis=0) if g[-1] < h-11 \
                else np.full(w, means.mean())
            extent = np.where(np.abs(c_means - (ref_a+ref_b)/2) > th/2)[0]
            if len(extent) > w * prof.streak_min_length_ratio:
                found.append(StreakInfo(
                    orientation="horizontal", side=side,
                    start=int(g[0]), end=int(g[-1]),
                    length=int(len(extent)), max_dev=float(dev[g].max())))

    # 縦スジ: 上下ストリップで検出
    for side, region in [
        ("top", gray[OUTER_SKIP_PX:OUTER_SKIP_PX + sw, :]),
        ("bottom", gray[h - OUTER_SKIP_PX - sw:h - OUTER_SKIP_PX, :]),
    ]:
        if region.size == 0 or region.shape[0] < 5:
            continue
        means = region.mean(axis=0).astype(np.float64)
        fs = min(101, max(3, len(means) // 4 * 2 + 1))
        baseline = median_filter(means, size=fs)
        dev = np.abs(baseline - means)
        cols = np.where(dev > th)[0]
        if len(cols) == 0:
            continue
        for g in np.split(cols, np.where(np.diff(cols) > 5)[0] + 1):
            if len(g) < 2:
                continue
            c_slice = gray[:, g[0]:g[-1]+1]
            r_means = c_slice.mean(axis=1)
            ref_l = gray[:, max(0, g[0]-10):g[0]].mean(axis=1) if g[0] > 10 \
                else np.full(h, means.mean())
            ref_r = gray[:, g[-1]+1:min(w, g[-1]+11)].mean(axis=1) if g[-1] < w-11 \
                else np.full(h, means.mean())
            extent = np.where(np.abs(r_means - (ref_l+ref_r)/2) > th/2)[0]
            if len(extent) > h * prof.streak_min_length_ratio:
                found.append(StreakInfo(
                    orientation="vertical", side=side,
                    start=int(g[0]), end=int(g[-1]),
                    length=int(len(extent)), max_dev=float(dev[g].max())))

    return found


def draw_streak_arrows(img: np.ndarray, streaks: list[StreakInfo]) -> np.ndarray:
    """検出したスジに斜めの矢印を描画した画像を返す。
    矢印は内側 (本文側) から外側 (余白のスジ) に向ける。
    スジの線と重ならないよう斜め方向にずらして描く。
    """
    annotated = img.copy()
    if len(annotated.shape) == 2:
        annotated = cv2.cvtColor(annotated, cv2.COLOR_GRAY2BGR)
    h, w = annotated.shape[:2]

    RED = (0, 0, 255)  # BGR
    thickness = max(3, w // 700)
    tip = 0.25
    arrow_len = max(140, w // 11)   # 矢印の長さ (斜辺)
    gap = max(20, w // 150)         # スジから矢印先端までの隙間
    # 斜めにするためのオフセット (矢印の根元を主軸方向にずらす量)
    skew = arrow_len * 0.8

    for s in streaks:
        if s.orientation == "horizontal":
            row = (s.start + s.end) // 2
            if s.side == "left":
                # 左余白のスジ: 先端は左下、根元は右上 (斜め)
                x_tip = OUTER_SKIP_PX + gap
                tip_pt = (x_tip, row)
                tail_pt = (int(x_tip + arrow_len), int(row - skew))
            else:  # right
                x_tip = w - OUTER_SKIP_PX - gap
                tip_pt = (x_tip, row)
                tail_pt = (int(x_tip - arrow_len), int(row - skew))
            cv2.arrowedLine(annotated, tail_pt, tip_pt, RED, thickness, tipLength=tip)
        else:  # vertical
            col = (s.start + s.end) // 2
            if s.side == "top":
                y_tip = OUTER_SKIP_PX + gap
                tip_pt = (col, y_tip)
                tail_pt = (int(col - skew), int(y_tip + arrow_len))
            else:  # bottom
                y_tip = h - OUTER_SKIP_PX - gap
                tip_pt = (col, y_tip)
                tail_pt = (int(col - skew), int(y_tip - arrow_len))
            cv2.arrowedLine(annotated, tail_pt, tip_pt, RED, thickness, tipLength=tip)

    return annotated

# ============================================================
# 1ファイル処理
# ============================================================

def process_image(input_path_str: str, output_dir_str: str,
                  line_detected_dir_str: str, prof_dict: dict) -> ProcessResult:
    prof = ScannerProfile(**prof_dict)
    input_path = Path(input_path_str)
    output_dir = Path(output_dir_str)
    line_detected_dir = Path(line_detected_dir_str)
    filename = input_path.name
    stem = input_path.stem
    log = []

    img = cv2.imread(str(input_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        return ProcessResult(filename, "", "", (0,0), (0,0), 0, False,
                             {}, [], 0.0, False, "読み込み失敗")

    gray = to_gray(img)
    h, w = gray.shape
    cm = "カラー" if len(img.shape) == 3 else "グレースケール"
    log.append(f"処理中: {filename} ({w}x{h}, {cm})")

    bg_med = estimate_background(gray)
    log.append(f"  背景色: {bg_med:.0f}")

    edges = detect_all_edges(gray, bg_med, prof)
    edges_info = {}
    for name, e in [("左", edges.left), ("右", edges.right),
                    ("上", edges.top), ("下", edges.bottom)]:
        edges_info[name] = (e.detected, e.position)
        log.append(f"  {name}: {'pos=' + str(e.position) if e.detected else '検出不能'}")

    streaks = detect_streaks(gray, prof)
    has_streaks = len(streaks) > 0
    if has_streaks:
        log.append("  ⚠ スジ警告:")
        log.extend(s.describe() for s in streaks)
        # クロップ前の元画像に矢印を描画して line-detected/ に保存
        try:
            annotated = draw_streak_arrows(img, streaks)
            ld_path = line_detected_dir / f"{stem}.png"
            cv2.imwrite(str(ld_path), annotated)
            log.append(f"  スジ確認画像: line-detected/{stem}.png")
        except Exception as e:
            log.append(f"  [WARN] スジ確認画像の生成失敗: {e}")

    tilt = estimate_tilt(edges) if prof.enable_tilt else 0.0
    tilt_corrected = False
    if prof.enable_tilt and abs(tilt) >= prof.min_tilt_deg:
        log.append(f"  傾き補正: {tilt:.4f}°")
        img = rotate_image(img, tilt)
        gray = to_gray(img)
        edges = detect_all_edges(gray, bg_med, prof)
        tilt_corrected = True
        h2, w2 = gray.shape
        log.append(f"  再検出 (回転後 {w2}x{h2}):")
        for name, e in [("左", edges.left), ("右", edges.right),
                        ("上", edges.top), ("下", edges.bottom)]:
            edges_info[name] = (e.detected, e.position)
            if e.detected:
                log.append(f"    {name}: pos={e.position}")
    else:
        log.append(f"  傾き: {tilt:.4f}°{'' if prof.enable_tilt else ' (補正無効)'}")

    # クロップ
    hf, wf = gray.shape
    x1 = edges.left.position if edges.left.detected else 0
    x2 = edges.right.position if edges.right.detected else wf
    y1 = edges.top.position if edges.top.detected else 0
    y2 = edges.bottom.position if edges.bottom.detected else hf

    m = prof.adf_margin_px
    if m > 0:
        x1 += m; x2 -= m; y1 += m; y2 -= m
        log.append(f"  追加マージン: {m}px")

    if x2 <= x1 or y2 <= y1:
        return ProcessResult(filename, "", "", (w,h), (0,0), tilt, tilt_corrected,
                             edges_info, streaks, 0.0, False,
                             f"クロップ領域不正: ({x1},{y1})-({x2},{y2})", log)

    cropped = img[y1:y2, x1:x2]
    cw, ch = cropped.shape[1], cropped.shape[0]
    ar = cw / ch if ch > 0 else 0.0

    if has_streaks and not stem.endswith("-line"):
        out_stem = f"{stem}-line"
    else:
        out_stem = stem

    output_name = f"{out_stem}.png"
    output_path = output_dir / output_name
    cv2.imwrite(str(output_path), cropped)

    pct = (1 - (cw * ch) / (w * h)) * 100
    log.append(f"  出力: {output_name} ({cw}x{ch}, AR={ar:.4f}, -{pct:.1f}%)")

    return ProcessResult(filename, output_name, str(output_path), (w,h), (cw,ch),
                         tilt, tilt_corrected, edges_info, streaks, ar, True, "", log)

# ============================================================
# アス比・面積チェック
# ============================================================

def _rename_output(r: ProcessResult, suffix: str):
    old_path = Path(r.output_path)
    if not old_path.exists():
        return
    new_stem = old_path.stem
    if suffix not in new_stem:
        new_stem = f"{new_stem}{suffix}"
    new_path = old_path.parent / f"{new_stem}{old_path.suffix}"
    if new_path == old_path:
        return
    try:
        old_path.rename(new_path)
        r.output_name = new_path.name
        r.output_path = str(new_path)
    except OSError:
        pass


def check_outliers(results: list[ProcessResult], sigma: float) -> list[str]:
    successful = [r for r in results if r.success and r.aspect_ratio > 0]
    if len(successful) < 3:
        return []

    ratios = np.array([r.aspect_ratio for r in successful])
    areas = np.array([r.cropped_size[0] * r.cropped_size[1]
                      for r in successful], dtype=np.float64)

    mean_ar, std_ar = float(ratios.mean()), float(ratios.std())
    mean_area, std_area = float(areas.mean()), float(areas.std())

    log = []
    log.append(f"アス比: 平均={mean_ar:.4f}, σ={std_ar:.4f}, "
               f"範囲=[{mean_ar - sigma*std_ar:.4f}, {mean_ar + sigma*std_ar:.4f}]")
    log.append(f"面積: 平均={mean_area:.0f}, σ={std_area:.0f}, "
               f"範囲=[{mean_area - sigma*std_area:.0f}, {mean_area + sigma*std_area:.0f}]")

    outliers = []
    for r in successful:
        ar_dev = abs(r.aspect_ratio - mean_ar)
        area = r.cropped_size[0] * r.cropped_size[1]
        area_dev = abs(area - mean_area)

        is_ratio = std_ar > 1e-6 and ar_dev > sigma * std_ar
        is_size = std_area > 1e-6 and area_dev > sigma * std_area

        if is_ratio:
            _rename_output(r, "-ratio")
            outliers.append(f"  [ratio] {r.output_name} "
                            f"(AR={r.aspect_ratio:.4f}, {ar_dev/std_ar:.1f}σ)")
        if is_size:
            _rename_output(r, "-size")
            outliers.append(f"  [size] {r.output_name} "
                            f"(面積={area:.0f}, {area_dev/std_area:.1f}σ)")

    if not outliers:
        log.append("  外れ値なし")
    else:
        log.extend(outliers)

    return log

# ============================================================
# ログ出力
# ============================================================

def write_log(results: list[ProcessResult], outlier_log: list[str],
              path: Path, version: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{version} 処理結果\n")
        f.write(f"実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'='*70}\n\n")

        s_count = sum(1 for r in results if r.success)
        e_count = sum(1 for r in results if not r.success)
        st_count = sum(1 for r in results if r.streaks)
        f.write(f"合計: {len(results)} "
                f"(成功: {s_count}, エラー: {e_count}, スジ: {st_count})\n")

        # エラー一覧
        errors = [r for r in results if not r.success]
        if errors:
            for r in errors:
                f.write(f"  [ERROR] {r.input_name}: {r.error_msg}\n")

        # スジ一覧
        streak_files = [r for r in results if r.streaks]
        if streak_files:
            for r in streak_files:
                f.write(f"  [スジ] {r.input_name} → {r.output_name}\n")

        # アス比・面積外れ値
        if outlier_log:
            for line in outlier_log:
                f.write(f"{line}\n")

        f.write("\n")

        # 各ファイル詳細
        for r in results:
            f.write(f"--- {r.input_name} ---\n")
            if not r.success:
                f.write(f"  [ERROR] {r.error_msg}\n\n")
                continue
            ow, oh = r.original_size
            cw, ch = r.cropped_size
            f.write(f"  出力: {r.output_name}\n")
            f.write(f"  サイズ: {ow}x{oh} → {cw}x{ch} (AR={r.aspect_ratio:.4f})\n")
            f.write(f"  傾き: {r.tilt_deg:.4f}°{' (補正済)' if r.tilt_corrected else ''}\n")
            for side, (det, pos) in r.edges_detected.items():
                f.write(f"  {side}: {'pos='+str(pos) if det else '検出不能'}\n")
            if r.streaks:
                f.write("  ⚠ スジ:\n")
                for sv in r.streaks:
                    f.write(f"  {sv.describe()}\n")
            f.write("\n")

# ============================================================
# メインランナー
# ============================================================

def run(prof: ScannerProfile, version: str):
    script_dir = Path(sys.argv[0]).resolve().parent
    input_dir = script_dir / "crop"
    output_dir = script_dir / "output"
    line_detected_dir = script_dir / "line-detected"
    log_path = script_dir / "crop_result.txt"

    if not input_dir.is_dir():
        print(f"[ERROR] crop/ フォルダが見つかりません: {input_dir}")
        sys.exit(1)

    # output/ を初期化
    if output_dir.is_dir():
        for f in output_dir.iterdir():
            if f.is_file():
                f.unlink()
    output_dir.mkdir(exist_ok=True)

    # line-detected/ を初期化 (output/ と同様、無条件でクリア)
    if line_detected_dir.is_dir():
        for f in line_detected_dir.iterdir():
            if f.is_file():
                f.unlink()
    line_detected_dir.mkdir(exist_ok=True)

    files = sorted([
        f for f in input_dir.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    ])
    if not files:
        print(f"[ERROR] 対応ファイルなし ({', '.join(SUPPORTED_EXTENSIONS)})")
        sys.exit(1)

    print(f"{version}")
    print(f"プロファイル: {prof.name}")
    print(f"入力: {input_dir} ({len(files)} ファイル)")
    if prof.enable_streak and not HAS_SCIPY:
        print("[WARN] scipy 未インストール → スジ検出無効")

    workers = max(1, multiprocessing.cpu_count() - 1)
    print(f"並列処理: {workers} プロセス")

    prof_dict = prof.__dict__
    results = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(process_image, str(f), str(output_dir),
                        str(line_detected_dir), prof_dict): f
            for f in files
        }
        for fut in as_completed(futures):
            f = futures[fut]
            try:
                r = fut.result()
                results.append(r)
                print(f"\n{'='*60}")
                for line in r.log_lines:
                    print(line)
            except Exception as ex:
                print(f"\n[ERROR] {f.name}: {ex}")
                import traceback
                traceback.print_exc()
                results.append(ProcessResult(
                    f.name, "", "", (0,0), (0,0), 0, False,
                    {}, [], 0.0, False, str(ex)))

    results.sort(key=lambda r: r.input_name)

    outlier_log = []
    if prof.enable_ratio_check:
        print(f"\n{'='*60}")
        outlier_log = check_outliers(results, prof.ratio_sigma)
        for line in outlier_log:
            print(line)

    write_log(results, outlier_log, log_path, version)

    s = sum(1 for r in results if r.success)
    e = sum(1 for r in results if not r.success)
    st = sum(1 for r in results if r.streaks)
    print(f"\n{'='*60}")
    print(f"完了: {s} 成功, {e} エラー, {st} スジ検出")
    print(f"結果ログ: {log_path}")
