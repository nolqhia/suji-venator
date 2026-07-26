import unittest
from unittest import mock

import cv2
import numpy as np

import crop_core as core


def make_profile(*, region_detect):
    return core.ScannerProfile(
        name="test",
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
        color_dist_threshold=25.0,
        region_detect=region_detect,
    )


class RefineEdgeTests(unittest.TestCase):
    def test_refines_biased_coarse_positions_from_both_sides(self):
        band = np.full(120, 220.0, np.float32)
        band[45:76] = 245.0

        self.assertEqual(core._refine_edge(band, "left", 30, 20), 45)
        self.assertEqual(core._refine_edge(band, "right", 90, 20), 75)

    def test_keeps_coarse_position_when_reference_window_is_outside(self):
        band = np.full(120, 220.0, np.float32)

        self.assertEqual(core._refine_edge(band, "left", 0, 20), 0)
        self.assertEqual(core._refine_edge(band, "right", 119, 20), 119)


class RegionDetectionTests(unittest.TestCase):
    def test_16bit_chroma_threshold_uses_8bit_scale(self):
        h, w = 600, 600
        image = np.full((h, w, 3), 220 * 257, np.uint16)
        checker = ((np.indices((h, w)).sum(axis=0) & 1) * 2 - 1).astype(
            np.int32
        )
        noisy = image.astype(np.int32)
        noisy[:, :, 0] += checker * 20
        noisy[:, :, 2] -= checker * 20
        image = np.clip(noisy, 0, 65535).astype(np.uint16)
        image[220:380, 160:440] = (180 * 257, 220 * 257, 250 * 257)

        edges, angle = core.detect_paper_region(
            image, core.to_gray(image), make_profile(region_detect=True)
        )

        self.assertTrue(all((
            edges.left.detected,
            edges.right.detected,
            edges.top.detected,
            edges.bottom.detected,
        )))
        self.assertLessEqual(abs(edges.left.position - 160), 1)
        self.assertLessEqual(abs(edges.right.position - 439), 1)
        self.assertLessEqual(abs(edges.top.position - 220), 1)
        self.assertLessEqual(abs(edges.bottom.position - 379), 1)
        self.assertAlmostEqual(angle, 0.0, places=4)

    def test_does_not_upscale_images_narrower_than_target(self):
        image = np.full((400, 600, 3), 220, np.uint8)

        with mock.patch.object(
            core.cv2, "resize", wraps=core.cv2.resize
        ) as resize:
            core.detect_paper_region(
                image, core.to_gray(image), make_profile(region_detect=True)
            )

        resize.assert_not_called()


class TiltConventionTests(unittest.TestCase):
    def setUp(self):
        self.base = np.full((1400, 1800, 3), 220, np.uint8)
        cv2.rectangle(
            self.base, (180, 160), (1620, 1240), (70, 130, 200), -1
        )

    def test_region_angle_matches_opencv_rotation_sign(self):
        profile = make_profile(region_detect=True)
        base = np.full((1400, 1800, 3), 220, np.uint8)
        cv2.rectangle(base, (360, 320), (1440, 1080), (70, 130, 200), -1)
        rotated = core.rotate_image(base, 2.0)
        _, measured = core.detect_paper_region(
            rotated, core.to_gray(rotated), profile
        )
        corrected = core.rotate_image(rotated, -measured)
        _, residual = core.detect_paper_region(
            corrected, core.to_gray(corrected), profile
        )

        self.assertGreater(measured, 0.0)
        self.assertAlmostEqual(measured, 2.0, delta=0.05)
        self.assertAlmostEqual(residual, 0.0, delta=0.05)

    def test_edge_scan_angle_matches_opencv_rotation_sign(self):
        profile = make_profile(region_detect=False)
        rotated = core.rotate_image(self.base, -2.0)
        gray = core.to_gray(rotated)
        edges = core.detect_all_edges(
            gray,
            core.estimate_background(gray),
            profile,
            rotated,
            core.estimate_background_color(rotated),
        )
        measured = core.estimate_tilt(edges)
        corrected = core.rotate_image(rotated, -measured)
        corrected_gray = core.to_gray(corrected)
        corrected_edges = core.detect_all_edges(
            corrected_gray,
            core.estimate_background(corrected_gray),
            profile,
            corrected,
            core.estimate_background_color(corrected),
        )
        residual = core.estimate_tilt(corrected_edges)

        self.assertLess(measured, 0.0)
        self.assertAlmostEqual(measured, -2.0, delta=0.05)
        self.assertAlmostEqual(residual, 0.0, delta=0.05)


if __name__ == "__main__":
    unittest.main()
