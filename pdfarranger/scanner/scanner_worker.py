"""
Standalone Scanner Worker for PDF Arranger Integration.
Runs in Python 3.10 with OpenCV, NumPy, WIA, and eSCL support.
"""

import os
import sys
import json
import argparse
import traceback
import io
import cv2
import numpy as np
from PIL import Image

# Ensure workspace is in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import wia_scanner
import stitch_daemon

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None


def do_check_status():
    try:
        name = wia_scanner.get_connected_scanner_name()
        if name:
            return {"success": True, "connected": True, "name": name}
        else:
            return {"success": True, "connected": False, "name": "No scanner detected"}
    except Exception as e:
        return {"success": False, "connected": False, "error": str(e)}


def do_scan_part(output_path, dpi=300, color_mode="color", source="flatbed", page_size="a4"):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        wia_scanner.play_sound("start", wait=False)
        scanner_name = wia_scanner.scan_to_file(output_path, dpi=dpi, color_mode=color_mode, source=source, page_size=page_size)
        wia_scanner.play_sound("part1", wait=False)
        return {
            "success": True,
            "path": output_path,
            "scanner": scanner_name,
            "part": 1,
            "dpi": dpi,
            "color_mode": color_mode,
            "source": source,
            "page_size": page_size
        }
    except Exception as e:
        wia_scanner.play_sound("error", wait=False)
        return {"success": False, "error": str(e), "traceback": traceback.format_exc()}


def do_stitch(part1_path, part2_path, output_path, dpi=300):
    try:
        if not os.path.exists(part1_path):
            raise FileNotFoundError(f"Part 1 file not found: {part1_path}")
        if not os.path.exists(part2_path):
            raise FileNotFoundError(f"Part 2 file not found: {part2_path}")

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        stitched = stitch_daemon.stitch_pair(part1_path, part2_path)
        if stitched is None:
            raise ValueError("Stitching failed to produce an image.")

        wia_scanner.save_scanned_image(stitched, output_path, dpi=dpi, quality=88)

        wia_scanner.play_sound("part2", wait=False)
        return {
            "success": True,
            "path": output_path,
            "dpi": dpi
        }
    except Exception as e:
        wia_scanner.play_sound("error", wait=False)
        return {"success": False, "error": str(e), "traceback": traceback.format_exc()}


def do_scan_single(output_path, dpi=300, color_mode="color", source="flatbed", page_size="a4"):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        wia_scanner.play_sound("start", wait=False)

        # Temporary scan path to allow border cropping before final format compression
        ext = os.path.splitext(output_path)[1].lower()
        temp_scan = output_path if ext == ".png" else output_path + ".temp.png"

        scanner_name = wia_scanner.scan_to_file(temp_scan, dpi=dpi, color_mode=color_mode, source=source, page_size=page_size)

        # Load and auto-crop dark scanner platen borders if needed
        img = stitch_daemon.load_image(temp_scan)
        if img is not None:
            cropped = stitch_daemon.auto_crop_borders(img) if page_size.lower() in ("auto", "a4", "letter") else img
            wia_scanner.save_scanned_image(cropped, output_path, dpi=dpi, quality=88)
        else:
            if temp_scan != output_path:
                os.replace(temp_scan, output_path)

        if temp_scan != output_path and os.path.exists(temp_scan):
            try:
                os.remove(temp_scan)
            except Exception:
                pass

        wia_scanner.play_sound("part2", wait=False)
        return {
            "success": True,
            "path": output_path,
            "scanner": scanner_name,
            "dpi": dpi,
            "color_mode": color_mode,
            "source": source,
            "page_size": page_size
        }
    except Exception as e:
        wia_scanner.play_sound("error", wait=False)
        return {"success": False, "error": str(e), "traceback": traceback.format_exc()}


def do_scan_feeder_batch(out_dir, prefix, dpi=300, color_mode="color", page_size="a4"):
    try:
        os.makedirs(out_dir, exist_ok=True)
        wia_scanner.play_sound("start", wait=False)

        def on_page_ready(path, page_num):
            msg = json.dumps({"page": page_num, "path": path})
            print(f"__PAGE_READY__{msg}", flush=True)

        pages = wia_scanner.scan_feeder_batch_to_files(
            out_dir=out_dir,
            prefix=prefix,
            dpi=dpi,
            color_mode=color_mode,
            page_size=page_size,
            page_callback=on_page_ready
        )

        wia_scanner.play_sound("finish_doc", wait=False)
        return {
            "success": True,
            "pages": len(pages),
            "files": pages,
            "dpi": dpi,
            "color_mode": color_mode,
            "page_size": page_size
        }
    except Exception as e:
        wia_scanner.play_sound("error", wait=False)
        return {"success": False, "error": str(e), "traceback": traceback.format_exc()}


def do_optimize_pdf(input_pdf, output_pdf=None, dpi=300, quality=85):
    """
    Optimizes a heavy PDF document:
    - Normalizes oversized 72/96 DPI uncompressed pages back to standard 300 DPI bounding boxes.
    - Compresses raw raster streams to lightweight JPEG (quality 85).
    - Typically reduces file size by 85-90% and eliminates rendering latency.
    """
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz

        if not os.path.exists(input_pdf):
            raise FileNotFoundError(f"File not found: {input_pdf}")

        if output_pdf is None:
            output_pdf = input_pdf

        temp_out = output_pdf + ".tmp.pdf"
        src_doc = fitz.open(input_pdf)
        opt_doc = fitz.open()

        for page in src_doc:
            imgs = page.get_images()
            if imgs:
                xref = imgs[0][0]
                base_img = src_doc.extract_image(xref)
                image_bytes = base_img["image"]
                im = Image.open(io.BytesIO(image_bytes))
            else:
                pix = page.get_pixmap(dpi=150)
                im = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")

            bio = io.BytesIO()
            im.save(bio, "PDF", resolution=float(dpi), quality=quality, optimize=True)
            pdoc = fitz.open("pdf", bio.getvalue())
            opt_doc.insert_pdf(pdoc)
            pdoc.close()

        opt_doc.save(temp_out, deflate=True)
        opt_doc.close()
        src_doc.close()

        if os.path.exists(output_pdf):
            os.remove(output_pdf)
        os.replace(temp_out, output_pdf)

        new_size = os.path.getsize(output_pdf)
        return {"success": True, "path": output_pdf, "size_bytes": new_size}
    except Exception as e:
        return {"success": False, "error": str(e), "traceback": traceback.format_exc()}


def main():
    parser = argparse.ArgumentParser(description="PDF Arranger Scanner Worker")
    subparsers = parser.add_subparsers(dest="command")

    # check-status
    subparsers.add_parser("check-status")

    # scan-part
    p_part = subparsers.add_parser("scan-part")
    p_part.add_argument("--out", required=True, help="Output image file path")
    p_part.add_argument("--dpi", type=int, default=300, help="Scan resolution (150, 300, 600)")
    p_part.add_argument("--color-mode", default="color", help="Color mode (color, gray, bw)")
    p_part.add_argument("--source", default="flatbed", help="Scan source (flatbed, feeder)")
    p_part.add_argument("--page-size", default="a4", help="Page scan size (a4, letter, legal, photo_4x6, auto, max)")

    # stitch
    p_stitch = subparsers.add_parser("stitch")
    p_stitch.add_argument("--part1", required=True, help="Part 1 image path")
    p_stitch.add_argument("--part2", required=True, help="Part 2 image path")
    p_stitch.add_argument("--out", required=True, help="Output stitched image/pdf path")
    p_stitch.add_argument("--dpi", type=int, default=300, help="Resolution (150, 300, 600)")

    # scan-single
    p_single = subparsers.add_parser("scan-single")
    p_single.add_argument("--out", required=True, help="Output image/pdf path")
    p_single.add_argument("--dpi", type=int, default=300, help="Scan resolution (150, 300, 600)")
    p_single.add_argument("--color-mode", default="color", help="Color mode (color, gray, bw)")
    p_single.add_argument("--source", default="flatbed", help="Scan source (flatbed, feeder)")
    p_single.add_argument("--page-size", default="a4", help="Page scan size (a4, letter, legal, photo_4x6, auto, max)")

    # scan-feeder-batch
    p_batch = subparsers.add_parser("scan-feeder-batch")
    p_batch.add_argument("--out-dir", required=True, help="Output directory for scanned pages")
    p_batch.add_argument("--prefix", default="Scan_ADF", help="Filename prefix")
    p_batch.add_argument("--dpi", type=int, default=300, help="Scan resolution (150, 300, 600)")
    p_batch.add_argument("--color-mode", default="color", help="Color mode (color, gray, bw)")
    p_batch.add_argument("--page-size", default="a4", help="Page scan size (a4, letter, legal, photo_4x6, auto, max)")

    # optimize-doc
    p_opt = subparsers.add_parser("optimize-doc")
    p_opt.add_argument("--in", dest="in_pdf", required=True, help="Input PDF file path")
    p_opt.add_argument("--out", dest="out_pdf", default=None, help="Output PDF file path")
    p_opt.add_argument("--dpi", type=int, default=300, help="Target resolution (default 300)")

    args = parser.parse_args()

    if args.command == "check-status":
        res = do_check_status()
    elif args.command == "scan-part":
        res = do_scan_part(args.out, dpi=args.dpi, color_mode=args.color_mode, source=args.source, page_size=args.page_size)
    elif args.command == "stitch":
        res = do_stitch(args.part1, args.part2, args.out, dpi=args.dpi)
    elif args.command == "scan-single":
        res = do_scan_single(args.out, dpi=args.dpi, color_mode=args.color_mode, source=args.source, page_size=args.page_size)
    elif args.command == "scan-feeder-batch":
        res = do_scan_feeder_batch(args.out_dir, prefix=args.prefix, dpi=args.dpi, color_mode=args.color_mode, page_size=args.page_size)
    elif args.command == "optimize-doc":
        res = do_optimize_pdf(args.in_pdf, output_pdf=args.out_pdf, dpi=args.dpi)
    else:
        res = {"success": False, "error": f"Unknown command: {args.command}"}

    # Print output as a single-line JSON with marker prefix
    print("__RESULT__" + json.dumps(res), flush=True)


if __name__ == "__main__":
    main()
