from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

STYLESHEET = """
QMainWindow, QWidget#central {
    background: #1b1d23;
    color: #e8eaed;
}
QWidget#sidebar, QScrollArea#sidebarScroll, QScrollArea#sidebarScroll > QWidget {
    background: #14161b;
}
QScrollArea#sidebarScroll {
    border: none;
}
QSplitter#mainSplitter::handle:horizontal {
    background: #2c3038;
    margin: 0;
    width: 4px;
}
QSplitter#mainSplitter::handle:horizontal:hover {
    background: #3d8bfd;
}
QScrollBar:vertical {
    background: #14161b;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #2a3140;
    min-height: 24px;
    border-radius: 4px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QLabel#title {
    font-size: 18px;
    font-weight: 600;
    color: #f2f4f8;
}
QLabel#hint, QLabel#status {
    color: #9aa3b2;
    font-size: 12px;
}
QGroupBox {
    border: 1px solid #2c3038;
    border-radius: 8px;
    margin-top: 12px;
    padding: 12px 10px 10px 10px;
    font-weight: 600;
    color: #c5cad3;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
}
QCheckBox {
    color: #e8eaed;
}
QDoubleSpinBox, QSpinBox {
    background: #0f1115;
    color: #e8eaed;
    border: 1px solid #2c3038;
    border-radius: 6px;
    padding: 4px 8px;
    min-width: 90px;
}
QRadioButton::indicator {
    width: 16px;
    height: 16px;
}
QWidget#previewCanvas, QWidget#previewInner {
    background: #12141a;
    border-radius: 10px;
}
QFrame#slot {
    background: #1f2229;
    border: 2px dashed #4a5160;
    border-radius: 10px;
}
QFrame#slot[filled="true"] {
    border: 2px solid #3d8bfd;
    background: #191c24;
}
QLabel#slotPreview {
    color: #9aa3b2;
    font-size: 13px;
}
QLabel#slotBadge {
    background: rgba(15, 17, 21, 180);
    color: #f2f4f8;
    font-size: 12px;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: 6px;
}
QPushButton#slotMark {
    background: #2a3140;
    color: #e8eaed;
    border: none;
    border-radius: 6px;
    padding: 0 8px;
    font-size: 11px;
}
QPushButton#slotMark:hover {
    background: #3d8bfd;
}
QLabel#trimPreview {
    background: #0f1115;
    border-radius: 8px;
}
QPushButton#slotClear {
    background: #2a3140;
    color: #e8eaed;
    border: none;
    border-radius: 14px;
    font-size: 16px;
}
QPushButton#slotClear:hover {
    background: #c4453c;
}
QPushButton#primary {
    background: #3d8bfd;
    color: white;
    border: none;
    border-radius: 8px;
    padding: 10px 14px;
    font-weight: 600;
}
QPushButton#primary:disabled {
    background: #2a3140;
    color: #6b7380;
}
QPushButton#primary:hover:!disabled {
    background: #5a9dff;
}
QPushButton#secondary {
    background: #2a3140;
    color: #e8eaed;
    border: none;
    border-radius: 8px;
    padding: 10px 14px;
}
QPushButton#secondary:disabled {
    color: #6b7380;
}
QPushButton#danger {
    background: #6b2b32;
    color: white;
    border: none;
    border-radius: 8px;
    padding: 10px 14px;
    font-weight: 600;
}
QPushButton#danger:disabled {
    background: #2a3140;
    color: #6b7380;
}
QPushButton#danger:hover:!disabled {
    background: #c4453c;
}
QSlider#timeline::groove:horizontal {
    height: 8px;
    background: #2a3140;
    border-radius: 4px;
}
QSlider#timeline::handle:horizontal {
    width: 16px;
    height: 16px;
    margin: -5px 0;
    background: #3d8bfd;
    border-radius: 8px;
}
QSlider#timeline::sub-page:horizontal {
    background: #3d8bfd;
    border-radius: 4px;
}
QProgressBar {
    background: #0f1115;
    border: 1px solid #2c3038;
    border-radius: 6px;
    text-align: center;
    color: #d0d6e0;
    height: 16px;
}
QProgressBar::chunk {
    background: #3d8bfd;
    border-radius: 5px;
}
QLabel#timecode, QLabel#alignNote {
    color: #9aa3b2;
    font-size: 12px;
}
QPlainTextEdit#scriptPreview {
    background: #0f1115;
    color: #b9c0cc;
    border: 1px solid #2c3038;
    border-radius: 8px;
    font-family: monospace;
    font-size: 11px;
}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#1b1d23"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e8eaed"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#12141a"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#e8eaed"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#2a3140"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#e8eaed"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#3d8bfd"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)
