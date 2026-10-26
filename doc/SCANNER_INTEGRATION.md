# PDF Arranger Background Scanning & SIFT Stitching Integration

## Overview
This subsystem integrates physical hardware scanning (Network eSCL AirScan and USB WIA 2.0) with non-destructive SIFT image stitching directly inside PDF Arranger.

### Key Capabilities
1. **Dual-Pass Stitch Scanning (`F7`)**:
   - Captures oversized pages (e.g., Legal, Ledger, large contracts) in two scans (top and bottom half).
   - Computer vision engine extracts 4,000 SIFT keypoints with BFMatcher and RANSAC affine transformation.
   - Automatically handles 180° rotation invariance when flipping documents on the platen glass.
   - Smooth linear seam blending and native 300 DPI DCT-compressed PDF page insertion.

2. **Single-Pass Scanning (`F8`)**:
   - Fast direct capture for standard A4, Letter, Photo (4×6"), or custom formats.
   - One-click Part 1 promotion (converts a partial scan into a completed single page).

3. **Multi-Page ADF Feeder Scanning**:
   - Supports continuous automated document feeder (ADF) batch scanning.
   - Dynamic page acquisition with inter-sheet mechanical roller delay and paper empty detection.

4. **Manual Page Stitching (`F9`)**:
   - Select any two pages in the document grid and stitch them together asynchronously.

5. **Document Optimization (`Ctrl+Alt+O`)**:
   - Auto-deskew, auto-crop borders, and recompression with live size reduction calculation.

6. **Non-Destructive Vector Cropping**:
   - Margins are adjusted via PDF `CropBox` (`Sides(l, r, t, b)`). Full scan rasters are preserved intact. Clicking `↺ Reset Crop` restores original borders.

7. **Audio Feedback**:
   - Hardware scan start beep, Part 1 chime, Part 2 completion chime, and celebration chime on export.

8. **Quick Settings & Disk Management**:
   - In-app popover to change DPI (150/300/600), Color Mode (Color/Gray/BW), Paper Size, and Platen vs. Feeder.
   - 1-click `🧹 Clean Temp Cache` to purge transient staging buffers without touching authentic documents.
