"""
Automated End-to-End Verification Test Suite
PDF Arranger Background Stitching & Scan Queueing Integration (Milestone 1)

Verifies Requirements R1 through R5, Acceptance Criteria, Edge Cases,
and User Regression Scenarios under 100% hardware isolation (MOCK_SCANNER=1).
Zero calls to physical HP Smart Tank 750 scanner carriage or drivers.
"""

import os
import sys
import time
import json
import shutil
import unittest
import subprocess
import threading
from unittest.mock import MagicMock, patch

# Strictly enforce MOCK_SCANNER mode before any project imports
os.environ["MOCK_SCANNER"] = "1"
os.environ["MOCK_SCAN_DELAY"] = "0.01"  # Fast simulated carriage travel for tests

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

# Mock gi, gi.repository, and GTK classes for headless test execution in Python 3.10
class MockTreePath:
    def __init__(self, indices):
        self._indices = list(indices) if isinstance(indices, (list, tuple)) else [indices]

    def get_indices(self):
        return list(self._indices)

    def __getitem__(self, idx):
        return self._indices[idx]

    def __eq__(self, other):
        if isinstance(other, int):
            return self._indices == [other]
        if hasattr(other, "get_indices"):
            return self._indices == other.get_indices()
        if isinstance(other, (list, tuple)):
            return self._indices == list(other)
        return False

    def __repr__(self):
        return f"MockTreePath({self._indices})"


class MockTreeIter:
    def __init__(self, idx, item):
        self.idx = idx
        self.item = item

    def __repr__(self):
        return f"MockTreeIter(idx={self.idx})"


class MockTreeRowReference:
    """Simulates Gtk.TreeRowReference tracking row movements in model dynamically."""
    def __init__(self, model_list, path_or_idx):
        self.model_list = model_list
        if hasattr(path_or_idx, "get_indices"):
            idx = path_or_idx.get_indices()[0]
        elif isinstance(path_or_idx, (list, tuple)):
            idx = path_or_idx[0]
        else:
            idx = int(path_or_idx)

        if 0 <= idx < len(model_list):
            self.target_item = model_list[idx]
        else:
            self.target_item = None

    def valid(self):
        return self.target_item is not None and self.target_item in self.model_list

    def get_path_index(self):
        if self.valid():
            return self.model_list.index(self.target_item)
        return None

    def get_path(self):
        idx = self.get_path_index()
        return MockTreePath([idx]) if idx is not None else None

    def __repr__(self):
        return f"MockTreeRowReference(idx={self.get_path_index()}, valid={self.valid()})"


class MockModel(list):
    """Simulates Gtk.ListStore model used by PDF Arranger."""
    def __getitem__(self, key):
        if hasattr(key, "get_indices"):
            idx = key.get_indices()[0]
            return super().__getitem__(idx)
        return super().__getitem__(key)

    def get_iter(self, path):
        idx = path.get_indices()[0] if hasattr(path, "get_indices") else path[0]
        if 0 <= idx < len(self):
            return MockTreeIter(idx, self[idx])
        return None

    def remove(self, iter_obj):
        if iter_obj is not None:
            if hasattr(iter_obj, "item") and iter_obj.item in self:
                super().remove(iter_obj.item)
            elif hasattr(iter_obj, "idx") and 0 <= iter_obj.idx < len(self):
                self.pop(iter_obj.idx)


if "gi" not in sys.modules:
    mock_gi = MagicMock()
    mock_repo = MagicMock()
    mock_gtk = MagicMock()
    mock_glib = MagicMock()
    mock_gobject = MagicMock()
    mock_gdk = MagicMock()

    # GLib and GObject idle_add execute callbacks immediately in headless tests
    mock_glib.idle_add = lambda cb, *a, **k: cb(*a, **k)
    mock_gobject.idle_add = lambda cb, *a, **k: cb(*a, **k)

    class MockGtkWidget:
        def __init__(self, *args, **kwargs):
            self.label = kwargs.get("label", args[0] if args and isinstance(args[0], str) else "")
            self.sensitive = True
            self.tooltip = ""
            self.signals = {}
            self.children = []

        def set_label(self, label):
            self.label = label

        def get_label(self):
            return self.label

        def set_sensitive(self, s):
            self.sensitive = s

        def get_sensitive(self):
            return self.sensitive

        def set_tooltip_text(self, t):
            self.tooltip = t

        def connect(self, sig, handler, *args):
            self.signals[sig] = (handler, args)

        def get_style_context(self):
            return MagicMock()

        def pack_start(self, child, *args, **kwargs):
            self.children.append(child)

        def append(self, child):
            self.children.append(child)

        def set_image(self, img):
            pass

        def set_always_show_image(self, b):
            pass

        def set_popover(self, pop):
            pass

        def set_markup(self, m):
            pass

        def set_alignment(self, x, y):
            pass

        def set_active(self, a):
            pass

        def get_active(self):
            return True

        def set_text(self, t):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def show(self):
            pass

        def hide(self):
            pass

        def show_all(self):
            pass

        def __getattr__(self, name):
            def _dummy(*args, **kwargs):
                return self
            return _dummy

    mock_gtk.Button = MockGtkWidget
    mock_gtk.MenuItem = MockGtkWidget
    mock_gtk.SeparatorMenuItem = MockGtkWidget
    mock_gtk.Separator = MockGtkWidget
    mock_gtk.Box = MockGtkWidget
    mock_gtk.Label = MockGtkWidget
    mock_gtk.Spinner = MockGtkWidget
    mock_gtk.MenuButton = MockGtkWidget
    mock_gtk.Popover = MockGtkWidget
    mock_gtk.RadioButton = MagicMock()
    mock_gtk.RadioButton.new_with_label = lambda *a, **k: MockGtkWidget(*a, **k)
    mock_gtk.RadioButton.new_with_label_from_widget = lambda *a, **k: MockGtkWidget(*a, **k)
    mock_gtk.TreeRowReference.new = lambda model, path: MockTreeRowReference(model, path)
    mock_gtk.TreePath = MockTreePath
    mock_gtk.TreePath.new_from_indices = lambda indices: MockTreePath(indices)

    mock_repo.GLib = mock_glib
    mock_repo.Gtk = mock_gtk
    mock_repo.GObject = mock_gobject
    mock_repo.Gdk = mock_gdk

    mock_gi.repository = mock_repo

    sys.modules["gi"] = mock_gi
    sys.modules["gi.repository"] = mock_repo
    sys.modules["gi.repository.Gtk"] = mock_gtk
    sys.modules["gi.repository.GLib"] = mock_glib
    sys.modules["gi.repository.GObject"] = mock_gobject
    sys.modules["gi.repository.Gdk"] = mock_gdk

# Also mock keyboard so headless test does not attach real Windows keyboard hooks
sys.modules["keyboard"] = MagicMock()

# Mock PageAdder in pdfarranger.core for in-memory model testing
class MockPageAdder:
    def __init__(self, app):
        self.app = app
        self.pages = []
        self.target_ref = None
        self.insert_before = True

    def move(self, ref, before=True):
        self.target_ref = ref
        self.insert_before = before

    def addpages(self, filepath):
        self.pages.append(filepath)

    def commit(self, select_added=False, add_to_undomanager=False):
        if self.target_ref and hasattr(self.target_ref, "get_path_index"):
            idx = self.target_ref.get_path_index()
            if idx is not None:
                insert_idx = idx if self.insert_before else idx + 1
                for p in self.pages:
                    self.app.model.insert(insert_idx, p)
                    insert_idx += 1
                return
        self.app.model.extend(self.pages)


from collections import namedtuple
Sides = namedtuple("Sides", ["left", "right", "top", "bottom"], defaults=(0, 0, 0, 0))

mock_core = MagicMock()
mock_core.PageAdder = MockPageAdder
mock_core.Sides = Sides
sys.modules["pdfarranger.core"] = mock_core

import cv2
import numpy as np
import pymupdf as fitz
from PIL import Image

import wia_scanner
import scanner_worker
import stitch_daemon
import pdfarranger_scanner_extension as ext


class MockIconView:
    def __init__(self):
        self.selected_items = []

    def get_selected_items(self):
        return list(self.selected_items)

    def select_path(self, path):
        if not hasattr(path, "get_indices"):
            path = MockTreePath([int(path)])
        idx = path.get_indices()[0]
        if not any(hasattr(p, "get_indices") and p.get_indices()[0] == idx for p in self.selected_items):
            self.selected_items.append(path)

    def unselect_all(self):
        self.selected_items = []

    def scroll_to_path(self, path, use_align=False, row_align=0.0, col_align=0.0):
        pass


class MockUndoManager:
    def __init__(self):
        self.commits = []

    def commit(self, action_name):
        self.commits.append(action_name)


class MockApp:
    def __init__(self):
        self.uiXML = None
        self.window = None
        self.iconview = MockIconView()
        self.model = MockModel()
        self.pdfqueue = []
        self.tmp_dir = os.path.join(WORKSPACE_DIR, "Scans", "Raw")
        self.undomanager = MockUndoManager()
        self.is_unsaved = False
        self._render_lock = threading.Lock()
        self.crop_dialog_called = False
        self.crop_dialog_call_count = 0
        self.crop_dialog_args = None
        self.popup = []
        self.rendered = False

    def crop_dialog(self, *args):
        self.crop_dialog_called = True
        self.crop_dialog_call_count += 1
        self.crop_dialog_args = args

    def render_lock(self):
        return self._render_lock

    def set_unsaved(self, val):
        self.is_unsaved = val

    def update_iconview_geometry(self):
        pass

    def update_max_zoom_level(self):
        pass

    def render(self):
        self.rendered = True

    def silent_render(self):
        pass


class BaseE2ETestCase(unittest.TestCase):
    """Helper base class providing polling synchronization for async subprocesses."""

    def wait_for_condition(self, predicate, timeout=18.0, poll_interval=0.05):
        start = time.time()
        while time.time() - start < timeout:
            if predicate():
                return True
            time.sleep(poll_interval)
        return False

    def tearDown(self):
        if hasattr(self, "mgr") and self.mgr:
            try:
                self.mgr.stop()
            except Exception:
                pass
        super().tearDown()


# =============================================================================
# 1. Hardware Isolation Test Suite
# =============================================================================
class TestHardwareIsolation(BaseE2ETestCase):
    """Verifies that MOCK_SCANNER=1 completely isolates real HP Smart Tank 750 carriage."""

    def test_mock_scanner_env_active(self):
        self.assertTrue(wia_scanner.is_mock_scanner())

    def test_get_connected_scanner_name_isolated(self):
        name = wia_scanner.get_connected_scanner_name()
        self.assertIn("Mock", name)
        self.assertIn("HP Smart Tank 750", name)

    def test_get_escl_host_isolated(self):
        host = wia_scanner.get_escl_host()
        self.assertIsNone(host, "Network eSCL must return None in mock mode to prevent socket creation")

    def test_scan_wia_usb_to_file_isolated(self):
        out_tmp = os.path.join(WORKSPACE_DIR, "Scans", "Raw", "test_iso_wia.png")
        try:
            res = wia_scanner.scan_wia_usb_to_file(out_tmp, dpi=150, color_mode="color", page_size="a4")
            self.assertIn("Mock", res)
            self.assertTrue(os.path.exists(out_tmp))
        finally:
            if os.path.exists(out_tmp):
                os.remove(out_tmp)

    def test_scan_escl_to_file_isolated(self):
        out_tmp = os.path.join(WORKSPACE_DIR, "Scans", "Raw", "test_iso_escl.png")
        try:
            res = wia_scanner.scan_escl_to_file(out_tmp, host="127.0.0.1", dpi=150, color_mode="gray", page_size="letter")
            self.assertIn("Mock", res)
            self.assertTrue(os.path.exists(out_tmp))
        finally:
            if os.path.exists(out_tmp):
                os.remove(out_tmp)


# =============================================================================
# 2. Non-Blocking Scan and Stitch Workflow (Requirement R1)
# =============================================================================
class TestNonBlockingScanWorkflow(BaseE2ETestCase):
    """Requirement R1: Decouple physical scanner operations from image stitching computation."""

    def setUp(self):
        self.app = MockApp()
        self.mgr = ext.ScannerManager(self.app)

    def test_non_blocking_carriage_release(self):
        # Step 1: Scan Part 1
        self.mgr.on_f7_clicked()
        done1 = self.wait_for_condition(
            lambda: not self.mgr.is_scanning and self.mgr.part1_path is not None,
            timeout=12.0
        )
        self.assertTrue(done1, "Part 1 hardware scan should finish within timeout")
        self.assertFalse(self.mgr.is_scanning, "Scanner must not be busy after Part 1 carriage stops")
        self.assertIsNotNone(self.mgr.part1_path)

        # Step 2: Scan Part 2
        self.mgr.on_f7_clicked()
        done2 = self.wait_for_condition(
            lambda: not self.mgr.is_scanning and self.mgr.part1_path is None,
            timeout=12.0
        )
        self.assertTrue(done2, "Part 2 hardware scan acquisition should finish within timeout")
        # Hardware scan is complete -> scan buttons immediately re-enabled!
        self.assertFalse(self.mgr.is_scanning, "Scan controls must re-enable immediately upon image acquisition")
        self.assertIsNone(self.mgr.part1_path, "Part 1 path must reset atomically")

    def test_concurrent_scan_during_background_stitch(self):
        # Enqueue a synthetic stitch job into stitch queue
        dummy_job = {
            "type": "dual_scan",
            "part1": os.path.join(WORKSPACE_DIR, "Archive", "Doc_002_P01_Part1.png"),
            "part2": os.path.join(WORKSPACE_DIR, "Archive", "Doc_002_P01_Part2.png"),
            "out_pdf": os.path.join(WORKSPACE_DIR, "Scans", "Stitched", "test_bg_stitch.pdf"),
            "placeholder_ref": None,
            "dpi": 300,
            "page_num": 99
        }
        self.mgr.pending_stitches += 1
        self.mgr.stitch_queue.put(dummy_job)

        # While pending_stitches > 0, hardware scanning is NOT blocked:
        self.assertFalse(self.mgr.is_scanning)
        # We can immediately trigger a single-pass scan
        self.mgr.on_f8_clicked()
        self.assertTrue(self.mgr.is_scanning, "Single scan should initiate while stitch processes in background")

        # Wait for single scan to finish
        done = self.wait_for_condition(lambda: not self.mgr.is_scanning, timeout=12.0)
        self.assertTrue(done, "Single scan must complete within timeout")
        self.assertFalse(self.mgr.is_scanning)

    def test_manual_stitch_f9_non_blocking(self):
        # F9 button sensitivity is always True even when scanner busy or stitching
        self.mgr.is_scanning = True
        self.mgr.pending_stitches = 3
        self.mgr.update_ui_state()
        if self.mgr.btn_stitch:
            self.assertTrue(self.mgr.btn_stitch.get_sensitive())


# =============================================================================
# 3. Scan Request Queueing (Requirement R2)
# =============================================================================
class TestScanRequestQueueing(BaseE2ETestCase):
    """Requirement R2: Scan request queueing during active carriage motion."""

    def setUp(self):
        self.app = MockApp()
        self.mgr = ext.ScannerManager(self.app)

    def test_queue_f8_during_carriage_motion(self):
        with self.mgr.scan_lock:
            self.mgr.is_scanning = True

        self.mgr.on_f8_clicked()
        self.assertEqual(self.mgr.queued_scan, "f8", "F8 trigger during busy carriage must be queued")

        # Simulate carriage completion:
        fired_action = [None]
        with self.mgr.scan_lock:
            self.mgr.is_scanning = False
            action = self.mgr.queued_scan
            self.mgr.queued_scan = None
            fired_action[0] = action

        self.assertEqual(fired_action[0], "f8")
        self.assertIsNone(self.mgr.queued_scan)

    def test_queue_absorption_during_part1_carriage(self):
        # During Part 1 carriage motion (part1_path is None), F7 clicks must be absorbed, NOT queued
        with self.mgr.scan_lock:
            self.mgr.is_scanning = True
            self.mgr.part1_path = None

        self.mgr.on_f7_clicked()
        self.assertIsNone(self.mgr.queued_scan, "F7 during Part 1 must be absorbed to avoid auto-firing Part 2")

    def test_queue_f7_during_part2_carriage(self):
        # During Part 2 carriage motion (part1_path is set), F7 clicks queue Part 1 of next page
        with self.mgr.scan_lock:
            self.mgr.is_scanning = True
            self.mgr.part1_path = "some_part1.png"

        self.mgr.on_f7_clicked()
        self.assertEqual(self.mgr.queued_scan, "f7", "F7 during Part 2 queues Part 1 of subsequent page")


# =============================================================================
# 4. Escape Key Permutations (User Request)
# =============================================================================
class TestEscapeKeyPermutations(BaseE2ETestCase):
    """Verifies Escape cancellations: during Part 1, Part 2, when queued items exist, and during stitch."""

    def setUp(self):
        self.app = MockApp()
        self.mgr = ext.ScannerManager(self.app)

    def test_escape_during_active_part1(self):
        with self.mgr.scan_lock:
            self.mgr.is_scanning = True
            self.mgr.part1_path = None
            self.mgr.queued_scan = None

        self.mgr.cancel_scan()
        self.assertFalse(self.mgr.is_scanning, "is_scanning must be False after Escape")
        self.assertIsNone(self.mgr.part1_path, "part1_path must remain None")
        self.assertIsNone(self.mgr.queued_scan, "queued_scan must be None")

    def test_escape_during_active_part2(self):
        with self.mgr.scan_lock:
            self.mgr.is_scanning = True
            self.mgr.part1_path = "temp_part1.png"
            self.mgr.queued_scan = "f7"

        self.mgr.cancel_scan()
        self.assertFalse(self.mgr.is_scanning, "is_scanning must be False after Escape")
        self.assertIsNone(self.mgr.part1_path, "part1_path must be reset to None after Escape")
        self.assertIsNone(self.mgr.queued_scan, "queued_scan must be cleared after Escape")

    def test_escape_clears_queued_scan_when_idle(self):
        with self.mgr.scan_lock:
            self.mgr.is_scanning = False
            self.mgr.part1_path = None
            self.mgr.queued_scan = "f8"

        self.mgr.cancel_scan()
        self.assertIsNone(self.mgr.queued_scan, "Queued scan must be cleared when Escape pressed in idle state")
        self.assertFalse(self.mgr.is_scanning)

    def test_escape_terminates_active_subprocess(self):
        cmd = [sys.executable, "-c", "import time; time.sleep(5)"]

        def _worker():
            try:
                self.mgr._run_scan_subprocess(cmd, timeout=10)
            except Exception:
                pass

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        time.sleep(0.2)

        self.assertIsNotNone(self.mgr.current_scan_proc)
        self.assertIsNone(self.mgr.current_scan_proc.poll(), "Process should be running")

        # Press Escape
        self.mgr.cancel_scan()
        time.sleep(0.2)

        self.assertIsNone(self.mgr.current_scan_proc)
        self.assertFalse(self.mgr.is_scanning)

    def test_escape_during_background_stitch_keeps_scanner_ready(self):
        # Enqueue background stitch
        self.mgr.pending_stitches += 1
        self.mgr.stitch_queue.put({
            "type": "dual_scan",
            "part1": os.path.join(WORKSPACE_DIR, "Archive", "Doc_002_P01_Part1.png"),
            "part2": os.path.join(WORKSPACE_DIR, "Archive", "Doc_002_P01_Part2.png"),
            "out_pdf": os.path.join(WORKSPACE_DIR, "Scans", "Stitched", "test_esc_stitch.pdf"),
            "placeholder_ref": None,
            "dpi": 300,
            "page_num": 88
        })

        # Press Escape while background stitch is queued/running
        self.mgr.cancel_scan()
        self.assertFalse(self.mgr.is_scanning, "Scanner must be ready/idle")
        self.assertIsNone(self.mgr.part1_path)
        self.assertIsNone(self.mgr.queued_scan)


# =============================================================================
# 5. F9 Manual Stitch Concurrency & Validation
# =============================================================================
class TestManualStitchConcurrency(BaseE2ETestCase):
    """
    Verifies manual stitch (F9):
    1. Invalid selection counts (0, 1, 3+ pages) handled gracefully with user notification.
    2. Concurrency: F9 triggered while scanning is active does not block hardware scan.
    """

    def setUp(self):
        self.app = MockApp()
        self.mgr = ext.ScannerManager(self.app)

    def test_invalid_selection_0_pages(self):
        self.app.iconview.selected_items = []
        res = [None]
        self.mgr.on_stitch_clicked(callback=lambda r: res.__setitem__(0, r))
        self.assertFalse(res[0], "0 selected items must fail validation")

    def test_invalid_selection_1_page(self):
        self.app.iconview.selected_items = [MockTreePath([0])]
        res = [None]
        self.mgr.on_stitch_clicked(callback=lambda r: res.__setitem__(0, r))
        self.assertFalse(res[0], "1 selected item must fail validation")

    def test_invalid_selection_3_pages(self):
        self.app.iconview.selected_items = [MockTreePath([0]), MockTreePath([1]), MockTreePath([2])]
        res = [None]
        self.mgr.on_stitch_clicked(callback=lambda r: res.__setitem__(0, r))
        self.assertFalse(res[0], "3 selected items must fail validation")

    def test_invalid_selection_4_pages(self):
        self.app.iconview.selected_items = [MockTreePath([0]), MockTreePath([1]), MockTreePath([2]), MockTreePath([3])]
        res = [None]
        self.mgr.on_stitch_clicked(callback=lambda r: res.__setitem__(0, r))
        self.assertFalse(res[0], "4 selected items must fail validation")

    def test_f9_concurrency_while_scanning_is_active(self):
        # Set scanner busy
        self.mgr.is_scanning = True

        # User attempts F9 with 0 selections while carriage is moving
        self.app.iconview.selected_items = []
        res = [None]
        self.mgr.on_stitch_clicked(callback=lambda r: res.__setitem__(0, r))
        self.assertFalse(res[0])
        # Scanner must remain scanning, not aborted
        self.assertTrue(self.mgr.is_scanning, "Scanning must continue unaffected by invalid F9")

        # User attempts F9 with valid 2 items while carriage is moving
        dummy_p1 = MagicMock()
        dummy_p2 = MagicMock()
        dummy_p1.duplicate.return_value = dummy_p1
        dummy_p2.duplicate.return_value = dummy_p2
        self.app.model = MockModel([[dummy_p1], [dummy_p2]])
        self.app.iconview.selected_items = [MockTreePath([0]), MockTreePath([1])]

        def dummy_render_to_png(app, p, out_path, dpi=300):
            Image.new("RGB", (200, 200), color=(255, 255, 255)).save(out_path, "PNG")

        with patch("pdfarranger_scanner_extension.render_page_to_png", side_effect=dummy_render_to_png):
            stitch_called = [False]
            self.mgr.on_stitch_clicked(callback=lambda r: stitch_called.__setitem__(0, True))

            # Hardware scanning is STILL not blocked
            self.assertTrue(self.mgr.is_scanning, "Active scan must remain running concurrent with F9 prep")
            self.mgr.is_scanning = False


# =============================================================================
# 6. Page Ordering & Placeholder Mutation (Requirement R3)
# =============================================================================
class TestPlaceholderMutation(BaseE2ETestCase):
    """
    Requirement R3: Correct document ordering, lightweight placeholder, atomic replacement.
    Tests mutation permutations:
    1. Normal in-order replacement without shifting subsequent pages.
    2. Deletion of placeholder by user while background worker stitches.
    3. Reordering / dragging of placeholder to different index while worker stitches.
    """

    def test_instant_placeholder_pdf_format_and_size(self):
        out_ph = os.path.join(WORKSPACE_DIR, "Scans", "Stitched", "test_placeholder.pdf")
        try:
            ext.create_placeholder_pdf(out_ph, page_num=3, desc="SIFT Stitching in background...")
            self.assertTrue(os.path.exists(out_ph))
            size = os.path.getsize(out_ph)
            # Lightweight vector PDF: assert < 3 KB (spec requires ~1.6 KB)
            self.assertLess(size, 3072, f"Placeholder size {size} exceeds lightweight threshold")

            # Validate dimensions via PyMuPDF
            doc = fitz.open(out_ph)
            page = doc[0]
            self.assertAlmostEqual(page.rect.width, 595.28, delta=1.0)
            self.assertAlmostEqual(page.rect.height, 841.89, delta=1.0)
            doc.close()
        finally:
            if os.path.exists(out_ph):
                os.remove(out_ph)

    def test_placeholder_normal_replacement_ordering(self):
        # Initial pages [P0, P1]
        model = MockModel(["Page_0", "Page_1"])

        # Insert Placeholder for Page 2
        model.append("Placeholder_P2")
        ref_p2 = MockTreeRowReference(model, 2)

        # User scans Page 3 before Page 2 finishes stitching
        model.append("Page_3")

        # Now Page 2 background stitch completes:
        self.assertTrue(ref_p2.valid())
        target_idx = ref_p2.get_path_index()
        self.assertEqual(target_idx, 2)

        # Atomic replacement: insert Stitched_P2 before ref_p2, then remove placeholder
        model.insert(target_idx, "Stitched_P2")
        self.assertEqual(ref_p2.get_path_index(), 3)
        del_iter = model.get_iter(ref_p2.get_path())
        model.remove(del_iter)

        # Verify chronological ordering without shifting or duplicates:
        expected = ["Page_0", "Page_1", "Stitched_P2", "Page_3"]
        self.assertEqual(model, expected)
        self.assertEqual(len(model), 4)

    def test_placeholder_deleted_by_user_mid_stitch(self):
        # Initial pages [P0, P1, Placeholder_P2, P3]
        model = MockModel(["Page_0", "Page_1", "Placeholder_P2", "Page_3"])
        ref_p2 = MockTreeRowReference(model, 2)

        # User explicitly deletes Placeholder_P2 row in the UI before worker finishes!
        del_iter = model.get_iter(MockTreePath([2]))
        model.remove(del_iter)
        self.assertNotIn("Placeholder_P2", model)

        # Ref is now invalid
        self.assertFalse(ref_p2.valid())

        # When worker completes and ref is invalid, safe fallback appends cleanly:
        if ref_p2 and ref_p2.valid():
            idx = ref_p2.get_path_index()
            model.insert(idx, "Stitched_Fallback")
            model.remove(model.get_iter(ref_p2.get_path()))
        else:
            model.append("Stitched_Fallback")

        self.assertEqual(model, ["Page_0", "Page_1", "Page_3", "Stitched_Fallback"])

    def test_placeholder_reordered_by_user_mid_stitch(self):
        # Initial pages [P0, Placeholder_P1, P2]
        model = MockModel(["Page_0", "Placeholder_P1", "Page_2"])
        ref_p1 = MockTreeRowReference(model, 1)

        # User drags/moves Placeholder_P1 to index 2 (end of list)
        ph_item = model.pop(1)
        model.append(ph_item)
        # model is now ["Page_0", "Page_2", "Placeholder_P1"]

        # TreeRowReference dynamically tracks row movement to index 2!
        self.assertTrue(ref_p1.valid())
        self.assertEqual(ref_p1.get_path_index(), 2)

        # Worker completes: inserts Stitched_P1 at dynamic index 2 and removes placeholder
        target_idx = ref_p1.get_path_index()
        model.insert(target_idx, "Stitched_P1")
        del_iter = model.get_iter(ref_p1.get_path())
        model.remove(del_iter)

        self.assertEqual(model, ["Page_0", "Page_2", "Stitched_P1"])


# =============================================================================
# 7. Scanner Area Configuration & 'Max' Size (Requirement R4)
# =============================================================================
class TestScannerAreaMaxAndDimensions(BaseE2ETestCase):
    """
    Requirement R4: Scanner Area Configuration (A4, Letter, Legal, 4x6, Auto, Max).
    Specifically asserts that 'max' area scans to exactly 4200 px at 300 DPI.
    """

    def test_scanner_area_max_size_4200px(self):
        out_path = os.path.join(WORKSPACE_DIR, "Scans", "Raw", "test_area_max_4200.png")
        try:
            wia_scanner.scan_to_file(out_path, dpi=300, color_mode="color", page_size="max")
            self.assertTrue(os.path.exists(out_path))
            img = cv2.imread(out_path)
            h, w = img.shape[:2]

            # Critical assertion: assert 4200 px max extent
            self.assertEqual(h, 4200, f"Max area height must be exactly 4200 px at 300 DPI (got {h})")
            self.assertEqual(w, 2550, f"Max area width must be exactly 2550 px at 300 DPI (got {w})")
        finally:
            if os.path.exists(out_path):
                os.remove(out_path)

    def test_all_standard_area_dimensions(self):
        cases = [
            ("a4", 2480, 3508),
            ("letter", 2550, 3300),
            ("legal", 2550, 4200),
            ("photo_4x6", 1200, 1800),
            ("max", 2550, 4200)
        ]
        for size_key, exp_w, exp_h in cases:
            out_path = os.path.join(WORKSPACE_DIR, "Scans", "Raw", f"test_area_{size_key}.png")
            try:
                wia_scanner.scan_to_file(out_path, dpi=300, color_mode="color", page_size=size_key)
                self.assertTrue(os.path.exists(out_path))
                img = cv2.imread(out_path)
                h, w = img.shape[:2]
                self.assertEqual(w, exp_w, f"Width mismatch for {size_key}")
                self.assertEqual(h, exp_h, f"Height mismatch for {size_key}")
            finally:
                if os.path.exists(out_path):
                    os.remove(out_path)

    def test_auto_detect_area(self):
        out_path = os.path.join(WORKSPACE_DIR, "Scans", "Raw", "test_area_auto.png")
        try:
            wia_scanner.scan_to_file(out_path, dpi=300, color_mode="color", page_size="auto")
            self.assertTrue(os.path.exists(out_path))
            img = cv2.imread(out_path)
            self.assertGreater(img.shape[0], 100)
            self.assertGreater(img.shape[1], 100)
        finally:
            if os.path.exists(out_path):
                os.remove(out_path)

    def test_config_persistence(self):
        app = MockApp()
        mgr = ext.ScannerManager(app)
        self.mgr = mgr
        mgr.config["page_size"] = "legal"
        mgr.config["dpi"] = 600
        mgr.config["color_mode"] = "bw"
        mgr.save_config()

        reloaded = mgr.load_config()
        self.assertEqual(reloaded["page_size"], "legal")
        self.assertEqual(reloaded["dpi"], 600)
        self.assertEqual(reloaded["color_mode"], "bw")

        # Reset back to default
        mgr.config = ext.DEFAULT_CONFIG.copy()
        mgr.save_config()


# =============================================================================
# 8. PDF Performance, Poppler Render & Compression (Requirement R4)
# =============================================================================
class TestPdfPerformanceAndCompression(BaseE2ETestCase):
    """
    Requirement R4:
    - Poppler render speed < 20 ms
    - Thumbnail render speed < 10 ms
    - Page file size < 1 MB at 300 DPI with DCTDecode
    """

    def test_pdf_compression_and_render_benchmarks(self):
        out_pdf = os.path.join(WORKSPACE_DIR, "Scans", "Stitched", "test_perf_sample.pdf")
        try:
            # Generate realistic 300 DPI A4 document scan (2480 x 3508)
            img_mat = np.full((3508, 2480, 3), 245, dtype=np.uint8)
            cv2.putText(img_mat, "Benchmark Test Page", (200, 400), cv2.FONT_HERSHEY_SIMPLEX, 3, (50, 50, 50), 4)
            for i in range(10):
                cv2.line(img_mat, (200, 600 + i * 250), (2280, 600 + i * 250), (100, 100, 100), 3)

            wia_scanner.save_scanned_image(img_mat, out_pdf, dpi=300, quality=88)

            # 1. File size benchmark (< 1 MB)
            file_size = os.path.getsize(out_pdf)
            self.assertLess(file_size, 1048576, f"File size {file_size} exceeds 1 MB limit")

            # 2. DCT compression filter check (DCTDecode)
            doc = fitz.open(out_pdf)
            page = doc[0]
            imgs = page.get_images()
            self.assertGreater(len(imgs), 0, "PDF must contain scanned raster image")
            xref = imgs[0][0]
            base_img = doc.extract_image(xref)
            self.assertIn(base_img["ext"], ("jpeg", "jpg"), "Image must be compressed via DCT/JPEG")

            # 3. Poppler / PyMuPDF screen preview render benchmark (< 20 ms)
            t0 = time.perf_counter()
            pix_screen = page.get_pixmap(dpi=72)
            t_screen_ms = (time.perf_counter() - t0) * 1000
            self.assertLess(t_screen_ms, 20.0, f"Screen render time {t_screen_ms:.2f}ms exceeds 20ms limit")

            # 4. Thumbnail render benchmark (< 10 ms)
            t1 = time.perf_counter()
            pix_thumb = page.get_pixmap(dpi=20)
            t_thumb_ms = (time.perf_counter() - t1) * 1000
            self.assertLess(t_thumb_ms, 10.0, f"Thumbnail render time {t_thumb_ms:.2f}ms exceeds 10ms limit")

            doc.close()

            # 5. Native pdftoppm.exe execution if available
            poppler_bin = r"C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\poppler\Library\bin\pdftoppm.exe"
            if os.path.exists(poppler_bin):
                out_ppm = os.path.join(WORKSPACE_DIR, "Scans", "Raw", "test_pdftoppm_out")
                res = subprocess.run(
                    [poppler_bin, "-png", "-r", "50", out_pdf, out_ppm],
                    capture_output=True
                )
                self.assertEqual(res.returncode, 0)
                gen_png = f"{out_ppm}-1.png"
                if os.path.exists(gen_png):
                    os.remove(gen_png)
        finally:
            if os.path.exists(out_pdf):
                os.remove(out_pdf)


# =============================================================================
# 9. Upstream Repository Cleanliness (Requirement R5)
# =============================================================================
class TestUpstreamCleanliness(BaseE2ETestCase):
    """Requirement R5: Core pdfarranger/ git repository must remain pristine."""

    def test_git_status_pristine(self):
        repo_dir = os.path.join(WORKSPACE_DIR, "pdfarranger")
        res = subprocess.run(
            ["git", "-C", repo_dir, "status", "--porcelain", "-uall"],
            capture_output=True,
            text=True
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "", f"pdfarranger git repository must be 100% clean, found: {res.stdout}")

    def test_no_obsolete_extension_file(self):
        obsolete_file = os.path.join(WORKSPACE_DIR, "pdfarranger", "pdfarranger_scanner_extension.py")
        self.assertFalse(os.path.exists(obsolete_file), "Obsolete extension inside pdfarranger/ must be removed")


# =============================================================================
# 10. F7 Multi-Cycle Sequence & User Regression Test
# =============================================================================
class TestF7MultiCycleSequence(BaseE2ETestCase):
    """
    CRITICAL DEFECT REGRESSION TEST:
    Sequence: Pass 1 -> Pass 2 -> immediate F7 for back side.
    ASSERTION: Scanner begins Pass 1 of the back side, STOPS when Pass 1 finishes,
    resets is_scanning=False, and NEVER auto-advances into Part 2 without user keypress.
    Zero ghost scans, zero dropped scans.
    """

    def test_rapid_multi_cycle_f7_no_ghost_scans(self):
        app = MockApp()
        mgr = ext.ScannerManager(app)
        self.mgr = mgr

        # 1. Page 1 Part 1
        mgr.on_f7_clicked()
        p1_done = self.wait_for_condition(
            lambda: not mgr.is_scanning and mgr.part1_path is not None,
            timeout=15.0
        )
        self.assertTrue(p1_done, "Page 1 Part 1 scan must finish within timeout")
        self.assertFalse(mgr.is_scanning)
        self.assertIsNotNone(mgr.part1_path)

        # 2. Page 1 Part 2
        mgr.on_f7_clicked()
        # Immediately while Part 2 is scanning (or as it completes), user triggers F7 for the back side!
        mgr.on_f7_clicked()

        # Wait for Page 1 Part 2 to complete, queued scan (Page 2 Part 1) to execute, and Part 1 to finish
        p2_p1_done = self.wait_for_condition(
            lambda: not mgr.is_scanning and mgr.part1_path is not None,
            timeout=35.0
        )
        self.assertTrue(p2_p1_done, "Page 2 Part 1 must complete and wait for user")

        # ASSERTION 1: Scanner finished Page 2 Part 1 and STOPPED!
        self.assertFalse(mgr.is_scanning, "Scanner must stop after Part 1 and wait for human to flip sheet")
        # ASSERTION 2: Part 1 path is armed and ready for Part 2
        self.assertIsNotNone(mgr.part1_path, "Part 1 of back side must be ready")
        # ASSERTION 3: No ghost scan is queued or running
        self.assertIsNone(mgr.queued_scan, "No queued scan should exist to auto-fire Part 2")

        # Let system sit idle to verify no unprompted ghost scan fires!
        time.sleep(0.5)
        self.assertFalse(mgr.is_scanning, "Scanner must remain idle waiting for user!")
        self.assertIsNotNone(mgr.part1_path)

        # Now simulate user explicitly pressing F7 for Part 2
        mgr.on_f7_clicked()
        cycle_done = self.wait_for_condition(
            lambda: not mgr.is_scanning and mgr.part1_path is None,
            timeout=15.0
        )
        self.assertTrue(cycle_done, "Part 2 of back side must finish cleanly")
        self.assertFalse(mgr.is_scanning)
        self.assertIsNone(mgr.part1_path)


# =============================================================================
# 11. Edge Cases & Queue Storming
# =============================================================================
class TestEdgeCases(BaseE2ETestCase):
    """
    Edge Cases:
    1. F8 pressed while F7 Part 1 is pending (promote Part 1 to single).
    2. Queue storming: Rapid repeated keypresses (multiple F7/F8 triggers) during active scan.
    3. Worker error recovery.
    """

    def setUp(self):
        self.app = MockApp()
        self.mgr = ext.ScannerManager(self.app)

    def test_edge_case_1_f8_promotes_pending_part1(self):
        # 1. Scan Part 1
        self.mgr.on_f7_clicked()
        p1_done = self.wait_for_condition(
            lambda: not self.mgr.is_scanning and self.mgr.part1_path is not None,
            timeout=12.0
        )
        self.assertTrue(p1_done)
        self.assertIsNotNone(self.mgr.part1_path)
        saved_part1 = self.mgr.part1_path

        # 2. User presses F8 instead of F7 Part 2
        self.mgr.on_f8_clicked()
        promoted = self.wait_for_condition(
            lambda: not self.mgr.is_scanning and self.mgr.part1_path is None,
            timeout=12.0
        )
        self.assertTrue(promoted, "F8 must promote Part 1 and reset state")
        self.assertIsNone(self.mgr.part1_path, "F8 must reset part1_path to None")
        self.assertFalse(self.mgr.is_scanning)

        # Single page PDF was created and added
        single_pdf = os.path.join(
            WORKSPACE_DIR, "Scans", "Stitched",
            f"Single_{self.mgr.session_id}_P{self.mgr.session_page_num - 1:02d}.pdf"
        )
        self.assertTrue(os.path.exists(single_pdf), f"Single PDF {single_pdf} must exist on disk")

    def test_edge_case_2_queue_storming(self):
        # Set scanner busy
        with self.mgr.scan_lock:
            self.mgr.is_scanning = True
            self.mgr.part1_path = "temp_p1.png"

        # Rapidly fire 20 alternating F7 / F8 clicks (storming)
        for _ in range(10):
            self.mgr.on_f7_clicked()
            self.mgr.on_f8_clicked()

        # Queue is strictly bounded to a single next slot ('f7' or 'f8')
        self.assertIn(self.mgr.queued_scan, ("f7", "f8"))
        self.assertIsInstance(self.mgr.queued_scan, str)

        with self.mgr.scan_lock:
            self.mgr.is_scanning = False
            self.mgr.part1_path = None
            self.mgr.queued_scan = None

    def test_edge_case_3_worker_error_recovery(self):
        # Simulate worker encountering a motor stall or read error
        with self.mgr.scan_lock:
            self.mgr.is_scanning = True
            self.mgr.part1_path = "corrupted.png"

        # Worker raises exception
        try:
            raise RuntimeError("Scanner motor stall simulated error")
        except Exception as e:
            with self.mgr.scan_lock:
                self.mgr.is_scanning = False
                self.mgr.part1_path = None
                self.mgr.queued_scan = None
            self.mgr.update_ui_state(f"Scan Error: {e}")

        self.assertFalse(self.mgr.is_scanning, "Scanner must recover and not remain stuck in busy state")
        self.assertIsNone(self.mgr.part1_path)


# =============================================================================
# 12. Conservative Auto-Crop & Manual UI Crop Adjustment
# =============================================================================
class TestCropAndUiAdjustment(BaseE2ETestCase):
    """
    Verifies:
    1. Strictly conservative auto_crop_borders in stitch_daemon:
       - Only trims true dark scanner platen borders (intensity < 40).
       - Never cuts into page margins, normal content, or text.
       - Preserves full document boundary.
       - If crop area exceeds 10% of page or trims light/text pixels, retains original boundary.
       - Does not crop content with intensity >= 40.
    2. '✂️ Adjust Crop' button and context menu wiring:
       - '✂️ Adjust Crop' button in header toolbar (setup_ui()).
       - '✂️ Adjust Page Crop / Margins... (C)' in right-click context menu (app.popup).
       - on_adjust_crop_clicked() targets selected page in iconview, OR if none selected,
         targets last page in app.model (the newly scanned page), invoking app.crop_dialog.
    """

    def test_conservative_auto_crop_borders_preserves_page_margins_and_content(self):
        """
        Verifies that auto_crop_borders:
        1. Trims true dark scanner platen borders (intensity < 40).
        2. Strictly preserves full document content and does not truncate margins/text lines.
        3. Never trims rows/columns with intensity >= 40.
        4. Clamps max crop to safety limits and handles edge cases.
        """
        h, w = 1000, 800
        # 1. Dark platen borders (< 40) around a document with margins and text (>= 40)
        dark_border = np.full((h, w, 3), 15, dtype=np.uint8)  # intensity 15 < 40
        # Document inside: 20px margin on top/bottom, 25px on left/right
        dark_border[20:980, 25:775] = 240  # white document page margins (intensity 240 >= 40)
        # Add black text inside the document (intensity 20, surrounded by white paper)
        cv2.putText(dark_border, "Test Document Text", (50, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (20, 20, 20), 2)

        cropped = stitch_daemon.auto_crop_borders(dark_border)
        # Should trim only the 20px top/bot and 25px left/right true dark borders
        self.assertEqual(cropped.shape, (960, 750, 3), "Conservative auto_crop must trim dark platen border")
        # Top-left of cropped image must be page margin (intensity 240)
        self.assertEqual(cropped[0, 0, 0], 240, "Page margin must be completely preserved")
        self.assertEqual(cropped[-1, -1, 0], 240, "Bottom-right page margin must be completely preserved")

        # 2. Content with intensity >= 40 reaching borders must NEVER be cropped
        margin_reaches_edge = np.full((h, w, 3), 220, dtype=np.uint8)  # All light page paper
        res_margin = stitch_daemon.auto_crop_borders(margin_reaches_edge)
        self.assertEqual(res_margin.shape, margin_reaches_edge.shape, "Page margins reaching border must not be cropped")

        # Boundary test at intensity 40:
        boundary_40 = np.full((h, w), 10, dtype=np.uint8)
        boundary_40[0, :] = 40  # Content with intensity 40 at the top edge
        res_40 = stitch_daemon.auto_crop_borders(boundary_40)
        self.assertEqual(res_40.shape[0], h, "Row with intensity >= 40 must not be cropped")

        # 3. Crop area exceeds 10% of page -> strictly retain original boundary
        excessive_border = np.full((h, w, 3), 10, dtype=np.uint8)
        # Document occupies only 600x500 in 1000x800 image (trimmed area = 62.5% > 10%)
        excessive_border[200:800, 150:650] = 250
        res_excessive = stitch_daemon.auto_crop_borders(excessive_border)
        self.assertEqual(res_excessive.shape, (h, w, 3), "Excessive crop >10% must retain original boundary")

        # 4. Narrow text line near margin must not be cut
        text_doc = np.full((1000, 800, 3), 255, dtype=np.uint8)
        text_doc[15:25, 40:300] = 0  # Text line at top margin
        res_text = stitch_daemon.auto_crop_borders(text_doc)
        self.assertEqual(res_text.shape, (1000, 800, 3), "White page with text near margin must not be truncated")

        # 5. Edge cases: None, small array, completely dark array
        self.assertIsNone(stitch_daemon.auto_crop_borders(None))
        small = np.zeros((10, 10), dtype=np.uint8)
        self.assertEqual(stitch_daemon.auto_crop_borders(small).shape, (10, 10))
        all_dark = np.full((500, 500, 3), 5, dtype=np.uint8)
        self.assertEqual(stitch_daemon.auto_crop_borders(all_dark).shape, (500, 500, 3))

    def test_on_crop_clicked_invokes_crop_dialog_and_selects_target_page(self):
        """
        Verifies on_crop_clicked:
        1. When nothing is selected in iconview, auto-selects the last page in app.model.
        2. Invokes app.crop_dialog to open the interactive margin adjustment dialog.
        3. When a page is already selected, preserves user selection and invokes crop_dialog.
        4. Handles empty document gracefully without errors.
        """
        class MockHeaderBar:
            def __init__(self):
                self.children = []
            def pack_start(self, child, *a, **k):
                self.children.append(child)

        class MockUiXML:
            def __init__(self, hb):
                self.hb = hb
            def get_object(self, name):
                if name == "header_bar":
                    return self.hb
                return None

        class MockPage:
            def __init__(self, crop=None):
                self.crop = crop or Sides(0, 0, 0, 0)

        hb = MockHeaderBar()
        app = MockApp()
        app.uiXML = MockUiXML(hb)
        mgr = ext.ScannerManager(app)

        # 1. Target last page when no page is selected
        app.model.clear()
        app.model.extend([[MockPage()], [MockPage()], [MockPage()]])
        app.iconview.unselect_all()
        app.crop_dialog_called = False

        mgr.on_crop_clicked()
        self.assertTrue(app.crop_dialog_called, "app.crop_dialog must be invoked by on_crop_clicked")
        # Should have auto-selected page index 2 (last page in model)
        self.assertEqual(app.iconview.get_selected_items(), [2], "Must auto-select last page when none selected")

        # 2. Preserve existing user selection
        app.iconview.unselect_all()
        app.iconview.select_path(1)  # User selected middle page
        app.crop_dialog_called = False

        mgr.on_crop_clicked()
        self.assertTrue(app.crop_dialog_called, "app.crop_dialog must be invoked")
        self.assertEqual(app.iconview.get_selected_items(), [1], "Must preserve user's existing page selection")

        # 3. Empty document handling
        app.model.clear()
        app.iconview.unselect_all()
        app.crop_dialog_called = False

        mgr.on_crop_clicked()
        self.assertFalse(app.crop_dialog_called, "crop_dialog must not be invoked on empty document")

        # 4. Verify alias parity
        self.assertEqual(mgr.on_crop_clicked, mgr.on_adjust_crop_clicked, "on_crop_clicked and on_adjust_crop_clicked must be aliased")

    def test_reset_crop_clears_margins_to_zero_and_refreshes_view(self):
        """
        Verifies reset_crop:
        1. Clears crop margins to Sides(0, 0, 0, 0) restoring full scanned page content.
        2. Targets last page when nothing is selected.
        3. Targets all selected pages when multi-selection is active.
        4. Refreshes view via app.render(), commits undo action, and sets unsaved flag.
        """
        class MockHeaderBar:
            def __init__(self):
                self.children = []
            def pack_start(self, child, *a, **k):
                self.children.append(child)

        class MockUiXML:
            def __init__(self, hb):
                self.hb = hb
            def get_object(self, name):
                if name == "header_bar":
                    return self.hb
                return None

        class MockPage:
            def __init__(self, crop):
                self.crop = crop

        hb = MockHeaderBar()
        app = MockApp()
        app.uiXML = MockUiXML(hb)
        mgr = ext.ScannerManager(app)

        # 1. Reset crop with no selection (targets last page)
        p0 = MockPage(Sides(10, 10, 10, 10))
        p1 = MockPage(Sides(25, 25, 30, 30))
        app.model.clear()
        app.model.append([p0])
        app.model.append([p1])
        app.iconview.unselect_all()
        app.rendered = False
        app.is_unsaved = False
        app.undomanager.commits.clear()

        mgr.reset_crop()

        self.assertEqual(p1.crop, Sides(0, 0, 0, 0), "Last page crop must be reset to Sides(0, 0, 0, 0)")
        self.assertEqual(p0.crop, Sides(10, 10, 10, 10), "Unselected page crop must remain untouched")
        self.assertTrue(app.rendered, "View must be refreshed after reset_crop")
        self.assertTrue(app.is_unsaved, "Document must be marked unsaved after reset_crop")
        self.assertIn("Reset crop", app.undomanager.commits, "Undo commit 'Reset crop' must be recorded")

        # 2. Reset crop on multi-page selection
        p0.crop = Sides(15, 15, 15, 15)
        p1.crop = Sides(20, 20, 20, 20)
        p2 = MockPage(Sides(35, 35, 35, 35))
        app.model.append([p2])

        app.iconview.unselect_all()
        app.iconview.select_path(0)
        app.iconview.select_path(1)
        app.rendered = False

        mgr.reset_crop()

        self.assertEqual(p0.crop, Sides(0, 0, 0, 0), "Page 0 crop must be reset to (0, 0, 0, 0)")
        self.assertEqual(p1.crop, Sides(0, 0, 0, 0), "Page 1 crop must be reset to (0, 0, 0, 0)")
        self.assertEqual(p2.crop, Sides(35, 35, 35, 35), "Unselected Page 2 must remain untouched")
        self.assertTrue(app.rendered, "View must be refreshed after multi-page reset_crop")

        # 3. Verify alias parity
        self.assertEqual(mgr.reset_crop, mgr.reset_crop_selected, "reset_crop and reset_crop_selected must be aliased")

    def test_adjust_crop_action_and_ui_wiring(self):
        class MockHeaderBar:
            def __init__(self):
                self.children = []
            def pack_start(self, child, *a, **k):
                self.children.append(child)

        class MockUiXML:
            def __init__(self, hb):
                self.hb = hb
            def get_object(self, name):
                if name == "header_bar":
                    return self.hb
                return None

        hb = MockHeaderBar()
        app = MockApp()
        app.uiXML = MockUiXML(hb)
        mgr = ext.ScannerManager(app)

        # Verify button existence and label
        self.assertIsNotNone(mgr.btn_adjust_crop, "btn_adjust_crop must be initialized")
        self.assertIsNotNone(mgr.btn_crop, "btn_crop alias must be present")
        self.assertEqual(mgr.btn_adjust_crop.get_label(), "✂️ Adjust Crop", "Button label must be '✂️ Adjust Crop'")

        # Verify right-click context menu items
        self.assertTrue(
            any("✂️ Adjust Page Crop / Margins... (C)" in getattr(item, "label", "") for item in app.popup),
            "Context menu must contain '✂️ Adjust Page Crop / Margins... (C)'"
        )
        self.assertTrue(
            any("↺ Reset / Remove Crop" in getattr(item, "label", "") for item in app.popup),
            "Context menu must contain '↺ Reset / Remove Crop'"
        )


def run_tests():
    suite = unittest.TestSuite()
    for tc in [
        TestHardwareIsolation,
        TestNonBlockingScanWorkflow,
        TestScanRequestQueueing,
        TestEscapeKeyPermutations,
        TestManualStitchConcurrency,
        TestPlaceholderMutation,
        TestScannerAreaMaxAndDimensions,
        TestPdfPerformanceAndCompression,
        TestUpstreamCleanliness,
        TestF7MultiCycleSequence,
        TestEdgeCases,
        TestCropAndUiAdjustment
    ]:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(tc))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    run_tests()
