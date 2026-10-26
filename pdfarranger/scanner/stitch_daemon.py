import os
import sys
import time
import glob
import re
import math
import threading
from pathlib import Path
from datetime import datetime
import cv2
import numpy as np
try:
    import pymupdf as fitz
except ImportError:
    import fitz

from wia_scanner import scan_to_file, play_sound, get_connected_scanner_name
try:
    from app_logger import log_info, log_error, log_exception
except ImportError:
    def log_info(msg): print(f"[INFO] {msg}")
    def log_error(msg): print(f"[ERROR] {msg}")
    def log_exception(context=""): print(f"[EXCEPTION] {context}")

try:
    import keyboard
    KEYBOARD_AVAILABLE = True
except ImportError:
    KEYBOARD_AVAILABLE = False

# Automatically resolve paths relative to this script directory
BASE_DIR = Path(__file__).resolve().parent
RAW_DIR = os.getenv("SCAN_RAW_DIR", str(BASE_DIR / "Raw"))
PROCESSED_DIR = os.getenv("SCAN_PROCESSED_DIR", str(BASE_DIR / "Processed"))
ARCHIVE_DIR = os.getenv("SCAN_ARCHIVE_DIR", str(BASE_DIR / "Archive"))

# Shortcut keys
SCAN_HOTKEY = os.getenv("SCAN_HOTKEY", "F7")
FINISH_HOTKEY = os.getenv("SCAN_FINISH_HOTKEY", "F6")
SINGLE_HOTKEY = os.getenv("SCAN_SINGLE_HOTKEY", "F8")

scan_lock = threading.Lock()


try:
    from session_manager import load_session, save_session, get_next_default_doc_name
except ImportError:
    def load_session():
        return {"doc_name": "Doc_001", "page_number": 1, "part_number": 1, "pages_stitched": 0}
    def save_session(state):
        pass
    def get_next_default_doc_name(processed_dir=None):
        return "Doc_001"


def update_window_title(doc_name, page_num, part_num=1):
    """Sets the Windows command prompt title bar dynamically."""
    try:
        import ctypes
        title = f"Scan Stitcher | Active: [{doc_name}.pdf] | Next: Page {page_num} (Part {part_num})"
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass


class DocumentSession:
    """Manages multi-page document naming, page numbers, single-pass and dual-pass parts."""

    def __init__(self, processed_dir):
        self.processed_dir = processed_dir
        state = load_session()
        self.doc_name = state.get("doc_name", get_next_default_doc_name(self.processed_dir))
        self.page_number = state.get("page_number", 1)
        self.part_number = state.get("part_number", 1)
        self.pages_stitched = state.get("pages_stitched", 0)
        self.pending_part1_file = None
        self.lock = threading.Lock()
        update_window_title(self.doc_name, self.page_number, self.part_number)

    def sync_to_file(self):
        state = {
            "doc_name": self.doc_name,
            "page_number": self.page_number,
            "part_number": self.part_number,
            "pages_stitched": self.pages_stitched,
        }
        save_session(state)
        update_window_title(self.doc_name, self.page_number, self.part_number)

    def set_custom_name(self, name):
        with self.lock:
            name = name.strip()
            if not name:
                return
            if name.lower().endswith(".pdf"):
                name = name[:-4]
            clean_name = "".join(c for c in name if c.isalnum() or c in (" ", "_", "-")).strip()
            if clean_name:
                self.doc_name = clean_name
                # Check if this PDF already exists in Processed to resume
                pdf_path = os.path.join(self.processed_dir, f"{self.doc_name}.pdf")
                if os.path.exists(pdf_path):
                    try:
                        doc = fitz.open(pdf_path)
                        existing_pages = len(doc)
                        doc.close()
                        self.page_number = existing_pages + 1
                        self.pages_stitched = existing_pages
                        self.part_number = 1
                        print(f"[+] Resumed existing '{self.doc_name}.pdf' ({existing_pages} pages). Next scan is Page {self.page_number}!")
                    except Exception:
                        pass
                else:
                    self.page_number = 1
                    self.pages_stitched = 0
                    self.part_number = 1
                    print(f"[+] New document initialized: '{self.doc_name}.pdf'")
                self.sync_to_file()

    def get_current_pdf_path(self):
        with self.lock:
            return os.path.join(self.processed_dir, f"{self.doc_name}.pdf")

    def get_next_dual_filename(self):
        with self.lock:
            return f"{self.doc_name}_P{self.page_number:02d}_Part{self.part_number}.png"

    def get_single_page_filename(self):
        with self.lock:
            return f"{self.doc_name}_P{self.page_number:02d}_Single.png"

    def advance_part_after_scan(self, saved_filepath):
        with self.lock:
            if self.part_number == 1:
                self.part_number = 2
                self.pending_part1_file = saved_filepath
            else:
                self.part_number = 1
                self.pending_part1_file = None
                self.page_number += 1
            self.sync_to_file()

    def record_page_added(self):
        with self.lock:
            self.pages_stitched += 1
            self.sync_to_file()
            return self.pages_stitched

    def promote_pending_to_single_page(self):
        """Converts an already-scanned Part 1 into a completed standalone single page."""
        with self.lock:
            if not self.pending_part1_file or not os.path.exists(self.pending_part1_file):
                return None
            target_file = self.pending_part1_file
            self.part_number = 1
            self.pending_part1_file = None
            self.page_number += 1
            self.sync_to_file()
            return target_file

    def reload_from_file_if_changed(self):
        """Sync in-memory state with session_state.json if updated externally."""
        with self.lock:
            state = load_session()
            if state:
                file_doc = state.get("doc_name")
                file_page = state.get("page_number", 1)
                file_part = state.get("part_number", 1)
                file_stitched = state.get("pages_stitched", 0)
                if (file_doc and file_doc != self.doc_name) or file_page != self.page_number or file_part != self.part_number:
                    self.doc_name = file_doc or self.doc_name
                    self.page_number = file_page
                    self.part_number = file_part
                    self.pages_stitched = file_stitched

    def resume_document(self, target_name=None):
        """Re-opens a finished document and sets next scan to page count + 1."""
        with self.lock:
            if not os.path.exists(self.processed_dir):
                print("[!] No Processed directory found.")
                return False
            pdf_files = [f for f in os.listdir(self.processed_dir) if f.lower().endswith(".pdf")]
            if not pdf_files:
                print("[!] No documents found in Processed directory to resume.")
                return False
            if target_name:
                clean = target_name.strip()
                if clean.lower().endswith(".pdf"):
                    clean = clean[:-4]
                match = f"{clean}.pdf"
                if match not in pdf_files:
                    print(f"[!] Could not find '{match}' in Processed directory.")
                    print(f"Available: {', '.join(pdf_files)}")
                    return False
                selected_file = match
            else:
                pdf_files.sort(
                    key=lambda f: os.path.getmtime(os.path.join(self.processed_dir, f)),
                    reverse=True,
                )
                selected_file = pdf_files[0]

            doc_name = os.path.splitext(selected_file)[0]
            pdf_path = os.path.join(self.processed_dir, selected_file)
            try:
                doc = fitz.open(pdf_path)
                page_count = len(doc)
                doc.close()
            except Exception as e:
                print(f"[!] Could not inspect PDF '{selected_file}': {e}")
                return False

            self.doc_name = doc_name
            self.page_number = page_count + 1
            self.part_number = 1
            self.pages_stitched = page_count
            self.pending_part1_file = None
            self.sync_to_file()

            print("=" * 65)
            print(f"[+] RESUMED DOCUMENT: {selected_file}")
            print(f"[*] Existing Pages:    {page_count}")
            print(f"[*] Next Scan Will Be: Page {self.page_number} (Part 1)")
            print("=" * 65)
            play_sound("success", wait=False)
            return True

    def finish_document(self):
        with self.lock:
            # If a single scan was pending, automatically include it as the final single page
            if self.pending_part1_file and os.path.exists(self.pending_part1_file):
                print("\n[*] Pending Part 1 detected: Automatically including as final Single Page...")
                img = load_image(self.pending_part1_file)
                if img is not None:
                    pdf_path = os.path.join(self.processed_dir, f"{self.doc_name}.pdf")
                    append_to_pdf(img, pdf_path)
                    self.pages_stitched += 1
                    dest = os.path.join(ARCHIVE_DIR, os.path.basename(self.pending_part1_file))
                    if os.path.exists(dest):
                        os.remove(dest)
                    os.replace(self.pending_part1_file, dest)
                self.pending_part1_file = None
                self.part_number = 1

            if self.pages_stitched == 0:
                print(f"\n[*] Current document '{self.doc_name}' has 0 pages. Ready to scan.")
                return False

            pdf_path = os.path.join(self.processed_dir, f"{self.doc_name}.pdf")
            print("\n" + "=" * 68)
            print(f"[+] DOCUMENT COMPLETE: {os.path.basename(pdf_path)}")
            print(f"[*] Total Pages In PDF: {self.pages_stitched}")
            print(f"[*] Saved to: {pdf_path}")
            print("=" * 68)
            play_sound("finish_doc")

            # Roll to next document
            self.doc_name = get_next_default_doc_name(self.processed_dir)
            self.page_number = 1
            self.part_number = 1
            self.pages_stitched = 0
            self.pending_part1_file = None
            self.sync_to_file()

            print(f"\n[*] Ready for next document! Active: [{self.doc_name}.pdf]")
            print(f"[*] (Press {SCAN_HOTKEY} for 2-part scan, {SINGLE_HOTKEY} for single page, or type document name)\n")
            return True


session = DocumentSession(PROCESSED_DIR)


def detect_and_correct_orientation(img_top, img_bot):
    """
    Detects if the bottom half (img_bot) was scanned rotated 180 degrees.
    Uses ORB feature matching and affine estimation to compute the rotation angle.
    Returns: (corrected_img_bot, was_flipped, angle_deg)
    """
    try:
        orb = cv2.ORB_create(2500)
        kp1, des1 = orb.detectAndCompute(img_top, None)
        kp2, des2 = orb.detectAndCompute(img_bot, None)
        if des1 is None or des2 is None or len(des1) < 10 or len(des2) < 10:
            return img_bot, False, 0.0

        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        matches = bf.knnMatch(des1, des2, k=2)
        good = [m[0] for m in matches if len(m) == 2 and m[0].distance < 0.75 * m[1].distance]
        if len(good) < 5:
            return img_bot, False, 0.0

        src_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

        M, inliers = cv2.estimateAffinePartial2D(
            dst_pts, src_pts, method=cv2.RANSAC, ransacReprojThreshold=5.0
        )
        if M is None or inliers is None or np.sum(inliers) < 5:
            return img_bot, False, 0.0

        angle_deg = math.degrees(math.atan2(M[1, 0], M[0, 0]))

        # If rotation is close to 180 deg (or > 90 deg), the bottom half is upside down
        if abs(angle_deg) > 90.0:
            img_bot_corrected = cv2.rotate(img_bot, cv2.ROTATE_180)
            return img_bot_corrected, True, angle_deg

        return img_bot, False, angle_deg
    except Exception as e:
        print(f"[-] Orientation check notice: {e}")
        return img_bot, False, 0.0


def trigger_usb_scan():
    """Triggers standard dual-pass scan (F7): Top Half -> Bottom Half -> Auto-Stitch."""
    if not scan_lock.acquire(blocking=False):
        print("\n[!] Scanner is currently busy with a scan. Please wait...")
        play_sound("busy")
        return

    def _worker():
        try:
            session.reload_from_file_if_changed()
            curr_part = session.part_number
            curr_page = session.page_number
            doc_name = session.doc_name
            filename = session.get_next_dual_filename()
            output_path = os.path.join(RAW_DIR, filename)

            part_desc = "Top Half (Part 1/2)" if curr_part == 1 else "Bottom Half (Part 2/2)"
            print(f"\n[*] [{doc_name}] Scanning Page {curr_page} - {part_desc}...")
            play_sound("start")

            scan_to_file(output_path)
            session.advance_part_after_scan(output_path)

            if curr_part == 1:
                print(f"[+] Saved: {filename}")
                print(f"    --> Slide/rotate document and press [{SCAN_HOTKEY}] for Bottom Half.")
                print(f"    --> (Or press [{SINGLE_HOTKEY}] if this page is actually a Single/Small Page)")
                play_sound("part1")
            else:
                print(f"[+] Saved: {filename} -> Fusing Page {curr_page} (auto-detecting 180 deg rotation)...")
                play_sound("part2")

        except Exception as e:
            print(f"[!] Direct USB scan error: {e}")
            log_exception(f"Direct USB scan error: {e}")
            play_sound("error")
        finally:
            scan_lock.release()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def trigger_single_page_scan():
    """Triggers a 1-pass scan for pages <= A4 (F8), or promotes an already-scanned Part 1."""
    if not scan_lock.acquire(blocking=False):
        print("\n[!] Scanner is currently busy with a scan. Please wait...")
        play_sound("busy")
        return

    def _worker():
        try:
            session.reload_from_file_if_changed()
            # Case 1: User already scanned Part 1 with F7 and now marks it as a single page
            promoted_file = session.promote_pending_to_single_page()
            if promoted_file:
                print(f"\n[*] Converting previous scan to completed Single Page...")
                img = load_image(promoted_file)
                if img is None:
                    raise ValueError(f"Could not load promoted scan: {promoted_file}")
                pdf_path = session.get_current_pdf_path()
                append_to_pdf(img, pdf_path)
                total = session.record_page_added()
                print(f"[+] Added Single Page to {os.path.basename(pdf_path)} (Total: {total} pages).")
                # Archive
                dest = os.path.join(ARCHIVE_DIR, os.path.basename(promoted_file))
                if os.path.exists(dest):
                    os.remove(dest)
                os.replace(promoted_file, dest)
                play_sound("part2")
                return

            # Case 2: Direct 1-pass capture from scanner
            doc_name = session.doc_name
            curr_page = session.page_number
            filename = session.get_single_page_filename()
            output_path = os.path.join(RAW_DIR, filename)

            print(f"\n[*] [{doc_name}] Scanning Page {curr_page} as Single-Pass Page (<= A4)...")
            play_sound("start")

            scan_to_file(output_path)
            img = load_image(output_path)
            if img is None:
                raise ValueError(f"Could not load scanned image: {output_path}")

            pdf_path = session.get_current_pdf_path()
            append_to_pdf(img, pdf_path)
            session.page_number += 1
            total = session.record_page_added()

            print(f"[+] Captured Single Page {total} -> Added to {os.path.basename(pdf_path)}!")
            print(f"[*] Current Document '{doc_name}.pdf' now has {total} page(s).\n")
            play_sound("part2")

            # Archive raw file
            dest = os.path.join(ARCHIVE_DIR, filename)
            if os.path.exists(dest):
                os.remove(dest)
            os.replace(output_path, dest)

        except Exception as e:
            print(f"[!] Single page scan error: {e}")
            log_exception(f"Single page scan error: {e}")
            play_sound("error")
        finally:
            scan_lock.release()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def trigger_finish_document():
    """Finalizes current document and starts a new one (F6)."""
    session.finish_document()


def console_listener():
    """Allows typing custom document names or pressing Enter in the terminal to scan."""
    while True:
        try:
            line = input().strip()
            if not line:
                trigger_usb_scan()
            elif line.lower() in ("1", "single", "s", "f8"):
                trigger_single_page_scan()
            elif line.lower() in ("finish", "done", "next", "f6"):
                session.finish_document()
            elif line.lower() in ("resume", "undo", "back"):
                session.resume_document()
            elif line.lower().startswith("resume "):
                session.resume_document(line[7:].strip())
            elif line.lower() in ("status", "info", "current", "where", "?"):
                from show_status import display_status
                display_status()
            elif line.lower() in ("select", "switch", "choose", "menu", "dropdown", "change"):
                from select_document_gui import open_selector
                open_selector()
                session.reload_from_file_if_changed()
            elif line.lower() in ("manage", "pages", "review"):
                from manage_pages_gui import main as run_manager
                run_manager()
                session.reload_from_file_if_changed()
            elif line.lower() in ("delete", "del", "droplast"):
                from delete_last_page import delete_last_page
                delete_last_page(1)
                session.reload_from_file_if_changed()
            else:
                session.set_custom_name(line)
        except (EOFError, KeyboardInterrupt):
            break


def load_image(filepath):
    """Safely loads an image handling Unicode paths on Windows."""
    try:
        data = np.fromfile(filepath, dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception as e:
        print(f"[!] Error loading image '{filepath}': {e}")
        return None


def is_file_ready(filepath):
    """Ensure the scanner has completely finished writing the file."""
    try:
        if not os.path.isfile(filepath):
            return False
        if os.path.getsize(filepath) == 0:
            return False
        with open(filepath, "rb"):
            return True
    except (IOError, PermissionError):
        return False


def get_sorted_scans():
    """Fetch all unstitched image files in Raw sorted by creation/modification time."""
    valid_exts = ("*.jpg", "*.jpeg", "*.png", "*.tif", "*.tiff", "*.bmp")
    files = set()
    for ext in valid_exts:
        files.update(glob.glob(os.path.join(RAW_DIR, ext)))
        files.update(glob.glob(os.path.join(RAW_DIR, ext.upper())))
    sorted_files = sorted(list(files), key=lambda f: (os.path.getmtime(f), os.path.basename(f)))
    return sorted_files


def auto_crop_borders(img, dark_threshold=40, max_crop_fraction=0.10):
    """
    Refactored strictly conservative auto-crop:
    - Only trims true dark scanner platen borders (intensity < dark_threshold, default 40).
    - Never cuts into page margins, normal page content, or document text.
    - Preserves full document boundary.
    - If crop area exceeds 10% of the page or would trim light/text pixels (intensity >= dark_threshold),
      the original boundary is fully retained.
    """
    if img is None or not hasattr(img, "shape") or img.size == 0:
        return img

    try:
        if not isinstance(img, np.ndarray) or img.ndim < 2:
            return img

        h, w = img.shape[:2]
        if h < 20 or w < 20:
            return img

        if len(img.shape) == 3:
            if img.shape[2] == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            elif img.shape[2] == 4:
                gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
            else:
                gray = img[:, :, 0]
        else:
            gray = img

        # Identify pixels that belong to page content, margins, or text (intensity >= dark_threshold)
        content_mask = (gray >= dark_threshold)

        # If image has no content pixels (all platen / black) or all content pixels (no platen border)
        if not np.any(content_mask) or np.all(content_mask):
            return img

        # Determine the exact bounding envelope containing ALL content / margin pixels
        rows_with_content = np.any(content_mask, axis=1)
        cols_with_content = np.any(content_mask, axis=0)

        top_crop = int(np.argmax(rows_with_content))
        bot_crop = int(h - np.argmax(rows_with_content[::-1]))
        left_crop = int(np.argmax(cols_with_content))
        right_crop = int(w - np.argmax(cols_with_content[::-1]))

        # Validate bounding box sanity
        if bot_crop <= top_crop or right_crop <= left_crop:
            return img

        total_area = h * w
        cropped_area = (bot_crop - top_crop) * (right_crop - left_crop)
        trimmed_area = total_area - cropped_area
        trimmed_ratio = trimmed_area / float(total_area)

        # Strictly conservative: if crop area exceeds 10% of page, retain original boundary
        if trimmed_ratio > max_crop_fraction:
            return img

        if left_crop > 0 or right_crop < w or top_crop > 0 or bot_crop < h:
            return img[top_crop:bot_crop, left_crop:right_crop]

        return img
    except Exception:
        return img


def stitch_pair(img_path1, img_path2):
    """
    Stitches two planar document halves with true 2D rigid planar alignment:
    - Auto-detects and corrects 180° rotation on the bottom half.
    - Uses pure white background canvas (never black).
    - Prevents exposure-compensator blotches and multi-band blurring.
    - Auto-crops dark scanner platen borders and lid margins.
    """
    img1 = load_image(img_path1)
    img2 = load_image(img_path2)

    if img1 is None or img2 is None:
        raise ValueError(f"Corrupt or unreadable image file: '{img_path1}' or '{img_path2}'")

    # Check and correct 180° rotation on bottom half
    img2_corrected, was_flipped, angle = detect_and_correct_orientation(img1, img2)
    if was_flipped:
        print(f"[*] Detected bottom half scanned upside down ({angle:.1f} deg). Auto-rotated 180 deg upright.")
        img2 = img2_corrected
    else:
        img2 = img2_corrected

    h1, w1 = img1.shape[:2]
    h2, w2 = img2.shape[:2]

    # SIFT feature detection
    sift = cv2.SIFT_create(4000)
    kp1, des1 = sift.detectAndCompute(img1, None)
    kp2, des2 = sift.detectAndCompute(img2, None)

    if des1 is None or des2 is None or len(des1) < 10 or len(des2) < 10:
        print("[-] Insufficient features. Applying clean vertical fallback stack.")
        h2_aligned = int(h2 * (w1 / w2))
        img2_resized = cv2.resize(img2, (w1, h2_aligned), interpolation=cv2.INTER_AREA)
        return auto_crop_borders(np.vstack((img1, img2_resized)))

    bf = cv2.BFMatcher()
    matches = bf.knnMatch(des1, des2, k=2)
    good = [m[0] for m in matches if len(m) == 2 and m[0].distance < 0.75 * m[1].distance]

    if len(good) < 8:
        print("[-] Few feature matches. Applying clean vertical fallback stack.")
        h2_aligned = int(h2 * (w1 / w2))
        img2_resized = cv2.resize(img2, (w1, h2_aligned), interpolation=cv2.INTER_AREA)
        return auto_crop_borders(np.vstack((img1, img2_resized)))

    src_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

    M, inliers = cv2.estimateAffinePartial2D(dst_pts, src_pts, method=cv2.RANSAC, ransacReprojThreshold=4.0)
    if M is None or inliers is None or np.sum(inliers) < 8:
        print("[-] Affine estimation rejected. Applying clean vertical fallback stack.")
        h2_aligned = int(h2 * (w1 / w2))
        img2_resized = cv2.resize(img2, (w1, h2_aligned), interpolation=cv2.INTER_AREA)
        return auto_crop_borders(np.vstack((img1, img2_resized)))

    # Compute bounding box
    corners_img2 = np.float32([[0, 0], [w2, 0], [w2, h2], [0, h2]]).reshape(-1, 1, 2)
    warped_corners = cv2.transform(corners_img2, M)
    all_corners = np.vstack([
        np.float32([[0, 0], [w1, 0], [w1, h1], [0, h1]]).reshape(-1, 1, 2),
        warped_corners
    ])

    min_x = int(np.floor(all_corners[:, 0, 0].min()))
    max_x = int(np.ceil(all_corners[:, 0, 0].max()))
    min_y = int(np.floor(all_corners[:, 0, 1].min()))
    max_y = int(np.ceil(all_corners[:, 0, 1].max()))

    canvas_w = max_x - min_x
    canvas_h = max_y - min_y

    # Pure white background canvas (never black!)
    canvas = np.ones((canvas_h, canvas_w, 3), dtype=np.uint8) * 255

    T = np.float32([[1, 0, -min_x], [0, 1, -min_y]])
    M_combined = np.vstack([M, [0, 0, 1]])
    T_combined = np.vstack([T, [0, 0, 1]])
    full_M = (T_combined @ M_combined)[:2, :]

    # Warp img2
    warped_img2 = np.ones_like(canvas) * 255
    mask_img2 = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    cv2.warpAffine(img2, full_M, (canvas_w, canvas_h), dst=warped_img2, borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
    cv2.warpAffine(np.ones((h2, w2), dtype=np.uint8) * 255, full_M, (canvas_w, canvas_h), dst=mask_img2, borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    # Warp img1
    cv2.warpAffine(img1, T, (canvas_w, canvas_h), dst=canvas, borderMode=cv2.BORDER_TRANSPARENT)
    mask_img1 = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    cv2.warpAffine(np.ones((h1, w1), dtype=np.uint8) * 255, T, (canvas_w, canvas_h), dst=mask_img1, borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    # Smooth linear vertical blend across overlap zone
    overlap = (mask_img1 > 0) & (mask_img2 > 0)
    y_overlap = np.where(overlap)[0]
    if len(y_overlap) > 0:
        y_start, y_end = y_overlap.min(), y_overlap.max()
        alpha = np.zeros((canvas_h, 1, 1), dtype=np.float32)
        alpha[:y_start] = 1.0
        alpha[y_end:] = 0.0
        alpha[y_start:y_end] = np.linspace(1.0, 0.0, y_end - y_start).reshape(-1, 1, 1)

        result = np.where(mask_img1[:, :, None] == 0, warped_img2,
                 np.where(mask_img2[:, :, None] == 0, canvas,
                          (canvas * alpha + warped_img2 * (1.0 - alpha)).astype(np.uint8)))
    else:
        result = np.where(mask_img2[:, :, None] > 0, warped_img2, canvas)

    # Auto-crop scanner bed margins
    cropped_result = auto_crop_borders(result)
    return cropped_result


def append_to_pdf(stitched_img, output_pdf_path, dpi=300):
    """Appends an OpenCV image matrix to a target PDF with proper 300 DPI metadata and compact compression."""
    from PIL import Image
    import io

    if len(stitched_img.shape) == 3 and stitched_img.shape[2] == 3:
        pil_img = Image.fromarray(cv2.cvtColor(stitched_img, cv2.COLOR_BGR2RGB))
    else:
        pil_img = Image.fromarray(stitched_img)

    bio = io.BytesIO()
    pil_img.save(bio, "PDF", resolution=float(dpi), quality=88, optimize=True)
    page_doc = fitz.open("pdf", bio.getvalue())

    if os.path.exists(output_pdf_path):
        doc = fitz.open(output_pdf_path)
        doc.insert_pdf(page_doc)
        try:
            doc.save(output_pdf_path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        except Exception:
            temp_path = output_pdf_path + ".tmp"
            doc.save(temp_path, deflate=True)
            doc.close()
            os.replace(temp_path, output_pdf_path)
            page_doc.close()
            return
        doc.close()
    else:
        page_doc.save(output_pdf_path, deflate=True)

    page_doc.close()


def process_queue():
    files = get_sorted_scans()

    # Process whenever at least one full pair (2 parts) is available
    while len(files) >= 2:
        file1, file2 = files[0], files[1]

        if not (is_file_ready(file1) and is_file_ready(file2)):
            time.sleep(1)
            if not (is_file_ready(file1) and is_file_ready(file2)):
                break

        print(f"[*] Processing scan pair: {os.path.basename(file1)} + {os.path.basename(file2)}")

        try:
            stitched = stitch_pair(file1, file2)
            output_pdf = session.get_current_pdf_path()

            append_to_pdf(stitched, output_pdf)
            total_pages = session.record_page_added()
            print(f"[+] Page {total_pages} fused and appended to: {os.path.basename(output_pdf)}")
            print(f"[*] Current Document '{session.doc_name}.pdf' now has {total_pages} page(s).")
            print(f"[*] Ready for next page [{SCAN_HOTKEY}] or finish document [{FINISH_HOTKEY}].\n")

            # Archive the raw components
            for f in (file1, file2):
                dest = os.path.join(ARCHIVE_DIR, os.path.basename(f))
                if os.path.exists(dest):
                    os.remove(dest)
                os.replace(f, dest)

            session.pending_part1_file = None

        except Exception as e:
            print(f"[!] Stitching error: {e}")
            break

        # Refresh remaining queue
        files = get_sorted_scans()


def main():
    for d in (RAW_DIR, PROCESSED_DIR, ARCHIVE_DIR):
        os.makedirs(d, exist_ok=True)

    scanner_name = get_connected_scanner_name()

    print("=" * 70)
    print(" Automated Multi-Page Document Scan Stitcher")
    print("=" * 70)
    print(f"[*] Raw drop zone:        {RAW_DIR}")
    print(f"[*] Processed output dir: {PROCESSED_DIR}")
    print(f"[*] Archive directory:    {ARCHIVE_DIR}")

    if scanner_name:
        print(f"[*] Connected USB Scanner: {scanner_name}")
    else:
        print("[!] No USB scanner detected via WIA. Ready for external scan drops.")

    print("-" * 70)
    print(f"[*] Active Document:      [{session.doc_name}.pdf]")
    print(f"[*] Dual-Pass Scan (2p):  [{SCAN_HOTKEY}]   (Captures Top/Bottom & auto-stitches)")
    print(f"[*] Single-Pass Scan (1p): [{SINGLE_HOTKEY}]   (Pages <= A4, direct to PDF)")
    print(f"[*] Finish & Start Next:  [{FINISH_HOTKEY}]   (Finalizes current PDF and starts next)")
    print(f"[*] Console Shortcuts:    [Enter] scan, [1] single, [menu] select doc dropdown, [status] info")
    print("=" * 70 + "\n")

    # Setup global hotkeys
    if KEYBOARD_AVAILABLE:
        try:
            keyboard.add_hotkey(SCAN_HOTKEY, trigger_usb_scan)
            keyboard.add_hotkey(FINISH_HOTKEY, trigger_finish_document)
            keyboard.add_hotkey(SINGLE_HOTKEY, trigger_single_page_scan)
            keyboard.add_hotkey(f"shift+{SCAN_HOTKEY.lower()}", trigger_single_page_scan)
        except Exception as e:
            print(f"[-] Could not bind global hotkeys: {e}")

    # Start console input listener in background thread
    t_input = threading.Thread(target=console_listener, daemon=True)
    t_input.start()

    try:
        while True:
            process_queue()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[*] Stopping daemon cleanly. Goodbye!")


if __name__ == "__main__":
    main()
