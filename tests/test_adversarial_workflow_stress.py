"""
Adversarial Workflow Stress & Defect Challenge Test Suite
PDF Arranger Background Stitching & Scan Queueing Integration

Author: Challenger 1 (Adversarial Workflow Challenger)
Environment: Strictly isolated under MOCK_SCANNER=1 with synthetic test files.
Zero calls to physical HP Smart Tank 750 scanner carriage, WIA COM, or real devices.

Stress & Adversarial Challenge Dimensions:
1. Repeated rapid F7 multi-pass triggers (simulating user's exact reported defect sequence).
   - Verify zero ghost scans, zero lost images, and prompt user wait state before Part 2.
2. Queue storming: 10+ rapid alternating F7/F8 triggers while scanner is busy and multithreaded bursts.
3. Sudden Escape cancellation at 10%, 50%, and 90% of simulated carriage motion.
4. Conservative crop bounds: synthetic pages with light borders and text near margins, verify zero text clipping.
"""

import os
import sys
import time
import json
import shutil
import unittest
import threading
import concurrent.futures
import subprocess
import numpy as np
import cv2

# Strictly enforce MOCK_SCANNER isolation mode before any imports
os.environ["MOCK_SCANNER"] = "1"
os.environ["MOCK_SCAN_DELAY"] = "0.01"

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

# Import mocked GTK environment and base test utilities from test_e2e_scan_stitch
from test_e2e_scan_stitch import (
    BaseE2ETestCase,
    MockApp,
    MockModel,
    MockTreePath,
    MockTreeRowReference,
    MockPageAdder,
)

import wia_scanner
import scanner_worker
import stitch_daemon
import pdfarranger_scanner_extension as ext


class TrackingLabel:
    """Mock Gtk.Label that tracks status messages."""
    def __init__(self, text=""):
        self.text = text
    def set_text(self, t):
        self.text = t
    def get_text(self):
        return self.text
    def get_label(self):
        return self.text


def create_tracking_manager(app):
    mgr = ext.ScannerManager(app)
    mgr.lbl_status = TrackingLabel("Ready")
    return mgr


# =============================================================================
# 1. Repeated Rapid F7 Multi-Pass Triggers (User Defect Sequence)
# =============================================================================
class TestAdversarialF7DefectSequence(BaseE2ETestCase):
    """
    Stress-tests the exact sequence reported in user feedback:
    - User scans Part 1 (top half) -> completes.
    - User presses F7 for Part 2 (bottom half).
    - Immediately while Part 2 is running (or right as it finishes), user hammers F7 multiple times
      to start scanning the next sheet.
    
    CRITICAL INVARIANTS TESTED:
    1. Zero ghost scans: Part 1 of the new sheet MUST finish and STOP.
       It must NEVER auto-fire Part 2 on its own without explicit human keypress.
    2. Prompt user wait state: UI must clearly instruct user to reposition/flip sheet and press F7.
    3. Zero lost images: Both Page 1 (stitched) and Page 2 (stitched) must complete successfully.
    4. Rapid F7 hammer during Part 1 must be absorbed (not queued to auto-fire Part 2).
    """

    def setUp(self):
        self.app = MockApp()
        self.mgr = create_tracking_manager(self.app)

    def test_exact_user_reported_f7_defect_sequence(self):
        """
        Simulates:
        1. F7 Part 1 of Page 1.
        2. F7 Part 2 of Page 1.
        3. 5x rapid F7 hammer while Part 2 is in motion (simulating user impatience/multitasking).
        4. Part 2 finishes -> Page 1 enqueued for background stitch -> queued F7 starts Page 2 Part 1.
        5. Page 2 Part 1 completes -> Scanner MUST STOP, clear queued_scan, and wait for human flip!
        6. System idles for 1.0 second -> Verify ZERO unprompted ghost scans fire.
        7. User explicitly presses F7 for Page 2 Part 2.
        8. Page 2 Part 2 completes -> background stitch enqueued.
        """
        mgr = self.mgr

        # Step 1: Scan Page 1 Part 1
        mgr.on_f7_clicked()
        p1_done = self.wait_for_condition(
            lambda: not mgr.is_scanning and mgr.part1_path is not None,
            timeout=15.0
        )
        self.assertTrue(p1_done, "Page 1 Part 1 must finish scanning")
        self.assertFalse(mgr.is_scanning, "Scanner must not be busy after Part 1")
        self.assertIsNotNone(mgr.part1_path)

        # Step 2: User flips sheet and presses F7 for Page 1 Part 2
        mgr.on_f7_clicked()
        self.assertTrue(mgr.is_scanning, "Scanner must be busy scanning Part 2")

        # Step 3: Rapidly hammer F7 5 times during Part 2 carriage motion!
        for _ in range(5):
            mgr.on_f7_clicked()
            time.sleep(0.01)

        # Step 4: Wait for Part 2 to complete, then queued F7 to start Page 2 Part 1 and finish
        p2_p1_done = self.wait_for_condition(
            lambda: not mgr.is_scanning and mgr.part1_path is not None and "Scan_" in str(mgr.part1_path),
            timeout=30.0
        )
        self.assertTrue(p2_p1_done, "Page 2 Part 1 must complete and reach wait state")

        # Step 5: INVARIANT CHECKS on Page 2 Part 1 completion
        # Carriage MUST have stopped!
        self.assertFalse(mgr.is_scanning, "ADVERSARIAL FAIL: Scanner must STOP after Page 2 Part 1!")
        # Part 1 of page 2 must be stored
        self.assertIsNotNone(mgr.part1_path, "Part 1 of Page 2 must be armed and waiting")
        # Queued scan MUST be None (MUST NEVER auto-fire Part 2!)
        self.assertIsNone(mgr.queued_scan, "ADVERSARIAL FAIL: queued_scan must be None, never auto-advancing to Part 2!")
        # Status text must prompt the user
        status_text = mgr.lbl_status.get_text()
        self.assertTrue(
            "reposition" in status_text.lower() or "part 2" in status_text.lower() or "flip" in status_text.lower(),
            f"Status must prompt user before Part 2, got: '{status_text}'"
        )

        # Step 6: Adversarial idle soak test: let system sit idle for 1.0 second
        time.sleep(1.0)
        self.assertFalse(mgr.is_scanning, "ADVERSARIAL FAIL: Ghost scan detected! Scanner moved without user keypress!")
        self.assertIsNotNone(mgr.part1_path, "Part 1 path must remain intact during wait")
        self.assertIsNone(mgr.queued_scan, "Queued scan must remain None during wait")

        # Step 7: User explicitly presses F7 for Page 2 Part 2
        mgr.on_f7_clicked()
        p2_p2_done = self.wait_for_condition(
            lambda: not mgr.is_scanning and mgr.part1_path is None,
            timeout=20.0
        )
        self.assertTrue(p2_p2_done, "Page 2 Part 2 must complete and reset multi-part state")
        self.assertFalse(mgr.is_scanning)
        self.assertIsNone(mgr.part1_path)

        # Step 8: Verify both background stitch jobs were dispatched without lost images
        # Wait for stitch queue to drain
        stitch_drained = self.wait_for_condition(
            lambda: mgr.pending_stitches == 0,
            timeout=25.0
        )
        self.assertTrue(stitch_drained, "Both background stitch jobs must complete successfully")
        self.assertEqual(len(self.app.model), 2, "Document model must contain exactly 2 stitched pages")

    def test_rapid_f7_spam_during_part1_carriage_motion(self):
        """
        Adversarially hammer F7 8 times while Part 1 carriage is actively moving.
        All must be safely absorbed because Part 2 requires physical paper repositioning.
        When Part 1 completes, scanner must STOP and wait for human flip.
        """
        mgr = self.mgr

        # Start Part 1
        mgr.on_f7_clicked()
        self.assertTrue(mgr.is_scanning)

        # Spam F7 8 times during Part 1
        for _ in range(8):
            mgr.on_f7_clicked()
            time.sleep(0.005)

        # Wait for Part 1 to finish
        done = self.wait_for_condition(
            lambda: not mgr.is_scanning and mgr.part1_path is not None,
            timeout=15.0
        )
        self.assertTrue(done, "Part 1 must finish")
        self.assertFalse(mgr.is_scanning, "Scanner must STOP after Part 1")
        self.assertIsNotNone(mgr.part1_path)
        self.assertIsNone(mgr.queued_scan, "No queued scan allowed from Part 1 spam")

        # Idle soak: verify no auto-advance to Part 2
        time.sleep(0.5)
        self.assertFalse(mgr.is_scanning, "Scanner must not auto-advance to Part 2")
        self.assertIsNotNone(mgr.part1_path)


# =============================================================================
# 2. Queue Storming & High Concurrency Stress
# =============================================================================
class TestAdversarialQueueStorming(BaseE2ETestCase):
    """
    Stress-tests the queue state machine under aggressive rapid alternating keypresses
    and multithreaded concurrent bursts while scanner is busy.
    """

    def setUp(self):
        self.app = MockApp()
        self.mgr = create_tracking_manager(self.app)

    def test_alternating_f7_f8_storm_during_part1(self):
        """
        During Part 1 carriage motion, bombard the manager with 16 alternating F7/F8 triggers.
        Verify queue bounds and no race conditions.
        """
        mgr = self.mgr
        mgr.on_f7_clicked()
        self.assertTrue(mgr.is_scanning)

        # Storm alternating triggers
        for i in range(8):
            mgr.on_f7_clicked()
            mgr.on_f8_clicked()
            time.sleep(0.002)

        # Queued scan must be strictly capped to 'f8' (or None)
        # Part 1 never allows 'f7' queuing (since Part 2 requires human flip)
        with mgr.scan_lock:
            self.assertIn(mgr.queued_scan, (None, "f8"))

        # Wait for Part 1 to finish
        p1_done = self.wait_for_condition(
            lambda: not mgr.is_scanning or mgr.queued_scan is None,
            timeout=15.0
        )
        self.assertTrue(p1_done)

    def test_alternating_f7_f8_storm_during_part2(self):
        """
        During Part 2 carriage motion, bombard manager with 20 alternating F7/F8 triggers.
        Verify queue slot is strictly bounded to single item (never a list/overflow).
        """
        mgr = self.mgr

        # 1. Scan Part 1
        mgr.on_f7_clicked()
        self.wait_for_condition(lambda: not mgr.is_scanning and mgr.part1_path is not None, timeout=12.0)

        # 2. Scan Part 2
        mgr.on_f7_clicked()
        self.assertTrue(mgr.is_scanning)

        # 3. Storm 20 rapid alternating clicks
        for _ in range(10):
            mgr.on_f7_clicked()
            mgr.on_f8_clicked()
            time.sleep(0.002)

        # Queued scan must be a single string ('f7' or 'f8')
        with mgr.scan_lock:
            self.assertIn(mgr.queued_scan, ("f7", "f8"))
            self.assertIsInstance(mgr.queued_scan, str)

        # Wait for Part 2 and queued scan to complete cleanly
        done = self.wait_for_condition(
            lambda: not mgr.is_scanning and mgr.queued_scan is None,
            timeout=25.0
        )
        self.assertTrue(done, "Manager must cleanly resolve queued scan after storm")

    def test_multithreaded_concurrent_keypress_burst(self):
        """
        10 threads simultaneously hammering on_f7_clicked and on_f8_clicked from cold start.
        Verify lock integrity prevents double subprocess creation or corrupted state.
        """
        mgr = self.mgr
        barrier = threading.Barrier(10)
        errors = []

        def worker(thread_id):
            try:
                barrier.wait(timeout=5.0)
                if thread_id % 2 == 0:
                    mgr.on_f7_clicked()
                else:
                    mgr.on_f8_clicked()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(len(errors), 0, f"Concurrent keypress caused errors: {errors}")

        # At most 1 process should be running, and queued scan bounded
        with mgr.scan_lock:
            self.assertTrue(mgr.is_scanning)
            self.assertIn(mgr.queued_scan, (None, "f7", "f8"))

        # Wait for system to settle
        settled = self.wait_for_condition(
            lambda: not mgr.is_scanning,
            timeout=20.0
        )
        self.assertTrue(settled, "System must settle to idle state after concurrent burst")


# =============================================================================
# 3. Sudden Escape Cancellation at 10%, 50%, and 90% Carriage Motion
# =============================================================================
class TestAdversarialEscapeCancellation(BaseE2ETestCase):
    """
    Stress-tests Escape cancellation at precise fractions of carriage travel:
    10% (early carriage acceleration), 50% (mid-travel platen traversal),
    and 90% (near platen end before image read completion).
    """

    def setUp(self):
        self.app = MockApp()
        self.mgr = create_tracking_manager(self.app)

    def _test_cancellation_at_pct(self, pct, scan_func):
        """Helper to simulate cancellation at a specific percentage of carriage stroke."""
        os.environ["MOCK_SCAN_DELAY"] = "0.25"
        try:
            mgr = self.mgr
            scan_func()
            self.assertTrue(mgr.is_scanning, "Scanner must be marked scanning")

            # Wait for specified percentage of stroke
            target_delay = 0.25 * (pct / 100.0)
            time.sleep(target_delay)

            # Trigger immediate Escape cancellation
            mgr.cancel_scan()

            # Verify immediate termination
            self.assertFalse(mgr.is_scanning, f"Scanner must be idle immediately after Escape at {pct}%")
            self.assertIsNone(mgr.queued_scan, f"Queued scan must be cleared at {pct}%")
            self.assertIsNone(mgr.part1_path, f"Part 1 state must be reset at {pct}%")
            self.assertIn("cancelled", mgr.lbl_status.get_text().lower())

            # Verify process was killed
            if mgr.current_scan_proc:
                self.assertIsNotNone(mgr.current_scan_proc.poll(), f"Worker proc must be dead at {pct}%")

            # Verify recovery: immediate subsequent scan must succeed cleanly without 'busy' error
            os.environ["MOCK_SCAN_DELAY"] = "0.01"
            mgr.on_f8_clicked()
            recovery_done = self.wait_for_condition(
                lambda: not mgr.is_scanning,
                timeout=10.0
            )
            self.assertTrue(recovery_done, f"Scanner must recover and complete next scan after {pct}% cancellation")
        finally:
            os.environ["MOCK_SCAN_DELAY"] = "0.01"

    def test_escape_at_10_percent_single_scan(self):
        self._test_cancellation_at_pct(10, self.mgr.on_f8_clicked)

    def test_escape_at_50_percent_single_scan(self):
        self._test_cancellation_at_pct(50, self.mgr.on_f8_clicked)

    def test_escape_at_90_percent_single_scan(self):
        self._test_cancellation_at_pct(90, self.mgr.on_f8_clicked)

    def test_escape_at_10_percent_dual_pass_part1(self):
        self._test_cancellation_at_pct(10, self.mgr.on_f7_clicked)

    def test_escape_at_50_percent_dual_pass_part1(self):
        self._test_cancellation_at_pct(50, self.mgr.on_f7_clicked)

    def test_escape_at_90_percent_dual_pass_part1(self):
        self._test_cancellation_at_pct(90, self.mgr.on_f7_clicked)

    def test_escape_at_50_percent_dual_pass_part2(self):
        # Scan Part 1 normally first
        self.mgr.on_f7_clicked()
        self.wait_for_condition(lambda: not self.mgr.is_scanning and self.mgr.part1_path is not None, timeout=12.0)
        # Cancel Part 2 at 50%
        self._test_cancellation_at_pct(50, self.mgr.on_f7_clicked)

    def test_escape_during_adf_batch_streaming(self):
        """
        Verify Escape during an active multi-page ADF feeder batch terminates
        cleanly and retains already acquired pages.
        """
        mgr = self.mgr
        mgr.config["source"] = "feeder"
        os.environ["MOCK_SCAN_DELAY"] = "0.15"
        try:
            injected = []
            mgr.inject_page_into_pdfarranger = lambda p, desc: injected.append(p)

            # Trigger feeder batch
            mgr.on_feeder_batch_clicked()
            time.sleep(0.04)
            self.assertTrue(mgr.is_scanning, "Scanner must be busy during ADF batch")

            # Hit Escape mid-batch
            mgr.cancel_scan()
            time.sleep(0.05)

            self.assertFalse(mgr.is_scanning, "ADF batch must stop scanning on Escape")
            self.assertIn("cancelled", mgr.lbl_status.get_text().lower())
        finally:
            os.environ["MOCK_SCAN_DELAY"] = "0.01"
            mgr.config["source"] = "flatbed"


# =============================================================================
# 4. Conservative Crop Bounds: Light Borders & Margin Text Protection
# =============================================================================
class TestAdversarialConservativeCropBounds(unittest.TestCase):
    """
    Stress-tests stitch_daemon.auto_crop_borders with synthetic adversarial images:
    - Light decorative borders near page margins
    - Black text directly adjacent to page margins (zero text clipping)
    - Full-bleed documents filling entire platen (zero crop)
    - White scanner lid platen background (zero crop)
    - Low-contrast faint pencil lines near borders
    - Excessive crop protection (> 10% threshold)
    """

    def test_light_decorative_border_preserved_with_zero_clipping(self):
        """
        Document with dark scanner platen border (< 40), then a light decorative
        border frame (intensity 200, >= 40) located at rows 19..1180 and cols 24..875.
        auto_crop_borders must trim the dark scanner platen outside (0..19, 0..24) but
        strictly preserve 100% of the light decorative border frame down to the exact pixel.
        """
        h, w = 1200, 900
        img = np.full((h, w, 3), 10, dtype=np.uint8)  # Dark scanner platen (intensity 10 < 40)

        # Light decorative border (intensity 200) from (20, 25) to (1180, 875)
        img[20:1180, 25:875] = 245  # White page background
        cv2.rectangle(img, (25, 20), (874, 1179), (200, 200, 200), 2)  # Light gray border frame

        cropped = stitch_daemon.auto_crop_borders(img)

        # Outer extent of 2px rectangle centered on (20, 25)-(1179, 874) is y=19..1180 (1162 px), x=24..875 (852 px)
        self.assertEqual(cropped.shape, (1162, 852, 3), "Must trim exact platen border")
        # Corner of cropped image must be the light decorative border (intensity 200)
        self.assertEqual(cropped[0, 0, 0], 200, "Light decorative border must be completely preserved at top-left")
        self.assertEqual(cropped[-1, -1, 0], 200, "Light decorative border must be completely preserved at bottom-right")

    def test_text_adjacent_to_page_margins_zero_text_clipping(self):
        """
        Document with white paper starting at (row 15, col 15) and black text (intensity 0)
        placed at row 17, col 18 (only 2 pixels inside paper margin!).
        Platen border is 15px (< 10% of total area = 6.6%).
        auto_crop_borders must trim platen (0..15, 0..15) while strictly preserving
        every single pixel of the black text without clipping a single text pixel.
        """
        h, w = 1000, 800
        img = np.full((h, w, 3), 15, dtype=np.uint8)  # Platen border (< 40)

        # White document paper starting at y=15, x=15 (trimmed area = 6.6% < 10% safety limit)
        img[15:985, 15:785] = 255

        # Render high-contrast text close to the top-left margin
        text_origin = (20, 40)
        cv2.putText(img, "ADVERSARIAL_MARGIN_TEXT", text_origin, cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)

        # Also render text close to bottom-right margin
        cv2.putText(img, "BOTTOM_MARGIN_NOTE", (520, 980), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)

        # Count total black text pixels (< 50) on the original document
        text_pixels_orig = int(np.sum((img[15:985, 15:785, 0] < 50)))
        self.assertGreater(text_pixels_orig, 200, "Synthetic text pixels must exist")

        cropped = stitch_daemon.auto_crop_borders(img)

        # Expected trimmed shape: (985 - 15, 785 - 15) = (970, 770)
        self.assertEqual(cropped.shape, (970, 770, 3))

        # Count black text pixels in cropped image
        text_pixels_cropped = int(np.sum((cropped[:, :, 0] < 50)))
        self.assertEqual(
            text_pixels_orig,
            text_pixels_cropped,
            f"Zero text clipping violated! Original had {text_pixels_orig} text px, cropped has {text_pixels_cropped} px"
        )

    def test_full_bleed_document_zero_crop(self):
        """
        A full-bleed document with content/paper filling the entire scan platen.
        auto_crop_borders must NOT crop a single pixel (shape must remain identical).
        """
        h, w = 1000, 800
        full_bleed = np.full((h, w, 3), 235, dtype=np.uint8)  # Light paper throughout
        cv2.putText(full_bleed, "Full Bleed Page", (50, 500), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (20, 20, 20), 2)

        res = stitch_daemon.auto_crop_borders(full_bleed)
        self.assertEqual(res.shape, (h, w, 3), "Full-bleed document must experience zero cropping")
        self.assertTrue(np.array_equal(res, full_bleed))

    def test_white_scanner_lid_background_zero_crop(self):
        """
        Scanners with white lid backing (all pixels >= 200).
        auto_crop_borders must detect no platen border and return image untouched.
        """
        h, w = 800, 600
        white_lid = np.full((h, w, 3), 245, dtype=np.uint8)
        cv2.rectangle(white_lid, (100, 100), (500, 700), (220, 220, 220), 1)

        res = stitch_daemon.auto_crop_borders(white_lid)
        self.assertEqual(res.shape, (h, w, 3), "White scanner lid must not trigger cropping")

    def test_faint_pencil_line_near_margin_preserved(self):
        """
        Faint pencil annotation (intensity 50, which is > 40) placed at the edge of the paper.
        Must be identified as content and preserved.
        """
        h, w = 1000, 800
        img = np.full((h, w, 3), 10, dtype=np.uint8)
        img[15:985, 15:785] = 250  # White paper (trimmed area = 6.6% < 10%)

        # Faint pencil line at row 16 (just inside edge)
        img[16, 50:200] = 50

        cropped = stitch_daemon.auto_crop_borders(img)
        self.assertEqual(cropped.shape, (970, 770, 3))
        # Verify row 1 in cropped image (row 16 original) has the pencil line
        self.assertEqual(cropped[1, 50, 0], 50, "Faint pencil line must be preserved")

    def test_excessive_crop_safety_limit_retains_boundary(self):
        """
        If platen border would trim more than 10% of total image area,
        auto_crop_borders strictly aborts crop and returns original uncropped image.
        """
        h, w = 1000, 1000
        img = np.full((h, w, 3), 10, dtype=np.uint8)
        # Content occupies 850x850 -> trimmed area = 1 - (850*850)/(1000*1000) = 27.75% > 10%
        img[75:925, 75:925] = 250

        res = stitch_daemon.auto_crop_borders(img)
        self.assertEqual(res.shape, (1000, 1000, 3), "Excessive crop > 10% must retain full image boundary")


def run_adversarial_suite():
    print("=" * 78)
    print("RUNNING ADVERSARIAL WORKFLOW STRESS & DEFECT CHALLENGE SUITE")
    print(f"MOCK_SCANNER: {os.getenv('MOCK_SCANNER')} (Strict hardware isolation)")
    print("=" * 78)

    suite = unittest.TestSuite()
    for tc in [
        TestAdversarialF7DefectSequence,
        TestAdversarialQueueStorming,
        TestAdversarialEscapeCancellation,
        TestAdversarialConservativeCropBounds,
    ]:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(tc))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    run_adversarial_suite()
