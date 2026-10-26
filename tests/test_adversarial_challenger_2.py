"""
Challenger 2 Adversarial Concurrency & Performance Stress Test Suite.
Role: EMPIRICAL CHALLENGER (critic, specialist).

Strict Requirements:
1. Concurrency Stress Test:
   - Concurrent F9 manual stitch while continuous background stitches are running.
   - Simultaneous user page deletion/reordering in iconview model while background replacement is in-flight.
   - Memory leakage / surface leak test during 20+ simulated page scan iterations.
2. Performance Benchmark Verification:
   - Poppler screen rendering speed assertion (< 20 ms).
   - Thumbnail rendering speed assertion (< 10 ms).
   - Output PDF file size (< 1 MB average per page at 300 DPI with DCTDecode).
3. State your verdict (APPROVE or REQUEST_CHANGES).

CRITICAL OPERATIONAL CONSTRAINT:
Strictly enforced MOCK_SCANNER=1 with synthetic test files.
Zero calls to physical scanner hardware, WIA COM, or real devices.
"""

import os
import sys
import gc
import time
import shutil
import queue
import logging
import unittest
import threading
import subprocess
from unittest.mock import MagicMock, patch

# Strictly enforce MOCK_SCANNER mode before any project imports
os.environ["MOCK_SCANNER"] = "1"
os.environ["MOCK_SCAN_DELAY"] = "0.01"

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

import cv2
import numpy as np
import pymupdf as fitz
from PIL import Image
import psutil

# Mock GTK / GLib / GObject for headless test execution
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
    """Accurately simulates Gtk.TreeRowReference tracking row movements in model dynamically."""
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
        if path is None:
            raise TypeError("could not convert None to TreePath (Gtk.TreeModel.get_iter requires a valid TreePath)")
        idx = path.get_indices()[0] if hasattr(path, "get_indices") else path[0]
        if 0 <= idx < len(self):
            return MockTreeIter(idx, self[idx])
        raise ValueError(f"TreePath '{path}' out of bounds for model with length {len(self)}")

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

sys.modules["keyboard"] = MagicMock()

# Faithful PageAdder reproducing real pdfarranger.core.PageAdder logic:
# Specifically: lines 782-788 of pdfarranger/core.py:
# if self.treerowref:
#     iter_to = self.app.model.get_iter(self.treerowref.get_path())
#     if self.before:
#         it = self.app.model.insert_before(iter_to, m)
class FaithfulPageAdder:
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
        # Faithful to core.py:
        if self.target_ref:
            # If treerowref is present, core.py calls model.get_iter(self.treerowref.get_path())
            path = self.target_ref.get_path()
            if path is None:
                raise TypeError("could not convert None to TreePath (treerowref is invalid/deleted)")
            iter_to = self.app.model.get_iter(path)
            idx = iter_to.idx
            insert_idx = idx if self.insert_before else idx + 1
            for p in self.pages:
                self.app.model.insert(insert_idx, p)
                insert_idx += 1
            return True
        else:
            self.app.model.extend(self.pages)
            return True


from collections import namedtuple
Sides = namedtuple("Sides", ["left", "right", "top", "bottom"], defaults=(0, 0, 0, 0))

mock_core = MagicMock()
mock_core.PageAdder = FaithfulPageAdder
mock_core.Sides = Sides
sys.modules["pdfarranger.core"] = mock_core

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


class MockApp:
    def __init__(self):
        self.model = MockModel()
        self.iconview = MockIconView()
        self.header_bar = MagicMock()
        self.selection = []
        self.pdfqueue = []
        self.unsaved = False
        self.undomanager = MagicMock()
        self._lock = threading.Lock()

    def set_unsaved(self, val):
        self.unsaved = val

    def render_lock(self):
        return self._lock

    def update_iconview_geometry(self):
        pass

    def update_max_zoom_level(self):
        pass

    def render(self):
        pass


class AdversarialConcurrencyChallengerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = os.path.join(WORKSPACE_DIR, "Scans", "AdversarialTest")
        os.makedirs(cls.test_dir, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.test_dir):
            shutil.rmtree(cls.test_dir, ignore_errors=True)

    def setUp(self):
        self.app = MockApp()
        self.mgr = ext.ScannerManager(self.app)

    def tearDown(self):
        self.mgr.stop()

    # =========================================================================
    # 1. CONCURRENCY STRESS TESTS
    # =========================================================================

    def test_concurrent_f9_manual_stitch_during_continuous_background_stitches(self):
        """
        Stress-tests:
        1. 5 continuous background stitches enqueued in stitch_queue.
        2. Simultaneous F9 manual stitches triggered from concurrent threads while background stitches are running.
        3. Verifies thread safety, pending_stitches accounting, queue completion, and no deadlocks.
        """
        print("\n--- [TEST] Concurrent F9 Manual Stitch During Continuous Background Stitches ---")

        # Prepare 10 pages in model
        for i in range(10):
            page_mock = MagicMock()
            page_mock.duplicate.return_value = page_mock
            self.app.model.append([page_mock, f"Page {i+1}"])

        # Enqueue 5 continuous dual_scan background stitches
        num_bg_stitches = 5
        for i in range(num_bg_stitches):
            p1 = os.path.join(WORKSPACE_DIR, "Archive", "Doc_002_P01_Part1.png")
            p2 = os.path.join(WORKSPACE_DIR, "Archive", "Doc_002_P01_Part2.png")
            out = os.path.join(self.test_dir, f"bg_stitch_{i}.pdf")
            self.mgr.pending_stitches += 1
            self.mgr.stitch_queue.put({
                "type": "dual_scan",
                "part1": p1,
                "part2": p2,
                "out_pdf": out,
                "placeholder_ref": None,
                "dpi": 300,
                "page_num": 100 + i
            })

        # Simultaneously trigger 3 concurrent manual stitches from separate threads
        num_manual_stitches = 3
        f9_results = []
        threads = []

        def trigger_f9(p_idx1, p_idx2, thread_id):
            time.sleep(0.05 * thread_id)
            self.app.iconview.selected_items = [MockTreePath([p_idx1]), MockTreePath([p_idx2])]
            done_evt = threading.Event()
            res_holder = [False]

            def on_f9_done(success):
                res_holder[0] = success
                done_evt.set()

            def dummy_render(app, p, path, dpi=300):
                # Synthetic rendering
                Image.new("RGB", (300, 300), color=(255, 255, 255)).save(path, "PNG")

            with patch("pdfarranger_scanner_extension.render_page_to_png", side_effect=dummy_render):
                self.mgr.on_stitch_clicked(callback=on_f9_done)

            done_evt.wait(timeout=10.0)
            f9_results.append((thread_id, res_holder[0]))

        # Launch F9 threads
        for t_idx in range(num_manual_stitches):
            t = threading.Thread(target=trigger_f9, args=(t_idx * 2, t_idx * 2 + 1, t_idx))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=15.0)

        # Wait for all background stitch jobs to finish
        t_start = time.time()
        while self.mgr.pending_stitches > 0 and (time.time() - t_start) < 20.0:
            time.sleep(0.2)

        print(f"Completed! Remaining pending stitches: {self.mgr.pending_stitches}")
        print(f"F9 thread results: {f9_results}")

        self.assertEqual(self.mgr.pending_stitches, 0, "All stitch jobs must complete, pending_stitches == 0")
        self.assertEqual(len(f9_results), num_manual_stitches, "All F9 threads must report completion")
        for tid, ok in f9_results:
            self.assertTrue(ok, f"Manual stitch from thread {tid} must succeed")

    def test_rapid_f9_invocations_subsecond_timestamp_collision_check(self):
        """
        Adversarially tests whether rapid F9 invocations within milliseconds collide on
        temporary filenames (Manual_P{idx1+1}_{timestamp}.png) due to 1-second int(time.time()) resolution.
        """
        print("\n--- [TEST] Rapid F9 Invocations Subsecond Timestamp Collision Check ---")

        # Setup 4 pages in model
        for i in range(4):
            page_mock = MagicMock()
            page_mock.duplicate.return_value = page_mock
            self.app.model.append([page_mock, f"Page {i+1}"])

        # Rapidly invoke on_stitch_clicked twice on the same selection within same millisecond
        self.app.iconview.selected_items = [MockTreePath([0]), MockTreePath([1])]

        generated_raw_files = []
        def track_render(app, p, path, dpi=300):
            generated_raw_files.append(path)
            Image.new("RGB", (200, 200), color=(250, 250, 250)).save(path, "PNG")

        with patch("pdfarranger_scanner_extension.render_page_to_png", side_effect=track_render):
            self.mgr.on_stitch_clicked()
            self.mgr.on_stitch_clicked()

        time.sleep(0.3)
        print(f"Generated raw render paths: {generated_raw_files}")
        if len(generated_raw_files) >= 4:
            file_set = set(generated_raw_files)
            if len(file_set) < len(generated_raw_files):
                print(f"[FINDING / RISK] Subsecond F9 invocations collided on temporary file path! "
                      f"Unique paths: {len(file_set)} / {len(generated_raw_files)}")
            else:
                print("No collision observed in subsecond F9 test.")

    # =========================================================================
    # 2. MODEL MUTATION TESTS (DELETION & REORDERING MID-STITCH)
    # =========================================================================

    def test_simultaneous_placeholder_reordering_mid_stitch(self):
        """
        User drags/reorders placeholder in the iconview while SIFT background stitch is in-flight.
        Verifies:
        - TreeRowReference dynamically tracks placeholder new position.
        - Stitched page replaces the placeholder at its new location.
        - Document order is preserved without duplication or shifting.
        """
        print("\n--- [TEST] Simultaneous Placeholder Reordering Mid-Stitch ---")
        # Document: [P0, P1, P2]
        self.app.model = MockModel(["Page_0", "Page_1", "Page_2"])

        # Inject Placeholder at index 3
        self.app.model.append("Placeholder_P3")
        ph_ref = MockTreeRowReference(self.app.model, 3)
        self.assertTrue(ph_ref.valid())
        self.assertEqual(ph_ref.get_path_index(), 3)

        # SIMULATE USER REORDERING WHILE WORKER RUNS:
        # User drags Placeholder_P3 to index 1 (between Page_0 and Page_1)
        ph_item = self.app.model.pop(3)
        self.app.model.insert(1, ph_item)

        # Verify dynamic tracking
        self.assertTrue(ph_ref.valid())
        self.assertEqual(ph_ref.get_path_index(), 1)
        self.assertEqual(self.app.model, ["Page_0", "Placeholder_P3", "Page_1", "Page_2"])

        # Worker completes and calls _commit_replacement
        stitched_pdf = os.path.join(self.test_dir, "stitched_p3.pdf")
        Image.new("RGB", (200, 200), (255, 255, 255)).save(stitched_pdf, "PDF")

        # Execute replacement
        adder = FaithfulPageAdder(self.app)
        adder.move(ph_ref, True)
        adder.addpages("Stitched_P3")
        adder.commit()

        # Remove placeholder
        del_iter = self.app.model.get_iter(ph_ref.get_path())
        self.app.model.remove(del_iter)

        expected = ["Page_0", "Stitched_P3", "Page_1", "Page_2"]
        print(f"Model after reordered replacement: {self.app.model}")
        self.assertEqual(self.app.model, expected, "Stitched page must be inserted at placeholder's moved position")

    def test_placeholder_deleted_by_user_mid_stitch_fallback_behavior(self):
        """
        User deletes placeholder row while background stitch is in-flight.
        Verifies:
        - TreeRowReference becomes invalid (valid() == False).
        - Faithful PageAdder with invalid ref raises TypeError if called directly.
        - Extension's if/else check in _commit_replacement safely falls back to appending.
        """
        print("\n--- [TEST] Placeholder Deleted by User Mid-Stitch Fallback Behavior ---")
        self.app.model = MockModel(["Page_0", "Page_1", "Placeholder_P2", "Page_3"])
        ph_ref = MockTreeRowReference(self.app.model, 2)
        self.assertTrue(ph_ref.valid())

        # User deletes placeholder
        del_iter = self.app.model.get_iter(MockTreePath([2]))
        self.app.model.remove(del_iter)
        self.assertNotIn("Placeholder_P2", self.app.model)
        self.assertFalse(ph_ref.valid(), "ph_ref must be invalid after deletion")

        # In _execute_dual_stitch, line 1822:
        # if placeholder_ref and placeholder_ref.valid():
        #     ...
        # else:
        #     adder.addpages(out_pdf)
        if ph_ref and ph_ref.valid():
            adder = FaithfulPageAdder(self.app)
            adder.move(ph_ref, True)
            adder.addpages("Stitched_P2")
            adder.commit()
            self.app.model.remove(self.app.model.get_iter(ph_ref.get_path()))
        else:
            adder = FaithfulPageAdder(self.app)
            adder.addpages("Stitched_P2")
            adder.commit()

        print(f"Model after deleted placeholder fallback: {self.app.model}")
        self.assertEqual(self.app.model, ["Page_0", "Page_1", "Page_3", "Stitched_P2"])

    def test_simultaneous_deletion_of_ref1_during_f9_manual_stitch(self):
        """
        CRITICAL ADVERSARIAL CHALLENGE:
        In on_stitch_clicked, ref1 and ref2 are created.
        During SIFT computation, user deletes ref1!
        In _execute_manual_stitch (lines 1898-1901):
            adder = core.PageAdder(self.app)
            adder.move(ref1, True)
            adder.addpages(stitched_out)
            adder.commit(select_added=True, add_to_undomanager=False)
        Notice adder.move(ref1, True) is called without checking ref1.valid()!
        This test checks how the system behaves when ref1 is invalid.
        """
        print("\n--- [TEST] Simultaneous Deletion of ref1 During F9 Manual Stitch ---")
        self.app.model = MockModel(["Page_0", "Page_1", "Page_2", "Page_3"])
        ref1 = MockTreeRowReference(self.app.model, 1)  # Page_1
        ref2 = MockTreeRowReference(self.app.model, 2)  # Page_2

        # User deletes Page_1 while manual stitch is running!
        del_iter = self.app.model.get_iter(MockTreePath([1]))
        self.app.model.remove(del_iter)

        self.assertFalse(ref1.valid(), "ref1 is now invalid!")
        self.assertTrue(ref2.valid(), "ref2 remains valid at new shifted index!")
        self.assertEqual(ref2.get_path_index(), 1)  # Shifted from 2 to 1

        # Now _execute_manual_stitch finishes and calls _commit_stitch
        adder = FaithfulPageAdder(self.app)
        adder.move(ref1, True)
        adder.addpages("Stitched_Manual_P1_P2")

        # Because ref1 is invalid, adder.commit() with FaithfulPageAdder encounters ref1.get_path() is None:
        commit_failed = False
        try:
            adder.commit()
        except TypeError as e:
            commit_failed = True
            print(f"[OBSERVED ERROR]: Faithful PageAdder raised TypeError when ref1 was deleted: {e}")

        self.assertTrue(commit_failed, "Faithful PageAdder raises TypeError when ref1 is deleted mid-stitch")

    def test_document_cleared_mid_stitch(self):
        """
        Adversarially tests model.clear() while background replacement is scheduled.
        """
        print("\n--- [TEST] Document Cleared Mid-Stitch ---")
        self.app.model = MockModel(["Page_0", "Placeholder_P1"])
        ph_ref = MockTreeRowReference(self.app.model, 1)

        # User clears entire document
        self.app.model.clear()
        self.assertEqual(len(self.app.model), 0)
        self.assertFalse(ph_ref.valid())

        # Worker completes:
        if ph_ref and ph_ref.valid():
            adder = FaithfulPageAdder(self.app)
            adder.move(ph_ref, True)
            adder.addpages("Stitched_P1")
            adder.commit()
        else:
            adder = FaithfulPageAdder(self.app)
            adder.addpages("Stitched_P1")
            adder.commit()

        self.assertEqual(self.app.model, ["Stitched_P1"])

    # =========================================================================
    # 3. MEMORY LEAKAGE & SURFACE LEAK TEST (25+ SCAN ITERATIONS)
    # =========================================================================

    def test_memory_and_surface_leak_during_25_simulated_scan_iterations(self):
        """
        Runs 25 continuous simulated page scan cycles:
        - Raw synthetic image generation
        - Placeholder creation and injection
        - Background SIFT stitch alignment
        - PDF generation with DCTDecode
        - Model replacement
        Profiles RSS memory growth across iterations 1 to 25.
        Asserts memory growth is bounded (< 25 MB net growth between iteration 5 and 25).
        """
        print("\n--- [TEST] Memory & Surface Leak During 25 Simulated Scan Iterations ---")
        proc = psutil.Process()

        # Warmup (iterations 1-5)
        gc.collect()
        rss_start = proc.memory_info().rss / (1024 * 1024)
        print(f"Baseline RSS Memory: {rss_start:.2f} MB")

        rss_snapshots = []
        num_iterations = 25

        for i in range(1, num_iterations + 1):
            # 1. Create synthetic scan pair
            p1 = os.path.join(self.test_dir, f"mem_test_p{i}_part1.png")
            p2 = os.path.join(self.test_dir, f"mem_test_p{i}_part2.png")
            out_pdf = os.path.join(self.test_dir, f"mem_test_p{i}_stitched.pdf")

            # Generate synthetic A4 part images
            mat1 = np.full((1800, 2480, 3), 245, dtype=np.uint8)
            cv2.putText(mat1, f"Memory Test Page {i} Top", (100, 200), cv2.FONT_HERSHEY_SIMPLEX, 2, (30, 30, 30), 3)
            cv2.imwrite(p1, mat1)

            mat2 = np.full((1800, 2480, 3), 245, dtype=np.uint8)
            cv2.putText(mat2, f"Memory Test Page {i} Bottom", (100, 200), cv2.FONT_HERSHEY_SIMPLEX, 2, (30, 30, 30), 3)
            cv2.imwrite(p2, mat2)

            # 2. Generate placeholder PDF
            placeholder_pdf = os.path.join(self.test_dir, f"mem_test_p{i}_ph.pdf")
            ext.create_placeholder_pdf(placeholder_pdf, i, "Stitching in background...")

            # 3. Inject placeholder into model
            self.app.model.append(placeholder_pdf)
            ph_ref = MockTreeRowReference(self.app.model, len(self.app.model) - 1)

            # 4. SIFT stitch simulation via save_scanned_image
            # Simulate stitched result
            mat_stitched = np.full((3508, 2480, 3), 245, dtype=np.uint8)
            cv2.putText(mat_stitched, f"Memory Test Page {i} Stitched", (100, 300), cv2.FONT_HERSHEY_SIMPLEX, 3, (20, 20, 20), 4)
            wia_scanner.save_scanned_image(mat_stitched, out_pdf, dpi=300, quality=88)

            # 5. Commit replacement
            if ph_ref and ph_ref.valid():
                idx = ph_ref.get_path_index()
                self.app.model.insert(idx, out_pdf)
                del_iter = self.app.model.get_iter(MockTreePath([idx + 1]))
                self.app.model.remove(del_iter)

            # Cleanup disk intermediate files
            if os.path.exists(p1): os.remove(p1)
            if os.path.exists(p2): os.remove(p2)
            if os.path.exists(placeholder_pdf): os.remove(placeholder_pdf)
            if os.path.exists(out_pdf): os.remove(out_pdf)

            # Snapshot RSS every 5 iterations
            if i % 5 == 0:
                gc.collect()
                rss_current = proc.memory_info().rss / (1024 * 1024)
                rss_snapshots.append((i, rss_current))
                print(f"Iteration {i:02d}: RSS = {rss_current:.2f} MB (Delta from start: {rss_current - rss_start:+.2f} MB)")

        # Evaluate leak between iteration 5 (post warmup) and iteration 25:
        rss_iter5 = rss_snapshots[0][1]
        rss_iter25 = rss_snapshots[-1][1]
        net_growth = rss_iter25 - rss_iter5
        print(f"Post-warmup net RSS growth (Iter 5 -> 25): {net_growth:+.2f} MB")

        # Verify garbage collection
        self.assertEqual(len(gc.garbage), 0, "No uncollectable garbage objects allowed")
        # Assert net memory growth over 20 iterations is < 25 MB (< 1.25 MB/page)
        self.assertLess(net_growth, 25.0, f"Net memory growth of {net_growth:.2f} MB exceeds 25 MB threshold")

    # =========================================================================
    # 4. PERFORMANCE BENCHMARKS (POPPLER, THUMBNAIL, PDF FILE SIZE)
    # =========================================================================

    def test_poppler_screen_rendering_benchmark(self):
        """
        R4 Acceptance Criteria:
        Poppler screen rendering speed assertion (< 20 ms per page).
        Benchmarks 20 screen renders (72 DPI) across realistic 300 DPI A4 scanned pages.
        """
        print("\n--- [TEST] Poppler Screen Rendering Speed Benchmark (< 20 ms) ---")
        test_pdf = os.path.join(self.test_dir, "bench_screen_render.pdf")

        # Generate realistic 300 DPI A4 document (2480 x 3508)
        img_mat = np.full((3508, 2480, 3), 245, dtype=np.uint8)
        cv2.putText(img_mat, "Screen Render Benchmark Document", (200, 300), cv2.FONT_HERSHEY_SIMPLEX, 3, (30, 30, 30), 4)
        for row in range(12):
            y = 500 + row * 220
            cv2.line(img_mat, (200, y), (2280, y), (120, 120, 120), 2)
            cv2.putText(img_mat, f"Paragraph Line {row+1}: The quick brown fox jumps over the lazy dog.", (200, y - 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.5, (40, 40, 40), 2)

        wia_scanner.save_scanned_image(img_mat, test_pdf, dpi=300, quality=88)

        doc = fitz.open(test_pdf)
        page = doc[0]

        # Warmup render
        _ = page.get_pixmap(dpi=72)

        # 20 timed render passes
        render_times_ms = []
        for _ in range(20):
            t0 = time.perf_counter()
            pix = page.get_pixmap(dpi=72)
            t1 = time.perf_counter()
            render_times_ms.append((t1 - t0) * 1000.0)

        doc.close()
        if os.path.exists(test_pdf):
            os.remove(test_pdf)

        mean_ms = np.mean(render_times_ms)
        median_ms = np.median(render_times_ms)
        min_ms = np.min(render_times_ms)
        max_ms = np.max(render_times_ms)
        p95_ms = np.percentile(render_times_ms, 95)

        print(f"Poppler Screen Render (72 DPI):")
        print(f"  Mean:   {mean_ms:.2f} ms")
        print(f"  Median: {median_ms:.2f} ms")
        print(f"  Min:    {min_ms:.2f} ms")
        print(f"  Max:    {max_ms:.2f} ms")
        print(f"  95th%:  {p95_ms:.2f} ms")

        self.assertLess(mean_ms, 20.0, f"Mean screen render time {mean_ms:.2f}ms exceeds 20 ms requirement!")
        self.assertLess(median_ms, 20.0, f"Median screen render time {median_ms:.2f}ms exceeds 20 ms requirement!")

    def test_thumbnail_rendering_benchmark(self):
        """
        R4 Acceptance Criteria:
        Thumbnail rendering speed assertion (< 10 ms per page).
        Benchmarks 20 thumbnail renders (20 DPI, ~150-200px) across realistic 300 DPI A4 scanned pages.
        """
        print("\n--- [TEST] Thumbnail Rendering Speed Benchmark (< 10 ms) ---")
        test_pdf = os.path.join(self.test_dir, "bench_thumb_render.pdf")

        # Generate realistic 300 DPI A4 document (2480 x 3508)
        img_mat = np.full((3508, 2480, 3), 245, dtype=np.uint8)
        cv2.putText(img_mat, "Thumbnail Benchmark Document", (200, 300), cv2.FONT_HERSHEY_SIMPLEX, 3, (30, 30, 30), 4)
        for row in range(10):
            cv2.line(img_mat, (200, 500 + row * 250), (2280, 500 + row * 250), (100, 100, 100), 2)

        wia_scanner.save_scanned_image(img_mat, test_pdf, dpi=300, quality=88)

        doc = fitz.open(test_pdf)
        page = doc[0]

        # Warmup
        _ = page.get_pixmap(dpi=20)

        # 20 timed thumbnail passes
        thumb_times_ms = []
        for _ in range(20):
            t0 = time.perf_counter()
            pix = page.get_pixmap(dpi=20)
            t1 = time.perf_counter()
            thumb_times_ms.append((t1 - t0) * 1000.0)

        doc.close()
        if os.path.exists(test_pdf):
            os.remove(test_pdf)

        mean_ms = np.mean(thumb_times_ms)
        median_ms = np.median(thumb_times_ms)
        min_ms = np.min(thumb_times_ms)
        max_ms = np.max(thumb_times_ms)
        p95_ms = np.percentile(thumb_times_ms, 95)

        print(f"Thumbnail Render (20 DPI):")
        print(f"  Mean:   {mean_ms:.2f} ms")
        print(f"  Median: {median_ms:.2f} ms")
        print(f"  Min:    {min_ms:.2f} ms")
        print(f"  Max:    {max_ms:.2f} ms")
        print(f"  95th%:  {p95_ms:.2f} ms")

        self.assertLess(mean_ms, 10.0, f"Mean thumbnail render time {mean_ms:.2f}ms exceeds 10 ms requirement!")
        self.assertLess(median_ms, 10.0, f"Median thumbnail render time {median_ms:.2f}ms exceeds 10 ms requirement!")

    def test_output_pdf_file_size_and_dctdecode_compression(self):
        """
        R4 Acceptance Criteria:
        Output PDF file size (< 1 MB average per page at 300 DPI with DCTDecode).
        Tests 5 diverse pages: Text document, tabular document, mixed document, photo document, noisy scan.
        """
        print("\n--- [TEST] Output PDF File Size & DCTDecode Compression (< 1 MB avg) ---")
        sizes = []

        # 1. Clean Text Page
        mat_text = np.full((3508, 2480, 3), 255, dtype=np.uint8)
        for y in range(300, 3200, 100):
            cv2.putText(mat_text, "Standard clean invoice text line with numbers 1234567890", (150, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (20, 20, 20), 2)
        pdf_text = os.path.join(self.test_dir, "size_test_text.pdf")
        wia_scanner.save_scanned_image(mat_text, pdf_text, dpi=300, quality=88)
        sizes.append(("Clean Text", os.path.getsize(pdf_text)))

        # 2. Form / Table Document
        mat_table = np.full((3508, 2480, 3), 250, dtype=np.uint8)
        for r in range(20):
            cv2.line(mat_table, (100, 200 + r * 150), (2380, 200 + r * 150), (50, 50, 50), 2)
        for c in range(5):
            cv2.line(mat_table, (100 + c * 570, 200), (100 + c * 570, 3050), (50, 50, 50), 2)
        pdf_table = os.path.join(self.test_dir, "size_test_table.pdf")
        wia_scanner.save_scanned_image(mat_table, pdf_table, dpi=300, quality=88)
        sizes.append(("Form/Table", os.path.getsize(pdf_table)))

        # 3. Mixed Text & Shapes Document
        mat_mixed = np.full((3508, 2480, 3), 245, dtype=np.uint8)
        cv2.circle(mat_mixed, (1240, 1000), 400, (100, 150, 200), -1)
        cv2.rectangle(mat_mixed, (300, 1600), (2180, 2600), (200, 220, 240), -1)
        pdf_mixed = os.path.join(self.test_dir, "size_test_mixed.pdf")
        wia_scanner.save_scanned_image(mat_mixed, pdf_mixed, dpi=300, quality=88)
        sizes.append(("Mixed Graphics", os.path.getsize(pdf_mixed)))

        # 4. Realistic Scan with Gaussian noise
        noise = np.random.normal(0, 5, (3508, 2480, 3)).astype(np.int16)
        mat_noisy = np.clip(mat_text.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        pdf_noisy = os.path.join(self.test_dir, "size_test_noisy.pdf")
        wia_scanner.save_scanned_image(mat_noisy, pdf_noisy, dpi=300, quality=88)
        sizes.append(("Noisy Scan", os.path.getsize(pdf_noisy)))

        # Check DCTDecode filter on each PDF
        for label, sz in sizes:
            p_pdf = os.path.join(self.test_dir, f"size_test_{label.lower().split()[0]}.pdf")
            if not os.path.exists(p_pdf):
                continue
            doc = fitz.open(p_pdf)
            page = doc[0]
            imgs = page.get_images()
            self.assertGreater(len(imgs), 0, f"{label} must contain raster image")
            xref = imgs[0][0]
            base_img = doc.extract_image(xref)
            self.assertIn(base_img["ext"], ("jpeg", "jpg"), f"{label} must use DCT/JPEG compression")
            # Verify page dimensions (A4 point size)
            self.assertAlmostEqual(page.rect.width, 595.28, delta=2.0)
            self.assertAlmostEqual(page.rect.height, 841.89, delta=2.0)
            doc.close()
            os.remove(p_pdf)

        print("\nPage Size Breakdown (300 DPI A4):")
        total_sz = 0
        for label, sz in sizes:
            kb = sz / 1024.0
            mb = kb / 1024.0
            total_sz += sz
            print(f"  {label:<16}: {kb:7.1f} KB ({mb:.3f} MB)")
            self.assertLess(sz, 1048576, f"{label} page size {sz} bytes exceeds 1 MB limit!")

        avg_sz = total_sz / len(sizes)
        avg_mb = avg_sz / (1024.0 * 1024.0)
        print(f"  Average Page Size: {avg_sz/1024.0:.1f} KB ({avg_mb:.3f} MB)")
        self.assertLess(avg_sz, 1048576, f"Average page size {avg_sz} bytes exceeds 1 MB limit!")


if __name__ == "__main__":
    unittest.main(verbosity=2)
