# PDF Arranger package initialization

# Auto-initialize hardware scanner & stitcher extension if available
try:
    from pdfarranger.scanner import install_hook as _install_scanner_hook
    _install_scanner_hook()
except Exception:
    pass
