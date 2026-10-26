import os
import sys
import time
import subprocess
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102

VK_F7 = 0x76
VK_F8 = 0x77
VK_F9 = 0x78
VK_ESCAPE = 0x1B
VK_C = 0x43

def find_pdfarranger_hwnd():
    found_hwnd = [None]
    def enum_cb(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                title = buff.value
                if "PDF Arranger" in title or "pdfarranger" in title.lower():
                    found_hwnd[0] = hwnd
                    return False
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumWindowsProc(enum_cb), 0)
    return found_hwnd[0]

def send_key(hwnd, vk):
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, 0)

def main():
    log_file = os.path.join(r"c:\Users\Admin\Documents\automated scan stitcher", "scanner_extension.log")
    start_pos = 0
    if os.path.exists(log_file):
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            start_pos = f.tell()

    exe_path = r"C:\Users\Admin\AppData\Local\Programs\PDF Arranger\pdfarranger.exe"
    env = os.environ.copy()
    env["MOCK_SCANNER"] = "1"
    env["MOCK_SCAN_DELAY"] = "0.05"

    print(f"Launching {exe_path}...")
    proc = subprocess.Popen([exe_path], env=env)

    try:
        hwnd = None
        for _ in range(20):
            time.sleep(0.5)
            hwnd = find_pdfarranger_hwnd()
            if hwnd:
                break

        if not hwnd:
            print("ERROR: Could not find PDF Arranger window!")
            return 1

        print(f"PDF Arranger Window HWND: {hwnd}")
        user32.SetForegroundWindow(hwnd)
        time.sleep(1.0)

        # 1. Test F7
        print("Sending F7...")
        send_key(hwnd, VK_F7)
        time.sleep(0.8)

        # 2. Test Escape
        print("Sending Escape...")
        send_key(hwnd, VK_ESCAPE)
        time.sleep(0.8)

        # 3. Test F8
        print("Sending F8...")
        send_key(hwnd, VK_F8)
        time.sleep(1.2)

        # 4. Test F9
        print("Sending F9...")
        send_key(hwnd, VK_F9)
        time.sleep(0.8)

        # 5. Test C
        print("Sending C...")
        send_key(hwnd, VK_C)
        time.sleep(0.8)

        # Dismiss dialog with Escape
        print("Sending Escape to dismiss any modal dialog...")
        send_key(hwnd, VK_ESCAPE)
        time.sleep(0.5)

    finally:
        print("Closing PDF Arranger process...")
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    # Read log
    print("\n================ NEW LOG OUTPUT ================")
    new_log = ""
    if os.path.exists(log_file):
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            f.seek(start_pos)
            new_log = f.read()
            print(new_log)

    f7_ok = "Window key F7 received" in new_log and "Scan 2-Part (F7) activated" in new_log
    esc_ok = "Escape key received" in new_log
    f8_ok = "Window key F8 received" in new_log and "Scan Single" in new_log
    f9_ok = "Window key F9 received" in new_log
    c_ok = "Window key C received" in new_log and "Adjust Crop" in new_log

    print("\n================ VERIFICATION MATRIX ================")
    print(f"[*] Hotkey F7 (Scan 2-Part) responded:            {f7_ok}")
    print(f"[*] Hotkey Escape (Cancel Scan) responded:        {esc_ok}")
    print(f"[*] Hotkey F8 (Single / Feeder Scan) responded:   {f8_ok}")
    print(f"[*] Hotkey F9 (Stitch Selected) responded:        {f9_ok}")
    print(f"[*] Hotkey C (Adjust Crop / Margins) responded:   {c_ok}")

    all_passed = f7_ok and esc_ok and f8_ok and f9_ok and c_ok
    if all_passed:
        print("\nSUCCESS: All 5 hotkeys responded properly to the running PDF Arranger application!")
        return 0
    else:
        print("\nFAILURE: Some hotkeys did not respond properly.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
