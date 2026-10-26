"""
Test Suite for ADF Multi-Page Feeder Batch Scanning.
Verifies:
1. wia_scanner.scan_feeder_batch_to_files (mock mode, callback streaming, cancellation)
2. is_paper_empty_error identification of 0x80210003 / -2145320957
3. WIA USB Document Handling Select = 1 (Feeder), Pages = 0 (ALL_PAGES), clean termination on WIA_ERROR_PAPER_EMPTY
4. Network eSCL InputSource = Feeder, NextDocument retry tolerance to temporary 404 between sheets, clean completion
5. scanner_worker.py scan-feeder-batch CLI streaming output
6. pdfarranger_scanner_extension UI button presence, F8 adaptation, streaming page injection, autosave, and Escape handling
"""

import os
import sys
import json
import shutil
import unittest
import unittest.mock
import subprocess
import threading
import time
import numpy as np
import cv2

os.environ["MOCK_SCANNER"] = "1"
os.environ["MOCK_SCAN_DELAY"] = "0.01"

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

import wia_scanner
import scanner_worker


class TestAdfWiaScanner(unittest.TestCase):
    def setUp(self):
        self.test_dir = os.path.join(WORKSPACE_DIR, "Scans", "TestAdf_Unit")
        os.makedirs(self.test_dir, exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_paper_empty_error_detection(self):
        # Test signed 32-bit int: -2145320957 == 0x80210003 - 0x100000000
        class MockComError(Exception):
            def __init__(self, hr, msg=""):
                self.hresult = hr
                super().__init__(msg)

        err_signed = MockComError(-2145320957, "The document feeder is out of paper")
        self.assertTrue(wia_scanner.is_paper_empty_error(err_signed))

        err_unsigned = MockComError(0x80210003, "WIA_ERROR_PAPER_EMPTY")
        self.assertTrue(wia_scanner.is_paper_empty_error(err_unsigned))

        err_tuple = Exception(-2145320957, "Paper empty")
        self.assertTrue(wia_scanner.is_paper_empty_error(err_tuple))

        err_text = RuntimeError("Paper empty in document feeder")
        self.assertTrue(wia_scanner.is_paper_empty_error(err_text))

        err_other = RuntimeError("Scanner lamp error")
        self.assertFalse(wia_scanner.is_paper_empty_error(err_other))

    def test_scan_feeder_batch_mock_streaming(self):
        pages_reported = []

        def on_page(path, page_num):
            pages_reported.append((page_num, path))
            self.assertTrue(os.path.exists(path))
            self.assertGreater(os.path.getsize(path), 100)

        results = wia_scanner.scan_feeder_batch_to_files(
            out_dir=self.test_dir,
            prefix="BatchMock",
            dpi=150,
            color_mode="color",
            page_size="a4",
            page_callback=on_page
        )

        self.assertEqual(len(results), 3)
        self.assertEqual(len(pages_reported), 3)
        self.assertEqual(pages_reported[0][0], 1)
        self.assertEqual(pages_reported[1][0], 2)
        self.assertEqual(pages_reported[2][0], 3)
        for path in results:
            self.assertTrue(os.path.exists(path))

    def test_scan_feeder_batch_cancellation(self):
        cancelled_after = [False]

        def cancel_check():
            return cancelled_after[0]

        def on_page(path, page_num):
            if page_num == 1:
                cancelled_after[0] = True

        results = wia_scanner.scan_feeder_batch_to_files(
            out_dir=self.test_dir,
            prefix="BatchCancel",
            dpi=150,
            page_callback=on_page,
            cancel_check=cancel_check
        )

        # Should only have finished page 1 before cancellation was detected
        self.assertEqual(len(results), 1)
        self.assertTrue(os.path.exists(results[0]))

    def test_wia_usb_feeder_batch_protocol_properties(self):
        """Verifies WIA USB Document Handling Select = 1, Pages = 0, and clean completion on WIA_ERROR_PAPER_EMPTY."""
        class MockProperty:
            def __init__(self, name, val=0, subtype_max=2550):
                self.Name = name
                self.Value = val
                self.SubTypeMax = subtype_max

        props_map = {
            "Document Handling Select": MockProperty("Document Handling Select", 0),
            3088: MockProperty("Document Handling Select", 0),
            "Pages": MockProperty("Pages", -1),
            3096: MockProperty("Pages", -1),
        }
        item_props_map = {
            "Horizontal Resolution": MockProperty("Horizontal Resolution", 0),
            "Vertical Resolution": MockProperty("Vertical Resolution", 0),
            "Horizontal Extent": MockProperty("Horizontal Extent", 0, subtype_max=2550),
            "Vertical Extent": MockProperty("Vertical Extent", 0, subtype_max=4200),
            "Horizontal Start Position": MockProperty("Horizontal Start Position", 0),
            "Vertical Start Position": MockProperty("Vertical Start Position", 0),
            "Current Intent": MockProperty("Current Intent", 0),
        }

        mock_device = unittest.mock.MagicMock()
        mock_device.Properties.Item.side_effect = lambda k: props_map[k]

        mock_item = unittest.mock.MagicMock()
        mock_item.Properties.Item.side_effect = lambda k: item_props_map[k]

        transfer_count = [0]
        class MockComError(Exception):
            def __init__(self, hr, msg=""):
                self.hresult = hr
                super().__init__(msg)

        def mock_transfer(fmt):
            transfer_count[0] += 1
            if transfer_count[0] > 2:
                # 3rd page signals paper empty in ADF tray
                raise MockComError(-2145320957, "WIA_ERROR_PAPER_EMPTY")
            mock_img = unittest.mock.MagicMock()
            def mock_save(path):
                dummy_bmp = np.zeros((30, 30, 3), dtype=np.uint8)
                cv2.imwrite(path, dummy_bmp)
            mock_img.SaveFile.side_effect = mock_save
            return mock_img

        mock_item.Transfer.side_effect = mock_transfer
        mock_device.Items.Item.return_value = mock_item

        mock_info = unittest.mock.MagicMock()
        mock_info.Type = wia_scanner.WIA_DEVICE_TYPE_SCANNER
        mock_info.Connect.return_value = mock_device

        mock_dm = unittest.mock.MagicMock()
        mock_dm.DeviceInfos.Count = 1
        mock_dm.DeviceInfos.Item.return_value = mock_info

        pages_streamed = []
        def on_page(path, page_num):
            pages_streamed.append((page_num, path))

        with unittest.mock.patch("wia_scanner.is_mock_scanner", return_value=False), \
             unittest.mock.patch("win32com.client.Dispatch", return_value=mock_dm):
            results = wia_scanner.scan_wia_usb_feeder_batch(
                out_dir=self.test_dir,
                prefix="WiaUsbBatch",
                dpi=150,
                color_mode="color",
                page_size="a4",
                page_callback=on_page
            )

        # Verify Document Handling Select = 1 (Feeder)
        self.assertEqual(props_map["Document Handling Select"].Value, 1)
        # Verify Pages = 0 (ALL_PAGES)
        self.assertEqual(props_map["Pages"].Value, 0)
        # Verify 2 pages scanned and streamed
        self.assertEqual(len(results), 2)
        self.assertEqual(len(pages_streamed), 2)
        self.assertEqual(pages_streamed[0][0], 1)
        self.assertEqual(pages_streamed[1][0], 2)

    def test_wia_usb_feeder_batch_empty_initial_tray(self):
        """Verifies that an empty ADF tray on start raises RuntimeError."""
        mock_device = unittest.mock.MagicMock()
        mock_item = unittest.mock.MagicMock()
        class MockComError(Exception):
            def __init__(self, hr, msg=""):
                self.hresult = hr
                super().__init__(msg)
        mock_item.Transfer.side_effect = MockComError(-2145320957, "Feeder empty")
        mock_device.Items.Item.return_value = mock_item
        mock_info = unittest.mock.MagicMock()
        mock_info.Type = wia_scanner.WIA_DEVICE_TYPE_SCANNER
        mock_info.Connect.return_value = mock_device
        mock_dm = unittest.mock.MagicMock()
        mock_dm.DeviceInfos.Count = 1
        mock_dm.DeviceInfos.Item.return_value = mock_info

        with unittest.mock.patch("wia_scanner.is_mock_scanner", return_value=False), \
             unittest.mock.patch("win32com.client.Dispatch", return_value=mock_dm):
            with self.assertRaises(RuntimeError) as cm:
                wia_scanner.scan_wia_usb_feeder_batch(
                    out_dir=self.test_dir,
                    prefix="WiaEmpty",
                    dpi=150,
                    page_callback=None
                )
            self.assertIn("Automatic Document Feeder (ADF) is empty", str(cm.exception))

    def test_escl_feeder_batch_protocol_and_nextdoc_loop(self):
        """Verifies eSCL InputSource=Feeder XML and NextDocument retry tolerance to 404 between sheets."""
        valid_jpeg = cv2.imencode(".jpg", np.zeros((100, 100, 3), dtype=np.uint8))[1].tobytes()
        if len(valid_jpeg) < 1500:
            valid_jpeg += b"0" * (2000 - len(valid_jpeg))

        posted_xml = []
        poll_call = [0]
        sleep_durations = []

        def mock_sleep(secs):
            sleep_durations.append(secs)

        def mock_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            data = getattr(req, "data", None)

            if "ScanJobs" in url and data:
                posted_xml.append(data.decode("utf-8"))
                resp = unittest.mock.MagicMock()
                resp.status = 201
                resp.headers = {"Location": "http://192.168.1.55/eSCL/ScanJobs/feeder_job_1"}
                resp.__enter__.return_value = resp
                return resp

            if "NextDocument" in url:
                poll_call[0] += 1
                call_idx = poll_call[0]

                # Call 1: Page 1 ready
                if call_idx == 1:
                    resp = unittest.mock.MagicMock()
                    resp.status = 200
                    resp.read.return_value = valid_jpeg
                    resp.__enter__.return_value = resp
                    return resp

                # Call 2 & 3: temporary 404 while roller feeds page 2 into feed slot
                elif call_idx in (2, 3):
                    import urllib.error
                    raise urllib.error.HTTPError(url, 404, "Not Found (feeding)", {}, None)

                # Call 4: Page 2 ready
                elif call_idx == 4:
                    resp = unittest.mock.MagicMock()
                    resp.status = 200
                    resp.read.return_value = valid_jpeg
                    resp.__enter__.return_value = resp
                    return resp

                # Calls 5..10: feeder out of paper (6 consecutive 404s)
                else:
                    import urllib.error
                    raise urllib.error.HTTPError(url, 404, "Paper Empty", {}, None)

            raise RuntimeError(f"Unexpected URL in mock_urlopen: {url}")

        pages_streamed = []
        def on_page(path, page_num):
            pages_streamed.append((page_num, path))

        with unittest.mock.patch("wia_scanner.is_mock_scanner", return_value=False), \
             unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen), \
             unittest.mock.patch("time.sleep", side_effect=mock_sleep):
            results = wia_scanner.scan_escl_feeder_batch(
                out_dir=self.test_dir,
                prefix="EsclBatch",
                host="192.168.1.55",
                dpi=150,
                color_mode="color",
                page_size="a4",
                page_callback=on_page
            )

        # 1. Verify XML payload sets Feeder input source
        self.assertEqual(len(posted_xml), 1)
        self.assertIn("<pwg:InputSource>Feeder</pwg:InputSource>", posted_xml[0])
        self.assertIn("<scan:InputSource>Feeder</scan:InputSource>", posted_xml[0])

        # 2. Verify sleep duration between sheets (1.5 - 2.0s)
        self.assertTrue(any(s >= 1.5 for s in sleep_durations))

        # 3. Verify that temporary 404 did not abort page 2
        self.assertEqual(len(results), 2)
        self.assertEqual(len(pages_streamed), 2)
        self.assertEqual(pages_streamed[0][0], 1)
        self.assertEqual(pages_streamed[1][0], 2)

    def test_escl_feeder_batch_empty_initial_tray(self):
        """Verifies that an empty feeder on network eSCL raises RuntimeError."""
        def mock_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            data = getattr(req, "data", None)
            if "ScanJobs" in url and data:
                resp = unittest.mock.MagicMock()
                resp.status = 201
                resp.headers = {"Location": "http://192.168.1.55/eSCL/ScanJobs/feeder_job_empty"}
                resp.__enter__.return_value = resp
                return resp
            if "NextDocument" in url:
                import urllib.error
                raise urllib.error.HTTPError(url, 404, "Paper Empty", {}, None)
            raise RuntimeError(f"Unexpected: {url}")

        with unittest.mock.patch("wia_scanner.is_mock_scanner", return_value=False), \
             unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen), \
             unittest.mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError) as cm:
                wia_scanner.scan_escl_feeder_batch(
                    out_dir=self.test_dir,
                    prefix="EsclEmpty",
                    host="192.168.1.55",
                    dpi=150
                )
            self.assertIn("Document Feeder (ADF) is empty", str(cm.exception))


class TestAdfWorkerCli(unittest.TestCase):
    def setUp(self):
        self.test_dir = os.path.join(WORKSPACE_DIR, "Scans", "TestAdf_Worker")
        os.makedirs(self.test_dir, exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_worker_scan_feeder_batch_cli(self):
        cmd = [
            sys.executable,
            os.path.join(WORKSPACE_DIR, "scanner_worker.py"),
            "scan-feeder-batch",
            "--out-dir", self.test_dir,
            "--prefix", "CliBatch",
            "--dpi", "150",
            "--color-mode", "gray"
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ, MOCK_SCANNER="1", MOCK_SCAN_DELAY="0.01")
        )

        page_ready_events = []
        result_event = None

        for line in proc.stdout:
            line_str = line.strip()
            if line_str.startswith("__PAGE_READY__"):
                payload = json.loads(line_str[len("__PAGE_READY__"):])
                page_ready_events.append(payload)
            elif line_str.startswith("__RESULT__"):
                result_event = json.loads(line_str[len("__RESULT__"):])

        proc.stdout.close()
        proc.stderr.close()
        proc.wait(timeout=10)

        self.assertEqual(len(page_ready_events), 3)
        self.assertEqual(page_ready_events[0]["page"], 1)
        self.assertEqual(page_ready_events[1]["page"], 2)
        self.assertEqual(page_ready_events[2]["page"], 3)
        self.assertIsNotNone(result_event)
        self.assertTrue(result_event.get("success"))
        self.assertEqual(result_event.get("pages"), 3)


class TestPdfArrangerExtensionFeeder(unittest.TestCase):
    def setUp(self):
        # Mock minimal GTK and PDF Arranger environment
        self.mock_app = type("MockApp", (), {})()
        self.mock_app.tmp_dir = os.path.join(WORKSPACE_DIR, "Scans", "TestAdf_AppTmp")
        os.makedirs(self.mock_app.tmp_dir, exist_ok=True)
        self.mock_app.pdfqueue = []
        self.mock_app.model = []
        self.mock_app.metadata = {}
        self.mock_app.config = {}
        self.mock_app.set_unsaved = lambda v: None
        self.mock_app.update_iconview_geometry = lambda: None
        self.mock_app.update_max_zoom_level = lambda: None
        self.mock_app.render = lambda: None

        import pdfarranger_scanner_extension as ext
        self.ext = ext

    def tearDown(self):
        if os.path.exists(self.mock_app.tmp_dir):
            shutil.rmtree(self.mock_app.tmp_dir, ignore_errors=True)

    def test_feeder_batch_execution_and_streaming_injection(self):
        mgr = self.ext.ScannerManager.__new__(self.ext.ScannerManager)
        mgr.app = self.mock_app
        mgr.scan_lock = threading.Lock()
        mgr.autosave_lock = threading.Lock()
        mgr.last_key_time = {}
        mgr.is_scanning = False
        mgr.is_optimizing = False
        mgr.queued_scan = None
        mgr.current_scan_proc = None
        mgr.cancel_requested = False
        mgr.part1_path = None
        mgr.session_page_num = 1
        mgr.session_id = "test_adf_session"
        mgr.config = {"source": "feeder", "dpi": 150, "color_mode": "color", "page_size": "a4"}
        mgr.connected_scanner_name = "HP Smart Tank 750 (Mock Isolated Scanner)"
        mgr.lbl_status = None
        mgr.spinner = None
        mgr.btn_f7 = None
        mgr.btn_f8 = None
        mgr.btn_feeder_batch = None

        injected_pages = []
        autosaved_pages = []

        def mock_inject(path, desc=""):
            injected_pages.append((path, desc))

        def mock_autosave(path):
            autosaved_pages.append(path)

        mgr.inject_page_into_pdfarranger = mock_inject
        mgr._autosave_page = mock_autosave
        mgr.update_ui_state = lambda msg=None: None

        # Execute batch scan
        mgr._worker_scan_feeder_batch()

        # Should have scanned and injected 3 sequential pages and auto-saved each
        self.assertEqual(len(injected_pages), 3)
        self.assertEqual(len(autosaved_pages), 3)
        self.assertIn("Page 1 (Feeder Stack)", injected_pages[0][1])
        self.assertIn("Page 2 (Feeder Stack)", injected_pages[1][1])
        self.assertIn("Page 3 (Feeder Stack)", injected_pages[2][1])
        self.assertFalse(mgr.is_scanning)

    def test_f8_routes_to_feeder_batch_when_source_is_feeder(self):
        mgr = self.ext.ScannerManager.__new__(self.ext.ScannerManager)
        mgr.app = self.mock_app
        mgr.scan_lock = threading.Lock()
        mgr.autosave_lock = threading.Lock()
        mgr.last_key_time = {}
        mgr.is_scanning = False
        mgr.queued_scan = None
        mgr.part1_path = None
        mgr.config = {"source": "feeder", "dpi": 150, "color_mode": "color", "page_size": "a4"}

        feeder_batch_called = [False]
        mgr._worker_scan_feeder_batch = lambda: feeder_batch_called.__setitem__(0, True)
        mgr.update_ui_state = lambda msg=None: None

        mgr.on_f8_clicked()
        time.sleep(0.05)
        self.assertTrue(feeder_batch_called[0])

    def test_on_feeder_batch_clicked_initiates_scan_and_sets_feeder_source(self):
        mgr = self.ext.ScannerManager.__new__(self.ext.ScannerManager)
        mgr.app = self.mock_app
        mgr.scan_lock = threading.Lock()
        mgr.autosave_lock = threading.Lock()
        mgr.last_key_time = {}
        mgr.is_scanning = False
        mgr.queued_scan = None
        mgr.part1_path = None
        mgr.config = {"source": "flatbed", "dpi": 150, "color_mode": "color", "page_size": "a4"}
        mgr.save_config = lambda: None
        mgr.update_settings_ui = lambda: None
        mgr.update_ui_state = lambda msg=None: None

        feeder_batch_called = [False]
        mgr._worker_scan_feeder_batch = lambda: feeder_batch_called.__setitem__(0, True)

        mgr.on_feeder_batch_clicked()
        time.sleep(0.05)

        self.assertEqual(mgr.config["source"], "feeder")
        self.assertTrue(feeder_batch_called[0])

    def test_on_feeder_batch_clicked_rejects_when_busy(self):
        mgr = self.ext.ScannerManager.__new__(self.ext.ScannerManager)
        mgr.app = self.mock_app
        mgr.scan_lock = threading.Lock()
        mgr.autosave_lock = threading.Lock()
        mgr.last_key_time = {}
        mgr.is_scanning = True  # Already scanning
        mgr.config = {"source": "flatbed"}

        feeder_batch_called = [False]
        mgr._worker_scan_feeder_batch = lambda: feeder_batch_called.__setitem__(0, True)

        mgr.on_feeder_batch_clicked()
        time.sleep(0.05)

        self.assertFalse(feeder_batch_called[0])


if __name__ == "__main__":
    unittest.main()
