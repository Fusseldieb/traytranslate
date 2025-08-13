import os
import sys
import base64
import threading
import ctypes
import time
import markdown

from openai import OpenAI, APIError, APIConnectionError, APIStatusError

from PySide6.QtCore import (
    Qt, QRect, QPoint, QBuffer, QByteArray, QIODevice, Signal, QObject, QTimer, QSize, QThread, Slot, QAbstractNativeEventFilter
)
from PySide6.QtGui import (
    QGuiApplication, QIcon, QPainter, QColor, QPen, QCursor, QAction, QKeySequence, QShortcut, QTextCursor, QPixmap
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QSystemTrayIcon, QMenu, QLabel, QTextEdit, QStyle, QTextBrowser
)

from dotenv import load_dotenv

from ctypes import wintypes

load_dotenv()

# --- Windows global hotkey setup ---
WM_HOTKEY      = 0x0312
MOD_ALT        = 0x0001
MOD_CONTROL    = 0x0002
MOD_SHIFT      = 0x0004
MOD_WIN        = 0x0008
VK_SNAPSHOT    = 0x2C   # Print Screen
HOTKEY_ID      = 1      # any non-zero id

user32 = ctypes.windll.user32
RegisterHotKey   = user32.RegisterHotKey
UnregisterHotKey = user32.UnregisterHotKey
RegisterHotKey.argtypes   = [wintypes.HWND, wintypes.INT, wintypes.UINT, wintypes.UINT]
RegisterHotKey.restype    = wintypes.BOOL
UnregisterHotKey.argtypes = [wintypes.HWND, wintypes.INT]
UnregisterHotKey.restype  = wintypes.BOOL

PROMPT_TEXT_MD = (
    "Please translate the following image into Brazilian Portuguese. "
    "Output in Markdown, preserving the document\'s structure where helpful. Do not any code blocks around the text."
    "Do not add any commentary before or after; include ONLY the translation. "
    "The user that is providing you the image is your friend, so feel free to use a more personal tone, if appropriate to the context."
)

OPENAI_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o")  # change to gpt-4o-mini if you prefer

class WinHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, on_hotkey):
        super().__init__()
        self.on_hotkey = on_hotkey

    def nativeEventFilter(self, eventType, message):
        # eventType is typically 'windows_generic_MSG' on PySide6
        if eventType.startsWith(b'windows_'):
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                # invoke on Qt thread asap
                QTimer.singleShot(0, self.on_hotkey)
        return False, 0

class TranslatorWorker(QObject):
    chunk = Signal(str)     # partial text
    done = Signal()         # finished without error
    error = Signal(str)

    @Slot(bytes)
    def run(self, png_bytes: bytes):
        try:
            client = OpenAI()
            b64 = base64.b64encode(png_bytes).decode("utf-8")
            with client.responses.stream(
                model=OPENAI_MODEL,
                input=[{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": PROMPT_TEXT_MD},
                        {"type": "input_image", "image_url": f"data:image/png;base64,{b64}"}
                    ]
                }],
                timeout=60,
                temperature=0.6,
            ) as stream:
                for event in stream:
                    if event.type == "response.output_text.delta":
                        self.chunk.emit(event.delta)
                        # gentle throttle so UI can breathe
                        time.sleep(0.05)
                _final = stream.get_final_response()
            self.done.emit()
        except Exception as e:
            self.error.emit(f"Unexpected error: {e}")

class Overlay(QWidget):
    requestClose = Signal()

    STATE_IDLE = 0
    STATE_SELECTING = 1
    STATE_WAITING = 2
    STATE_RESULT = 3

    def __init__(self):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(QCursor(Qt.CrossCursor))
        self.setMouseTracking(True)
        self._frozen_pm = None  # holds the stitched, frozen desktop
        self._status_rect = None
        self.state = self.STATE_IDLE
        self.dragging = False
        self.start_pt = QPoint()
        self.end_pt = QPoint()
        self.selection = QRect()

        # Buffer for streamed Markdown/plaintext
        self._md_buffer = ""

        # Instruction label (top)
        self.instruction = QLabel(self)
        self.instruction.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.instruction.setStyleSheet(
            "color: white; font-size: 16px; background: rgba(0,0,0,0); padding: 8px 12px; "
            "border-radius: 8px;"
        )
        self.instruction.setText("Drag to select. Press Enter to translate. Esc to cancel.")
        self.instruction.adjustSize()
        self.instruction.hide()

        # Center label while waiting
        self.waiting = QLabel("Translating...", self)
        self.waiting.setStyleSheet(
            "color: white; font-size: 20px; background: rgba(0,0,0,0); padding: 14px 20px; "
            "border-radius: 12px;"
        )
        self.waiting.setAlignment(Qt.AlignCenter)
        self.waiting.hide()

        # Preview of the captured selection
        self.preview = QLabel(self)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet(
            "background: rgba(0,0,0,0); padding: 8px; border-radius: 12px; "
            "border: 1px solid rgba(255,255,255,120);"
        )
        self.preview.hide()
        self._preview_pixmap = None

        # Result view (Markdown rendered)
        self.result = QTextBrowser(self)
        self.result.setOpenExternalLinks(True)
        self.result.setStyleSheet(
            "QTextBrowser { color: white; background: rgba(0,0,0,150); font-size: 20px; "
            "padding: 16px; border-radius: 16px; }"
        )
        self.result.hide()
        self._md_buffer = []  # NEW: buffer for streamed Markdown

        # Cover full virtual desktop
        vg = QGuiApplication.primaryScreen().virtualGeometry()
        self.setGeometry(vg)

        # Let Esc/Enter work regardless of which child has focus.
        self._esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._esc.setContext(Qt.ApplicationShortcut)
        self._esc.activated.connect(self.finish)

        self._enter = QShortcut(QKeySequence(Qt.Key_Return), self)
        self._enter.setContext(Qt.ApplicationShortcut)
        self._enter.activated.connect(lambda: self._maybe_capture())
        self._enter2 = QShortcut(QKeySequence(Qt.Key_Enter), self)
        self._enter2.setContext(Qt.ApplicationShortcut)
        self._enter2.activated.connect(lambda: self._maybe_capture())

        # Optional: keep result box from stealing focus
        self.result.setFocusPolicy(Qt.NoFocus)

    def _maybe_capture(self):
        if self.state == self.STATE_SELECTING and not self.selection.isNull():
            self._capture_and_translate()

    def _primary_geom(self):
        return QGuiApplication.primaryScreen().geometry()
    
    def _snapshot_virtual_desktop(self):
        """
        Capture all screens and stitch them into a single QPixmap matching
        QGuiApplication.primaryScreen().virtualGeometry().
        """
        try:
            vg = QGuiApplication.primaryScreen().virtualGeometry()
            if vg.isNull():
                self._frozen_pm = None
                return

            canvas = QPixmap(vg.size())
            canvas.fill(QColor(0, 0, 0, 255))

            p = QPainter(canvas)
            for screen in QGuiApplication.screens():
                sg = screen.geometry()
                # grabWindow(0) captures the whole screen in screen-local coords
                pm = screen.grabWindow(0)
                # place it into the big canvas at (sg.x - vg.x, sg.y - vg.y)
                p.drawPixmap(sg.topLeft() - vg.topLeft(), pm)
            p.end()

            self._frozen_pm = canvas
        except Exception:
            self._frozen_pm = None
    
    def _on_worker_chunk(self, piece: str):
        if self.state != self.STATE_RESULT:
            if self.waiting.isVisible():
                # Use same vertical position but make it wider
                old_rect = self.waiting.geometry()
                wider_w = min(int(self.width() * 0.7), 800)  # 70% of overlay width, capped at 800px
                wider_x = max(0, old_rect.center().x() - wider_w // 2)
                wider_h = min(int(self.height() * 0.7), 600)   # Add height control here
                self._status_rect = QRect(wider_x, old_rect.y(), wider_w, wider_h)

                self._status_rect = QRect(wider_x, old_rect.y(), wider_w, wider_h)
            else:
                self._status_rect = None

            self.state = self.STATE_RESULT
            self.waiting.hide()
            # Keep preview visible
            self.result.clear()
            self.result.show()

            # Apply our wider geometry immediately
            if self._status_rect is not None:
                self.result.setGeometry(self._status_rect)

            self.raise_()
            self.activateWindow()
            self.setFocus(Qt.ActiveWindowFocusReason)
            self.grabKeyboard()
            self.layoutFloatingWidgets()


        # Buffer streamed text as Markdown
        self._md_buffer.append(piece)
        md_text = "".join(self._md_buffer)

        # Convert Markdown -> HTML (supports fenced code & tables)
        html_body = markdown.markdown(
            md_text,
            extensions=["fenced_code", "tables", "sane_lists", "codehilite"]
        )

        # Wrap with dark-friendly styling so text stays readable
        themed_html = f"""
        <html>
        <head>
        <meta charset="utf-8">
        <style>
            body {{
            color: #ffffff; background: transparent; font-size: 18px; text-align: center;
            }}
            a {{ color: #9cd3ff; }}
            code, pre {{ background: rgba(255,255,255,0.08); border-radius: 6px; padding: 0.2em 0.4em; }}
            pre {{ padding: 12px; overflow-x: auto; }}
            table {{ border-collapse: collapse; width: 100%; }}
            th, td {{ border: 1px solid rgba(255,255,255,0.25); padding: 6px 8px; }}
            h1, h2, h3, h4 {{ margin-top: 0.6em; }}
            ul, ol {{ padding-left: 1.4em; }}
        </style>
        </head>
        <body>{html_body}</body>
        </html>
        """

        # Updating the full HTML each chunk keeps formatting correct as the text grows
        self.result.setHtml(themed_html)
        QApplication.processEvents()


    def _on_worker_done(self):
        pass

    def _on_worker_error(self, err: str):
        self.state = self.STATE_RESULT
        self._md_buffer = []
        self.waiting.hide()
        self.preview.hide()
        self.result.setMarkdown(f"**Error:** {err}")
        self.result.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.ActiveWindowFocusReason)
        self.grabKeyboard()
        self.layoutFloatingWidgets()

    def start(self):
        # Take a frozen snapshot before showing overlay so nothing moves
        self._snapshot_virtual_desktop()

        self.state = self.STATE_SELECTING
        self.dragging = False
        self.selection = QRect()
        self._status_rect = None
        pg = self._primary_geom()
        top_left_local = self.mapFromGlobal(QPoint(pg.x() + 20, pg.y() + 20))
        self.instruction.move(top_left_local)
        self.instruction.show()
        self.waiting.hide()
        self.preview.hide()
        self.result.hide()
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.ActiveWindowFocusReason)
        self.grabKeyboard()
        self.layoutFloatingWidgets()
        self._md_buffer = []
        self.update()

    def finish(self):
        self.releaseKeyboard()
        self.hide()
        self._frozen_pm = None
        self.state = self.STATE_IDLE
        self._status_rect = None
        self.dragging = False
        self.selection = QRect()
        self.instruction.hide()
        self.waiting.hide()
        self.preview.hide()
        self._preview_pixmap = None
        self.result.hide()
        self.requestClose.emit()

    # ---- Input handling ----
    def keyPressEvent(self, event):
        k = event.key()
        if k == Qt.Key_Escape:
            self.finish()
            return
        if k in (Qt.Key_Return, Qt.Key_Enter):
            if self.state == self.STATE_SELECTING and not self.selection.isNull():
                self._capture_and_translate()
            elif self.state in (self.STATE_RESULT, self.STATE_WAITING):
                pass
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if self.state != self.STATE_SELECTING:
            return
        if event.button() == Qt.LeftButton:
            gp = event.globalPosition().toPoint() if hasattr(event, "globalPosition") else event.globalPos()
            self.dragging = True
            self.start_pt = gp
            self.end_pt = gp
            self.selection = QRect(self.start_pt, self.end_pt).normalized()
            self.update()

    def mouseMoveEvent(self, event):
        if self.state != self.STATE_SELECTING or not self.dragging:
            return
        gp = event.globalPosition().toPoint() if hasattr(event, "globalPosition") else event.globalPos()
        self.end_pt = gp
        self.selection = QRect(self.start_pt, self.end_pt).normalized()
        self.update()

    def mouseReleaseEvent(self, event):
        if self.state != self.STATE_SELECTING:
            return
        if event.button() == Qt.LeftButton:
            self.dragging = False
            gp = event.globalPosition().toPoint() if hasattr(event, "globalPosition") else event.globalPos()
            self.end_pt = gp
            self.selection = QRect(self.start_pt, self.end_pt).normalized()
            self.update()

    # ---- Drawing ----
    def paintEvent(self, event):
        if self.state == self.STATE_IDLE:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # 1) Draw the frozen desktop (full virtual desktop)
        if self._frozen_pm and not self._frozen_pm.isNull():
            p.drawPixmap(0, 0, self._frozen_pm)
        else:
            # Fallback
            p.fillRect(self.rect(), QColor(0, 0, 0, 255))

        # 2) Dim everything
        shade = QColor(0, 0, 0, 100 if self.state == self.STATE_SELECTING else 150)
        p.fillRect(self.rect(), shade)

        # 3) If selecting, "unshade" the selection by repainting the frozen snapshot
        if self.state == self.STATE_SELECTING and not self.selection.isNull() and self._frozen_pm and not self._frozen_pm.isNull():
            # Target rect in widget coords (we span the virtual desktop starting at (0,0))
            r_widget = QRect(self.mapFromGlobal(self.selection.topLeft()),
                            self.mapFromGlobal(self.selection.bottomRight())).normalized()

            # Source rect in the frozen pixmap (convert global -> virtual-desktop-local)
            vg = QGuiApplication.primaryScreen().virtualGeometry()
            r_global = self.selection.normalized()
            src = QRect(r_global.topLeft() - vg.topLeft(), r_global.size()).normalized()

            # Clamp source to pixmap bounds
            src = src.intersected(QRect(0, 0, self._frozen_pm.width(), self._frozen_pm.height()))
            if not src.isEmpty():
                # Draw the original (undimmed) pixels back over the dim layer
                p.drawPixmap(r_widget, self._frozen_pm, src)

            # Border
            pen = QPen(Qt.white)
            pen.setWidth(2)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.drawRect(r_widget)

        p.end()
        self.layoutFloatingWidgets()

    def layoutFloatingWidgets(self):
        try:
            pg = self._primary_geom()
            center_local = self.mapFromGlobal(pg.center())
            PW, PH = pg.width(), pg.height()

            # --- Compute/remember a "status box" rect (either waiting or result) ---
            status_rect = None

            if self.waiting.isVisible():
                self.waiting.adjustSize()
                pad = 16
                w = self.waiting.sizeHint().width() + pad * 2
                h = self.waiting.sizeHint().height() + pad * 2
                x = center_local.x() - w // 2
                y = center_local.y() - h // 2
                status_rect = QRect(x, y, w, h)
                self.waiting.setGeometry(status_rect)
                # live-update cache so when we switch to result we reuse this spot
                self._status_rect = status_rect

            # If result is visible and we have a cached rect, use it.
            if self.result.isVisible():
                if self._status_rect is not None:
                    status_rect = QRect(self._status_rect)  # copy
                    self.result.setGeometry(status_rect)
                else:
                    # Fallback: center a modest box if no cache exists
                    pad = 16
                    w = min(int(PW * 0.5), 600)
                    h = min(int(PH * 0.3), 280)
                    x = center_local.x() - w // 2
                    y = center_local.y() - h // 2
                    status_rect = QRect(x, y, w, h)
                    self.result.setGeometry(status_rect)
                    self._status_rect = status_rect

            # --- Position the preview relative to the status box (above or below) ---
            if self.preview.isVisible():
                spacing = 16
                top_margin = 60
                bottom_margin = 40

                natural_max_w = int(PW * 0.5)
                natural_max_h = int(PH * 0.5)

                if self._preview_pixmap:
                    src_w = max(1, self._preview_pixmap.width())
                    src_h = max(1, self._preview_pixmap.height())
                    aspect = src_w / src_h
                else:
                    aspect = 1.6

                # If we have a status box (waiting or result), place preview above/below it.
                if status_rect is not None:
                    wx, wy, ww, wh = status_rect.x(), status_rect.y(), status_rect.width(), status_rect.height()
                    avail_above_h = max(0, wy - top_margin - spacing)
                    avail_below_h = max(0, (center_local.y() + PH//2) - (wy + wh) - bottom_margin - spacing)
                    place_above = avail_above_h >= 120
                    max_h = min(natural_max_h, (avail_above_h if place_above else avail_below_h))
                else:
                    # No status box: approximate placement above midline
                    place_above = True
                    max_h = natural_max_h

                if max_h < 60:
                    max_h = 120
                max_w = min(natural_max_w, int(max_h * aspect))

                if self._preview_pixmap:
                    pm = self._preview_pixmap.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    self.preview.setPixmap(pm)
                    pw, ph = pm.width(), pm.height()
                else:
                    pw, ph = max_w, max_h

                px = center_local.x() - pw // 2
                if status_rect is not None:
                    if place_above:
                        py = max(top_margin, status_rect.y() - spacing - ph)
                    else:
                        py = min(self.height() - bottom_margin - ph, status_rect.y() + status_rect.height() + spacing)
                else:
                    py = max(top_margin, center_local.y() - PH//4 - ph//2)

                self.preview.setGeometry(px, py, pw, ph)

            # If neither waiting nor result is visible but result should have a large box,
            # keep your original big centered layout (rare with this flow).
            if not self.waiting.isVisible() and not self.result.isVisible():
                pass

        except Exception:
            pass
        
    # ---- Capture & translate ----
    def _capture_and_translate(self):
        if self.selection.isNull():
            return

        # Use the pre-frozen desktop so content doesn't move
        if not self._frozen_pm or self._frozen_pm.isNull():
            # Fallback: no frozen image; do nothing (or you could keep your old live-grab here)
            return

        sel = self.selection.normalized()
        x, y, w, h = sel.x(), sel.y(), sel.width(), sel.height()

        # Convert global coords to virtual-desktop-local coords
        vg = QGuiApplication.primaryScreen().virtualGeometry()
        vx = x - vg.x()
        vy = y - vg.y()

        # Clamp to bounds just in case
        vx = max(0, min(vx, self._frozen_pm.width()  - 1))
        vy = max(0, min(vy, self._frozen_pm.height() - 1))
        w  = max(1, min(w, self._frozen_pm.width()  - vx))
        h  = max(1, min(h, self._frozen_pm.height() - vy))

        # Crop the frozen snapshot
        pm = self._frozen_pm.copy(vx, vy, w, h)

        self.state = self.STATE_WAITING
        self.instruction.hide()
        self.waiting.show()
        self.preview.show()
        self.result.hide()
        self._preview_pixmap = pm
        self.layoutFloatingWidgets()
        self.update()

        # Encode PNG from the cropped, frozen pixmap
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.WriteOnly)
        pm.save(buf, "PNG")
        buf.close()
        png_bytes = bytes(ba)

        # Launch the worker (unchanged)
        self._thread = QThread(self)
        self._worker = TranslatorWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(lambda: self._worker.run(png_bytes))

        self._worker.chunk.connect(self._on_worker_chunk)
        self._worker.done.connect(self._on_worker_done)
        self._worker.error.connect(self._on_worker_error)

        self._worker.done.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)

        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

class TrayApp(QApplication):
    def __init__(self, argv):
        super().__init__(argv)
        self.setQuitOnLastWindowClosed(False)

        # Tray
        self.tray = QSystemTrayIcon(self)
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DesktopIcon)
        self.tray.setIcon(icon)

        menu = QMenu()

        quit_action = QAction("Quit", self.tray)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)

        self.tray.activated.connect(self._on_tray_activated)
        self.tray.setToolTip("Image → Portuguese (Ctrl+Alt+PrtScr)")
        self.tray.setVisible(True)
        self.tray.show()

        self.overlay = Overlay()
        self.overlay.requestClose.connect(self.on_overlay_closed)

        # --- Register Ctrl+Alt+PrintScreen ---
        self._hotkey_filter = WinHotkeyFilter(self.trigger_selection)
        self.installNativeEventFilter(self._hotkey_filter)

        ok = RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT, VK_SNAPSHOT)
        if not ok:
            VK_F9 = 0x78
            ok = RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT, VK_F9)
            if ok:
                self.tray.setToolTip("Image → Portuguese (Ctrl+Alt+F9)")
            else:
                self.tray.setToolTip("Image → Portuguese (no hotkey registered)")
        else:
            self.tray.setToolTip("Image → Portuguese (Ctrl+Alt+PrtScr)")

        self.aboutToQuit.connect(self._cleanup_hotkey)

    def _on_tray_activated(self, reason):
        # Trigger on left click or double click
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.trigger_selection()

    def _cleanup_hotkey(self):
        UnregisterHotKey(None, HOTKEY_ID)

    def _hotkey_release(self, *_args, **_kwargs):
        QTimer.singleShot(0, self.trigger_selection)

    def trigger_selection(self):
        if self.overlay.state == self.overlay.STATE_IDLE:
            vg = QGuiApplication.primaryScreen().virtualGeometry()
            self.overlay.setGeometry(vg)
            self.overlay.start()

    def on_overlay_closed(self):
        pass

    def quit_app(self):
        self.overlay.finish()
        QTimer.singleShot(0, self.quit)


def main():
    app = TrayApp(sys.argv)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
