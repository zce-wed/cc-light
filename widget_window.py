"""Present a premultiplied BGRA frame on the Tk window using desktop composition."""

import ctypes as ct
from ctypes import wintypes as wt
import struct
import tkinter as tk


class TkBitmap:
    """Canvas presentation when the native compositor is unavailable."""
    def __init__(self, canvas, width, height):
        self.canvas = canvas
        self.item = canvas.create_image(0, 0, anchor="nw")
        self.header = ("P6\n%d %d\n255\n" % (width, height)).encode("ascii")
        try:
            canvas.winfo_toplevel().attributes("-transparentcolor", "#010109")
            self.background = (1, 1, 9)
        except tk.TclError:
            self.background = (28, 32, 42)

    def present(self, frame):
        rgb = bytearray()
        for b, g, r, a in struct.iter_unpack("4B", frame):
            if a < 128:
                rgb.extend(self.background)
            elif a == 255:
                rgb.extend((r, g, b))
            else:
                rgb.extend(round(v * 255 / a) for v in (r, g, b))
        self.image = tk.PhotoImage(master=self.canvas, data=self.header + rgb, format="PPM")
        self.canvas.itemconfigure(self.item, image=self.image)


class LayeredWindow:
    def __init__(self, root, width, height):
        self.root = root
        self.size = wt.SIZE(width, height)
        self.origin = wt.POINT(0, 0)
        # AC_SRC_OVER, flags, overall opacity, AC_SRC_ALPHA.
        self.blend = (ct.c_ubyte * 4)(0, 0, 255, 1)
        # Top-down 32-bit BI_RGB DIB.
        self.header = ct.create_string_buffer(struct.pack(
            "<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0))

        self.user = ct.WinDLL("user32", use_last_error=True)
        self.gdi = ct.WinDLL("gdi32", use_last_error=True)
        for fn, result, args in (
            (self.user.GetAncestor, wt.HWND, [wt.HWND, wt.UINT]),
            (self.user.GetWindowLongW, wt.LONG, [wt.HWND, ct.c_int]),
            (self.user.SetWindowLongW, wt.LONG, [wt.HWND, ct.c_int, wt.LONG]),
            (self.user.UpdateLayeredWindow, wt.BOOL, [wt.HWND, wt.HDC, ct.c_void_p,
             ct.POINTER(wt.SIZE), wt.HDC, ct.POINTER(wt.POINT), wt.COLORREF, ct.c_void_p, wt.DWORD]),
            (self.gdi.CreateCompatibleDC, wt.HDC, [wt.HDC]),
            (self.gdi.CreateDIBSection, wt.HBITMAP, [wt.HDC, ct.c_void_p, wt.UINT,
             ct.POINTER(ct.c_void_p), wt.HANDLE, wt.DWORD]),
            (self.gdi.SelectObject, wt.HGDIOBJ, [wt.HDC, wt.HGDIOBJ]),
            (self.gdi.DeleteObject, wt.BOOL, [wt.HGDIOBJ]),
            (self.gdi.DeleteDC, wt.BOOL, [wt.HDC]),
        ):
            fn.restype, fn.argtypes = result, args

    def present(self, frame):
        if len(frame) != self.size.cx * self.size.cy * 4:
            raise ValueError("Incorrect layered window frame size")
        hwnd = self.user.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT: Tk's outer HWND
        style = self.user.GetWindowLongW(hwnd, -20)            # GWL_EXSTYLE
        if not style & 0x80000:                                # WS_EX_LAYERED
            self.user.SetWindowLongW(hwnd, -20, style | 0x80000)

        dc = self.gdi.CreateCompatibleDC(None)
        if not dc:
            raise ct.WinError(ct.get_last_error())
        bitmap = previous = None
        try:
            bits = ct.c_void_p()
            bitmap = self.gdi.CreateDIBSection(dc, self.header, 0, ct.byref(bits), None, 0)
            if not bitmap:
                raise ct.WinError(ct.get_last_error())
            previous = self.gdi.SelectObject(dc, bitmap)
            if not previous:
                raise ct.WinError(ct.get_last_error())
            ct.memmove(bits, frame, len(frame))
            # A null destination point keeps Tk's current position, including during drag.
            if not self.user.UpdateLayeredWindow(hwnd, None, None, ct.byref(self.size), dc,
                                                 ct.byref(self.origin), 0, self.blend, 2):
                raise ct.WinError(ct.get_last_error())
        finally:
            if previous:
                self.gdi.SelectObject(dc, previous)
            if bitmap:
                self.gdi.DeleteObject(bitmap)
            self.gdi.DeleteDC(dc)
