import os
import sys
import time
import threading
try:
    import winsound
except ImportError:
    winsound = None

import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import pythoncom
    import win32com.client
except ImportError:
    pythoncom = None
    win32com = None

# WIA Constants
WIA_DEVICE_TYPE_SCANNER = 1
WIA_FORMAT_BMP = "{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}"
WIA_FORMAT_PNG = "{B96B3CAF-0728-11D3-9D7B-0000F81EF32E}"
WIA_FORMAT_JPEG = "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}"

# WIA Error Codes
WIA_ERROR_PAPER_EMPTY = 0x80210003      # 2162237443 or -2145320957 (signed 32-bit COM error)
WIA_ERROR_PAPER_JAM = 0x80210002        # -2145320958
WIA_ERROR_PAPER_PROBLEM = 0x80210004    # -2145320956

def is_paper_empty_error(e):
    """
    Checks if an exception is WIA_ERROR_PAPER_EMPTY (0x80210003 / -2145320957).
    """
    hr = getattr(e, "hresult", None)
    if hr is None and hasattr(e, "args") and len(e.args) > 0 and isinstance(e.args[0], int):
        hr = e.args[0]
    if hr is not None:
        if (hr & 0xFFFFFFFF) == 0x80210003 or hr == -2145320957:
            return True

    msg = str(e).lower()
    indicators = [
        "0x80210003",
        "-2145320957",
        "paper empty",
        "paper_empty",
        "out of paper",
        "feeder is empty",
        "feeder empty",
        "tray empty",
        "no paper in feeder",
        "wia_error_paper_empty"
    ]
    return any(ind in msg for ind in indicators)

# Default Scan Settings
DEFAULT_DPI = int(os.getenv("SCAN_DPI", "300"))
DEFAULT_COLOR_MODE = os.getenv("SCAN_COLOR_MODE", "color")  # "color", "grayscale", "bw"
DEFAULT_SOURCE = os.getenv("SCAN_SOURCE", "flatbed")  # "flatbed", "feeder"
DEFAULT_PAGE_SIZE = os.getenv("SCAN_PAGE_SIZE", "a4")  # "a4", "letter", "legal", "photo_4x6", "auto", "max"

def is_mock_scanner():
    """Returns True if mock scanner hardware isolation mode is enabled."""
    return os.getenv("MOCK_SCANNER", "0").lower().strip() in ("1", "true", "yes")

def normalize_page_size(page_size):
    """Normalizes page size name variants to standard keys."""
    k = str(page_size).lower().strip()
    if k in ("max", "max area", "max_area", "entire scan area (max platen)"):
        return "max"
    if k in ("photo", "photo_4x6", "4x6", "4 x 6", "photo 4x6"):
        return "photo_4x6"
    return k

PAGE_SIZES_INCHES = {
    "a4": (8.2677, 11.6929),       # 210 x 297 mm
    "letter": (8.5, 11.0),          # 215.9 x 279.4 mm
    "legal": (8.5, 14.0),           # 215.9 x 355.6 mm (Feeder / ADF)
    "photo_4x6": (4.0, 6.0),        # 101.6 x 152.4 mm (Photo)
    "4x6": (4.0, 6.0),
    "max": (8.5, 14.0),             # Platen / Feeder maximum (2550 x 4200 px at 300 DPI)
    "max area": (8.5, 14.0),
    "max_area": (8.5, 14.0),
    "auto": (8.5, 14.0)             # Auto-detect & crop to content
}

# Known network endpoints for the HP Smart Tank 750
PRINTER_HOSTS = [
    os.getenv("SCAN_PRINTER_IP", "192.168.68.69:8080"),
    "192.168.68.69:8080",
    "192.168.68.69",
    "HP09AC51.local:8080",
    "HP09AC51.local",
    "HP28C5C809AC51.local:8080",
    "HP28C5C809AC51.local",
]


def save_scanned_image(img_mat, output_filepath, dpi=300, quality=88):
    """
    Saves scanned image matrix with exact DPI metadata and optimal compression.
    Supports native PDF, JPEG, and PNG.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_filepath)), exist_ok=True)
    ext = os.path.splitext(output_filepath)[1].lower()

    if len(img_mat.shape) == 3 and img_mat.shape[2] == 3:
        im = Image.fromarray(cv2.cvtColor(img_mat, cv2.COLOR_BGR2RGB))
    elif len(img_mat.shape) == 2:
        im = Image.fromarray(img_mat)
    else:
        im = Image.fromarray(img_mat)

    if ext == ".pdf":
        im.save(output_filepath, "PDF", resolution=float(dpi), quality=quality, optimize=True)
    elif ext in (".jpg", ".jpeg"):
        im.save(output_filepath, "JPEG", dpi=(dpi, dpi), quality=quality, optimize=True)
    elif ext == ".png":
        im.save(output_filepath, "PNG", dpi=(dpi, dpi), optimize=True)
    else:
        cv2.imwrite(output_filepath, img_mat)



def play_sound(sound_type, wait=True):
    """
    Plays audio feedback through the active Windows audio device (e.g. EDIFIER R33BT / headphones / speakers).
    Ensures Bluetooth audio buffers are completely delivered and not cut off by script exit.
    """
    sound_map = {
        "start": r"C:\Windows\Media\Windows Navigation Start.wav",
        "part1": r"C:\Windows\Media\ding.wav",
        "part2": r"C:\Windows\Media\chimes.wav",
        "success": r"C:\Windows\Media\chimes.wav",
        "finish_doc": r"C:\Windows\Media\tada.wav",
        "busy": r"C:\Windows\Media\Windows Background.wav",
        "error": r"C:\Windows\Media\chord.wav",
        "cancel": r"C:\Windows\Media\Windows Hardware Remove.wav",
    }
    if not winsound:
        return
    path = sound_map.get(sound_type)
    if not path or not os.path.exists(path):
        return

    def _play():
        try:
            winsound.PlaySound(path, winsound.SND_FILENAME)
        except Exception:
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass

    if wait:
        _play()
    else:
        t = threading.Thread(target=_play, daemon=False)
        t.start()


def get_escl_host(timeout=0.6):
    """Checks if HP Smart Tank is reachable over the local network via eSCL protocol."""
    if is_mock_scanner():
        return None
    candidates = []
    for h in PRINTER_HOSTS:
        candidates.append(h)
        if ":" not in h:
            candidates.append(f"{h}:8080")
    seen = set()
    unique_candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    for host in unique_candidates:
        try:
            url = f"http://{host}/eSCL/ScannerStatus"
            req = urllib.request.Request(url, headers={"User-Agent": "ScanStitcher/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return host
        except Exception:
            continue
    return None


def scan_mock_to_file(output_filepath, dpi=DEFAULT_DPI, color_mode=DEFAULT_COLOR_MODE, source=DEFAULT_SOURCE, page_size=DEFAULT_PAGE_SIZE):
    """
    Simulates high-fidelity scanner acquisition in full hardware isolation.
    Bypasses Windows COM WIA and network eSCL calls completely so active
    user scanning on physical HP Smart Tank 750 is never disturbed.
    """
    sim_delay = float(os.getenv("MOCK_SCAN_DELAY", "0.05"))
    if sim_delay > 0:
        time.sleep(sim_delay)

    fname_lower = os.path.basename(output_filepath).lower()
    img_mat = None

    # 1. If Part 1 or Part 2, check for Archive samples so SIFT stitching can execute realistically
    archive_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Archive")
    if "part1" in fname_lower:
        sample_path = os.path.join(archive_dir, "Doc_002_P01_Part1.png")
        if os.path.exists(sample_path):
            img_mat = cv2.imread(sample_path)
    elif "part2" in fname_lower:
        sample_path = os.path.join(archive_dir, "Doc_002_P01_Part2.png")
        if os.path.exists(sample_path):
            img_mat = cv2.imread(sample_path)

    # 2. If single or sample not found, check for single scan sample
    if img_mat is None and ("single" in fname_lower or os.path.exists(os.path.join(archive_dir, "Doc_003_P01_Single.png"))):
        sample_path = os.path.join(archive_dir, "Doc_003_P01_Single.png")
        if os.path.exists(sample_path):
            img_mat = cv2.imread(sample_path)

    # 3. Calculate target pixel dimensions based on page_size and DPI
    size_key = normalize_page_size(page_size)
    w_in, h_in = PAGE_SIZES_INCHES.get(size_key, PAGE_SIZES_INCHES["a4"])
    target_w = int(round(w_in * dpi))
    target_h = int(round(h_in * dpi))
    if size_key == "max":
        target_w = int(round(8.5 * dpi))
        target_h = int(round(14.0 * dpi))

    # 4. If synthetic creation needed
    if img_mat is None:
        img_mat = np.full((target_h, target_w, 3), 255, dtype=np.uint8)
        # Add synthetic document features (margins, text line placeholders, header)
        cv2.rectangle(img_mat, (int(target_w * 0.05), int(target_h * 0.05)),
                              (int(target_w * 0.95), int(target_h * 0.95)), (210, 210, 210), 2)
        cv2.putText(img_mat, f"Synthetic Mock Scan: {size_key.upper()} ({dpi} DPI)",
                    (int(target_w * 0.1), int(target_h * 0.1)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0 * (dpi / 300.0), (30, 30, 30), 2)
        for row in range(5):
            y_pos = int(target_h * (0.2 + row * 0.12))
            cv2.line(img_mat, (int(target_w * 0.1), y_pos), (int(target_w * 0.85), y_pos), (180, 180, 180), 2)
    else:
        # If an existing sample was loaded:
        # For part1 and part2, retain their aspect and keypoints (scale by dpi/300 if needed)
        if "part1" in fname_lower or "part2" in fname_lower:
            if dpi != 300:
                scale = dpi / 300.0
                new_w = max(10, int(round(img_mat.shape[1] * scale)))
                new_h = max(10, int(round(img_mat.shape[0] * scale)))
                img_mat = cv2.resize(img_mat, (new_w, new_h))
        else:
            # For standard pages, resize or crop to match requested size_key and DPI
            if size_key == "max":
                img_mat = cv2.resize(img_mat, (target_w, target_h))
            elif size_key in PAGE_SIZES_INCHES and size_key != "auto":
                img_mat = cv2.resize(img_mat, (target_w, target_h))
            elif size_key == "auto":
                # For auto-detect test, ensure borders exist so auto_crop_borders can be exercised
                try:
                    import stitch_daemon
                    cropped = stitch_daemon.auto_crop_borders(img_mat)
                    if cropped is not None and cropped.size > 0:
                        img_mat = cropped
                except Exception:
                    pass

    # 5. Apply color mode
    cm = str(color_mode).lower().strip()
    if cm in ("gray", "grayscale"):
        if len(img_mat.shape) == 3:
            img_mat = cv2.cvtColor(img_mat, cv2.COLOR_BGR2GRAY)
    elif cm in ("bw", "text", "mono", "monochrome"):
        if len(img_mat.shape) == 3:
            gray = cv2.cvtColor(img_mat, cv2.COLOR_BGR2GRAY)
        else:
            gray = img_mat
        _, img_mat = cv2.threshold(gray, 128, 255, cv2.THRESH_BINARY)

    # 6. Save image using optimal compression and DPI metadata
    save_scanned_image(img_mat, output_filepath, dpi=dpi, quality=88)
    return "HP Smart Tank 750 (Mock Isolated Scanner)"


def decode_scanned_bytes_to_cv2(data: bytes, dpi: int = 300) -> np.ndarray:
    """
    Robustly decodes raw scanner payload bytes (JPEG, PNG, TIFF, or single-page PDF from eSCL)
    into an OpenCV BGR uint8 matrix.
    """
    if not data:
        return None

    # 1. Standard image payload (JPEG / PNG / BMP)
    try:
        nparr = np.frombuffer(data, np.uint8)
        mat = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if mat is not None:
            return mat
    except Exception:
        pass

    # 2. If it is a PDF payload (%PDF-...) from eSCL AirScan
    if data.startswith(b"%PDF-") or b"/Type /XObject" in data[:1024]:
        # Try PyMuPDF / fitz
        try:
            import fitz
            doc = fitz.open(stream=data, filetype="pdf")
            if len(doc) > 0:
                page = doc[0]
                imgs = page.get_images()
                if imgs:
                    xref = imgs[0][0]
                    base_img = doc.extract_image(xref)
                    raw_bytes = base_img.get("image")
                    if raw_bytes:
                        nparr = np.frombuffer(raw_bytes, np.uint8)
                        mat = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                        if mat is not None:
                            return mat
                pix = page.get_pixmap(dpi=dpi)
                mat = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, pix.n))
                if pix.n == 4:
                    return cv2.cvtColor(mat, cv2.COLOR_RGBA2BGR)
                elif pix.n == 3:
                    return cv2.cvtColor(mat, cv2.COLOR_RGB2BGR)
                elif pix.n == 1:
                    return cv2.cvtColor(mat, cv2.COLOR_GRAY2BGR)
                return mat
        except Exception:
            pass

        # Try pikepdf
        try:
            import pikepdf
            from PIL import Image
            import io
            with pikepdf.open(io.BytesIO(data)) as pdf:
                if len(pdf.pages) > 0:
                    page = pdf.pages[0]
                    for img_name in page.images:
                        raw_im = page.images[img_name]
                        pil_im = Image.open(io.BytesIO(raw_im.read_bytes()))
                        return cv2.cvtColor(np.array(pil_im), cv2.COLOR_RGB2BGR)
        except Exception:
            pass

    # 3. Direct JPEG stream extraction (find SOI 0xFFD8 and EOI 0xFFD9)
    soi = data.find(b"\xff\xd8")
    if soi != -1:
        eoi = data.rfind(b"\xff\xd9")
        jpeg_bytes = data[soi:eoi+2] if (eoi != -1 and eoi > soi) else data[soi:]
        try:
            nparr = np.frombuffer(jpeg_bytes, np.uint8)
            mat = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if mat is not None:
                return mat
        except Exception:
            pass

    return None


def scan_escl_to_file(output_filepath, host, dpi=DEFAULT_DPI, color_mode=DEFAULT_COLOR_MODE, source=DEFAULT_SOURCE, page_size=DEFAULT_PAGE_SIZE):
    """
    Scans directly from HP Smart Tank 750 over local network via eSCL protocol.
    Completely wireless, zero USB cables, zero disconnects or driver crashes.
    """
    if is_mock_scanner():
        return scan_mock_to_file(output_filepath, dpi=dpi, color_mode=color_mode, source=source, page_size=page_size)

    job_url = f"http://{host}/eSCL/ScanJobs"
    cm_str = "RGB24"
    if color_mode.lower() in ("gray", "grayscale"):
        cm_str = "Grayscale8"
    elif color_mode.lower() in ("bw", "text", "mono", "monochrome"):
        cm_str = "BlackAndWhite1"

    input_src = "Feeder" if source.lower() in ("feeder", "adf") else "Platen"

    # Configure Scan Region for page size in 300ths of an inch (PWG 5102.4)
    size_key = normalize_page_size(page_size)
    scan_region_xml = ""
    if size_key in PAGE_SIZES_INCHES and size_key != "auto":
        w_in, h_in = PAGE_SIZES_INCHES[size_key]
        reg_w = min(2550, int(round(w_in * 300)))
        max_h = 4200 if (input_src == "Feeder" or size_key == "max") else 3508
        reg_h = min(max_h, int(round(h_in * 300)))
        scan_region_xml = f"""  <scan:ScanRegions>
    <scan:ScanRegion>
      <scan:XOffset>0</scan:XOffset>
      <scan:YOffset>0</scan:YOffset>
      <scan:Width>{reg_w}</scan:Width>
      <scan:Height>{reg_h}</scan:Height>
    </scan:ScanRegion>
  </scan:ScanRegions>
"""

    settings_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <scan:Intent>Document</scan:Intent>
  <scan:InputSource>{input_src}</scan:InputSource>
  <scan:ColorMode>{cm_str}</scan:ColorMode>
  <scan:XResolution>{dpi}</scan:XResolution>
  <scan:YResolution>{dpi}</scan:YResolution>
  <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
{scan_region_xml}</scan:ScanSettings>
"""
    req = urllib.request.Request(
        job_url,
        data=settings_xml.encode("utf-8"),
        headers={"Content-Type": "text/xml", "User-Agent": "ScanStitcher/1.0"}
    )

    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 201:
            raise RuntimeError(f"Scanner rejected scan job with status {resp.status}")
        location = resp.headers.get("Location")
        if not location:
            raise RuntimeError("Scanner did not return Job Location header.")

    if location.startswith("http"):
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(location)
        doc_url = urlunparse((parsed.scheme or "http", host, parsed.path, parsed.params, parsed.query, parsed.fragment))
    else:
        doc_url = f"http://{host}" + ("/" if not location.startswith("/") else "") + location
    doc_url = doc_url.rstrip("/") + "/NextDocument"

    # Wait for scanner hardware carriage to traverse platen and generate image
    img_data = None
    for attempt in range(25):
        try:
            req_doc = urllib.request.Request(doc_url, headers={"User-Agent": "ScanStitcher/1.0"})
            with urllib.request.urlopen(req_doc, timeout=40) as doc_resp:
                img_data = doc_resp.read()
                if img_data and len(img_data) > 1000:
                    break
        except Exception:
            time.sleep(1.0)

    if not img_data:
        raise RuntimeError("Failed to retrieve image stream from network scanner.")

    # Convert to image matrix and save to output path
    img = decode_scanned_bytes_to_cv2(img_data, dpi=dpi)
    if img is None:
        raise ValueError("Could not decode scanned image matrix from network stream.")

    if size_key == "auto":
        try:
            import stitch_daemon
            cropped = stitch_daemon.auto_crop_borders(img)
            if cropped is not None and cropped.size > 0:
                img = cropped
        except Exception:
            pass

    save_scanned_image(img, output_filepath, dpi=dpi, quality=88)
    return "HP Smart Tank 750 (Wireless / Network - Zero-Cable)"


def scan_wia_usb_to_file(output_filepath, dpi=DEFAULT_DPI, color_mode=DEFAULT_COLOR_MODE, source=DEFAULT_SOURCE, page_size=DEFAULT_PAGE_SIZE):
    """Directly commands the USB scanner via Windows Image Acquisition (WIA)."""
    if is_mock_scanner():
        return scan_mock_to_file(output_filepath, dpi=dpi, color_mode=color_mode, source=source, page_size=page_size)

    pythoncom.CoInitialize()
    temp_bmp = output_filepath + ".tmp.bmp"
    dm = None
    infos = None
    scanner_info = None
    device = None
    scan_item = None
    image = None

    try:
        dm = win32com.client.Dispatch("WIA.DeviceManager")
        infos = dm.DeviceInfos

        # Search for connected scanner with retry
        scanner_info = None
        max_attempts = 4
        for attempt in range(max_attempts):
            infos = dm.DeviceInfos
            for i in range(1, infos.Count + 1):
                item = infos.Item(i)
                if item.Type == WIA_DEVICE_TYPE_SCANNER:
                    scanner_info = item
                    break
                item = None

            if not scanner_info and infos.Count > 0:
                scanner_info = infos.Item(1)

            if scanner_info:
                break

            if attempt < max_attempts - 1:
                print(f"[*] USB scanner not detected (attempt {attempt+1}/{max_attempts}). Retrying...")
                time.sleep(1.2)

        if not scanner_info:
            raise RuntimeError(
                "HP Smart Tank was not detected by Windows over USB.\n"
                "[*] Note: Even if printer screen is ON, Windows has dropped the physical USB link.\n"
                "[*] Fix: Unplug and replug the USB cable, or use the direct Wireless Network mode."
            )

        device_name = str(scanner_info.Properties.Item("Name").Value)

        # Connect to scanner with retry
        device = None
        for conn_attempt in range(4):
            try:
                device = scanner_info.Connect()
                break
            except Exception as e:
                if conn_attempt < 3:
                    time.sleep(1.5)
                else:
                    raise e

        scanner_info = None
        infos = None

        # Set Document Handling Select: 1 = Feeder/ADF, 2 = Flatbed
        try:
            handling_val = 1 if source.lower() in ("feeder", "adf") else 2
            device.Properties.Item("Document Handling Select").Value = handling_val
        except Exception as ex_h:
            print(f"[*] Note: Document Handling Select ({source}): {ex_h}")

        scan_item = device.Items.Item(1)

        # Set DPI Resolution
        try:
            scan_item.Properties.Item("Horizontal Resolution").Value = dpi
            scan_item.Properties.Item("Vertical Resolution").Value = dpi
        except Exception:
            pass

        # Set Scan Size / Page Extent
        try:
            p_horiz_ext = scan_item.Properties.Item("Horizontal Extent")
            p_vert_ext = scan_item.Properties.Item("Vertical Extent")
            p_horiz_start = scan_item.Properties.Item("Horizontal Start Position")
            p_vert_start = scan_item.Properties.Item("Vertical Start Position")

            p_horiz_start.Value = 0
            p_vert_start.Value = 0

            try:
                max_w = int(p_horiz_ext.SubTypeMax)
            except Exception:
                max_w = int(round(8.5 * dpi))
            try:
                max_h = int(p_vert_ext.SubTypeMax)
            except Exception:
                max_h = int(round(14.0 * dpi))

            size_key = normalize_page_size(page_size)
            if size_key == "max":
                p_horiz_ext.Value = min(max_w, int(round(8.5 * dpi)))
                p_vert_ext.Value = min(max_h, int(round(14.0 * dpi)))
            elif size_key in PAGE_SIZES_INCHES and size_key != "auto":
                w_in, h_in = PAGE_SIZES_INCHES[size_key]
                target_w = min(max_w, int(round(w_in * dpi)))
                target_h = min(max_h, int(round(h_in * dpi)))
                p_horiz_ext.Value = target_w
                p_vert_ext.Value = target_h
            else:
                p_horiz_ext.Value = max_w
                p_vert_ext.Value = max_h
        except Exception as ex_size:
            print(f"[*] Note: Page size extent configuration ({page_size}): {ex_size}")

        # Set Color Intent (1=Color, 2=Grayscale, 4=B&W Text)
        try:
            intent_val = 1
            if color_mode.lower() in ("gray", "grayscale"):
                intent_val = 2
            elif color_mode.lower() in ("bw", "text", "mono", "monochrome"):
                intent_val = 4
            scan_item.Properties.Item("Current Intent").Value = intent_val
        except Exception:
            pass

        if os.path.exists(temp_bmp):
            os.remove(temp_bmp)

        # Transfer image data directly over USB with auto-fallback and busy retries
        image = None
        for transfer_attempt in range(3):
            try:
                image = scan_item.Transfer(WIA_FORMAT_BMP)
                break
            except Exception as ex_tr:
                err_str = str(ex_tr).lower()
                # Case A: Feeder tray is empty -> automatically scan from Flatbed glass platen
                if handling_val == 1 and (is_paper_empty_error(ex_tr) or "no documents left" in err_str or "-2145320957" in err_str):
                    print("[*] Feeder tray has no paper. Automatically scanning from Flatbed glass platen...")
                    try:
                        device.Properties.Item("Document Handling Select").Value = 2 # Flatbed
                        scan_item = device.Items.Item(1)
                        image = scan_item.Transfer(WIA_FORMAT_BMP)
                        break
                    except Exception as ex_fb:
                        raise ex_tr from ex_fb
                # Case B: Scanner carriage is returning home / busy
                elif "device is busy" in err_str or "-2145320954" in err_str:
                    if transfer_attempt < 2:
                        print(f"[*] Scanner is resetting/busy (attempt {transfer_attempt+1}/3). Waiting 2.0s...")
                        time.sleep(2.0)
                        continue
                raise ex_tr

        if image is None:
            raise RuntimeError("WIA Transfer failed to acquire image.")
        image.SaveFile(temp_bmp)

        # Release COM objects before file conversion
        image = None
        scan_item = None
        device = None
        dm = None

        # Convert and save with optimal compression and DPI tags
        img_mat = cv2.imread(temp_bmp)
        if img_mat is not None:
            if os.path.exists(output_filepath):
                os.remove(output_filepath)

            # Auto-crop if requested
            if page_size.lower() == "auto":
                try:
                    import stitch_daemon
                    cropped = stitch_daemon.auto_crop_borders(img_mat)
                    if cropped is not None and cropped.size > 0:
                        img_mat = cropped
                except Exception:
                    pass

            save_scanned_image(img_mat, output_filepath, dpi=dpi, quality=88)
            try:
                os.remove(temp_bmp)
            except Exception:
                pass
        else:
            if os.path.exists(output_filepath):
                os.remove(output_filepath)
            os.replace(temp_bmp, output_filepath)

        return f"{device_name} (USB WIA)"

    finally:
        image = None
        scan_item = None
        device = None
        scanner_info = None
        infos = None
        dm = None
        if pythoncom:
            pythoncom.CoUninitialize()


def get_connected_scanner_name(retries=2):
    """
    Detects and returns the active scanner name.
    Prioritizes fast direct USB cable connection; automatically refreshes WIA and checks network fallback if USB is offline.
    """
    if is_mock_scanner():
        return "HP Smart Tank 750 (Mock Isolated Scanner)"

    # 1. Check direct USB cable first with retries
    for attempt in range(max(1, retries)):
        pythoncom.CoInitialize()
        dm = None
        infos = None
        item = None
        try:
            dm = win32com.client.Dispatch("WIA.DeviceManager")
            infos = dm.DeviceInfos
            for i in range(1, infos.Count + 1):
                item = infos.Item(i)
                if item.Type == WIA_DEVICE_TYPE_SCANNER:
                    name = str(item.Properties.Item("Name").Value)
                    return f"{name} (Fast USB Cable)"
                item = None
            if infos.Count > 0:
                name = str(infos.Item(1).Properties.Item("Name").Value)
                return f"{name} (Fast USB Cable)"
        except Exception:
            pass
        finally:
            item = None
            infos = None
            dm = None
            pythoncom.CoUninitialize()

        if attempt < retries - 1:
            time.sleep(0.3)

    # 2. Check network eSCL as available zero-cable backup
    host = get_escl_host(timeout=0.6)
    if host:
        return f"HP Smart Tank 750 (Network Ready - Auto Fallback)"

    return None


def scan_to_file(output_filepath, dpi=DEFAULT_DPI, color_mode=DEFAULT_COLOR_MODE, source=DEFAULT_SOURCE, page_size=DEFAULT_PAGE_SIZE):
    """
    Scans a single pass directly to output_filepath.
    Uses high-speed direct USB cable connection as primary.
    If Windows ever drops the USB port, automatically falls back to direct network connection
    so your scan completes without forcing you to unplug/replug the physical cable!
    """
    if is_mock_scanner():
        return scan_mock_to_file(output_filepath, dpi=dpi, color_mode=color_mode, source=source, page_size=page_size)

    usb_error = None
    try:
        return scan_wia_usb_to_file(output_filepath, dpi=dpi, color_mode=color_mode, source=source, page_size=page_size)
    except Exception as e:
        usb_error = e
        print(f"[*] USB cable link issue: {e}. Attempting automated network fallback...")

    # Seamless fallback: if USB dropped, complete scan over direct local network
    host = get_escl_host(timeout=0.8)
    if host:
        try:
            print(f"[*] Seamlessly recovering via network ({host}) so you don't need to touch the cable...")
            return scan_escl_to_file(output_filepath, host, dpi=dpi, color_mode=color_mode, source=source, page_size=page_size)
        except Exception as net_err:
            print(f"[-] Network fallback attempt also failed: {net_err}")

    # If both failed, raise the detailed error
    err_msg = (
        f"Scan failed over both USB and Network.\n"
        f"USB Error: {usb_error}\n"
        f"[*] Check USB cable connection, or verify HP Smart Tank is connected to WiFi (192.168.68.69)."
    )
    raise RuntimeError(err_msg) from usb_error


def scan_mock_feeder_batch(out_dir, prefix, dpi=DEFAULT_DPI, color_mode=DEFAULT_COLOR_MODE, page_size=DEFAULT_PAGE_SIZE, page_callback=None, cancel_check=None):
    """
    Simulates feeding a multi-page document stack from Automatic Document Feeder (ADF)
    in full hardware isolation under MOCK_SCANNER=1.
    Generates 3 sequential pages (or MOCK_ADF_PAGES), saving each as PDF/PNG
    and triggering page_callback(path, page_num) in real-time.
    """
    os.makedirs(out_dir, exist_ok=True)
    num_pages = int(os.getenv("MOCK_ADF_PAGES", "3"))
    sim_delay = float(os.getenv("MOCK_SCAN_DELAY", "0.05"))

    ext = ".pdf"
    clean_prefix = prefix
    if clean_prefix.lower().endswith(".pdf"):
        clean_prefix = clean_prefix[:-4]
    elif clean_prefix.lower().endswith(".png"):
        ext = ".png"
        clean_prefix = clean_prefix[:-4]

    scanned_files = []
    size_key = normalize_page_size(page_size)
    w_in, h_in = PAGE_SIZES_INCHES.get(size_key, PAGE_SIZES_INCHES["a4"])
    target_w = int(round(w_in * dpi))
    target_h = int(round(h_in * dpi))

    archive_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Archive")
    sample_single = os.path.join(archive_dir, "Doc_003_P01_Single.png")

    for p in range(1, num_pages + 1):
        if cancel_check and cancel_check():
            print(f"[*] Mock ADF feeder batch cancelled at page {p}.")
            break

        if sim_delay > 0:
            time.sleep(sim_delay)

        page_path = os.path.join(out_dir, f"{clean_prefix}_P{p:02d}{ext}")

        # Generate page image
        img_mat = None
        if os.path.exists(sample_single):
            base_img = cv2.imread(sample_single)
            if base_img is not None:
                img_mat = base_img.copy()
                if size_key in PAGE_SIZES_INCHES and size_key != "auto":
                    img_mat = cv2.resize(img_mat, (target_w, target_h))

        if img_mat is None:
            img_mat = np.full((target_h, target_w, 3), 255, dtype=np.uint8)

        # Draw clear header badge and page content indicating ADF sheet
        cv2.rectangle(img_mat, (int(target_w * 0.04), int(target_h * 0.04)),
                              (int(target_w * 0.96), int(target_h * 0.96)), (215, 215, 215), 2)
        cv2.rectangle(img_mat, (int(target_w * 0.08), int(target_h * 0.08)),
                              (int(target_w * 0.92), int(target_h * 0.16)), (240, 245, 255), -1)
        cv2.putText(img_mat, f"ADF FEEDER BATCH - PAGE {p} OF {num_pages}",
                    (int(target_w * 0.1), int(target_h * 0.13)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0 * (dpi / 300.0), (30, 80, 180), 2)
        cv2.putText(img_mat, f"Paper Source: Feeder (ADF) | Resolution: {dpi} DPI | Size: {size_key.upper()}",
                    (int(target_w * 0.1), int(target_h * 0.20)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7 * (dpi / 300.0), (60, 60, 60), 2)

        for line_i in range(8):
            y_pos = int(target_h * (0.28 + line_i * 0.08))
            cv2.line(img_mat, (int(target_w * 0.1), y_pos), (int(target_w * 0.88), y_pos), (195, 195, 195), 2)

        # Apply color mode
        cm = str(color_mode).lower().strip()
        if cm in ("gray", "grayscale"):
            if len(img_mat.shape) == 3:
                img_mat = cv2.cvtColor(img_mat, cv2.COLOR_BGR2GRAY)
        elif cm in ("bw", "text", "mono", "monochrome"):
            if len(img_mat.shape) == 3:
                gray = cv2.cvtColor(img_mat, cv2.COLOR_BGR2GRAY)
            else:
                gray = img_mat
            _, img_mat = cv2.threshold(gray, 128, 255, cv2.THRESH_BINARY)

        save_scanned_image(img_mat, page_path, dpi=dpi, quality=88)
        scanned_files.append(page_path)

        if page_callback:
            page_callback(page_path, p)

    return scanned_files


def scan_wia_usb_feeder_batch(out_dir, prefix, dpi=DEFAULT_DPI, color_mode=DEFAULT_COLOR_MODE, page_size=DEFAULT_PAGE_SIZE, page_callback=None, cancel_check=None):
    """
    Directly commands the Automatic Document Feeder (ADF) via Windows Image Acquisition (WIA).
    Loops scan_item.Transfer(WIA_FORMAT_BMP) until paper is exhausted (WIA_ERROR_PAPER_EMPTY / 0x80210003),
    saving each page in real-time and calling page_callback(path, page_num).
    """
    if is_mock_scanner():
        return scan_mock_feeder_batch(out_dir, prefix, dpi=dpi, color_mode=color_mode, page_size=page_size, page_callback=page_callback, cancel_check=cancel_check)

    if pythoncom:
        pythoncom.CoInitialize()
    dm = None
    infos = None
    scanner_info = None
    device = None
    scan_item = None
    scanned_files = []

    ext = ".pdf"
    clean_prefix = prefix
    if clean_prefix.lower().endswith(".pdf"):
        clean_prefix = clean_prefix[:-4]
    elif clean_prefix.lower().endswith(".png"):
        ext = ".png"
        clean_prefix = clean_prefix[:-4]

    try:
        dm = win32com.client.Dispatch("WIA.DeviceManager")
        infos = dm.DeviceInfos

        for i in range(1, infos.Count + 1):
            item = infos.Item(i)
            if item.Type == WIA_DEVICE_TYPE_SCANNER:
                scanner_info = item
                break

        if not scanner_info and infos.Count > 0:
            scanner_info = infos.Item(1)

        if not scanner_info:
            raise RuntimeError(
                "HP Smart Tank feeder was not detected by Windows over USB.\n"
                "[*] Check USB cable connection, or use the direct Wireless Network mode."
            )

        device = scanner_info.Connect()

        # Set Document Handling Select: 1 = Feeder/ADF
        try:
            device.Properties.Item("Document Handling Select").Value = 1
        except Exception as ex_h:
            try:
                device.Properties.Item(3088).Value = 1
            except Exception:
                try:
                    device.Properties.Item("3088").Value = 1
                except Exception:
                    print(f"[*] Note: Document Handling Select (Feeder): {ex_h}")

        # In WIA standard, Pages = 0 instructs the feeder driver to scan ALL pages continuously until empty
        try:
            device.Properties.Item("Pages").Value = 0
        except Exception:
            try:
                device.Properties.Item(3096).Value = 0
            except Exception:
                try:
                    device.Properties.Item("3096").Value = 0
                except Exception:
                    try:
                        device.Properties.Item("Pages").Value = 1
                    except Exception:
                        pass

        scan_item = device.Items.Item(1)

        # Set DPI Resolution
        try:
            scan_item.Properties.Item("Horizontal Resolution").Value = dpi
            scan_item.Properties.Item("Vertical Resolution").Value = dpi
        except Exception:
            pass

        # Set Scan Size / Page Extent
        try:
            p_horiz_ext = scan_item.Properties.Item("Horizontal Extent")
            p_vert_ext = scan_item.Properties.Item("Vertical Extent")
            p_horiz_start = scan_item.Properties.Item("Horizontal Start Position")
            p_vert_start = scan_item.Properties.Item("Vertical Start Position")

            p_horiz_start.Value = 0
            p_vert_start.Value = 0

            try:
                max_w = int(p_horiz_ext.SubTypeMax)
            except Exception:
                max_w = int(round(8.5 * dpi))
            try:
                max_h = int(p_vert_ext.SubTypeMax)
            except Exception:
                max_h = int(round(14.0 * dpi))

            size_key = normalize_page_size(page_size)
            if size_key == "max":
                p_horiz_ext.Value = min(max_w, int(round(8.5 * dpi)))
                p_vert_ext.Value = min(max_h, int(round(14.0 * dpi)))
            elif size_key in PAGE_SIZES_INCHES and size_key != "auto":
                w_in, h_in = PAGE_SIZES_INCHES[size_key]
                target_w = min(max_w, int(round(w_in * dpi)))
                target_h = min(max_h, int(round(h_in * dpi)))
                p_horiz_ext.Value = target_w
                p_vert_ext.Value = target_h
            else:
                p_horiz_ext.Value = max_w
                p_vert_ext.Value = max_h
        except Exception as ex_size:
            print(f"[*] Note: Feeder size configuration: {ex_size}")

        # Set Color Intent (1=Color, 2=Grayscale, 4=B&W Text)
        try:
            intent_val = 1
            if color_mode.lower() in ("gray", "grayscale"):
                intent_val = 2
            elif color_mode.lower() in ("bw", "text", "mono", "monochrome"):
                intent_val = 4
            scan_item.Properties.Item("Current Intent").Value = intent_val
        except Exception:
            pass

        page_num = 1
        while True:
            if cancel_check and cancel_check():
                print(f"[*] ADF Feeder batch cancelled before page {page_num}.")
                break

            temp_bmp = os.path.join(out_dir, f"{clean_prefix}_tmp_{page_num}.bmp")
            if os.path.exists(temp_bmp):
                try:
                    os.remove(temp_bmp)
                except Exception:
                    pass

            image = None
            try:
                # Transfer page from feeder
                image = scan_item.Transfer(WIA_FORMAT_BMP)
                image.SaveFile(temp_bmp)
            except Exception as ex_t:
                if is_paper_empty_error(ex_t):
                    if page_num == 1:
                        raise RuntimeError("Automatic Document Feeder (ADF) is empty. Please place pages in the feeder tray.") from ex_t
                    print(f"[*] ADF Feeder empty after {page_num - 1} page(s). Feeder batch complete.")
                    break
                raise ex_t
            finally:
                image = None

            page_filename = f"{clean_prefix}_P{page_num:02d}{ext}"
            page_path = os.path.join(out_dir, page_filename)

            # Convert and save
            img_mat = cv2.imread(temp_bmp)
            if img_mat is not None:
                if page_size.lower() == "auto":
                    try:
                        import stitch_daemon
                        cropped = stitch_daemon.auto_crop_borders(img_mat)
                        if cropped is not None and cropped.size > 0:
                            img_mat = cropped
                    except Exception:
                        pass
                save_scanned_image(img_mat, page_path, dpi=dpi, quality=88)
                try:
                    os.remove(temp_bmp)
                except Exception:
                    pass
            else:
                os.replace(temp_bmp, page_path)

            scanned_files.append(page_path)
            if page_callback:
                page_callback(page_path, page_num)

            page_num += 1

        return scanned_files

    finally:
        scan_item = None
        device = None
        scanner_info = None
        infos = None
        dm = None
        if pythoncom:
            pythoncom.CoUninitialize()


def scan_escl_feeder_batch(out_dir, prefix, host, dpi=DEFAULT_DPI, color_mode=DEFAULT_COLOR_MODE, page_size=DEFAULT_PAGE_SIZE, page_callback=None, cancel_check=None):
    """
    Scans a batch of pages from HP Smart Tank ADF feeder over local network via eSCL protocol.
    Continuously retrieves NextDocument until 404/410/empty, saving each page in real-time.
    """
    if is_mock_scanner():
        return scan_mock_feeder_batch(out_dir, prefix, dpi=dpi, color_mode=color_mode, page_size=page_size, page_callback=page_callback, cancel_check=cancel_check)

    job_url = f"http://{host}/eSCL/ScanJobs"
    cm_str = "RGB24"
    if color_mode.lower() in ("gray", "grayscale"):
        cm_str = "Grayscale8"
    elif color_mode.lower() in ("bw", "text", "mono", "monochrome"):
        cm_str = "BlackAndWhite1"

    size_key = normalize_page_size(page_size)
    scan_region_xml = ""
    if size_key in PAGE_SIZES_INCHES and size_key != "auto":
        w_in, h_in = PAGE_SIZES_INCHES[size_key]
        reg_w = min(2550, int(round(w_in * 300)))
        reg_h = min(4200, int(round(h_in * 300)))
        scan_region_xml = f"""  <scan:ScanRegions>
    <scan:ScanRegion>
      <scan:XOffset>0</scan:XOffset>
      <scan:YOffset>0</scan:YOffset>
      <scan:Width>{reg_w}</scan:Width>
      <scan:Height>{reg_h}</scan:Height>
    </scan:ScanRegion>
  </scan:ScanRegions>
"""

    settings_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <scan:Intent>Document</scan:Intent>
  <pwg:InputSource>Feeder</pwg:InputSource>
  <scan:InputSource>Feeder</scan:InputSource>
  <scan:ColorMode>{cm_str}</scan:ColorMode>
  <scan:XResolution>{dpi}</scan:XResolution>
  <scan:YResolution>{dpi}</scan:YResolution>
  <pwg:DocumentFormat>application/pdf</pwg:DocumentFormat>
  <scan:DocumentFormatExt>application/pdf</scan:DocumentFormatExt>
{scan_region_xml}</scan:ScanSettings>
"""
    req = urllib.request.Request(
        job_url,
        data=settings_xml.encode("utf-8"),
        headers={"Content-Type": "text/xml", "User-Agent": "ScanStitcher/1.0"}
    )

    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 201:
            raise RuntimeError(f"Scanner rejected feeder job with status {resp.status}")
        location = resp.headers.get("Location")
        if not location:
            raise RuntimeError("Scanner did not return Job Location header.")

    if location.startswith("http"):
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(location)
        doc_url = urlunparse((parsed.scheme or "http", host, parsed.path, parsed.params, parsed.query, parsed.fragment))
    else:
        doc_url = f"http://{host}" + ("/" if not location.startswith("/") else "") + location
    doc_url = doc_url.rstrip("/") + "/NextDocument"

    ext = ".pdf"
    clean_prefix = prefix
    if clean_prefix.lower().endswith(".pdf"):
        clean_prefix = clean_prefix[:-4]
    elif clean_prefix.lower().endswith(".png"):
        ext = ".png"
        clean_prefix = clean_prefix[:-4]

    scanned_files = []
    page_num = 1

    while True:
        if cancel_check and cancel_check():
            break

        # Give physical ADF feeder roller time between consecutive sheets to engage the next page
        if page_num > 1:
            time.sleep(2.0)

        img_data = None
        consecutive_404 = 0
        for attempt in range(35):
            if cancel_check and cancel_check():
                break
            try:
                req_doc = urllib.request.Request(doc_url, headers={"User-Agent": "ScanStitcher/1.0"})
                with urllib.request.urlopen(req_doc, timeout=40) as doc_resp:
                    if doc_resp.status == 200:
                        chunk = doc_resp.read()
                        if chunk and len(chunk) > 1000:
                            img_data = chunk
                            break
                    elif doc_resp.status in (404, 410):
                        consecutive_404 += 1
                        if consecutive_404 >= 6:
                            img_data = None
                            break
                        time.sleep(1.2)
                    elif doc_resp.status in (503, 409):
                        # Hardware feeding/scanning in progress
                        time.sleep(1.5)
            except urllib.error.HTTPError as he:
                if he.code in (404, 410):
                    consecutive_404 += 1
                    # Do NOT abort on the first 404! The roller takes 2-4 seconds to feed the next sheet
                    if consecutive_404 >= 6:
                        img_data = None
                        break
                    time.sleep(1.2)
                elif he.code in (503, 409):
                    time.sleep(1.5)
                else:
                    time.sleep(1.0)
            except Exception:
                time.sleep(1.0)

        if not img_data:
            if page_num == 1:
                raise RuntimeError("Document Feeder (ADF) is empty or paper was not detected.")
            # Normal completion when all pages in feeder have been scanned
            print(f"[*] ADF Feeder batch complete. Total pages scanned: {page_num - 1}")
            break

        img = decode_scanned_bytes_to_cv2(img_data, dpi=dpi)
        if img is None:
            print(f"[-] Could not decode image payload for feeder page {page_num}, finishing batch.")
            break

        if size_key == "auto":
            try:
                import stitch_daemon
                cropped = stitch_daemon.auto_crop_borders(img)
                if cropped is not None and cropped.size > 0:
                    img = cropped
            except Exception:
                pass

        page_filename = f"{clean_prefix}_P{page_num:02d}{ext}"
        page_path = os.path.join(out_dir, page_filename)
        save_scanned_image(img, page_path, dpi=dpi, quality=88)

        scanned_files.append(page_path)
        if page_callback:
            page_callback(page_path, page_num)

        page_num += 1

    return scanned_files


def scan_feeder_batch_to_files(out_dir, prefix, dpi=DEFAULT_DPI, color_mode=DEFAULT_COLOR_MODE, page_size=DEFAULT_PAGE_SIZE, page_callback=None, cancel_check=None):
    """
    Scans a multi-page stack from Automatic Document Feeder (ADF) to out_dir.
    Streams each page as it finishes via page_callback(page_path, page_num).
    Automatically uses fast direct USB cable connection with network eSCL fallback.
    Cleanly handles WIA_ERROR_PAPER_EMPTY when paper is exhausted.
    """
    if is_mock_scanner():
        return scan_mock_feeder_batch(out_dir, prefix, dpi=dpi, color_mode=color_mode, page_size=page_size, page_callback=page_callback, cancel_check=cancel_check)

    usb_err = None
    try:
        return scan_wia_usb_feeder_batch(out_dir, prefix, dpi=dpi, color_mode=color_mode, page_size=page_size, page_callback=page_callback, cancel_check=cancel_check)
    except Exception as e:
        usb_err = e
        print(f"[*] USB Feeder batch issue: {e}. Attempting network eSCL fallback...")

    host = get_escl_host(timeout=0.8)
    if host:
        try:
            print(f"[*] Executing ADF feeder batch over network ({host})...")
            return scan_escl_feeder_batch(out_dir, prefix, host, dpi=dpi, color_mode=color_mode, page_size=page_size, page_callback=page_callback, cancel_check=cancel_check)
        except Exception as net_err:
            print(f"[-] Network ADF feeder fallback also failed: {net_err}")

    raise RuntimeError(f"ADF Feeder batch scan failed over both USB and Network: {usb_err}") from usb_err


if __name__ == "__main__":
    scanner_name = get_connected_scanner_name()
    if scanner_name:
        print(f"Connected Scanner: {scanner_name}")
    else:
        print("No scanner detected.")
