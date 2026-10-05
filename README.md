# suji-venator

**ADF 自炊スキャン画像の縦筋・横筋（スジ）を検出し、余白を自動除去するツール**
**Detects vertical/horizontal streaks in self-scanned images and auto-crops margins**
Claude Code / Codex で書きました。

---

## 概要 / Overview

**日本語**

`suji-venator`（スジ・ヴェナトール = スジ狩人）は、自炊（書籍の自己電子化）で生じるスキャン画像を後処理するツールです。主な目的は、ADF（自動給紙）スキャナのローラーやガラス面に付着したゴミ由来の**スジ（筋）を検出して警告する**ことです。あわせて、スキャナのガラス面背景に生じる余白を自動でクロップし、紙面の傾きを補正します。

スキャナドライバにもガラス面の汚れ検知機能がありますが、取りこぼしが多く実用になりませんでした。そこで、スキャン後の画像から余白のスジを直接検出する方式にしています。

**English**

`suji-venator` ("suji" = streak, Latin "venator" = hunter) is a post-processing tool for self-scanned book images. Its primary purpose is to **detect and flag streaks** caused by debris on the rollers or glass surface of ADF (Auto Document Feeder) scanners. It also automatically crops the scanner-background margins and corrects page skew.

Scanner drivers do offer glass-dirt detection, but it missed too much to rely on. This tool instead detects streaks directly in the margins of the scanned image.

![sampleImage](sample.png)

---

## 仕組み / How It Works

**日本語**

任意の絵柄の中からスジを見つけるのは不良設定問題です。線画や網点とスジを一般的に区別する方法はなく、無理に試みれば誤検出だらけになります。

一方、ローラーやガラス面に由来するスジは**搬送経路に固定された欠陥**なので、スキャン方向に全長にわたって現れます。そこで本ツールは、紙より 5mm 広くスキャンして**既知の無地領域を意図的に作り**、そこだけを調べます。背景が平坦なため「局所中央値からの偏差」という単純な判定で足り、ADF プロファイルでは **4階調**の偏差まで拾えます。絵柄の中でこの感度は使えません。

余白でスジが見つかれば、同じスジは紙面上も横切っています。したがって余白だけを調べれば実用上は十分です。

**つまり「紙より 5mm 広くスキャンする」は妥協や回避策ではなく、問題を解ける形に変えている中核の前提です。**

**English**

Finding a streak inside arbitrary artwork is an ill-posed problem: there is no general way to tell a streak from line art or halftone, and attempting it produces mostly false positives.

Streaks from rollers or the glass surface, however, are **defects fixed in the paper path**, so they run the full length of the scan. This tool therefore scans about 5 mm wider than the paper to **deliberately create a known-blank observation window**, and looks only there. Because that region is flat, a simple "deviation from the local median" test suffices — sensitive enough to catch a **4-gray-level** deviation in the ADF profile, which would be unusable inside artwork.

If a streak appears in the margin, the same streak also crosses the page. Examining only the margin is therefore sufficient in practice.

**In short, scanning 5 mm wider is not a workaround — it is the core premise that makes the problem tractable.**

---

## 実行例 / Example Output

スジを検出すると、検出位置を指す赤い矢印を描いた確認用画像が `line-detected/` に出力されます。
When a streak is found, an annotated copy with red arrows pointing at it is written to `line-detected/`.

![スジ検出の例](sample-detected-160.png)

目視では気づきにくいスジも同じように検出されます。
Streaks that are easy to miss by eye are detected just the same.

![見落としやすいスジの例](sample-detected-086.png)

切り出し後の画像は `output/` に出力されます。スジが検出されたページはファイル名に `-line` が付きます。
The cropped page goes to `output/`, with `-line` appended to the filename when a streak was detected.

![切り出し後の出力](sample-output-160.png)

> 掲載した画像は、紙面の内容にモザイク処理を施しています（スジと紙面の境目が分かるよう、該当部分は残しています）。
> The page content in these images is pixelated; the streak and the paper edge are left intact so they remain visible.

---

## フラットベッド対応について / Flatbed Support（調整中 / Work in Progress）

**日本語**

`crop-flatbed.py` は現在調整中です。

狙いは、**プラテンに紙を適当に置いても使えるようにする**ことです。原稿の傾きを補正したうえで余白を切り抜くため、スキャナに紙をまっすぐ正確に置く必要がなくなります。画集の 1 ページでも、帯やカードのような小さな紙片でも、プラテンのどこに置いても構いません。

そのため、**スキャン範囲はプラテン全体に設定してください**。紙がどこにあるか分からない前提で画像全体から紙面を探すので、スキャン範囲を紙に合わせて絞ってしまうと意味がなくなります。

ただし、**プラテンの角に突き当てて置かないでください**。スキャン可能範囲は物理的な角まで届いていないことが多く、スキャナの指示どおり角に合わせると紙の端が範囲外になって切れてしまいます。写っていないものは後から復元できません。また紙の端が画像の端に接すると、その辺には背景が写らないため紙面端を検出できず、その辺はクロップされません。**四辺すべてに背景が写る位置（プラテンの中央寄り）に置いてください。**

**English**

`crop-flatbed.py` is still being tuned.

The goal is to let you **place the paper casually on the platen**: the tool corrects the skew and then crops the margins, so there is no need to align the sheet precisely. A page from an art book, or a small strip such as an obi, can sit anywhere on the glass.

For this to work, **set the scan area to the entire platen**. The tool searches the whole image for the paper on the assumption that its position is unknown, so narrowing the scan area to the sheet defeats the purpose.

Do **not** butt the sheet into the corner of the platen, however. The scannable area usually does not reach the physical corner, so aligning the paper to it as the scanner instructs leaves part of the sheet outside the captured area — and what was never captured cannot be recovered. A paper edge flush with the image border also leaves no background on that side, so that edge cannot be detected or cropped. **Place the sheet so that background is visible on all four sides (toward the middle of the platen).**

---

## 特徴 / Features

- **スジ検出 / Streak detection**: 余白領域の輝度偏差から縦筋・横筋を検出し、該当ファイル名に `-line` を付加
- **余白除去 / Margin cropping**: 紙面端を検出してガラス面の余白を除去。ADF は端から内側へ走査し、フラットベッドは画像全体を領域分割して検出します
- **傾き補正 / Skew correction**: 検出した紙面端から傾きを推定し回転補正（フラットベッド向け）
- **検出失敗の通知 / Failure flagging**: 紙面端を検出できなかった場合は黙って画像端にフォールバックせず、`-edge` を付けて警告
- **異常検出 / Outlier detection**: 出力のアスペクト比・面積を統計的に集計し、外れ値に `-ratio` / `-size` を付加
- **並列処理 / Parallel processing**: 複数CPUコアでバッチ処理を高速化

---

## 対応スキャナ / Supported Scanners

| プロファイル / Profile | スキャナ / Scanner | 想定用途 / Use case |
|---|---|---|
| `crop-adf.py` | Epson DS-571W (ADF) | 書籍・漫画本文 / Book & manga pages |
| `crop-flatbed.py` | Epson GT-X830 (flatbed) | 画集・見開き / Art books & spreads |

いずれも**白背景スキャナ**（背景がほぼ白〜淡灰色）を前提としています。黒背景スキャナには未対応です。
Both assume **white-background scanners** (background is near-white to light gray). Black-background scanners are not supported.

---

## 動作要件 / Requirements

- ADF では、スキャン範囲を紙より 5mm ほど大きく設定してください。スジ検出は画像端から 15〜75px の帯を調べるため、そこが紙面ではなくスキャナ背景である必要があります（600dpi で約3.2mm、400dpi で約4.8mm に相当。低解像度ではもう少し広めに）。
  For ADF scans, set the scan area about 5mm larger than the paper. Streak detection examines a band 15–75 px in from the image edge, and that band must fall on the scanner background rather than the paper (about 3.2 mm at 600 dpi, 4.8 mm at 400 dpi; allow more at lower resolutions).
- 原稿はノド（綴じ側）を上にした横向きで ADF に給紙して、ドライバで回転させてください。これによりスジが横方向に出るため、左右の余白で検出できます。
  Feed pages sideways into the ADF with the gutter (binding edge) facing up, and set rotate in driver. This makes streaks run horizontally, so they can be detected in the left/right margins.
- Python 3.10 以上 / Python 3.10+
- 依存ライブラリ / Dependencies: `opencv-python`, `numpy`, `scipy`

```bash
pip install -r requirements.txt
```

---

## 使い方 / Usage

1. スクリプトと同じ階層に `crop/` フォルダを作成し、処理対象ファイル（1冊分）を入れます。
   Create a `crop/` folder next to the script and place your files (one book's worth) inside.

2. スキャナに応じてスクリプトを実行します。
   Run the script matching your scanner.

```bash
# ADF (DS-571W) の場合 / For ADF
python crop-adf.py

# フラットベッド (GT-X830) の場合 / For flatbed
python crop-flatbed.py
```

3. 処理済み画像が `output/` に PNG で出力され、処理結果が `crop_result.txt` に記録されます。
   Processed images are written to `output/` as PNG, and a summary is logged to `crop_result.txt`.

### フォルダ構成 / Folder structure

```
your-work-folder/
├── crop-adf.py
├── crop-flatbed.py
├── crop_core.py
├── crop/              # 入力 / Input (place files here)
│   ├── 001.png
│   └── 002.tif
├── output/            # 出力 / Output (auto-created, cleared each run)
│   ├── 001.png
│   └── 002.png
├── line-detected/     # スジ確認用の矢印付き画像 / Streak-annotated images (auto-created, cleared each run)
│   └── 002.png
└── crop_result.txt    # 処理ログ / Processing log
```

対応入力形式 / Supported input formats: `.png`, `.tif`, `.tiff`, `.bmp`
グレースケール・カラー・アルファ付き（BGRA）・16bit に対応します。検出は 8bit グレースケールに変換して行いますが、クロップは元画像に対して行うため、出力は入力の bit 深度とチャンネルを保持します。
Grayscale, color, alpha (BGRA) and 16-bit inputs are supported. Detection runs on an 8-bit grayscale copy, while cropping is applied to the original, so the output keeps the input's bit depth and channels.

出力は常に PNG / Output is always PNG.

> **注意 / Note**: `output/` は実行のたびに中身が削除されます。
> The `output/` folder is **cleared at the start of every run**.

---

## 出力ファイル名の規則 / Output Naming Convention

検出された問題に応じて、出力ファイル名に接尾辞が付きます。
Suffixes are appended to output filenames based on detected issues.

| 接尾辞 / Suffix | 意味 / Meaning |
|---|---|
| `-line` | スジを検出 / Streak detected |
| `-edge` | 紙面端の検出に失敗し画像端でフォールバック / Edge detection failed (fell back to image border) |
| `-ratio` | アスペクト比が外れ値 / Aspect ratio outlier |
| `-size` | 面積が外れ値 / Area (size) outlier |

例 / Example: `022.png` にスジがあれば `022-line.png` として出力されます。
複数該当する場合は連結されます（例: `088-ratio-size.png`）。
Multiple issues are concatenated (e.g., `088-ratio-size.png`).

---

## スクリプトの違い / Profile Differences

| 項目 / Item | `crop-adf.py` | `crop-flatbed.py` |
|---|---|---|
| 紙面検出 / Paper detection | 端から内側へ走査 / Scan inward from borders | 領域分割 / Whole-image segmentation*** |
| 傾き補正 / Skew correction | 無効 / Off* | 有効 / On |
| スジ検出 / Streak detection | 有効 / On | 無効 / Off** |
| 追加マージン / Extra margin | 8px | なし / None |
| 異常検出 / Outlier check | 有効 / On | 無効 / Off |

\* DS-571W のドライバ側「Correct Paper Skew」で補正済みを前提としています。
   Assumes skew is already corrected by the DS-571W driver's "Correct Paper Skew" option.

\*\* フラットベッドにはADF由来のスジが発生しないためです。
   Flatbed scans do not produce ADF-style streaks.

\*\*\* 端からの走査は紙面が画面の大半を占める前提のため、プラテン中央に置いた小さな原稿には届きません。フラットベッドでは画像全体を領域分割して紙面を探します（`region_detect`）。
   Scanning inward assumes the paper fills most of the frame, so it cannot reach a small document placed in the middle of the platen. The flatbed profile segments the whole image instead (`region_detect`).

---

## 既知の制約 / Known Limitations

- **コントラストの弱いスジ**は検出できない場合があります。スジの輝度が余白の背景色とほぼ同じ場合、原理的に検出が困難です。その場合は `streak_threshold` を下げることで検出感度を上げられます（ただし誤検出が増える可能性があります）。
  **Faint streaks** may go undetected. If a streak's brightness matches the margin background, detection is fundamentally difficult. In such cases, lowering `streak_threshold` increases sensitivity (at the cost of more false positives).

- **紙面コンテンツが端まで描かれている**漫画ページなどでは、紙面端の検出精度が落ちることがあります。その場合は `ScannerProfile` の各パラメータ（`shadow_range_high` や `ratio_sigma` など）を調整することで対応できます。
  Edge detection accuracy may degrade on pages where **content extends to the paper edge** (e.g., full-bleed manga). When this happens, you can compensate by tuning the `ScannerProfile` parameters (such as `shadow_range_high` or `ratio_sigma`).

- フラットベッドでは、原稿をプラテンの**外周（ビネットで暗くなる帯）から離して**置いてください。原稿が暗い外周に接していると、両者が一つの領域として繋がり検出に失敗することがあります。
  On the flatbed, place the document **away from the darker vignetted border** of the platen. If the document touches that border, the two can merge into one region and detection may fail.

- フラットベッドの領域検出（`region_detect`）は**1画像に原稿1枚**を前提とします。複数枚を並べて置いた場合、最も大きい1枚だけが切り出されます。また画面面積の 0.1% 未満の極小な原稿は検出対象外です。
  Flatbed region detection assumes **one document per image**. If several are placed side by side, only the largest is cropped. Documents smaller than 0.1% of the image area are not detected.

- スジ検出は**余白領域のみ**を対象とします。紙面内部（コンテンツ領域）を横切るスジは検出対象外です。しかしながら、原理的にそういったスジは余白部分にも現れるため、実用上は問題ないでしょう。
  Streak detection only examines **margin regions**; streaks crossing the content area are out of scope. In practice, however, such streaks also appear in the margins, so this is rarely an issue.

- **単体 .exe を配布しない理由**: `ScannerProfile` のパラメータをスキャナや原稿に合わせて編集して使う前提のツールのため、スクリプトのまま配布しています。また opencv / numpy / scipy を同梱すると 150〜400MB になり、起動も遅くなります。
  **Why there is no standalone .exe**: the tool is meant to be used by editing the `ScannerProfile` parameters for your scanner and material, so it ships as plain scripts. Bundling opencv / numpy / scipy would also produce a 150–400 MB binary with slow startup.

---

## パラメータ調整 / Tuning

各スクリプト冒頭の `ScannerProfile` で主要パラメータを調整できます。
Key parameters can be adjusted in the `ScannerProfile` at the top of each script.

| パラメータ / Parameter | 説明 / Description |
|---|---|
| `shadow_range_high` | 紙面判定の上側閾値。狭めると浅め、広げると深めにクロップ / Upper threshold for paper detection |
| `shadow_dip_threshold` | 影ディップ判定の深さ（右辺の影除去） / Shadow dip depth (right-edge shadow removal) |
| `streak_threshold` | スジ判定の輝度偏差。下げると敏感、上げると保守的 / Streak deviation threshold |
| `color_dist_threshold` | カラー入力時の紙面判定に使う彩度距離。色付き紙が背景と等輝度でも分離できる / Chroma-distance threshold for paper detection (color input) |
| `region_detect` | 端から走査せず画像全体を領域分割して紙面を検出。プラテン中央に置いた小さな原稿や、紙と背景の輝度差が小さい場合に有効（フラットベッドで既定 ON） / Detect the paper by segmenting the whole image instead of scanning inward from the borders (default ON for flatbed) |
| `adf_margin_px` | クロップ後の追加カット量 / Extra crop after edge detection |
| `ratio_sigma` | アス比・面積の外れ値判定の σ 倍数 / Sigma multiplier for outlier detection |

---

パラメータの調整に迷ったら、スクリプト・README.md・サンプル画像を LLM に渡して相談すると良いでしょう。
If you are unsure how to tune the parameters, try feeding the scripts, README.md, and sample images to an LLM and asking for advice.

## ライセンス / License

MIT License — see [LICENSE](LICENSE).
