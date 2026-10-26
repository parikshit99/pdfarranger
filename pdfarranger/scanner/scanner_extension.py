"""
PDF Arranger Native Scanner & SIFT Stitcher Extension.
Seamlessly integrates:
1. HP Smart Tank 750 dual-pass (F7) scanning with SIFT 2D rigid planar alignment.
2. Single-pass (F8) scanning with auto-crop.
3. Manual SIFT stitching of any two selected pages in the document grid (F9 / Shift+F7 / Context Menu).
4. Quick Access Menu & Toolbar Toggle for Scanner Settings:
   - Input Source: Flatbed (Glass) vs. Automatic Document Feeder (ADF)
   - Resolution: 150, 300, 600 DPI
   - Color Mode: Full Color, Grayscale, Black & White
   - Persistent settings stored in scanner_config.json
"""

import os
import sys
import json
import time
import queue
import threading
import subprocess
import traceback

try:
    from pdfarranger.scanner import wia_scanner
except ImportError:
    try:
        from . import wia_scanner
    except (ImportError, ValueError):
        try:
            import wia_scanner
        except Exception:
            wia_scanner = None

try:
    from pdfarranger.scanner import stitch_daemon
except ImportError:
    try:
        from . import stitch_daemon
    except (ImportError, ValueError):
        try:
            import stitch_daemon
        except Exception:
            stitch_daemon = None

# Ensure PDF Arranger runtime library is in sys.path for gi and dependencies when running in Python 3.12+
if sys.version_info >= (3, 12):
    PDFARRANGER_LIB = os.path.join(
        os.environ.get("LOCALAPPDATA", r"C:\Users\Admin\AppData\Local"),
        r"Programs\PDF Arranger\lib"
    )
    if os.path.exists(PDFARRANGER_LIB) and PDFARRANGER_LIB not in sys.path:
        sys.path.append(PDFARRANGER_LIB)

# Module and workspace paths
SCANNER_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(SCANNER_MODULE_DIR, "scanner_worker.py")):
    WORKSPACE_DIR = SCANNER_MODULE_DIR
else:
    WORKSPACE_DIR = os.path.abspath(os.path.join(SCANNER_MODULE_DIR, "..", ".."))

PYTHON_EXE = os.path.join(
    os.environ.get("LOCALAPPDATA", r"C:\Users\Admin\AppData\Local"),
    r"Programs\Python\Python310\python.exe"
)
if not os.path.exists(PYTHON_EXE):
    PYTHON_EXE = sys.executable or "python"

WORKER_SCRIPT = os.path.join(SCANNER_MODULE_DIR, "scanner_worker.py")
if not os.path.exists(WORKER_SCRIPT):
    WORKER_SCRIPT = os.path.join(WORKSPACE_DIR, "scanner_worker.py")

# Config path: look in module dir, then local workspace, then AppData
CONFIG_FILE = os.path.join(SCANNER_MODULE_DIR, "scanner_config.json")
if not os.path.exists(CONFIG_FILE):
    CONFIG_FILE = os.path.join(WORKSPACE_DIR, "scanner_config.json")

# Scans directory: prefer active workspace Scans dir, fallback to user AppData
WORKSPACE_SCANS_DIR = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Documents", "automated scan stitcher", "Scans")
if os.path.exists(WORKSPACE_SCANS_DIR):
    SCANS_DIR = WORKSPACE_SCANS_DIR
else:
    USER_DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "pdfarranger")
    SCANS_DIR = os.path.join(USER_DATA_DIR, "Scans")

RAW_DIR = os.path.join(SCANS_DIR, "Raw")
STITCHED_DIR = os.path.join(SCANS_DIR, "Stitched")
LOG_FILE = os.path.join(os.path.dirname(SCANS_DIR), "scanner_extension.log")

CREATE_NO_WINDOW = 0x08000000

DEFAULT_CONFIG = {
    "source": "flatbed",     # "flatbed" or "feeder"
    "dpi": 300,              # 150, 300, 600
    "color_mode": "color",   # "color", "gray", "bw"
    "page_size": "a4"        # "a4", "letter", "legal", "photo_4x6", "auto", "max"
}

PAGE_SIZE_SHORT = {
    "a4": "A4",
    "letter": "Letter",
    "legal": "Legal",
    "photo_4x6": "4×6\"",
    "auto": "Auto-Size",
    "max": "Max Area"
}


def log(msg):
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {msg}\n")
    except Exception:
        pass


def idle_add_safe(callback, *args, **kwargs):
    """
    Schedules callback on the GTK main loop via GLib.idle_add.
    GLib.idle_add is thread-safe and always dispatches execution to the GTK GUI main thread.
    Never calls callback directly when GLib is available, preventing fatal cross-thread GTK crashes.
    """
    try:
        from gi.repository import GLib
        if hasattr(GLib, "idle_add"):
            return GLib.idle_add(callback, *args, **kwargs)
        return callback(*args, **kwargs)
    except (ImportError, AttributeError):
        try:
            return callback(*args, **kwargs)
        except Exception:
            return False


def render_page_to_png(app, p, output_path, dpi=300):
    """
    Renders a PDF Arranger Page object to a high-resolution PNG using Cairo and Poppler,
    accurately respecting rotation, crop, and white background.
    Safeguards against oversized imported pages (> 3500px).
    """
    import cairo
    from math import pi

    wpoi = p.size.width * (1 - p.crop.left - p.crop.right)
    hpoi = p.size.height * (1 - p.crop.top - p.crop.bottom)

    # Scale clamp to prevent Cairo memory blowout on 72/96 DPI uncompressed scans
    max_dim_pts = max(wpoi, hpoi)
    zoom = dpi / 72.0
    if max_dim_pts * zoom > 3600:
        zoom = 3500.0 / max(1.0, max_dim_pts)

    wpix = max(1, int(0.5 + wpoi * p.scale * zoom))
    hpix = max(1, int(0.5 + hpoi * p.scale * zoom))
    wpix0, hpix0 = (wpix, hpix) if p.angle in [0, 180] else (hpix, wpix)
    rotation = round((int(p.angle) % 360) / 90) * 90

    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, wpix0, hpix0)
    cr = cairo.Context(surface)
    # Fill solid white background
    cr.set_source_rgb(1, 1, 1)
    cr.paint()

    if rotation > 0:
        cr.translate(wpix0 / 2, hpix0 / 2)
        cr.rotate(-rotation * pi / 180)
        cr.translate(-wpix / 2, -hpix / 2)
    cr.scale(wpix / wpoi, hpix / hpoi)
    cr.translate(-p.crop.left * p.size.width, -p.crop.top * p.size.height)

    cr.save()
    if rotation > 0:
        cr.translate(*p.size.scaled(0.5))
        cr.rotate(rotation * pi / 180)
        cr.translate(*p.size_orig.scaled(-0.5))

    pdfdoc = app.pdfqueue[p.nfile - 1]
    poppler_page = pdfdoc.get_page(p.npage - 1)
    with pdfdoc.render_lock:
        poppler_page.render(cr)
    cr.restore()

    # Apply hide margins if present
    if hasattr(p, "hide") and p.hide and (p.hide.left > 0 or p.hide.right > 0 or p.hide.top > 0 or p.hide.bottom > 0):
        cr.set_source_rgb(1, 1, 1)
        cr.rectangle(0, 0, p.size.width, p.size.height)
        x = p.size.width * p.hide.left
        y = p.size.height * p.hide.top
        w = p.size.width * (1 - p.hide.left - p.hide.right)
        h = p.size.height * (1 - p.hide.top - p.hide.bottom)
        cr.rectangle(x, y, w, h)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.fill()

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    surface.write_to_png(output_path)
    return output_path


def create_placeholder_pdf(output_path, page_num, desc="Stitching in background..."):
    """
    Generates an instantaneous lightweight placeholder page (approx 1 KB)
    so the document layout and page numbers are preserved immediately
    while SIFT alignment computes in the background.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz

        doc = fitz.open()
        page = doc.new_page(width=595.28, height=841.89)  # Standard A4 points
        rect = fitz.Rect(50, 120, 545, 720)
        page.draw_rect(rect, color=(0.8, 0.82, 0.88), fill=(0.96, 0.97, 0.99), width=1.5, dashes="[5 5] 0")
        page.insert_text((75, 380), f"Page {page_num}", fontsize=28, color=(0.15, 0.35, 0.75))
        page.insert_text((75, 420), f"⚡ {desc}", fontsize=15, color=(0.3, 0.3, 0.3))
        page.insert_text((75, 450), "SIFT planar rigid alignment running in background thread...", fontsize=11, color=(0.55, 0.55, 0.55))
        page.insert_text((75, 475), "Scanner is active & ready for next page (F7 / F8)!", fontsize=11, color=(0.15, 0.6, 0.25))
        doc.save(output_path)
        doc.close()
        return output_path
    except Exception as e_fitz:
        log(f"pymupdf/fitz notice: {e_fitz}. Using PIL/raw PDF placeholder fallback.")
        try:
            from PIL import Image, ImageDraw
            im = Image.new("RGB", (595, 842), (248, 250, 253))
            draw = ImageDraw.Draw(im)
            draw.rectangle([50, 120, 545, 720], outline=(200, 210, 225), width=2)
            draw.text((75, 380), f"Page {page_num}", fill=(38, 89, 191))
            draw.text((75, 420), f"⚡ {desc}", fill=(76, 76, 76))
            draw.text((75, 450), "SIFT planar rigid alignment running in background thread...", fill=(140, 140, 140))
            draw.text((75, 475), "Scanner is active & ready for next page (F7 / F8)!", fill=(38, 150, 60))
            im.save(output_path, "PDF", resolution=72.0)
            return output_path
        except Exception:
            raw_pdf = (
                b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
                b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
                b"3 0 obj<</Type/Page/MediaBox[0 0 595.28 841.89]/Parent 2 0 R>>endobj\n"
                b"xref\n0 4\n0000000000 65535 f\n0000000009 00000 n\n0000000052 00000 n\n"
                b"0000000101 00000 n\ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n178\n%%EOF\n"
            )
            with open(output_path, "wb") as f_raw:
                f_raw.write(raw_pdf)
            return output_path


_ACTIVE_SCANNER_MANAGER = None
_GLOBAL_KEYBOARD_HOOKS_INSTALLED = False


def _ensure_global_hotkeys_registered():
    """Registers low-level Windows global hotkeys exactly once, delegating to active ScannerManager."""
    global _GLOBAL_KEYBOARD_HOOKS_INSTALLED
    if _GLOBAL_KEYBOARD_HOOKS_INSTALLED:
        return
    try:
        import keyboard

        def _on_global_key(key_name):
            mgr = _ACTIVE_SCANNER_MANAGER
            if not mgr:
                return

            # If the GTK window is currently focused, GTK's native key-press-event handles it
            if hasattr(mgr.app, "window") and mgr.app.window:
                is_active_func = getattr(mgr.app.window, "is_active", None)
                if callable(is_active_func) and is_active_func():
                    # Window is active and handles the key natively in GTK event loop
                    return

            if mgr._is_duplicate_hotkey(key_name, min_interval=0.5):
                return

            log(f">>> [GLOBAL HOTKEY] {key_name} triggered")
            now = time.time()
            if key_name == "F7":
                idle_add_safe(lambda: mgr.on_f7_clicked(press_time=now))
            elif key_name == "F8":
                idle_add_safe(mgr.on_f8_clicked)
            elif key_name in ("F9", "Shift+F7"):
                idle_add_safe(mgr.on_stitch_clicked)
            elif key_name == "Escape":
                idle_add_safe(mgr.cancel_scan)

        keyboard.add_hotkey("F7", lambda: _on_global_key("F7"))
        keyboard.add_hotkey("F8", lambda: _on_global_key("F8"))
        keyboard.add_hotkey("F9", lambda: _on_global_key("F9"))
        keyboard.add_hotkey("shift+f7", lambda: _on_global_key("Shift+F7"))
        keyboard.add_hotkey("Escape", lambda: _on_global_key("Escape"))
        _GLOBAL_KEYBOARD_HOOKS_INSTALLED = True
        log("Global hotkeys F7, F8, F9, Shift+F7 & Escape registered globally once")
    except Exception as e_kb:
        log(f"Global hotkeys via keyboard module skipped/unavailable: {e_kb}")


class ScannerManager:
    @classmethod
    def get_active_instance(cls):
        global _ACTIVE_SCANNER_MANAGER
        return _ACTIVE_SCANNER_MANAGER

    def __init__(self, app):
        global _ACTIVE_SCANNER_MANAGER
        _ACTIVE_SCANNER_MANAGER = self
        self.app = app
        self.scan_lock = threading.Lock()
        self.autosave_lock = threading.Lock()
        self.last_key_time = {}
        self.is_scanning = False
        self.is_optimizing = False
        self.queued_scan = None
        self.current_scan_proc = None
        self.cancel_requested = False
        self.stitch_queue = queue.Queue()
        self.pending_stitches = 0
        self.part1_path = None
        self.part1_completed_at = 0.0
        self._running = True
        self.session_page_num = 1
        self.session_id = time.strftime("%Y%m%d_%H%M%S")
        self.connected_scanner_name = "HP Smart Tank 750"

        # Load persisted scanner settings
        self.config = self.load_config()

        # UI elements
        self.btn_f7 = None
        self.btn_f8 = None
        self.btn_feeder_batch = None
        self.btn_stitch = None
        self.btn_optimize = None
        self.btn_crop = None
        self.btn_adjust_crop = None
        self.btn_source_toggle = None
        self.btn_reconnect = None
        self.btn_settings = None
        self.lbl_status = None
        self.lbl_popover_summary = None
        self.spinner = None

        # Popover radio button references
        self.radio_flatbed = None
        self.radio_feeder = None
        self.radio_sz_a4 = None
        self.radio_sz_letter = None
        self.radio_sz_legal = None
        self.radio_sz_4x6 = None
        self.radio_sz_auto = None
        self.radio_sz_max = None
        self.radio_150 = None
        self.radio_300 = None
        self.radio_600 = None
        self.radio_color = None
        self.radio_gray = None
        self.radio_bw = None

        os.makedirs(RAW_DIR, exist_ok=True)
        os.makedirs(STITCHED_DIR, exist_ok=True)

        self.setup_ui()
        self.setup_shortcuts()
        self.check_initial_scanner_status()

        # Start dedicated background stitch worker loop
        threading.Thread(target=self._stitch_worker_loop, daemon=True).start()

    def stop(self):
        """Stops background stitch threads cleanly."""
        self._running = False
        try:
            self.stitch_queue.put_nowait(None)
        except Exception:
            pass

    def _is_duplicate_hotkey(self, key_name, min_interval=0.5):
        """
        Deduplicates rapid consecutive keypresses (debouncing) or dual-registered listeners
        (e.g., both GTK window key-press-event and global keyboard hooks).
        Returns True if the event occurred within min_interval seconds of the last event.
        """
        now = time.time()
        with self.scan_lock:
            last = self.last_key_time.get(key_name, 0.0)
            if (now - last) < min_interval:
                return True
            self.last_key_time[key_name] = now
            return False

    # -------------------------------------------------------------------------
    # Configuration Management
    # -------------------------------------------------------------------------
    def load_config(self):
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    res = DEFAULT_CONFIG.copy()
                    res.update(cfg)
                    return res
        except Exception as e:
            log(f"Error loading {CONFIG_FILE}: {e}")
        return DEFAULT_CONFIG.copy()

    def save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
            log(f"Scanner settings updated: {self.config}")
        except Exception as e:
            log(f"Error saving {CONFIG_FILE}: {e}")
        self.update_settings_ui()

    def get_summary_text(self):
        src_label = "Flatbed" if self.config.get("source") == "flatbed" else "ADF Feeder"
        size_key = self.config.get("page_size", "a4")
        size_label = PAGE_SIZE_SHORT.get(size_key, size_key.upper())
        dpi_label = f"{self.config.get('dpi', 300)} DPI"
        cm = self.config.get("color_mode", "color")
        cm_label = "Color" if cm == "color" else ("Grayscale" if cm == "gray" else "B&W")
        return f"{src_label} • {size_label} • {dpi_label} • {cm_label}"

    def update_settings_ui(self):
        def _update():
            # Update quick toggle button
            is_feeder = self.config.get("source") == "feeder"
            if self.btn_source_toggle:
                label_text = "  ADF Feeder" if is_feeder else "  Flatbed"
                self.btn_source_toggle.set_label(label_text)
                icon_name = "printer-symbolic" if is_feeder else "scanner-symbolic"
                img = self.btn_source_toggle.get_image()
                if img:
                    img.set_from_icon_name(icon_name, 4)
                ctx = self.btn_source_toggle.get_style_context()
                if is_feeder:
                    ctx.add_class("scan-toggle-feeder")
                else:
                    ctx.remove_class("scan-toggle-feeder")

            # Update popover summary label
            if self.lbl_popover_summary:
                self.lbl_popover_summary.set_markup(f"<small>Active: <b>{self.get_summary_text()}</b></small>")

            # Update status bar label
            if self.lbl_status and not self.is_scanning:
                self.lbl_status.set_text(f"Scanner: {self.connected_scanner_name} ({self.get_summary_text()})")

            # Sync popover radio buttons if out of sync
            if self.radio_flatbed and self.radio_feeder:
                if is_feeder and not self.radio_feeder.get_active():
                    self.radio_feeder.set_active(True)
                elif not is_feeder and not self.radio_flatbed.get_active():
                    self.radio_flatbed.set_active(True)

            cur_sz = self.config.get("page_size", "a4")
            sz_map = {
                "a4": self.radio_sz_a4,
                "letter": self.radio_sz_letter,
                "legal": self.radio_sz_legal,
                "photo_4x6": self.radio_sz_4x6,
                "auto": self.radio_sz_auto,
                "max": self.radio_sz_max
            }
            target_r = sz_map.get(cur_sz)
            if target_r and not target_r.get_active():
                target_r.set_active(True)

            return False

        idle_add_safe(_update)

    def toggle_source(self):
        curr = self.config.get("source", "flatbed")
        new_src = "feeder" if curr == "flatbed" else "flatbed"
        self.config["source"] = new_src
        self.save_config()

    # -------------------------------------------------------------------------
    # UI Setup
    # -------------------------------------------------------------------------
    def setup_ui(self):
        try:
            import gi
            gi.require_version("Gtk", "3.0")
            gi.require_version("Gdk", "3.0")
            from gi.repository import Gtk, Gdk, GLib

            hb = self.app.uiXML.get_object("header_bar")
            if not hb:
                log("Header bar not found in uiXML")
                return

            # Apply custom CSS
            css_provider = Gtk.CssProvider()
            css = b"""
            .scan-box {
                margin-left: 8px;
                margin-right: 8px;
            }
            .scan-btn-stitch {
                font-weight: bold;
                border-radius: 5px;
            }
            .scan-btn-single {
                font-weight: 500;
                border-radius: 5px;
            }
            .scan-btn-feeder {
                font-weight: 500;
                border-radius: 5px;
            }
            .scan-btn-manual {
                font-weight: bold;
                border-radius: 5px;
                background-color: #f6f8fa;
            }
            .scan-btn-optimize {
                font-weight: 500;
                border-radius: 5px;
            }
            .scan-btn-crop {
                font-weight: 500;
                border-radius: 5px;
            }
            .scan-btn-reconnect {
                font-weight: 500;
                border-radius: 5px;
            }
            .scan-toggle-feeder {
                font-weight: bold;
                background-image: linear-gradient(to bottom, #2ec27e, #26a269);
                color: white;
            }
            .scan-status-lbl {
                font-size: 11px;
                color: #555555;
                margin-left: 6px;
                margin-right: 4px;
            }
            .popover-box {
                margin: 10px;
            }
            .popover-title {
                font-weight: bold;
                font-size: 13px;
                color: #1c71d8;
                margin-bottom: 6px;
            }
            .popover-section-title {
                font-weight: bold;
                font-size: 11px;
                color: #333333;
                margin-top: 6px;
                margin-bottom: 2px;
            }
            """
            css_provider.load_from_data(css)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(),
                css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

            # Container Box
            scan_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            scan_box.get_style_context().add_class("scan-box")

            # Separator
            sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
            sep.set_margin_start(4)
            sep.set_margin_end(4)
            scan_box.pack_start(sep, False, False, 0)

            # 1. Dual-Pass Stitch Button (F7)
            self.btn_f7 = Gtk.Button(label="Scan 2-Part (F7)")
            img_f7 = Gtk.Image.new_from_icon_name("scanner-symbolic", Gtk.IconSize.BUTTON)
            self.btn_f7.set_image(img_f7)
            self.btn_f7.set_always_show_image(True)
            self.btn_f7.set_tooltip_text("Dual-pass scan & SIFT stitch for books/large documents (Shortcut: F7)")
            self.btn_f7.get_style_context().add_class("suggested-action")
            self.btn_f7.get_style_context().add_class("scan-btn-stitch")
            self.btn_f7.connect("clicked", lambda w: self.on_f7_clicked())
            scan_box.pack_start(self.btn_f7, False, False, 0)

            # 2. Single-Pass Button (F8)
            self.btn_f8 = Gtk.Button(label="Scan Single (F8)")
            img_f8 = Gtk.Image.new_from_icon_name("document-new-symbolic", Gtk.IconSize.BUTTON)
            self.btn_f8.set_image(img_f8)
            self.btn_f8.set_always_show_image(True)
            self.btn_f8.set_tooltip_text("Single-pass scan for standard pages (Shortcut: F8)")
            self.btn_f8.get_style_context().add_class("scan-btn-single")
            self.btn_f8.connect("clicked", lambda w: self.on_f8_clicked())
            scan_box.pack_start(self.btn_f8, False, False, 0)

            # 2b. Dedicated Feeder Stack Button
            self.btn_feeder_batch = Gtk.Button(label="📑 Scan Feeder Stack")
            img_fb = Gtk.Image.new_from_icon_name("media-playlist-repeat-symbolic", Gtk.IconSize.BUTTON)
            self.btn_feeder_batch.set_image(img_fb)
            self.btn_feeder_batch.set_always_show_image(True)
            self.btn_feeder_batch.set_tooltip_text("Scan entire multi-page document stack from Automatic Document Feeder (ADF)")
            self.btn_feeder_batch.get_style_context().add_class("scan-btn-feeder")
            self.btn_feeder_batch.connect("clicked", lambda w: self.on_feeder_batch_clicked())
            scan_box.pack_start(self.btn_feeder_batch, False, False, 0)

            # 3. Manual Stitch of 2 Selected Pages (F9)
            self.btn_stitch = Gtk.Button(label="Stitch 2 Pages (F9)")
            img_stitch = Gtk.Image.new_from_icon_name("insert-link-symbolic", Gtk.IconSize.BUTTON)
            self.btn_stitch.set_image(img_stitch)
            self.btn_stitch.set_always_show_image(True)
            self.btn_stitch.set_tooltip_text("SIFT stitch 2 selected pages into a single unified page (Shortcut: F9)")
            self.btn_stitch.get_style_context().add_class("scan-btn-manual")
            self.btn_stitch.connect("clicked", lambda w: self.on_stitch_clicked())
            scan_box.pack_start(self.btn_stitch, False, False, 0)

            # 4. Fast-Load / Optimize Document Button
            self.btn_optimize = Gtk.Button(label="⚡ Optimize")
            img_opt = Gtk.Image.new_from_icon_name("system-run-symbolic", Gtk.IconSize.BUTTON)
            self.btn_optimize.set_image(img_opt)
            self.btn_optimize.set_always_show_image(True)
            self.btn_optimize.set_tooltip_text("Compress scans and fix oversized pages for instant fast-loading (90% size reduction)")
            self.btn_optimize.get_style_context().add_class("scan-btn-optimize")
            self.btn_optimize.connect("clicked", lambda w: self.on_optimize_clicked())
            scan_box.pack_start(self.btn_optimize, False, False, 0)

            # 5. Adjust Crop & Margins Button (C)
            self.btn_adjust_crop = Gtk.Button(label="✂️ Adjust Crop")
            self.btn_crop = self.btn_adjust_crop
            img_crop = Gtk.Image.new_from_icon_name("edit-cut-symbolic", Gtk.IconSize.BUTTON)
            self.btn_adjust_crop.set_image(img_crop)
            self.btn_adjust_crop.set_always_show_image(True)
            self.btn_adjust_crop.set_tooltip_text("Manual Page Crop & Margins adjustment dialog with live preview (Shortcut: C)")
            self.btn_adjust_crop.get_style_context().add_class("scan-btn-crop")
            self.btn_adjust_crop.connect("clicked", lambda w: self.on_adjust_crop_clicked())
            scan_box.pack_start(self.btn_adjust_crop, False, False, 0)

            # 6. Quick Source Toggle Button (Flatbed <-> Feeder/ADF)
            self.btn_source_toggle = Gtk.Button(label="  Flatbed")
            img_src = Gtk.Image.new_from_icon_name("scanner-symbolic", Gtk.IconSize.BUTTON)
            self.btn_source_toggle.set_image(img_src)
            self.btn_source_toggle.set_always_show_image(True)
            self.btn_source_toggle.set_tooltip_text("Click to toggle between Flatbed Glass and Automatic Document Feeder (ADF)")
            self.btn_source_toggle.connect("clicked", lambda w: self.toggle_source())
            scan_box.pack_start(self.btn_source_toggle, False, False, 0)

            # 7. Reconnect / Refresh Scanner Button
            self.btn_reconnect = Gtk.Button(label="🔄 Reconnect")
            img_rec = Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
            self.btn_reconnect.set_image(img_rec)
            self.btn_reconnect.set_always_show_image(True)
            self.btn_reconnect.set_tooltip_text("Reconnect Scanner: Refreshes USB WIA port or switches to direct WiFi fallback (192.168.68.69)")
            self.btn_reconnect.get_style_context().add_class("scan-btn-reconnect")
            self.btn_reconnect.connect("clicked", lambda w: self.reconnect_scanner())
            scan_box.pack_start(self.btn_reconnect, False, False, 0)

            # 8. Auto-Session Recovery Button
            self.btn_restore = Gtk.Button(label="📂 Restore Session")
            img_rest = Gtk.Image.new_from_icon_name("document-open-recent-symbolic", Gtk.IconSize.BUTTON)
            self.btn_restore.set_image(img_rest)
            self.btn_restore.set_always_show_image(True)
            self.btn_restore.set_tooltip_text("Instantly reloads your last auto-saved scanning session if the app crashed or closed")
            self.btn_restore.connect("clicked", lambda w: self.on_restore_session_clicked())
            scan_box.pack_start(self.btn_restore, False, False, 0)

            # 6. Quick Access Settings Menu Button (Popover)
            self.btn_settings = Gtk.MenuButton()
            img_set = Gtk.Image.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.BUTTON)
            self.btn_settings.set_image(img_set)
            self.btn_settings.set_always_show_image(True)
            self.btn_settings.set_tooltip_text("Scanner Settings (Flatbed/ADF, Scan Size, DPI, Color Mode)")

            # Build Popover Menu Content
            popover = Gtk.Popover()
            pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            pop_box.get_style_context().add_class("popover-box")

            lbl_title = Gtk.Label()
            lbl_title.set_markup("<span weight='bold' size='large'>Scanner Settings</span>")
            lbl_title.set_alignment(0, 0.5)
            pop_box.pack_start(lbl_title, False, False, 4)

            # --- Section 1: Scan Source ---
            lbl_src = Gtk.Label()
            lbl_src.set_markup("<b>Input Tray / Source:</b>")
            lbl_src.set_alignment(0, 0.5)
            pop_box.pack_start(lbl_src, False, False, 2)

            self.radio_flatbed = Gtk.RadioButton.new_with_label(None, "Flatbed (Platen Glass)")
            self.radio_feeder = Gtk.RadioButton.new_with_label_from_widget(self.radio_flatbed, "Automatic Document Feeder (ADF)")
            if self.config.get("source") == "feeder":
                self.radio_feeder.set_active(True)
            else:
                self.radio_flatbed.set_active(True)

            def on_source_radio(btn):
                if btn.get_active():
                    self.config["source"] = "feeder" if btn == self.radio_feeder else "flatbed"
                    self.save_config()

            self.radio_flatbed.connect("toggled", on_source_radio)
            self.radio_feeder.connect("toggled", on_source_radio)
            pop_box.pack_start(self.radio_flatbed, False, False, 0)
            pop_box.pack_start(self.radio_feeder, False, False, 0)

            pop_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)

            # --- Section 2: Scan Size (Page Area) ---
            lbl_sz = Gtk.Label()
            lbl_sz.set_markup("<b>Scan Size (Paper Area):</b>")
            lbl_sz.set_alignment(0, 0.5)
            pop_box.pack_start(lbl_sz, False, False, 2)

            self.radio_sz_a4 = Gtk.RadioButton.new_with_label(None, "A4 (210 × 297 mm - Standard)")
            self.radio_sz_letter = Gtk.RadioButton.new_with_label_from_widget(self.radio_sz_a4, "Letter (8.5 × 11 in)")
            self.radio_sz_legal = Gtk.RadioButton.new_with_label_from_widget(self.radio_sz_a4, "Legal (8.5 × 14 in - ADF Feeder)")
            self.radio_sz_4x6 = Gtk.RadioButton.new_with_label_from_widget(self.radio_sz_a4, "4 × 6 in (10 × 15 cm Photo)")
            self.radio_sz_auto = Gtk.RadioButton.new_with_label_from_widget(self.radio_sz_a4, "Auto-Detect Size (Auto-Crop)")
            self.radio_sz_max = Gtk.RadioButton.new_with_label_from_widget(self.radio_sz_a4, "Entire Scan Area (Max Platen)")

            cur_sz = self.config.get("page_size", "a4")
            if cur_sz == "letter":
                self.radio_sz_letter.set_active(True)
            elif cur_sz == "legal":
                self.radio_sz_legal.set_active(True)
            elif cur_sz == "photo_4x6":
                self.radio_sz_4x6.set_active(True)
            elif cur_sz == "auto":
                self.radio_sz_auto.set_active(True)
            elif cur_sz == "max":
                self.radio_sz_max.set_active(True)
            else:
                self.radio_sz_a4.set_active(True)

            def on_sz_radio(btn):
                if btn.get_active():
                    if btn == self.radio_sz_letter:
                        self.config["page_size"] = "letter"
                    elif btn == self.radio_sz_legal:
                        self.config["page_size"] = "legal"
                    elif btn == self.radio_sz_4x6:
                        self.config["page_size"] = "photo_4x6"
                    elif btn == self.radio_sz_auto:
                        self.config["page_size"] = "auto"
                    elif btn == self.radio_sz_max:
                        self.config["page_size"] = "max"
                    else:
                        self.config["page_size"] = "a4"
                    self.save_config()

            for r in (self.radio_sz_a4, self.radio_sz_letter, self.radio_sz_legal, self.radio_sz_4x6, self.radio_sz_auto, self.radio_sz_max):
                r.connect("toggled", on_sz_radio)
                pop_box.pack_start(r, False, False, 0)

            pop_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)

            # --- Section 3: DPI Resolution ---
            lbl_dpi = Gtk.Label()
            lbl_dpi.set_markup("<b>Resolution (DPI):</b>")
            lbl_dpi.set_alignment(0, 0.5)
            pop_box.pack_start(lbl_dpi, False, False, 2)

            self.radio_150 = Gtk.RadioButton.new_with_label(None, "150 DPI (Fast / Draft)")
            self.radio_300 = Gtk.RadioButton.new_with_label_from_widget(self.radio_150, "300 DPI (Standard - Recommended)")
            self.radio_600 = Gtk.RadioButton.new_with_label_from_widget(self.radio_150, "600 DPI (High Detail)")

            cur_dpi = self.config.get("dpi", 300)
            if cur_dpi == 150:
                self.radio_150.set_active(True)
            elif cur_dpi == 600:
                self.radio_600.set_active(True)
            else:
                self.radio_300.set_active(True)

            def on_dpi_radio(btn):
                if btn.get_active():
                    if btn == self.radio_150:
                        self.config["dpi"] = 150
                    elif btn == self.radio_600:
                        self.config["dpi"] = 600
                    else:
                        self.config["dpi"] = 300
                    self.save_config()

            self.radio_150.connect("toggled", on_dpi_radio)
            self.radio_300.connect("toggled", on_dpi_radio)
            self.radio_600.connect("toggled", on_dpi_radio)
            pop_box.pack_start(self.radio_150, False, False, 0)
            pop_box.pack_start(self.radio_300, False, False, 0)
            pop_box.pack_start(self.radio_600, False, False, 0)

            pop_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)

            # --- Section 4: Color Mode ---
            lbl_cm = Gtk.Label()
            lbl_cm.set_markup("<b>Color Mode:</b>")
            lbl_cm.set_alignment(0, 0.5)
            pop_box.pack_start(lbl_cm, False, False, 2)

            self.radio_color = Gtk.RadioButton.new_with_label(None, "Full Color")
            self.radio_gray = Gtk.RadioButton.new_with_label_from_widget(self.radio_color, "Grayscale")
            self.radio_bw = Gtk.RadioButton.new_with_label_from_widget(self.radio_color, "Black & White (Text)")

            cur_cm = self.config.get("color_mode", "color")
            if cur_cm == "gray":
                self.radio_gray.set_active(True)
            elif cur_cm == "bw":
                self.radio_bw.set_active(True)
            else:
                self.radio_color.set_active(True)

            def on_cm_radio(btn):
                if btn.get_active():
                    if btn == self.radio_gray:
                        self.config["color_mode"] = "gray"
                    elif btn == self.radio_bw:
                        self.config["color_mode"] = "bw"
                    else:
                        self.config["color_mode"] = "color"
                    self.save_config()

            self.radio_color.connect("toggled", on_cm_radio)
            self.radio_gray.connect("toggled", on_cm_radio)
            self.radio_bw.connect("toggled", on_cm_radio)
            pop_box.pack_start(self.radio_color, False, False, 0)
            pop_box.pack_start(self.radio_gray, False, False, 0)
            pop_box.pack_start(self.radio_bw, False, False, 0)

            pop_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)

            # Summary Label in Popover
            self.lbl_popover_summary = Gtk.Label()
            self.lbl_popover_summary.set_markup(f"<small>Active: <b>{self.get_summary_text()}</b></small>")
            self.lbl_popover_summary.set_alignment(0, 0.5)
            pop_box.pack_start(self.lbl_popover_summary, False, False, 2)

            pop_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)

            # --- Section 5: Cache & Disk Maintenance ---
            lbl_maint = Gtk.Label()
            lbl_maint.set_markup("<b>Temporary Cache & Disk Space:</b>")
            lbl_maint.set_alignment(0, 0.5)
            pop_box.pack_start(lbl_maint, False, False, 2)

            self.btn_clean_cache = Gtk.Button(label="🧹 Clean Temp Cache")
            self.btn_clean_cache.set_tooltip_text("Safely purge temporary placeholder files, test artifacts, and conversion buffers to free disk space")
            self.btn_clean_cache.connect("clicked", lambda w: self.on_clean_temp_cache_clicked())
            pop_box.pack_start(self.btn_clean_cache, False, False, 2)

            pop_box.show_all()
            popover.add(pop_box)
            self.btn_settings.set_popover(popover)
            scan_box.pack_start(self.btn_settings, False, False, 0)

            # Spinner
            self.spinner = Gtk.Spinner()
            scan_box.pack_start(self.spinner, False, False, 2)

            # Status label
            self.lbl_status = Gtk.Label(label=f"Checking scanner... ({self.get_summary_text()})")
            self.lbl_status.get_style_context().add_class("scan-status-lbl")
            scan_box.pack_start(self.lbl_status, False, False, 0)

            scan_box.show_all()
            self.spinner.hide()

            # Pack next to standard open/save controls in header bar
            hb.pack_start(scan_box)

            # Add "Stitch 2 Selected Pages" to Right-Click Context Menu
            if hasattr(self.app, "popup") and self.app.popup is not None:
                try:
                    sep_m = Gtk.SeparatorMenuItem()
                    sep_m.show()
                    self.app.popup.append(sep_m)

                    item_m = Gtk.MenuItem(label="Stitch 2 Selected Pages (F9)")
                    item_m.connect("activate", lambda w: self.on_stitch_clicked())
                    item_m.show()
                    self.app.popup.append(item_m)

                    item_feeder = Gtk.MenuItem(label="📑 Scan All Pages from Feeder (ADF)")
                    item_feeder.connect("activate", lambda w: self.on_feeder_batch_clicked())
                    item_feeder.show()
                    self.app.popup.append(item_feeder)

                    item_crop = Gtk.MenuItem(label="✂️ Adjust Page Crop / Margins... (C)")
                    item_crop.connect("activate", lambda w: self.on_adjust_crop_clicked())
                    item_crop.show()
                    self.app.popup.append(item_crop)

                    item_reset = Gtk.MenuItem(label="↺ Reset / Remove Crop")
                    item_reset.connect("activate", lambda w: self.reset_crop_selected())
                    item_reset.show()
                    self.app.popup.append(item_reset)

                    item_opt = Gtk.MenuItem(label="⚡ Optimize / Compress Document (Fast-Load)")
                    item_opt.connect("activate", lambda w: self.on_optimize_clicked())
                    item_opt.show()
                    self.app.popup.append(item_opt)

                    item_clean = Gtk.MenuItem(label="🧹 Clean Temporary Cache (Free Disk Space)")
                    item_clean.connect("activate", lambda w: self.on_clean_temp_cache_clicked())
                    item_clean.show()
                    self.app.popup.append(item_clean)
                    log("Added 'Stitch 2 Selected Pages', 'Adjust Crop Margins', 'Reset Crop', 'Optimize Document' & 'Clean Temp Cache' to right-click popup menu")
                except Exception as ex_m:
                    log(f"Context menu item hook note: {ex_m}")

            self.update_settings_ui()
            log("Scanner UI controls with Quick Access Settings packed into header bar successfully")

        except Exception as e:
            log(f"Error in setup_ui: {e}\nsys.path: {sys.path}\n{traceback.format_exc()}")

    def setup_shortcuts(self):
        try:
            from gi.repository import Gdk, GLib

            # 1. Window key_press_event with deduplication
            def on_key_press(widget, event):
                keyval = event.keyval
                keyname = Gdk.keyval_name(keyval) if hasattr(Gdk, "keyval_name") else ""
                shift_pressed = bool(event.state & Gdk.ModifierType.SHIFT_MASK) if hasattr(event, "state") else False

                if keyname in ("F7", "KP_F7") or keyval in (getattr(Gdk, "KEY_F7", 65476), 65476):
                    if self._is_duplicate_hotkey("F7" if not shift_pressed else "Shift+F7"):
                        return True
                    log(f">>> [KEYBOARD EVENT] Window key F7 received (shift={shift_pressed})")
                    now = time.time()
                    if shift_pressed:
                        idle_add_safe(self.on_stitch_clicked)
                    else:
                        idle_add_safe(lambda: self.on_f7_clicked(press_time=now))
                    return True
                elif keyname in ("F8", "KP_F8") or keyval in (getattr(Gdk, "KEY_F8", 65477), 65477):
                    if self._is_duplicate_hotkey("F8"):
                        return True
                    log(f">>> [KEYBOARD EVENT] Window key F8 received")
                    idle_add_safe(self.on_f8_clicked)
                    return True
                elif keyname in ("F9", "KP_F9") or keyval in (getattr(Gdk, "KEY_F9", 65478), 65478):
                    if self._is_duplicate_hotkey("F9"):
                        return True
                    log(f">>> [KEYBOARD EVENT] Window key F9 received")
                    idle_add_safe(self.on_stitch_clicked)
                    return True
                elif keyname in ("Escape", "Esc") or keyval in (getattr(Gdk, "KEY_Escape", 65307), 65307):
                    if self._is_duplicate_hotkey("Escape", min_interval=0.2):
                        return True
                    log(f">>> [KEYBOARD EVENT] Escape key received -> Requesting immediate scan cancel")
                    idle_add_safe(self.cancel_scan)
                    return True
                elif keyname in ("c", "C") or keyval in (getattr(Gdk, "KEY_c", 99), getattr(Gdk, "KEY_C", 67), 99, 67):
                    ctrl = bool(event.state & getattr(Gdk.ModifierType, "CONTROL_MASK", 4))
                    alt = bool(event.state & getattr(Gdk.ModifierType, "MOD1_MASK", 8))
                    if not ctrl and not alt:
                        if not self._is_duplicate_hotkey("C", min_interval=0.3):
                            log(">>> [KEYBOARD EVENT] Window key C received -> Adjust Crop")
                            idle_add_safe(self.on_crop_clicked)
                            return True
                return False

            if hasattr(self.app, "window") and self.app.window:
                self.app.window.connect("key-press-event", on_key_press)
                log("Window key-press-event connected for F7, F8, F9, Escape, and C")

            # 2. Register global hotkeys once at module level
            _ensure_global_hotkeys_registered()

        except Exception as e:
            log(f"Error in setup_shortcuts: {e}")

    def update_ui_state(self, status_msg=None):
        def _update():
            # Dual-pass scan button
            if self.btn_f7:
                self.btn_f7.set_sensitive(not self.is_scanning)
                if self.is_scanning:
                    self.btn_f7.set_label("Scanning...")
                elif self.part1_path is not None:
                    self.btn_f7.set_label("Scan Part 2/2 (F7)")
                else:
                    self.btn_f7.set_label("Scan 2-Part (F7)")

            # Single-pass scan button / Feeder batch button
            is_feeder = self.config.get("source") == "feeder"
            if self.btn_f8:
                self.btn_f8.set_sensitive(not self.is_scanning)
                if self.is_scanning:
                    self.btn_f8.set_label("Scanning...")
                elif self.part1_path is not None:
                    self.btn_f8.set_label("Finalize Part 1 (F8)")
                elif is_feeder:
                    self.btn_f8.set_label("Scan Feeder Batch (F8)")
                else:
                    self.btn_f8.set_label("Scan Single (F8)")

            # Dedicated Feeder Stack button
            if hasattr(self, "btn_feeder_batch") and self.btn_feeder_batch:
                self.btn_feeder_batch.set_sensitive(not self.is_scanning)
                if self.is_scanning:
                    self.btn_feeder_batch.set_label("Scanning Stack...")
                else:
                    self.btn_feeder_batch.set_label("📑 Scan Feeder Stack")

            # Manual stitch button is NEVER blocked by scanning or background stitching
            if self.btn_stitch:
                self.btn_stitch.set_sensitive(True)

            if self.btn_optimize:
                self.btn_optimize.set_sensitive(not self.is_optimizing)

            if self.btn_crop:
                self.btn_crop.set_sensitive(not self.is_scanning)
            if self.btn_adjust_crop:
                self.btn_adjust_crop.set_sensitive(not self.is_scanning)

            if self.btn_source_toggle:
                self.btn_source_toggle.set_sensitive(not self.is_scanning)
            if self.btn_reconnect:
                self.btn_reconnect.set_sensitive(not self.is_scanning)
            if self.btn_settings:
                self.btn_settings.set_sensitive(not self.is_scanning)

            # Spinner active if hardware scanning, background stitching, or optimizing
            if self.spinner:
                if self.is_scanning or self.pending_stitches > 0 or self.is_optimizing:
                    self.spinner.show()
                    self.spinner.start()
                else:
                    self.spinner.stop()
                    self.spinner.hide()

            # Status label
            if self.lbl_status:
                if status_msg:
                    self.lbl_status.set_text(status_msg)
                elif self.is_scanning:
                    q_note = " (Next scan queued)" if self.queued_scan else ""
                    self.lbl_status.set_text(f"Scanning carriage moving...{q_note} [{self.get_summary_text()}]")
                elif self.part1_path is not None:
                    self.lbl_status.set_text("Part 1 Done — Reposition page & Press F7 for Part 2")
                elif self.pending_stitches > 0:
                    self.lbl_status.set_text("Stitching in background...")
                else:
                    self.lbl_status.set_text(f"Scanner: {self.connected_scanner_name} [{self.get_summary_text()}]")

            return False

        idle_add_safe(_update)

    def set_ui_status(self, text, busy=False, btn7_text=None, btn8_text=None, btn_stitch_text=None):
        if busy:
            self.is_scanning = True
        self.update_ui_state(status_msg=text)

    def _check_and_trigger_queued_scan(self):
        if self.queued_scan:
            action = self.queued_scan
            self.queued_scan = None
            log(f"Triggering queued scan action: {action}")
            if action == "f7":
                idle_add_safe(self.on_f7_clicked)
            elif action == "f8":
                idle_add_safe(self.on_f8_clicked)

    def check_initial_scanner_status(self):
        def _worker():
            try:
                cmd = [PYTHON_EXE, WORKER_SCRIPT, "check-status"]
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                    timeout=10,
                    env=dict(os.environ)
                )
                data = self.parse_worker_output(res.stdout)
                if data and data.get("connected"):
                    self.connected_scanner_name = data.get("name", "Connected")
                    self.update_ui_state()
                    log(f"Scanner connected: {self.connected_scanner_name}")
                else:
                    self.update_ui_state("Scanner: Offline / Sleep")
                    log("Scanner offline or not detected")
            except Exception as e:
                log(f"check_initial_scanner_status error: {e}")
                self.update_ui_state()

        threading.Thread(target=_worker, daemon=True).start()

    def parse_worker_output(self, stdout):
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("__RESULT__"):
                json_str = line[len("__RESULT__"):]
                return json.loads(json_str)
        return None

    def cancel_scan(self):
        """
        Immediately cancels any active hardware scan, kills the worker subprocess,
        clears any queued scan action, and resets multi-part state when Escape is pressed.
        """
        log(">>> [ACTION] Immediate scan cancellation requested via Escape")
        with self.scan_lock:
            self.cancel_requested = True
            self.queued_scan = None
            self.part1_path = None
            self.is_scanning = False

            if self.current_scan_proc and self.current_scan_proc.poll() is None:
                try:
                    self.current_scan_proc.kill()
                    log("Killed active scanner worker process on Escape")
                except Exception as e:
                    log(f"Error killing scan process: {e}")

        self.update_ui_state("Scan cancelled by user [Escape]")
        try:
            if wia_scanner and hasattr(wia_scanner, "play_sound"):
                wia_scanner.play_sound("cancel", wait=False)
        except Exception:
            pass

    def _run_scan_subprocess(self, cmd, timeout=60):
        """
        Executes scanner worker subprocess with instantaneous Escape-key kill capability.
        """
        self.cancel_requested = False
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            env=dict(os.environ)
        )
        self.current_scan_proc = proc
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            raise TimeoutError("Scan process timed out")
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()
            except Exception:
                pass
            self.current_scan_proc = None

        if self.cancel_requested:
            self.cancel_requested = False
            raise RuntimeError("Scan cancelled by user (Escape)")

        return self.parse_worker_output(stdout), stderr

    def _run_streaming_scan_subprocess(self, cmd, on_page_ready=None, timeout=300):
        """
        Executes scanner worker subprocess with real-time streaming line-by-line output
        and instantaneous Escape cancellation capability.
        """
        self.cancel_requested = False
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            env=dict(os.environ)
        )
        self.current_scan_proc = proc
        result_data = None
        stderr_lines = []

        def _drain_stderr():
            try:
                for line in proc.stderr:
                    stderr_lines.append(line)
            except Exception:
                pass

        t_err = threading.Thread(target=_drain_stderr, daemon=True)
        t_err.start()

        try:
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith("__PAGE_READY__"):
                    json_str = line[len("__PAGE_READY__"):]
                    try:
                        info = json.loads(json_str)
                        if on_page_ready:
                            on_page_ready(info)
                    except Exception as e_json:
                        log(f"Error handling __PAGE_READY__: {e_json}")
                elif line.startswith("__RESULT__"):
                    json_str = line[len("__RESULT__"):]
                    try:
                        result_data = json.loads(json_str)
                    except Exception as e_res:
                        log(f"Error handling __RESULT__: {e_res}")

            proc.wait(timeout=10)
        except Exception as ex:
            try:
                proc.kill()
            except Exception:
                pass
            if not self.cancel_requested:
                raise ex
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()
            except Exception:
                pass
            self.current_scan_proc = None

        return result_data, "".join(stderr_lines)

    def on_adjust_crop_clicked(self):
        """
        Invokes PDF Arranger's native crop dialog (app.crop_dialog) on the selected page.
        If no page is selected, automatically selects the last scanned page so the user
        can adjust or fine-tune page margins with live interactive preview.
        If document has 0 pages, returns early without opening dialog.
        """
        log(">>> [ACTION] Adjust Page Crop activated")
        try:
            if not hasattr(self.app, "crop_dialog"):
                log("crop_dialog not available on app")
                return

            # Check if document has pages
            has_pages = False
            if hasattr(self.app, "model") and self.app.model is not None and len(self.app.model) > 0:
                has_pages = True

            if not has_pages:
                log("Adjust crop skipped: document is empty (0 pages)")
                return

            # Check if any page is selected in iconview
            if hasattr(self.app, "iconview") and self.app.iconview:
                sel = self.app.iconview.get_selected_items()
                if not sel:
                    # Auto-select the last page in the model if none currently selected
                    last_idx = len(self.app.model) - 1
                    try:
                        self.app.iconview.select_path(last_idx)
                    except Exception:
                        pass
                    if hasattr(self.app, "iv_selection_changed"):
                        self.app.iv_selection_changed()

            self.app.crop_dialog(None, None, None)
        except Exception as e:
            log(f"Error opening crop dialog: {e}")

    def reconnect_scanner(self):
        """
        Refreshes scanner connection across USB and direct wireless network (192.168.68.69:8080).
        Restores WIA link or engages network fallback seamlessly.
        """
        log(">>> [ACTION] Reconnect Scanner triggered")
        self.update_ui_state("Reconnecting scanner (USB / Network fallback)...")
        if self.spinner:
            self.spinner.show()
            self.spinner.start()

        def _bg_reconnect():
            try:
                cmd = [PYTHON_EXE, WORKER_SCRIPT, "check-status"]
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                    timeout=12,
                    env=dict(os.environ)
                )
                data = self.parse_worker_output(res.stdout)
                name = data.get("name") if data and data.get("connected") else None

                def _ui():
                    if self.spinner:
                        self.spinner.stop()
                        self.spinner.hide()
                    if name:
                        self.connected_scanner_name = name
                        self.update_ui_state(f"Ready: {name}")
                        log(f"Scanner reconnected successfully: {name}")
                        play_sound_safe("success")
                    else:
                        self.update_ui_state("Scanner offline (Check USB cable or WiFi 192.168.68.69)")
                        play_sound_safe("error")
                    return False
                idle_add_safe(_ui)
            except Exception as e:
                log(f"Error reconnecting scanner: {e}")
                idle_add_safe(lambda: self.update_ui_state(f"Reconnect error: {e}"))

        threading.Thread(target=_bg_reconnect, daemon=True).start()

    def _auto_reconnect_on_error(self):
        time.sleep(1.2)
        try:
            cmd = [PYTHON_EXE, WORKER_SCRIPT, "check-status"]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=10,
                env=dict(os.environ)
            )
            data = self.parse_worker_output(res.stdout)
            if data and data.get("connected"):
                name = data.get("name")
                self.connected_scanner_name = name
                idle_add_safe(lambda: self.update_ui_state(f"Recovered: {name} (Ready to scan)"))
        except Exception:
            pass

    def _autosave_page(self, page_pdf):
        """
        Appends every successfully acquired/stitched page into an auto-saved recovery PDF.
        Runs safely in a background daemon thread so disk recovery is always 100% up to date.
        Uses pikepdf (natively bundled with PDF Arranger) with fitz fallback.
        """
        lock = getattr(self, "autosave_lock", None)

        def _bg_save():
            if lock:
                lock.acquire()
            try:
                processed_dir = os.path.join(WORKSPACE_DIR, "Processed")
                os.makedirs(processed_dir, exist_ok=True)
                autosave_pdf = os.path.join(processed_dir, "Autosave_Current_Session.pdf")
                temp_as = autosave_pdf + ".tmp"

                saved = False
                try:
                    import pikepdf
                    if not os.path.exists(autosave_pdf) or os.path.getsize(autosave_pdf) == 0:
                        with pikepdf.open(page_pdf) as src:
                            src.save(temp_as)
                    else:
                        with pikepdf.open(autosave_pdf) as dst:
                            with pikepdf.open(page_pdf) as src:
                                dst.pages.extend(src.pages)
                            dst.save(temp_as)
                    os.replace(temp_as, autosave_pdf)
                    saved = True
                except Exception:
                    try:
                        import fitz
                        if not os.path.exists(autosave_pdf) or os.path.getsize(autosave_pdf) == 0:
                            with fitz.open(page_pdf) as src:
                                src.save(temp_as, deflate=True)
                        else:
                            doc = fitz.open(autosave_pdf)
                            with fitz.open(page_pdf) as src:
                                doc.insert_pdf(src)
                            doc.save(temp_as, deflate=True)
                            doc.close()
                        os.replace(temp_as, autosave_pdf)
                        saved = True
                    except Exception as e_fitz:
                        log(f"Autosave fallback fitz error: {e_fitz}")

                if saved:
                    log(f"Auto-saved scan '{os.path.basename(page_pdf)}' into {autosave_pdf}")
            except Exception as e_as:
                log(f"Auto-save warning: {e_as}")
            finally:
                if lock:
                    lock.release()

        threading.Thread(target=_bg_save, daemon=True).start()

    def on_restore_session_clicked(self):
        """
        Instantly restores the last auto-saved session into PDF Arranger.
        """
        log(">>> [ACTION] Restore Session activated")
        autosave_pdf = os.path.join(WORKSPACE_DIR, "Processed", "Autosave_Current_Session.pdf")
        if os.path.exists(autosave_pdf) and os.path.getsize(autosave_pdf) > 0:
            self.inject_page_into_pdfarranger(autosave_pdf, "Auto-Saved Session")
            self.update_ui_state("Restored auto-saved session!")
        else:
            self.update_ui_state("No auto-saved session found")

    # -------------------------------------------------------------------------
    # Dual-Pass (F7) Scanning Workflow
    # -------------------------------------------------------------------------
    def on_f7_clicked(self, press_time=None):
        log(">>> [ACTION] Scan 2-Part (F7) activated")
        with self.scan_lock:
            if self.is_scanning:
                if self.part1_path is None:
                    # Carriage is currently scanning Part 1 (top half).
                    # Part 2 requires the user to physically flip/rotate the document after carriage stops.
                    # Therefore, any F7 clicked during Part 1 carriage travel must be absorbed, NOT queued to auto-fire Part 2!
                    log("F7 clicked during Part 1 carriage movement -> Absorbed (must flip sheet before Part 2)")
                    self.update_ui_state("Scanning Part 1... (Flip sheet when carriage stops)")
                    return
                else:
                    # Carriage is currently scanning Part 2 (bottom half).
                    # User clicked F7 -> Record intent to scan Part 1 of the NEXT page when this 2-part cycle completes.
                    log("F7 clicked during Part 2 carriage movement -> Queued Part 1 for next page")
                    self.queued_scan = "f7"
                    self.update_ui_state()
                    return

            # CRITICAL DEFECT GUARD: Part 1 completion must NEVER auto-fire Part 2 from an accidental key buffer.
            # If F7 was pressed before or at the moment Part 1 finished, discard it.
            if self.part1_path is not None and press_time is not None:
                part1_time = getattr(self, "part1_completed_at", 0.0)
                if press_time <= part1_time:
                    log("F7 discarded: Key was pressed before Part 1 carriage finished. Part 2 requires explicit user keypress after carriage stops.")
                    return

            src_desc = "Feeder (ADF)" if self.config.get("source") == "feeder" else "Flatbed"
            if self.part1_path is None:
                self.is_scanning = True
                self.update_ui_state(f"Scanning Top Half (Part 1/2) [{src_desc}]...")
                threading.Thread(target=self._worker_scan_part1, daemon=True).start()
            else:
                self.is_scanning = True
                self.update_ui_state(f"Scanning Bottom Half (Part 2/2) [{src_desc}]...")
                threading.Thread(target=self._worker_scan_part2_and_stitch, daemon=True).start()

    def _worker_scan_part1(self):
        try:
            curr_page = self.session_page_num
            filename = f"Scan_{self.session_id}_P{curr_page:02d}_Part1.png"
            out_path = os.path.join(RAW_DIR, filename)

            cmd = [
                PYTHON_EXE, WORKER_SCRIPT, "scan-part",
                "--out", out_path,
                "--dpi", str(self.config.get("dpi", 300)),
                "--color-mode", self.config.get("color_mode", "color"),
                "--source", "flatbed",
                "--page-size", self.config.get("page_size", "a4")
            ]
            data, stderr = self._run_scan_subprocess(cmd, timeout=60)

            if not data or not data.get("success"):
                err = data.get("error") if data else stderr
                threading.Thread(target=self._auto_reconnect_on_error, daemon=True).start()
                raise RuntimeError(f"Scan Part 1 failed: {err}")

            with self.scan_lock:
                self.part1_path = out_path
                self.part1_completed_at = time.time()
                self.last_key_time["F7"] = time.time()
                self.is_scanning = False
                # CRITICAL: Part 2 of a 2-part scan MUST NEVER be automatically triggered
                # by a queued scan or without explicit user intent. Discard any queued scan.
                if self.queued_scan is not None:
                    log(f"Clearing queued scan '{self.queued_scan}' on Part 1 finish: Part 2 requires explicit user keypress")
                    self.queued_scan = None

            log(f"Part 1 saved to {out_path} ({self.get_summary_text()})")
            self.update_ui_state("Part 1 Done — Reposition page & Press F7 for Part 2")

        except Exception as e:
            if "cancelled by user" in str(e).lower() or "escape" in str(e).lower():
                log("Part 1 scan cancelled cleanly via Escape")
                with self.scan_lock:
                    self.is_scanning = False
                    self.part1_path = None
                    self.queued_scan = None
                self.update_ui_state("Scan cancelled by user [Escape]")
                return
            log(f"_worker_scan_part1 error: {e}")
            threading.Thread(target=self._auto_reconnect_on_error, daemon=True).start()
            with self.scan_lock:
                self.is_scanning = False
                self.part1_path = None
                self.queued_scan = None
            self.update_ui_state(f"Scan Error: {e}")

    def _worker_scan_part2_and_stitch(self):
        try:
            curr_page = self.session_page_num
            filename = f"Scan_{self.session_id}_P{curr_page:02d}_Part2.png"
            part2_path = os.path.join(RAW_DIR, filename)

            # 1. Hardware scan Part 2
            cmd_scan = [
                PYTHON_EXE, WORKER_SCRIPT, "scan-part",
                "--out", part2_path,
                "--dpi", str(self.config.get("dpi", 300)),
                "--color-mode", self.config.get("color_mode", "color"),
                "--source", "flatbed",
                "--page-size", self.config.get("page_size", "a4")
            ]
            data_scan, stderr_scan = self._run_scan_subprocess(cmd_scan, timeout=60)

            if not data_scan or not data_scan.get("success"):
                err = data_scan.get("error") if data_scan else stderr_scan
                raise RuntimeError(f"Scan Part 2 failed: {err}")

            # 2. Hardware scan finished! Carriage is immediately free
            # Atomic state reset: clear part1_path = None, is_scanning = False
            with self.scan_lock:
                part1_file = self.part1_path
                self.part1_path = None
                self.session_page_num += 1
                self.is_scanning = False
                queued_action = self.queued_scan
                self.queued_scan = None

            # 3. Output stitched PDF destination
            stitched_filename = f"Stitched_{self.session_id}_P{curr_page:02d}.pdf"
            stitched_path = os.path.join(STITCHED_DIR, stitched_filename)

            # 4. Queue SIFT stitching job to background worker
            job = {
                "type": "dual_scan",
                "part1": part1_file,
                "part2": part2_path,
                "out_pdf": stitched_path,
                "placeholder_ref": None,
                "dpi": self.config.get("dpi", 300),
                "page_num": curr_page
            }
            self.pending_stitches += 1
            self.stitch_queue.put(job)
            log(f"Enqueued background SIFT stitch for Page {curr_page} (pending: {self.pending_stitches})")

            # 5. Update UI status to "Stitching in background..."
            self.update_ui_state("Stitching in background...")

            # 6. Trigger queued scan for next page (starts Part 1 of next page)
            if queued_action:
                log(f"Triggering queued scan action after Part 2: {queued_action}")
                if queued_action == "f7":
                    idle_add_safe(self.on_f7_clicked)
                elif queued_action == "f8":
                    idle_add_safe(self.on_f8_clicked)

        except Exception as e:
            if "cancelled by user" in str(e).lower() or "escape" in str(e).lower():
                log("Part 2 scan cancelled cleanly via Escape")
                with self.scan_lock:
                    self.is_scanning = False
                    self.part1_path = None
                    self.queued_scan = None
                self.update_ui_state("Scan cancelled by user [Escape]")
                return
            log(f"_worker_scan_part2_and_stitch error: {e}")
            threading.Thread(target=self._auto_reconnect_on_error, daemon=True).start()
            with self.scan_lock:
                self.is_scanning = False
                self.part1_path = None
                self.queued_scan = None
            self.update_ui_state(f"Scan Part 2 Error: {e}")

    # -------------------------------------------------------------------------
    # Single-Pass (F8) & Feeder Stack (ADF) Scanning Workflow
    # -------------------------------------------------------------------------
    def on_f8_clicked(self):
        log(">>> [ACTION] Scan Single / Feeder Batch (F8) activated")
        with self.scan_lock:
            if self.is_scanning:
                log("F8 clicked during carriage movement -> Queued next scan action")
                self.queued_scan = "f8"
                self.update_ui_state()
                return

            if self.part1_path and os.path.exists(self.part1_path):
                self.is_scanning = True
                self.update_ui_state("Converting Part 1 to Single Page...")
                threading.Thread(target=self._worker_finalize_part1_as_single, daemon=True).start()
                return

            # If input source is Feeder/ADF, automatically perform multi-page feeder batch
            if self.config.get("source") == "feeder":
                self.is_scanning = True
                self.cancel_requested = False
                self.update_ui_state("ADF: Initializing feeder stack scan...")
                threading.Thread(target=self._worker_scan_feeder_batch, daemon=True).start()
                return

            src_desc = "Flatbed"
            self.is_scanning = True
            self.update_ui_state(f"Scanning Single Page [{src_desc}]...")
            threading.Thread(target=self._worker_scan_single, daemon=True).start()

    def on_feeder_batch_clicked(self):
        log(">>> [ACTION] Scan Feeder Stack (ADF Batch) activated")
        with self.scan_lock:
            if self.is_scanning:
                log("Feeder batch scan ignored: scanner is currently busy")
                return

            if self.part1_path is not None:
                self.part1_path = None

            self.config["source"] = "feeder"
            self.save_config()
            self.is_scanning = True
            self.cancel_requested = False
            self.update_ui_state("ADF: Initializing feeder stack scan...")
            self.update_settings_ui()
            threading.Thread(target=self._worker_scan_feeder_batch, daemon=True).start()

    def _worker_scan_feeder_batch(self):
        try:
            batch_ts = time.strftime("%Y%m%d_%H%M%S")
            start_page = self.session_page_num
            prefix = f"ADF_{batch_ts}"
            out_dir = STITCHED_DIR
            scanned_count = [0]

            cmd = [
                PYTHON_EXE, WORKER_SCRIPT, "scan-feeder-batch",
                "--out-dir", out_dir,
                "--prefix", prefix,
                "--dpi", str(self.config.get("dpi", 300)),
                "--color-mode", self.config.get("color_mode", "color"),
                "--page-size", self.config.get("page_size", "a4")
            ]

            def on_page_ready(page_info):
                page_path = page_info.get("path")
                page_num = page_info.get("page", 1)
                scanned_count[0] += 1
                with self.scan_lock:
                    self.session_page_num = max(self.session_page_num, start_page + scanned_count[0])

                # Immediately auto-save and inject page into PDF Arranger
                self._autosave_page(page_path)
                self.inject_page_into_pdfarranger(page_path, f"Page {page_num} (Feeder Stack)")
                self.update_ui_state(f"ADF: Scanned Page {page_num}... feeding next page...")
                try:
                    wia_scanner.play_sound("part1", wait=False)
                except Exception:
                    pass

            data, stderr = self._run_streaming_scan_subprocess(cmd, on_page_ready=on_page_ready, timeout=300)

            with self.scan_lock:
                self.is_scanning = False

            if self.cancel_requested:
                self.cancel_requested = False
                msg = f"ADF Batch cancelled [Escape] — Kept {scanned_count[0]} page(s) in document"
                log(msg)
                self.update_ui_state(msg)
                return

            if not data or not data.get("success"):
                err = data.get("error") if data else stderr
                if scanned_count[0] == 0:
                    if any(w in str(err).lower() for w in ["empty", "paper", "tray"]):
                        err = "Automatic Document Feeder (ADF) is empty. Please place pages in the feeder."
                    raise RuntimeError(f"ADF batch scan failed: {err}")
                else:
                    log(f"ADF scan loop ended: {err}")

            total_pages = data.get("pages", scanned_count[0]) if data else scanned_count[0]
            log(f"ADF Feeder batch completed successfully: {total_pages} page(s) scanned and injected")
            self.update_ui_state(f"ADF Feeder Complete! {total_pages} pages added to document.")
            try:
                wia_scanner.play_sound("finish_doc", wait=False)
            except Exception:
                pass

        except Exception as e:
            if "cancelled" in str(e).lower() or "escape" in str(e).lower():
                log("ADF batch scan cancelled cleanly via Escape")
                with self.scan_lock:
                    self.is_scanning = False
                self.update_ui_state("ADF scan cancelled [Escape]")
                return
            log(f"_worker_scan_feeder_batch error: {e}")
            threading.Thread(target=self._auto_reconnect_on_error, daemon=True).start()
            with self.scan_lock:
                self.is_scanning = False
            self.update_ui_state(f"ADF Scan Error: {e}")

    def _worker_finalize_part1_as_single(self):
        try:
            with self.scan_lock:
                curr_page = self.session_page_num
                target_path = self.part1_path
                self.part1_path = None

            out_pdf = os.path.join(STITCHED_DIR, f"Single_{self.session_id}_P{curr_page:02d}.pdf")
            try:
                from PIL import Image
                dpi_val = int(self.config.get("dpi", 300))
                if target_path and os.path.exists(target_path):
                    with Image.open(target_path) as im:
                        im.convert("RGB").save(out_pdf, "PDF", resolution=float(dpi_val), quality=88)
                else:
                    out_pdf = target_path
            except Exception as e_pil:
                log(f"Error converting Part 1 to Single PDF via PIL: {e_pil}")
                out_pdf = target_path

            with self.scan_lock:
                self.session_page_num += 1
                self.is_scanning = False
                queued_action = self.queued_scan
                self.queued_scan = None

            self.inject_page_into_pdfarranger(out_pdf, f"Page {curr_page} (Single)")
            self.update_ui_state()
            if queued_action:
                log(f"Triggering queued scan action after finalizing Part 1: {queued_action}")
                if queued_action == "f7":
                    idle_add_safe(self.on_f7_clicked)
                elif queued_action == "f8":
                    idle_add_safe(self.on_f8_clicked)

        except Exception as e:
            log(f"_worker_finalize_part1_as_single error: {e}")
            with self.scan_lock:
                self.is_scanning = False
                self.part1_path = None
                self.queued_scan = None
            self.update_ui_state(f"Error: {e}")

    def _worker_scan_single(self):
        try:
            curr_page = self.session_page_num
            filename = f"Single_{self.session_id}_P{curr_page:02d}.pdf"
            out_path = os.path.join(STITCHED_DIR, filename)

            cmd = [
                PYTHON_EXE, WORKER_SCRIPT, "scan-single",
                "--out", out_path,
                "--dpi", str(self.config.get("dpi", 300)),
                "--color-mode", self.config.get("color_mode", "color"),
                "--source", self.config.get("source", "flatbed"),
                "--page-size", self.config.get("page_size", "a4")
            ]
            data, stderr = self._run_scan_subprocess(cmd, timeout=60)

            if not data or not data.get("success"):
                err = data.get("error") if data else stderr
                if self.config.get("source") == "feeder" and any(w in str(err).lower() for w in ["empty", "paper", "tray"]):
                    err = "Document Feeder (ADF) is empty. Please place pages in the feeder."
                raise RuntimeError(f"Single pass scan failed: {err}")

            with self.scan_lock:
                self.session_page_num += 1
                self.is_scanning = False
                queued_action = self.queued_scan
                self.queued_scan = None

            self._autosave_page(out_path)
            self.inject_page_into_pdfarranger(out_path, f"Page {curr_page} (Single)")
            self.update_ui_state()
            if queued_action:
                log(f"Triggering queued scan action after Single scan: {queued_action}")
                if queued_action == "f7":
                    idle_add_safe(self.on_f7_clicked)
                elif queued_action == "f8":
                    idle_add_safe(self.on_f8_clicked)

        except Exception as e:
            if "cancelled by user" in str(e).lower() or "escape" in str(e).lower():
                log("Single scan cancelled cleanly via Escape")
                with self.scan_lock:
                    self.is_scanning = False
                    self.queued_scan = None
                self.update_ui_state("Scan cancelled by user [Escape]")
                return
            log(f"_worker_scan_single error: {e}")
            threading.Thread(target=self._auto_reconnect_on_error, daemon=True).start()
            with self.scan_lock:
                self.is_scanning = False
                self.queued_scan = None
            self.update_ui_state(f"Scan Error: {e}")

    # -------------------------------------------------------------------------
    # Value-Added Feature: Manual SIFT Stitching of 2 Selected Pages (F9)
    # -------------------------------------------------------------------------
    def on_stitch_clicked(self, callback=None):
        log(">>> [ACTION] Stitch 2 Selected Pages (F9) activated")
        selection = self.app.iconview.get_selected_items() if (hasattr(self.app, "iconview") and self.app.iconview) else []
        if len(selection) != 2:
            self.update_ui_state("Please select exactly 2 pages to stitch (Ctrl+Click to select 2)")
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass
            if callback:
                callback(False)
            return

        try:
            import gi
            from gi.repository import Gtk
            has_gtk = True
        except (ImportError, ModuleNotFoundError):
            has_gtk = False

        selection.sort(key=lambda x: x.get_indices()[0] if hasattr(x, "get_indices") else x)
        path1, path2 = selection[0], selection[1]
        idx1 = path1.get_indices()[0] if hasattr(path1, "get_indices") else path1
        idx2 = path2.get_indices()[0] if hasattr(path2, "get_indices") else path2

        if has_gtk:
            ref1 = Gtk.TreeRowReference.new(self.app.model, path1)
            ref2 = Gtk.TreeRowReference.new(self.app.model, path2)
        else:
            ref1 = None
            ref2 = None

        def _get_page_obj(idx):
            if idx < len(self.app.model):
                item = self.app.model[idx]
                if isinstance(item, (list, tuple)) and len(item) > 0:
                    row_obj = item[0]
                else:
                    row_obj = item
                if hasattr(row_obj, "duplicate"):
                    return row_obj.duplicate()
                return row_obj
            return None

        p1 = _get_page_obj(idx1)
        p2 = _get_page_obj(idx2)

        self.update_ui_state(f"Preparing pages {idx1+1} & {idx2+1} for background stitching...")

        threading.Thread(
            target=self._worker_prep_manual_stitch,
            args=(p1, p2, ref1, ref2, idx1, idx2, callback),
            daemon=True
        ).start()

    def _worker_prep_manual_stitch(self, p1, p2, ref1, ref2, idx1, idx2, callback=None):
        try:
            timestamp = int(time.time())
            tmp_p1 = os.path.join(RAW_DIR, f"Manual_P{idx1+1}_{timestamp}.png")
            tmp_p2 = os.path.join(RAW_DIR, f"Manual_P{idx2+1}_{timestamp}.png")
            stitched_out = os.path.join(STITCHED_DIR, f"Manual_Stitched_{idx1+1}_{idx2+1}_{timestamp}.pdf")

            log(f"Rendering Page {idx1+1} to {tmp_p1} at 300 DPI...")
            render_page_to_png(self.app, p1, tmp_p1, dpi=300)

            log(f"Rendering Page {idx2+1} to {tmp_p2} at 300 DPI...")
            render_page_to_png(self.app, p2, tmp_p2, dpi=300)

            job = {
                "type": "manual_stitch",
                "tmp_p1": tmp_p1,
                "tmp_p2": tmp_p2,
                "stitched_out": stitched_out,
                "ref1": ref1,
                "ref2": ref2,
                "idx1": idx1,
                "idx2": idx2,
                "callback": callback,
                "dpi": self.config.get("dpi", 300)
            }
            self.pending_stitches += 1
            self.stitch_queue.put(job)
            log(f"Enqueued manual stitch job for pages {idx1+1} & {idx2+1} (pending: {self.pending_stitches})")
            self.update_ui_state()

        except Exception as e:
            log(f"_worker_prep_manual_stitch error: {e}\n{traceback.format_exc()}")
            self.update_ui_state(f"Manual Stitch Prep Error: {e}")
            if callback:
                callback(False)

    # -------------------------------------------------------------------------
    # Background Stitch Worker Loop & Execution
    # -------------------------------------------------------------------------
    def _stitch_worker_loop(self):
        log("Started background stitch worker thread")
        while getattr(self, "_running", True):
            try:
                try:
                    job = self.stitch_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if job is None or not getattr(self, "_running", True):
                    break

                self.update_ui_state()

                job_type = job.get("type")
                if job_type == "dual_scan":
                    self._execute_dual_stitch(job)
                elif job_type == "manual_stitch":
                    self._execute_manual_stitch(job)

                self.pending_stitches = max(0, self.pending_stitches - 1)
                self.stitch_queue.task_done()
                self.update_ui_state()

            except Exception as e:
                log(f"_stitch_worker_loop error: {e}\n{traceback.format_exc()}")
                self.pending_stitches = max(0, self.pending_stitches - 1)
                self.update_ui_state()

    def _execute_dual_stitch(self, job):
        try:
            part1 = job["part1"]
            part2 = job["part2"]
            out_pdf = job["out_pdf"]
            dpi = job.get("dpi", 300)
            placeholder_ref = job.get("placeholder_ref")
            page_num = job.get("page_num", 1)

            log(f"Background stitch worker running for Page {page_num}...")
            cmd_stitch = [
                PYTHON_EXE, WORKER_SCRIPT, "stitch",
                "--part1", part1,
                "--part2", part2,
                "--out", out_pdf,
                "--dpi", str(dpi)
            ]
            res_stitch = subprocess.run(
                cmd_stitch,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=60,
                env=dict(os.environ)
            )
            data_stitch = self.parse_worker_output(res_stitch.stdout)
            if not data_stitch or not data_stitch.get("success"):
                try:
                    from PIL import Image
                    if part1 and os.path.exists(part1):
                        with Image.open(part1) as im:
                            im.convert("RGB").save(out_pdf, "PDF", resolution=float(dpi), quality=88)
                except Exception as e_fb:
                    log(f"Fallback single page save failed: {e_fb}")

            # Auto-save stitched page to recovery document immediately
            self._autosave_page(out_pdf)

            # Cleanly inject stitched page into PDF Arranger (no TreeRow deletion / no memory crash)
            self.inject_page_into_pdfarranger(out_pdf, f"Page {page_num} (Stitched)")
            log(f"Page {page_num} stitched and added to PDF Arranger: {out_pdf}")

        except Exception as e:
            log(f"_execute_dual_stitch error: {e}\n{traceback.format_exc()}")

    def _execute_manual_stitch(self, job):
        try:
            tmp_p1 = job["tmp_p1"]
            tmp_p2 = job["tmp_p2"]
            stitched_out = job["stitched_out"]
            ref1 = job["ref1"]
            ref2 = job["ref2"]
            idx1 = job["idx1"]
            idx2 = job["idx2"]
            callback = job.get("callback")
            dpi = job.get("dpi", 300)

            log(f"Executing background SIFT planar alignment for manual stitch (P{idx1+1} & P{idx2+1})...")
            cmd_stitch = [
                PYTHON_EXE, WORKER_SCRIPT, "stitch",
                "--part1", tmp_p1,
                "--part2", tmp_p2,
                "--out", stitched_out,
                "--dpi", str(dpi)
            ]
            res_stitch = subprocess.run(
                cmd_stitch,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=60,
                env=dict(os.environ)
            )
            data_stitch = self.parse_worker_output(res_stitch.stdout)
            if not data_stitch or not data_stitch.get("success"):
                err = data_stitch.get("error") if data_stitch else res_stitch.stderr
                raise RuntimeError(f"Manual SIFT stitch failed: {err}")

            def _commit_stitch():
                try:
                    from pdfarranger import core
                    from gi.repository import GObject

                    self.app.undomanager.commit("Stitch pages")

                    adder = core.PageAdder(self.app)
                    adder.move(ref1, True)
                    adder.addpages(stitched_out)
                    adder.commit(select_added=True, add_to_undomanager=False)

                    with self.app.render_lock():
                        if ref1.valid():
                            iter1 = self.app.model.get_iter(ref1.get_path())
                            self.app.model.remove(iter1)
                        if ref2.valid():
                            iter2 = self.app.model.get_iter(ref2.get_path())
                            self.app.model.remove(iter2)

                    self.app.set_unsaved(True)
                    self.app.update_iconview_geometry()
                    self.app.update_max_zoom_level()
                    GObject.idle_add(self.app.render)

                    self.update_ui_state(f"Stitched pages {idx1+1} & {idx2+1} into 1 page!")
                    log(f"Manual stitch successfully replaced pages {idx1+1} & {idx2+1} with {stitched_out}")
                    if callback:
                        callback(True)
                except Exception as ex:
                    log(f"Error in _commit_stitch: {ex}\n{traceback.format_exc()}")
                    if callback:
                        callback(False)
                finally:
                    # Clean up temporary manual stitch bitmap buffers to prevent disk bloat
                    for tmp_f in (tmp_p1, tmp_p2):
                        if tmp_f and os.path.exists(tmp_f):
                            try:
                                os.remove(tmp_f)
                                log(f"Cleaned up temporary manual stitch buffer: {tmp_f}")
                            except Exception as ex_del:
                                log(f"Could not remove temp manual buffer {tmp_f}: {ex_del}")
                return False

            idle_add_safe(_commit_stitch)

        except Exception as e:
            log(f"_execute_manual_stitch error: {e}\n{traceback.format_exc()}")
            self.update_ui_state(f"Stitch Error: {e}")
            callback = job.get("callback")
            if callback:
                callback(False)

    # -------------------------------------------------------------------------
    # Value-Added Feature: Document Fast-Load & Compression Optimization
    # -------------------------------------------------------------------------
    def on_optimize_clicked(self):
        log(">>> [ACTION] Optimize Document activated")
        if self.is_optimizing:
            log("Optimize ignored: optimization already running")
            return

        if len(self.app.model) == 0:
            self.update_ui_state("No pages in document to optimize")
            return

        self.is_optimizing = True
        self.update_ui_state("⚡ Optimizing document (compressing scans & normalizing dimensions)...")
        threading.Thread(target=self._worker_optimize_document, daemon=True).start()

    def _worker_optimize_document(self):
        from gi.repository import GLib

        try:
            timestamp = int(time.time())
            tmp_export = os.path.join(self.app.tmp_dir, f"export_before_opt_{timestamp}.pdf")
            tmp_opt = os.path.join(self.app.tmp_dir, f"optimized_{timestamp}.pdf")

            # 1. Export current pages in document to temporary PDF
            files = [(pdf.copyname, pdf.password) for pdf in self.app.pdfqueue]
            pages = [row[0].duplicate(incl_thumbnail=False) for row in self.app.model]

            from pdfarranger import exporter
            quit_flag = threading.Event()
            exporter.export(files, pages, self.app.metadata, [tmp_export], self.app.config, quit_flag)

            # 2. Run worker to normalize and compress scan streams
            cmd = [
                PYTHON_EXE, WORKER_SCRIPT, "optimize-doc",
                "--in", tmp_export,
                "--out", tmp_opt,
                "--dpi", str(self.config.get("dpi", 300))
            ]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=120,
                env=dict(os.environ)
            )
            data = self.parse_worker_output(res.stdout)
            if not data or not data.get("success"):
                err = data.get("error") if data else res.stderr
                raise RuntimeError(f"Optimization failed: {err}")

            new_sz_mb = data.get("size_bytes", 0) / (1024 * 1024)

            def _commit_opt():
                try:
                    from pdfarranger import core
                    from gi.repository import GObject

                    self.app.undomanager.commit("Optimize document")
                    with self.app.render_lock():
                        self.app.model.clear()

                    adder = core.PageAdder(self.app)
                    adder.addpages(tmp_opt)
                    adder.commit(select_added=False, add_to_undomanager=False)

                    self.app.set_unsaved(True)
                    self.app.update_iconview_geometry()
                    self.app.update_max_zoom_level()
                    GObject.idle_add(self.app.render)

                    self.is_optimizing = False
                    self.update_ui_state(f"⚡ Document optimized ({new_sz_mb:.1f} MB)! Instant fast-loading active.")
                    log(f"Document optimized to {tmp_opt} ({new_sz_mb:.2f} MB)")
                except Exception as ex:
                    log(f"_commit_opt error: {ex}\n{traceback.format_exc()}")
                    self.is_optimizing = False
                    self.update_ui_state(f"Error applying optimization: {ex}")

                return False

            idle_add_safe(_commit_opt)

        except Exception as e:
            log(f"_worker_optimize_document error: {e}\n{traceback.format_exc()}")
            self.is_optimizing = False
            self.update_ui_state(f"Optimization error: {e}")

    # -------------------------------------------------------------------------
    # Value-Added Feature: Manual Crop Adjustment & Margin Reset
    # -------------------------------------------------------------------------
    def on_adjust_crop_clicked(self, widget=None):
        """
        Manually adjust page crop / margins:
        - Targets currently selected page in iconview, OR if none selected, targets
          the last page in app.model (the newly scanned page).
        - Invokes PDF Arranger's native crop dialog (app.crop_dialog) so the user
          can interactively fine-tune page crop margins with live preview.
        """
        log(">>> [ACTION] Adjust Page Crop / Margins (C) activated")
        if self.is_scanning:
            log("Crop dialog ignored: scanning in progress")
            self.update_ui_state("Wait for scanning to finish before adjusting crop")
            return
        if not hasattr(self.app, "model") or len(self.app.model) == 0:
            log("on_adjust_crop_clicked: No pages in document to crop.")
            self.update_ui_state("No pages in document to crop")
            return

        has_iconview = hasattr(self.app, "iconview") and self.app.iconview
        selection = self.app.iconview.get_selected_items() if has_iconview else []

        if not selection:
            last_idx = len(self.app.model) - 1
            if last_idx >= 0 and has_iconview:
                try:
                    from gi.repository import Gtk
                    path = Gtk.TreePath.new_from_indices([last_idx])
                except Exception:
                    path = [last_idx]

                if hasattr(self.app.iconview, "unselect_all"):
                    try:
                        self.app.iconview.unselect_all()
                    except Exception:
                        pass

                if hasattr(self.app.iconview, "select_path"):
                    try:
                        self.app.iconview.select_path(path)
                    except Exception as ex_sel:
                        log(f"Auto-select page note: {ex_sel}")
                elif hasattr(self.app.iconview, "selected_items"):
                    self.app.iconview.selected_items.append(path)

                try:
                    if hasattr(self.app.iconview, "scroll_to_path"):
                        self.app.iconview.scroll_to_path(path, False, 0.0, 0.0)
                except Exception:
                    pass
                log(f"Auto-selected last page index {last_idx} for crop adjustment")

        # Invoke PDF Arranger's native crop dialog
        try:
            if hasattr(self.app, "crop_dialog") and callable(self.app.crop_dialog):
                try:
                    self.app.crop_dialog(None, None, None)
                except TypeError:
                    try:
                        self.app.crop_dialog(None, None)
                    except TypeError:
                        self.app.crop_dialog()
            elif hasattr(self.app, "action_crop") and callable(self.app.action_crop):
                self.app.action_crop()
            else:
                log("on_adjust_crop_clicked: app.crop_dialog not found on app")
                self.update_ui_state("Native crop dialog unavailable")
        except Exception as e:
            log(f"crop_dialog invocation error: {e}\n{traceback.format_exc()}")
            self.update_ui_state(f"Crop Error: {e}")

    on_crop_clicked = on_adjust_crop_clicked

    def reset_crop_selected(self, widget=None):
        """
        Resets crop margins to zero Sides(0, 0, 0, 0) on all selected pages
        (or the last page if none selected), restoring the complete uncropped scan.
        """
        log(">>> [ACTION] Reset / Remove Crop activated")
        if not hasattr(self.app, "model") or len(self.app.model) == 0:
            self.update_ui_state("No pages available to reset crop")
            return

        try:
            from pdfarranger.core import Sides
        except ImportError:
            from collections import namedtuple
            Sides = namedtuple("Sides", ["left", "right", "top", "bottom"])

        zero_crop = Sides(0, 0, 0, 0)

        has_iconview = hasattr(self.app, "iconview") and self.app.iconview
        selection = self.app.iconview.get_selected_items() if has_iconview else []

        indices = []
        if selection:
            for path in selection:
                indices.append(path.get_indices()[0])
        else:
            indices.append(len(self.app.model) - 1)

        modified = 0

        lock_ctx = self.app.render_lock() if hasattr(self.app, "render_lock") else None
        if lock_ctx:
            with lock_ctx:
                for idx in indices:
                    if 0 <= idx < len(self.app.model):
                        page = self.app.model[idx][0]
                        if hasattr(page, "crop"):
                            page.crop = zero_crop
                            modified += 1
        else:
            for idx in indices:
                if 0 <= idx < len(self.app.model):
                    page = self.app.model[idx][0]
                    if hasattr(page, "crop"):
                        page.crop = zero_crop
                        modified += 1

        if modified > 0:
            if hasattr(self.app, "undomanager") and self.app.undomanager:
                try:
                    self.app.undomanager.commit("Reset crop")
                except Exception as e_undo:
                    log(f"Undo commit note: {e_undo}")

            if hasattr(self.app, "set_unsaved"):
                self.app.set_unsaved(True)
            if hasattr(self.app, "update_iconview_geometry"):
                self.app.update_iconview_geometry()
            if hasattr(self.app, "update_max_zoom_level"):
                self.app.update_max_zoom_level()
            if hasattr(self.app, "render"):
                idle_add_safe(self.app.render)

            msg = f"Reset crop margins on {modified} page(s) — full scan restored"
            log(msg)
            self.update_ui_state(msg)

    reset_crop = reset_crop_selected

    # -------------------------------------------------------------------------
    # Disk Space & Temporary Files Cache Management
    # -------------------------------------------------------------------------
    def clean_temp_cache(self):
        """
        Safely cleans up temporary cache files:
        - Temporary manual stitch render files (Manual_P*.png, Manual_Stitched_*.png)
        - Temporary .tmp, .tmp.bmp, test_* mock artifacts
        - Orphaned placeholder PDFs (Placeholder_*.pdf)
        - Never touches Processed/*, authentic scans, or the active session's raw scans.
        Returns: (files_removed, bytes_freed)
        """
        freed_bytes = 0
        freed_count = 0
        active_sess = getattr(self, "session_id", None)

        # 1. Clean Scans/Raw
        if os.path.exists(RAW_DIR):
            for f in os.listdir(RAW_DIR):
                fp = os.path.join(RAW_DIR, f)
                if not os.path.isfile(fp):
                    continue
                # Never touch files belonging to the active session
                if active_sess and active_sess in f:
                    continue
                # Purge temporary test/manual render artifacts
                if (f.startswith("Manual_") or f.startswith("test_") or
                        f.endswith(".tmp") or f.endswith(".tmp.bmp") or f == "t.png" or
                        f.startswith("dummy_") or "mock" in f.lower()):
                    try:
                        sz = os.path.getsize(fp)
                        os.remove(fp)
                        freed_count += 1
                        freed_bytes += sz
                        log(f"[clean_temp_cache] Removed temporary raw artifact: {f}")
                    except Exception as ex:
                        log(f"[clean_temp_cache] Could not remove {f}: {ex}")

        # 2. Clean Scans/Stitched
        if os.path.exists(STITCHED_DIR):
            for f in os.listdir(STITCHED_DIR):
                fp = os.path.join(STITCHED_DIR, f)
                if not os.path.isfile(fp):
                    continue
                if active_sess and active_sess in f:
                    continue
                # Purge placeholders, test files, manual temp bitmaps
                if (f.startswith("Placeholder_") or f.startswith("test_") or
                        f.startswith("Manual_Stitched_") or "test" in f.lower() or
                        f.endswith(".tmp")):
                    try:
                        sz = os.path.getsize(fp)
                        os.remove(fp)
                        freed_count += 1
                        freed_bytes += sz
                        log(f"[clean_temp_cache] Removed temporary stitched artifact: {f}")
                    except Exception as ex:
                        log(f"[clean_temp_cache] Could not remove {f}: {ex}")

        # 3. Clean app.tmp_dir if exists
        tmp_dir = getattr(getattr(self, "app", None), "tmp_dir", None)
        if tmp_dir and os.path.exists(tmp_dir):
            for f in os.listdir(tmp_dir):
                fp = os.path.join(tmp_dir, f)
                if os.path.isfile(fp) and (f.startswith("export_") or f.startswith("optimized_") or f.endswith(".tmp")):
                    try:
                        sz = os.path.getsize(fp)
                        os.remove(fp)
                        freed_count += 1
                        freed_bytes += sz
                    except Exception:
                        pass

        log(f"[clean_temp_cache] Cleanup finished: removed {freed_count} files, freed {freed_bytes / (1024*1024):.2f} MB")
        return freed_count, freed_bytes

    def on_clean_temp_cache_clicked(self, widget=None):
        log(">>> [ACTION] Clean Temp Cache activated")
        if getattr(self, "is_cleaning_cache", False):
            self.update_ui_state("Cache cleanup is already running...")
            return

        self.is_cleaning_cache = True
        self.update_ui_state("🧹 Cleaning temporary cache & recovering disk space...")

        def _worker_clean():
            try:
                count, b_freed = self.clean_temp_cache()
                mb_freed = b_freed / (1024 * 1024)
                if count > 0:
                    msg = f"🧹 Cache Cleaned: Freed {mb_freed:.1f} MB ({count} temp files removed)"
                else:
                    msg = "🧹 Cache is already clean! (0 temp files found)"
                log(msg)
                self.update_ui_state(msg)
            except Exception as e:
                log(f"Cache cleanup error: {e}\n{traceback.format_exc()}")
                self.update_ui_state(f"Cache cleanup error: {e}")
            finally:
                self.is_cleaning_cache = False

        threading.Thread(target=_worker_clean, daemon=True).start()

    # -------------------------------------------------------------------------
    # PDF Arranger Live Page Injection & Placeholder Management
    # -------------------------------------------------------------------------
    def inject_page_into_pdfarranger(self, image_path, page_desc=""):
        def _inject():
            try:
                from pdfarranger import core

                adder = core.PageAdder(self.app)
                adder.addpages(image_path)
                if len(adder.pages) > 0:
                    adder.commit(select_added=True, add_to_undomanager=True)
                    log(f"Successfully committed page '{image_path}' into PDF Arranger")
                    self.update_ui_state(f"Added {page_desc} to document!")
                else:
                    log(f"No pages were added for '{image_path}'")
                    self.update_ui_state("Failed to add page to document")

            except Exception as e:
                log(f"inject_page_into_pdfarranger error: {e}\n{traceback.format_exc()}")
                self.update_ui_state(f"Error adding page: {e}")

            return False

        idle_add_safe(_inject)

    def _inject_placeholder_and_get_ref(self, placeholder_path):
        try:
            from pdfarranger import core
            from gi.repository import Gtk, GObject

            adder = core.PageAdder(self.app)
            adder.addpages(placeholder_path)
            adder.commit(select_added=True, add_to_undomanager=False)

            last_idx = len(self.app.model) - 1
            if last_idx >= 0:
                path = Gtk.TreePath.new_from_indices([last_idx])
                ref = Gtk.TreeRowReference.new(self.app.model, path)
                return ref
        except Exception as e:
            log(f"_inject_placeholder_and_get_ref error: {e}")
        return None


def install_hook():
    """
    Installs the scanner integration into PDF Arranger.
    Called when pdfarranger package is imported.
    """
    try:
        log(f"install_hook invoked in {sys.executable} (PID: {os.getpid()})")
        from pdfarranger import pdfarranger
        from gi.repository import GLib

        orig_init = pdfarranger.PdfArranger.__init__

        def hooked_init(self, *args, **kwargs):
            orig_init(self, *args, **kwargs)

            def on_activate(app):
                def init_scanner():
                    try:
                        app._scanner_manager = ScannerManager(app)
                    except Exception as e:
                        log(f"Error initializing ScannerManager: {e}\n{traceback.format_exc()}")
                    return False

                GLib.idle_add(init_scanner)

            self.connect("activate", on_activate)

        pdfarranger.PdfArranger.__init__ = hooked_init
        log("PDF Arranger Scanner Hook installed successfully")

    except Exception as e:
        log(f"Failed to install PDF Arranger scanner hook: {e}\n{traceback.format_exc()}")
