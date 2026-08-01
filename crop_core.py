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
    color_dist_threshold: float = 25.0  # カラー入力時の背景色距離しきい値
    region_detect: bool = False         # 領域分割で紙面を検出 (小さな原稿・低コントラスト)

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
    edges_failed: list = field(default_factory=list)  # 検出失敗した辺 (フォールバック)

CORNER_SIZE = 40
OUTER_SKIP_PX = 15
SCAN_DEPTH = 500
N_SAMPLES = 48
# クロップ位置は紙面境界の中央値ではなく内側寄りのパーセンタイルを使う。
# 紙が僅かに歪む/湾曲すると横エッジが完全な水平にならず、中央値で切ると
# 片側にグレーの楔が残る。内側寄せで残グレーを抑える (歪みが無ければ
# 全サンプル同値なので中央値と一致し、余計なロスは出ない)。
CROP_INWARD_PCT = 92

# 領域ベース検出 (region_detect) のパラメータ
REGION_TARGET_W = 1200      # 縮小後の幅。ノイズを平均化しつつ処理を軽くする
REGION_MIN_DELTA = 2.0      # 背景水準からの最小偏差 (階調)
REGION_K_NOISE = 6.0        # ノイズ σ の何倍を偏差しきい値とするか
REGION_CHROMA_THR = 8.0     # 彩度距離のしきい値 (カラー入力)
REGION_MIN_EDGE_STEP = 1.0  # 紙面端と認めるのに必要な境界直近の段差 (階調)
WHITE_EDGE_LEVEL = 250      # 画像端の白帯 (スキャナ由来の縁) とみなす輝度
REGION_MAX_WORKERS = 4      # 大判画像のメモリ消費を抑える並列上限

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


def estimate_background_color(img: np.ndarray) -> np.ndarray:
    """コーナー4隅の BGR 中央値を背景色として返す (カラー入力用)。
    グレースケールでは背景と等輝度でも、色付き紙は色距離で分離できる。
    BGRA は先頭3チャンネル (BGR) のみ使用。
    """
    bgr = img[:, :, :3]
    h, w = bgr.shape[:2]
    cs = min(CORNER_SIZE, h // 10, w // 10)
    corners = np.concatenate([
        bgr[:cs, :cs].reshape(-1, 3), bgr[:cs, -cs:].reshape(-1, 3),
        bgr[-cs:, :cs].reshape(-1, 3), bgr[-cs:, -cs:].reshape(-1, 3),
    ]).astype(np.float64)
    return np.median(corners, axis=0)

# ============================================================
# エッジ検出
# ============================================================

def _saturated_edge_run(line: np.ndarray, from_start: bool, limit: int) -> int:
    """走査線の端から続く飽和 (>=WHITE_EDGE_LEVEL) 画素の長さを返す。
    スキャナ/ドライバが画像の縁に付ける白帯を読み飛ばすために使う。
    """
    n = len(line)
    run = 0
    i = 0 if from_start else n - 1
    stepv = 1 if from_start else -1
    while 0 <= i < n and run < limit and line[i] >= WHITE_EDGE_LEVEL:
        i += stepv
        run += 1
    return run


def _detect_edge_one_side(gray: np.ndarray, side: str,
                          bg_med: float, prof: ScannerProfile,
                          img_color: np.ndarray = None,
                          bg_color: np.ndarray = None) -> EdgeResult:
    """1辺のエッジを検出。
    Stage 1: 外側から走査し「紙面」の最初のピクセルを見つける
    Stage 2: first_paper から外側方向にディップ(影の最深部)を探す
             ディップがあればそこでクロップ(影を完全除去)

    紙面判定 (is_paper) は輝度が背景帯の外にあるか、または
    (カラー入力時) 背景色からの色距離が閾値を超える場合に真。
    水色の紙のように背景グレーと等輝度でも、色距離で分離できる。
    """
    h, w = gray.shape
    s_low = bg_med - prof.shadow_range_low
    s_high = bg_med + prof.shadow_range_high
    dip_thresh = bg_med - prof.shadow_dip_threshold
    dip_search = prof.shadow_dip_search
    depth = min(SCAN_DEPTH, (w if side in ("left", "right") else h) // 2)
    use_color = img_color is not None and bg_color is not None
    color_thresh = prof.color_dist_threshold

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

        # 紙面判定マスク。
        # カラー入力では、背景からの単純な RGB 距離を使うと、紙面直前の
        # 無彩色な落ち影まで紙として検出してしまう。そこで明るさ成分を
        # 除いた色差を使い、無彩色の紙は輝度差で補完する。
        # 明るい側のしきい値には色距離相当の下限を設け、背景グレーの
        # 小さな明るさノイズで走査が止まらないようにする。
        if use_color:
            cline = (img_color[idx, :, :3] if is_horiz
                     else img_color[:, idx, :3]).astype(np.float64)
            color_delta = cline - bg_color
            chroma_delta = color_delta - color_delta.mean(axis=1, keepdims=True)
            chroma_dist = np.sqrt((chroma_delta ** 2).sum(axis=1))
            bright_threshold = max(prof.shadow_range_high,
                                   color_thresh / np.sqrt(3.0))
            is_paper = (
                (line < s_low)
                | (line > bg_med + bright_threshold)
                | (chroma_dist > color_thresh)
            )
        else:
            is_paper = (line < s_low) | (line > s_high)

        # 画像端の飽和 (255) 帯はスキャナ/ドライバ由来の縁であって紙面ではない。
        # ADF は紙より広い範囲をスキャンする前提なので、最外周は必ず背景側。
        # この帯が OUTER_SKIP_PX より厚いと走査開始点が帯の中に入り、
        # そこを紙面端と誤認する (実測で 27px の帯により下端が 57px ずれた)。
        edge_skip = OUTER_SKIP_PX + _saturated_edge_run(
            line, side in ("left", "top"), min(SCAN_DEPTH // 4, length // 10))

        # Stage 1: 外側から走査し、紙面の最初のピクセルを見つける
        if side in ("left", "top"):
            scan_range = range(edge_skip, edge_skip + depth)
        else:
            scan_range = range(length - edge_skip - 1,
                               length - edge_skip - depth - 1, -1)

        first_paper = None
        for i in scan_range:
            if is_paper[i]:
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
                    if is_paper[j]:
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
        positions = np.array([p for _, p in samples])
        # 紙面の内側寄りで切ってグレーの楔を残さない
        q = CROP_INWARD_PCT if side in ("left", "top") else 100 - CROP_INWARD_PCT
        return EdgeResult(int(np.percentile(positions, q)), True, samples)

    fallback = {"left": 0, "right": w - 1, "top": 0, "bottom": h - 1}[side]
    return EdgeResult(fallback, False)


def _refine_edge(band: np.ndarray, side: str, coarse: int, search: int) -> int:
    """1次元プロファイル上で粗い境界位置を精密化する。
    外側/内側それぞれの水準を取り、その中点を横切る位置を境界とみなす。
    紙が背景より暗くても明るくても動くよう、差の符号で向きを決める。
    """
    n = len(band)
    inward = 1 if side in ("left", "top") else -1   # 内側へ進む向き
    out_a, out_b = coarse - inward * 2 * search, coarse - inward * search
    in_a, in_b = coarse + inward * search, coarse + inward * 2 * search
    lo_out, hi_out = sorted((max(0, min(n, out_a)), max(0, min(n, out_b))))
    lo_in, hi_in = sorted((max(0, min(n, in_a)), max(0, min(n, in_b))))
    if hi_out - lo_out < 4 or hi_in - lo_in < 4:
        return coarse
    outside = float(np.median(band[lo_out:hi_out]))
    inside = float(np.median(band[lo_in:hi_in]))
    if abs(inside - outside) < 1e-6:
        return coarse
    mid = (outside + inside) / 2.0
    sgn = 1.0 if inside > outside else -1.0

    # 外側から内側へ走査し、紙面側の水準を数画素連続で超えた位置を境界とする
    start = coarse - inward * search
    run = 0
    for k in range(2 * search):
        i = start + inward * k
        if i < 0 or i >= n:
            break
        if sgn * (band[i] - mid) > 0:
            run += 1
            if run >= 3:
                return int(i - inward * (run - 1))
        else:
            run = 0
    return coarse


def _edge_line_angle(gray: np.ndarray, side: str, pos: int,
                     lo: int, hi: int, search: int, nseg: int = 9):
    """1辺を小区間に分けて境界を精密化し、直線を当てて傾きを求める。
    最小外接矩形はプラテンのゴミが連結すると大きく狂うため、
    実際の紙面境界そのものから角度を出す。符号は estimate_tilt と揃える。
    """
    step = (hi - lo) // nseg
    if step < 8:
        return None
    idxs, vals = [], []
    for i in range(nseg):
        a = lo + i * step
        b = a + step
        band = (gray[a:b, :].mean(axis=0, dtype=np.float32)
                if side in ("left", "right")
                else gray[:, a:b].mean(axis=1, dtype=np.float32))
        idxs.append(a + step / 2.0)
        vals.append(_refine_edge(band, side, pos, search))
    idxs = np.array(idxs, dtype=float)
    vals = np.array(vals, dtype=float)
    q1, q3 = np.percentile(vals, [25, 75])
    iqr = q3 - q1
    m = (vals >= q1 - 1.5 * iqr) & (vals <= q3 + 1.5 * iqr)
    if m.sum() < 4:
        return None
    slope = float(np.polyfit(idxs[m], vals[m], 1)[0])
    ang = float(np.degrees(np.arctan(slope)))
    # 縦辺は dx/dy、横辺は dy/dx。median で打ち消し合わないよう符号を揃える。
    return ang if side in ("left", "right") else -ang


def _border_bg_level(g: np.ndarray) -> float:
    """画像端から連結した領域 (=プラテン) の上位パーセンタイルを背景水準として返す。
    近傍差のフラッドフィルは緩やかな照明むら (ビネット) を辿り、
    紙面の段差で止まる。段差が小さく紙面へ漏れても、紙が少数派なら値は背景側に残る。
    領域を端連結に限ることで、明るい紙が画面の過半を占めても基準が紙側へ移らない。
    その中で上位パーセンタイルを取るのは、ビネットの暗い帯に引かれないため。
    端連結領域が極端に狭いときだけ画像全体の上位パーセンタイルに退避する。
    """
    sh, sw = g.shape
    g8 = cv2.GaussianBlur(g, (0, 0), 1.5)
    g8 = np.clip(g8, 0, 255).astype(np.uint8)
    ff = np.zeros((sh + 2, sw + 2), np.uint8)
    flags = 4 | cv2.FLOODFILL_MASK_ONLY | (1 << 8)
    step = max(1, min(sw, sh) // 40)
    seeds = ([(x, 0) for x in range(0, sw, step)] +
             [(x, sh - 1) for x in range(0, sw, step)] +
             [(0, y) for y in range(0, sh, step)] +
             [(sw - 1, y) for y in range(0, sh, step)])
    for sx, sy in seeds:
        if ff[sy + 1, sx + 1]:
            continue
        cv2.floodFill(g8, ff, (sx, sy), 0, 2, 2, flags)
    m = ff[1:-1, 1:-1] > 0
    if m.sum() < 0.02 * sw * sh:
        return float(np.percentile(g, 80))
    return float(np.percentile(g[m], 80))


def _near_edge_step(band: np.ndarray, pos: int, half: int = 6, gap: int = 2) -> float:
    """境界位置の直近だけで測る段差の大きさ。
    実際の紙面端は数画素で段差が立つが、照明むらの等高線ではほぼ 0 になる。
    """
    n = len(band)
    a0, a1 = max(0, pos - half), max(0, pos - gap)
    b0, b1 = min(n, pos + gap), min(n, pos + half)
    if a1 - a0 < 2 or b1 - b0 < 2:
        return 0.0
    return abs(float(np.median(band[b0:b1])) - float(np.median(band[a0:a1])))


def detect_paper_region(img: np.ndarray, gray: np.ndarray,
                        prof: ScannerProfile) -> tuple:
    """画像全体を領域分割して紙面の外接矩形を求める (フラットベッド向け)。

    端から内側へ走査する方式は「紙面が画面の大半を占める」前提のため、
    プラテン中央に置かれた小さな原稿には届かない (SCAN_DEPTH の外)。
    また紙と背景の輝度差が数階調しかない場合、単画素走査ではノイズに埋もれる。
    ここでは縮小画像でノイズを平均化し、背景水準からの偏差でマスクを作って
    最大連結成分を紙面とみなす。外周のビネット (枠状に暗い成分) は除外する。
    境界は全解像度のプロファイルで精密化する。

    戻り値: (Edges, 傾き角度[deg])
    """
    h, w = gray.shape
    scale = min(1.0, REGION_TARGET_W / float(w))
    sw = max(1, int(round(w * scale)))
    sh = max(1, int(round(h * scale)))
    small = (img if scale == 1.0
             else cv2.resize(img, (sw, sh), interpolation=cv2.INTER_AREA))
    g = to_gray(small).astype(np.float32)
    is_color = small.ndim == 3 and small.shape[2] >= 3

    # 背景 (プラテン) の水準。
    # 単純な上位パーセンタイルだと、明るい紙が画面の過半を占めたときに
    # 基準が紙側へ移り、紙ではなく外側がマスクされてしまう。
    # 画像端から連結した領域 = プラテンとみなして中央値を取る。
    bg_level = _border_bg_level(g)
    # ノイズは高周波成分から推定 (ビネットのような緩やかな変化を含めない)
    hf = g - cv2.GaussianBlur(g, (0, 0), 2.0)
    noise = max(float(np.median(np.abs(hf))) * 1.4826, 0.15)
    thr = max(REGION_MIN_DELTA, REGION_K_NOISE * noise)

    mask = np.abs(g - bg_level) > thr
    if is_color:
        bgr_src = small[:, :, :3]
        if bgr_src.dtype == np.uint16:
            bgr = (bgr_src >> 8).astype(np.float32)
        else:
            bgr = bgr_src.astype(np.float32)
        sel = np.abs(g - bg_level) < 2.0
        bg_col = (np.median(bgr[sel].reshape(-1, 3), axis=0) if sel.sum() > 100
                  else np.median(bgr.reshape(-1, 3), axis=0))
        delta = bgr - bg_col
        chroma = delta - delta.mean(axis=2, keepdims=True)
        mask |= np.sqrt((chroma ** 2).sum(axis=2)) > REGION_CHROMA_THR

    mask = mask.astype(np.uint8)
    kc = max(3, int(round(min(sw, sh) * 0.012)) | 1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((kc, kc), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))

    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    failed = Edges(EdgeResult(0, False), EdgeResult(w - 1, False),
                   EdgeResult(0, False), EdgeResult(h - 1, False))
    if n <= 1:
        return failed, 0.0

    best = None
    min_area = 0.001 * sw * sh          # 0.1% 未満はゴミとみなす
    for li in (np.argsort(stats[1:, cv2.CC_STAT_AREA])[::-1] + 1):
        x, y, cw, ch, area = stats[li]
        if area < min_area:
            break
        # 外周のビネット (枠状に暗い領域) を除外する。
        # 枠は四辺すべてに接し、かつ中心が穴になっている点で紙面と区別できる。
        # 紙面が全面に及ぶ場合は中心も成分に含まれるので除外されない。
        spans_all = (x <= 1 and y <= 1 and x + cw >= sw - 1 and y + ch >= sh - 1)
        if spans_all and lab[(y + ch // 2), (x + cw // 2)] != li:
            continue
        best = (li, x, y, cw, ch)
        break
    if best is None:
        return failed, 0.0
    li, sx, sy, scw, sch = best

    inv = 1.0 / scale
    x1 = int(round(sx * inv)); x2 = int(round((sx + scw) * inv))
    y1 = int(round(sy * inv)); y2 = int(round((sy + sch) * inv))
    x1 = max(0, min(w - 1, x1)); x2 = max(1, min(w, x2))
    y1 = max(0, min(h - 1, y1)); y2 = max(1, min(h, y2))

    # 全解像度での境界精密化。直交方向は紙面の中央60%だけ使う (角の影を避ける)
    # 探索半径は closing の外向きバイアス (カーネル幅) を確実に覆う大きさにする。
    search = max(16, int(round(kc * inv * 2)))
    ry1, ry2 = y1 + (y2 - y1) // 5, y2 - (y2 - y1) // 5
    rx1, rx2 = x1 + (x2 - x1) // 5, x2 - (x2 - x1) // 5
    band_h = band_v = None
    if ry2 - ry1 >= 4:
        band_h = gray[ry1:ry2, :].mean(axis=0, dtype=np.float32)
        x1 = _refine_edge(band_h, "left", x1, search)
        x2 = _refine_edge(band_h, "right", x2, search)
    if rx2 - rx1 >= 4:
        band_v = gray[:, rx1:rx2].mean(axis=1, dtype=np.float32)
        y1 = _refine_edge(band_v, "top", y1, search)
        y2 = _refine_edge(band_v, "bottom", y2, search)

    # 境界の急峻さを検証する。照明むら (ビネット) の等高線をマスクの縁として
    # 拾っただけの場合、そこには段差が無い。これを弾かないと存在しない領域を
    # 「検出成功」として黙って切り出してしまう。
    steps = []
    if band_h is not None:
        steps += [_near_edge_step(band_h, x1), _near_edge_step(band_h, x2)]
    if band_v is not None:
        steps += [_near_edge_step(band_v, y1), _near_edge_step(band_v, y2)]
    if not steps or float(np.median(steps)) < REGION_MIN_EDGE_STEP:
        return failed, 0.0

    # 傾きは四辺それぞれに直線を当てて求め、その中央値を使う
    angles = []
    for side, pos, lo, hi in (("left", x1, ry1, ry2), ("right", x2, ry1, ry2),
                              ("top", y1, rx1, rx2), ("bottom", y2, rx1, rx2)):
        if hi - lo < 4:
            continue
        a = _edge_line_angle(gray, side, pos, lo, hi, search)
        if a is not None:
            angles.append(a)
    angle = float(np.median(angles)) if angles else 0.0

    edges = Edges(EdgeResult(int(x1), True), EdgeResult(int(x2), True),
                  EdgeResult(int(y1), True), EdgeResult(int(y2), True))
    return edges, angle


def detect_all_edges(gray: np.ndarray, bg_med: float, prof: ScannerProfile,
                     img_color: np.ndarray = None,
                     bg_color: np.ndarray = None) -> Edges:
    return Edges(
        left=_detect_edge_one_side(gray, "left", bg_med, prof, img_color, bg_color),
        right=_detect_edge_one_side(gray, "right", bg_med, prof, img_color, bg_color),
        top=_detect_edge_one_side(gray, "top", bg_med, prof, img_color, bg_color),
        bottom=_detect_edge_one_side(gray, "bottom", bg_med, prof, img_color, bg_color),
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
        # OpenCV の画像座標 (y軸が下向き) で紙を正角 θ 回転すると、
        # 縦エッジの dx/dy は +tanθ、横エッジの dy/dx は -tanθ になる。
        # 横辺の符号を反転しないと median で打ち消し合う。
        angles.append(ang if is_vert else -ang)
    return float(np.median(angles)) if angles else 0.0

# ============================================================
# 回転
# ============================================================

def rotate_image(img: np.ndarray, angle_deg: float) -> np.ndarray:
    """回転後の外側は BORDER_REPLICATE で埋める。
    黒 (BORDER_CONSTANT) だと回転後の四隅の黒い楔が「背景と大きく異なる領域」
    となり、再検出でそこを紙面と誤認する。端の色を複製すれば背景が延長される。
    """
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
        borderMode=cv2.BORDER_REPLICATE,
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
    is_color = img.ndim == 3 and img.shape[2] >= 3
    cm = "カラー" if is_color else "グレースケール"
    log.append(f"処理中: {filename} ({w}x{h}, {cm})")

    bg_med = estimate_background(gray)
    # カラー入力では背景色 (BGR) も推定し、色距離で紙面を分離する。
    # 回転後は黒縁で隅が汚れるため、この bg_color を再利用する。
    bg_color = estimate_background_color(img) if is_color else None
    if is_color:
        log.append(f"  背景色: gray={bg_med:.0f}, BGR=({bg_color[0]:.0f},"
                   f"{bg_color[1]:.0f},{bg_color[2]:.0f})")
    else:
        log.append(f"  背景色: {bg_med:.0f}")

    region_angle = None
    if prof.region_detect:
        edges, region_angle = detect_paper_region(img, gray, prof)
    else:
        edges = detect_all_edges(gray, bg_med, prof,
                                 img if is_color else None, bg_color)
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

    if not prof.enable_tilt:
        tilt = 0.0
    elif region_angle is not None:
        tilt = region_angle
    else:
        tilt = estimate_tilt(edges)
    tilt_corrected = False
    if prof.enable_tilt and abs(tilt) >= prof.min_tilt_deg:
        log.append(f"  傾き補正: {tilt:.4f}°")
        # tilt は「検出した紙面の傾き」。打ち消すには逆向きに回す。
        # 同符号で回すと傾きが倍になる (合成画像で dx/dy が 2 倍になることを確認)。
        img = rotate_image(img, -tilt)
        gray = to_gray(img)
        if prof.region_detect:
            edges, _ = detect_paper_region(img, gray, prof)
        else:
            edges = detect_all_edges(gray, bg_med, prof,
                                     img if is_color else None, bg_color)
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

    # 安全網: 検出に失敗した辺は画像端へ無言でフォールバックしており、
    # 余白 (背景) が残るクロップミスになりやすい。該当があれば警告し、
    # 出力名に -edge を付けて後で目視確認できるようにする。
    edges_failed = [nm for nm, e in [("左", edges.left), ("右", edges.right),
                                     ("上", edges.top), ("下", edges.bottom)]
                    if not e.detected]
    if edges_failed:
        log.append(f"  ⚠ エッジ検出失敗: {'/'.join(edges_failed)} "
                   f"→ 画像端にフォールバック (要確認)")

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

    out_stem = stem
    if has_streaks and not out_stem.endswith("-line"):
        out_stem = f"{out_stem}-line"
    if edges_failed and "-edge" not in out_stem:
        out_stem = f"{out_stem}-edge"

    output_name = f"{out_stem}.png"
    output_path = output_dir / output_name
    cv2.imwrite(str(output_path), cropped)

    pct = (1 - (cw * ch) / (w * h)) * 100
    log.append(f"  出力: {output_name} ({cw}x{ch}, AR={ar:.4f}, -{pct:.1f}%)")

    return ProcessResult(filename, output_name, str(output_path), (w,h), (cw,ch),
                         tilt, tilt_corrected, edges_info, streaks, ar, True, "", log,
                         edges_failed)

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
        ed_count = sum(1 for r in results if r.edges_failed)
        f.write(f"合計: {len(results)} "
                f"(成功: {s_count}, エラー: {e_count}, スジ: {st_count}, "
                f"エッジ検出失敗: {ed_count})\n")

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

        # エッジ検出失敗一覧 (画像端へフォールバック=クロップミスの可能性)
        edge_files = [r for r in results if r.edges_failed]
        if edge_files:
            for r in edge_files:
                f.write(f"  [エッジ] {r.input_name} → {r.output_name} "
                        f"(失敗辺: {'/'.join(r.edges_failed)})\n")

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
    if prof.region_detect:
        workers = min(workers, REGION_MAX_WORKERS)
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
    ed = sum(1 for r in results if r.edges_failed)
    print(f"\n{'='*60}")
    print(f"完了: {s} 成功, {e} エラー, {st} スジ検出, {ed} エッジ検出失敗")
    if ed:
        print(f"⚠ エッジ検出失敗 {ed} 件 (出力名 -edge)。クロップを確認してください。")
    print(f"結果ログ: {log_path}")
