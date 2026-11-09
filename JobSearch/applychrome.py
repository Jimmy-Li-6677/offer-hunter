"""Apply window of the job-only Chrome profile: open it behind everything, keep it there, close it when the job is done.

  python applychrome.py open     start the job Chrome profile (if it is not running) WITHOUT taking focus and put its
                                 windows at the very bottom of the window stack - reachable from the taskbar, never in
                                 front of what the user is working in
  python applychrome.py keep     (NOT used by the workflow: re-stacking the window every 1.5 s made it flash and stall
                                 forms) loop that keeps pushing the windows to the bottom
  python applychrome.py back     push the windows to the bottom once
  python applychrome.py close    close every window of the job profile (Chrome exits with the last window)
  python applychrome.py status   print the windows / process ids

The browser Claude controls is always this profile (config launcher.chromeProfileDir); the orange frame + "Claude"
tab group from the extension mark its tabs. Windows only (ctypes).
"""
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

user32 = ctypes.windll.user32
HWND_BOTTOM = 1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
WM_CLOSE = 0x0010
SW_SHOWNOACTIVATE = 4


def config():
    with open(os.path.join(REPO, "private", "job-search-config.json"), encoding="utf-8") as f:
        return json.load(f).get("launcher", {})


def profile_dir():
    p = config().get("chromeProfileDir") or r"private\chrome-job-profile"
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(REPO, p))


def chrome_path():
    return config().get("chromePath") or r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def main_pids():
    """Process ids of the profile's browser process (the one without --type=)."""
    prof = profile_dir().replace("'", "''")
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object "
          "{ $_.CommandLine -like '*--user-data-dir=*' -and $_.CommandLine -notlike '*--type=*' -and "
          f"$_.CommandLine -like '*{prof}*' }} | ForEach-Object {{ $_.ProcessId }}")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout
    return {int(x) for x in out.split() if x.isdigit()}


def windows(pids):
    found = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                cls = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, cls, 64)
                if cls.value == "Chrome_WidgetWin_1":
                    found.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found


def to_bottom(hwnds):
    for h in hwnds:
        user32.SetWindowPos(h, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def cmd_open():
    pids = main_pids()
    if not pids:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = SW_SHOWNOACTIVATE
        # The window sits covered by other windows: without these flags Chrome pauses rendering / input of covered
        # windows (a stalled form, blank screenshots).
        subprocess.Popen([chrome_path(), f"--user-data-dir={profile_dir()}",
                          "--disable-features=CalculateNativeWinOcclusion",
                          "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
                          "--disable-background-timer-throttling", "--new-window", "about:blank"],
                         startupinfo=si)
        for _ in range(40):
            time.sleep(0.5)
            pids = main_pids()
            if pids and windows(pids):
                break
    for _ in range(6):  # the first window appears a moment after the process
        to_bottom(windows(pids))
        time.sleep(0.4)
    print(f"job Chrome open (pids {sorted(pids)}), windows pushed to the bottom: {len(windows(pids))}")


def cmd_keep():
    seen = False
    misses = 0
    while True:
        pids = main_pids()
        w = windows(pids) if pids else []
        if w:
            seen, misses = True, 0
            for _ in range(3):
                to_bottom(w)
                time.sleep(0.5)
        else:
            misses += 1
            if seen and misses >= 3:
                print("job Chrome closed - keep loop ends")
                return
            if not seen and misses >= 40:
                print("no job Chrome window appeared - keep loop ends")
                return
            time.sleep(1.5)


def cmd_close():
    pids = main_pids()
    w = windows(pids)
    for h in w:
        user32.PostMessageW(h, WM_CLOSE, 0, 0)
    for _ in range(20):
        time.sleep(0.5)
        if not main_pids():
            break
    print(f"closed {len(w)} window(s); job Chrome {'still running' if main_pids() else 'stopped'}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "open":
        cmd_open()
    elif cmd == "keep":
        cmd_keep()
    elif cmd == "back":
        to_bottom(windows(main_pids()))
    elif cmd == "close":
        cmd_close()
    elif cmd == "status":
        p = main_pids()
        print(f"profile {profile_dir()}\npids {sorted(p)}  windows {len(windows(p))}")
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
