import os
import sys
import base64
import ctypes
import time
import markdown

from openai import OpenAI

from PySide6.QtCore import (
    Qt, QRect, QPoint, QBuffer, QByteArray, QIODevice, Signal, QObject, QTimer, QThread, Slot, QAbstractNativeEventFilter
)
from PySide6.QtGui import (
    QGuiApplication, QPainter, QColor, QPen, QCursor, QKeySequence, QShortcut, QPixmap, QAction
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QSystemTrayIcon, QMenu, QLabel, QStyle, QTextBrowser
)

from dotenv import load_dotenv
from ctypes import wintypes

load_dotenv()

# --- Windows global hotkey setup ---
WM_HOTKEY   = 0x0312
MOD_ALT     = 0x0001
MOD_CONTROL = 0x0002
VK_SNAPSHOT = 0x2C   # Print Screen
HOTKEY_ID   = 1      # any non-zero id

user32 = ctypes.windll.user32
RegisterHotKey   = user32.RegisterHotKey
UnregisterHotKey = user32.UnregisterHotKey
RegisterHotKey.argtypes   = [wintypes.HWND, wintypes.INT, wintypes.UINT, wintypes.UINT]
RegisterHotKey.restype    = wintypes.BOOL
UnregisterHotKey.argtypes = [wintypes.HWND, wintypes.INT]
UnregisterHotKey.restype  = wintypes.BOOL

PROMPT_TEXT_MD = (
    "Please translate the following image into Brazilian Portuguese. "
    "Output in Markdown, preserving the document's structure where helpful. "
    "Do not use any code blocks around the text. "
    "Do not add any commentary before or after; include ONLY the translation. "
    "The user providing the image is your friend, so a friendly tone is OK if appropriate."
)

OPENAI_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o")  # or gpt-4o-mini


class WinHotkeyFilter(QAbstractNativeEventFilter):
    """Listens for the global Windows hotkey and triggers a callback on the Qt thread."""
    def __init__(self, on_hotkey):
        super().__init__()
        self.on_hotkey = on_hotkey

    def nativeEventFilter(self, eventType, message):
        if eventType.startsWith(b'windows_'):
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                QTimer.singleShot(0, self.on_hotkey)
        return False, 0


class TranslatorWorker(QObject):
    chunk = Signal(str)     # partial streamed text
    done = Signal()         # finished without error
    error = Signal(str)     # error message

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
                        {"type": "input_image", "image_url": f"data:image/png;base64,{b64}"},
                    ],
                }],
                timeout=60,
                temperature=0.6,
            ) as stream:
                for event in stream:
                    if event.type == "response.output_text.delta":
                        self.chunk.emit(event.delta)
                        time.sleep(0.04)  # gentle throttle so UI keeps up
                _ = stream.get_final_response()  # ensures completion
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

        self._frozen_pm: QPixmap | None = None
        self._status_rect: QRect | None = None
        self.state = self.STATE_IDLE
        self.dragging = False
        self.start_pt = QPoint()
        self.end_pt = QPoint()
        self.selection = QRect()
        self._preview_pixmap: QPixmap | None = None
        self._md_buffer: list[str] = []

        # Instruction label (top-left)
        self.instruction = QLabel(self)
        self.instruction.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.instruction.setStyleSheet(
            "color: white; font-size: 16px; background: rgba(0,0,0,0); padding: 8px 12px; border-radius: 8px;"
        )
        self.instruction.setText("Drag to select. Press Enter to translate. Esc to cancel.")
        self.instruction.hide()

        # Center label while waiting
        self.waiting = QLabel("Translating...", self)
        self.waiting.setStyleSheet(
            "color: white; font-size: 20px; background: rgba(0,0,0,0); padding: 14px 20px; border-radius: 12px;"
        )
        self.waiting.setAlignment(Qt.AlignCenter)
        self.waiting.hide()

        # Preview of the captured selection
        self.preview = QLabel(self)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet(
            "background: rgba(0,0,0,0); padding: 8px; border-radius: 12px; border: 1px solid rgba(255,255,255,120);"
        )
        self.preview.hide()

        # Result view (Markdown rendered)
        self.result = QTextBrowser(self)
        self.result.setOpenExternalLinks(True)
        self.result.setStyleSheet(
            "QTextBrowser { color: white; background: rgba(0,0,0,150); font-size: 20px; padding: 16px; border-radius: 16px; }"
        )
        self.result.setFocusPolicy(Qt.NoFocus)
        self.result.hide()

        # Cover full virtual desktop
        vg = QGuiApplication.primaryScreen().virtualGeometry()
        self.setGeometry(vg)

        # Shortcuts (Esc & Enter)
        self._esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._esc.setContext(Qt.ApplicationShortcut)
        self._esc.activated.connect(self.finish)

        for key in (Qt.Key_Return, Qt.Key_Enter):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(self._maybe_capture)

    # ---- Small helpers ----
    def _focus_overlay(self):
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.ActiveWindowFocusReason)
        self.grabKeyboard()

    def _primary_geom(self):
        return QGuiApplication.primaryScreen().geometry()

    def _snapshot_virtual_desktop(self):
        """
        Capture all screens and stitch them into a single QPixmap matching
        the virtual desktop geometry.
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
                pm = screen.grabWindow(0)  # whole screen
                p.drawPixmap(sg.topLeft() - vg.topLeft(), pm)
            p.end()

            self._frozen_pm = canvas
        except Exception:
            self._frozen_pm = None

    # ---- Streaming UI updates ----
    def _on_worker_chunk(self, piece: str):
        if self.state != self.STATE_RESULT:
            # Switch to result view, keep preview visible
            self.state = self.STATE_RESULT
            self.waiting.hide()
            self.result.clear()
            self.result.show()

            # Use a wider geometry if we already laid out the waiting box
            if self._status_rect:
                self.result.setGeometry(self._status_rect)

            self._focus_overlay()
            self.layoutFloatingWidgets()

        # Buffer streamed text as Markdown
        self._md_buffer.append(piece)
        md_text = "".join(self._md_buffer)

        # Convert Markdown -> HTML
        html_body = markdown.markdown(
            md_text,
            extensions=["fenced_code", "tables", "sane_lists", "codehilite"]
        )

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

        self.result.setHtml(themed_html)
        QApplication.processEvents()

    def _on_worker_done(self):
        pass

    def _on_worker_error(self, err: str):
        self.state = self.STATE_RESULT
        self._md_buffer.clear()
        self.waiting.hide()
        self.preview.hide()
        self.result.setMarkdown(f"**Error:** {err}")
        self.result.show()
        self._focus_overlay()
        self.layoutFloatingWidgets()

    # ---- Public controls ----
    def start(self):
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
        self._focus_overlay()
        self._md_buffer.clear()
        self.update()

    def finish(self):
        self.releaseKeyboard()
        self.hide()
        self._frozen_pm = None
        self.state = self.STATE_IDLE
        self._status_rect = None
        self.dragging = False
        self.selection = QRect()
        self._preview_pixmap = None

        self.instruction.hide()
        self.waiting.hide()
        self.preview.hide()
        self.result.hide()
        self.requestClose.emit()

    # ---- Input handling ----
    def _maybe_capture(self):
        if self.state == self.STATE_SELECTING and not self.selection.isNull():
            self._capture_and_translate()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.finish()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if self.state != self.STATE_SELECTING or event.button() != Qt.LeftButton:
            return
        gp = event.globalPosition().toPoint() if hasattr(event, "globalPosition") else event.globalPos()
        self.dragging = True
        self.start_pt = self.end_pt = gp
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
        if self.state != self.STATE_SELECTING or event.button() != Qt.LeftButton:
            return
        self.dragging = False
        gp = event.globalPosition().toPoint() if hasattr(event, "globalPosition") else event.globalPos()
        self.end_pt = gp
        self.selection = QRect(self.start_pt, self.end_pt).normalized()
        self.update()

    # ---- Drawing ----
    def paintEvent(self, _event):
        if self.state == self.STATE_IDLE:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # Draw frozen desktop or a black fallback
        if self._frozen_pm and not self._frozen_pm.isNull():
            p.drawPixmap(0, 0, self._frozen_pm)
        else:
            p.fillRect(self.rect(), QColor(0, 0, 0, 255))

        # Dim everything
        alpha = 100 if self.state == self.STATE_SELECTING else 150
        p.fillRect(self.rect(), QColor(0, 0, 0, alpha))

        # Unshade the selection during selection
        if self.state == self.STATE_SELECTING and not self.selection.isNull() and self._frozen_pm and not self._frozen_pm.isNull():
            r_widget = QRect(self.mapFromGlobal(self.selection.topLeft()),
                             self.mapFromGlobal(self.selection.bottomRight())).normalized()

            vg = QGuiApplication.primaryScreen().virtualGeometry()
            r_global = self.selection.normalized()
            src = QRect(r_global.topLeft() - vg.topLeft(), r_global.size()).normalized()
            src = src.intersected(QRect(0, 0, self._frozen_pm.width(), self._frozen_pm.height()))
            if not src.isEmpty():
                p.drawPixmap(r_widget, self._frozen_pm, src)

            pen = QPen(Qt.white)
            pen.setWidth(2)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.drawRect(r_widget)

        p.end()
        self.layoutFloatingWidgets()

    def layoutFloatingWidgets(self):
        """Position instruction, waiting/result box, and preview intelligently."""
        try:
            pg = self._primary_geom()
            center_local = self.mapFromGlobal(pg.center())
            PW, PH = pg.width(), pg.height()

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
                self._status_rect = status_rect

            if self.result.isVisible():
                # reuse previous rect if known; else center a reasonable default
                w = int(PW * 0.7)
                h = int(PH * 0.4)
                x = center_local.x() - w // 2
                y = center_local.y() - h // 2
                self._status_rect = QRect(x, y, w, h)
                status_rect = QRect(self._status_rect)
                self.result.setGeometry(status_rect)

            if self.preview.isVisible():
                spacing, top_margin, bottom_margin = 16, 60, 40
                natural_max_w = int(PW * 0.5)
                natural_max_h = int(PH * 0.5)

                aspect = 1.6
                if self._preview_pixmap:
                    src_w = max(1, self._preview_pixmap.width())
                    src_h = max(1, self._preview_pixmap.height())
                    aspect = src_w / src_h

                if status_rect is not None:
                    wx, wy, ww, wh = status_rect.x(), status_rect.y(), status_rect.width(), status_rect.height()
                    avail_above_h = max(0, wy - top_margin - spacing)
                    avail_below_h = max(0, (center_local.y() + PH//2) - (wy + wh) - bottom_margin - spacing)
                    place_above = avail_above_h >= 120
                    max_h = min(natural_max_h, (avail_above_h if place_above else avail_below_h))
                else:
                    place_above = True
                    max_h = natural_max_h

                max_h = max(max_h, 120)
                max_w = min(natural_max_w, int(max_h * aspect))

                if self._preview_pixmap:
                    pm = self._preview_pixmap.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    self.preview.setPixmap(pm)
                    pw, ph = pm.width(), pm.height()
                else:
                    pw, ph = max_w, max_h

                px = center_local.x() - pw // 2
                if status_rect is not None:
                    py = (max(top_margin, status_rect.y() - spacing - ph)
                          if place_above
                          else min(self.height() - bottom_margin - ph, status_rect.y() + status_rect.height() + spacing))
                else:
                    py = max(top_margin, center_local.y() - PH//4 - ph//2)

                self.preview.setGeometry(px, py, pw, ph)

        except Exception:
            pass

    # ---- Capture & translate ----
    def _capture_and_translate(self):
        if self.selection.isNull() or not self._frozen_pm or self._frozen_pm.isNull():
            return

        sel = self.selection.normalized()
        x, y, w, h = sel.x(), sel.y(), sel.width(), sel.height()

        vg = QGuiApplication.primaryScreen().virtualGeometry()
        vx = max(0, min(x - vg.x(), self._frozen_pm.width()  - 1))
        vy = max(0, min(y - vg.y(), self._frozen_pm.height() - 1))
        w  = max(1, min(w, self._frozen_pm.width()  - vx))
        h  = max(1, min(h, self._frozen_pm.height() - vy))

        pm = self._frozen_pm.copy(vx, vy, w, h)

        self.state = self.STATE_WAITING
        self.instruction.hide()
        self.waiting.show()
        self.preview.show()
        self.result.hide()
        self._preview_pixmap = pm
        self.layoutFloatingWidgets()
        self.update()

        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.WriteOnly)
        pm.save(buf, "PNG")
        buf.close()
        png_bytes = bytes(ba)

        # Worker thread
        self._thread = QThread(self)
        self._worker = TranslatorWorker()
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(lambda: self._worker.run(png_bytes))
        self._worker.chunk.connect(self._on_worker_chunk)
        self._worker.done.connect(self._on_worker_done)
        self._worker.error.connect(self._on_worker_error)

        # Clean up
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
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon) \
               or self.style().standardIcon(QStyle.StandardPixmap.SP_DesktopIcon)
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

        # Register Ctrl+Alt+PrintScreen (fallback to Ctrl+Alt+F9)
        self._hotkey_filter = WinHotkeyFilter(self.trigger_selection)
        self.installNativeEventFilter(self._hotkey_filter)

        ok = RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT, VK_SNAPSHOT)
        if not ok:
            VK_F9 = 0x78
            ok = RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT, VK_F9)
            self.tray.setToolTip("Image → Portuguese (Ctrl+Alt+F9)" if ok else "Image → Portuguese (no hotkey registered)")
        else:
            self.tray.setToolTip("Image → Portuguese (Ctrl+Alt+PrtScr)")

        self.aboutToQuit.connect(self._cleanup_hotkey)

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.trigger_selection()

    def _cleanup_hotkey(self):
        UnregisterHotKey(None, HOTKEY_ID)

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
