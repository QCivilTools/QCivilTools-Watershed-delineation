# -*- coding: utf-8 -*-
"""
qct_style.py — Shared UI style constants for all QCivilTools apps.
Import this in every app dialog to keep the suite visually consistent.
"""

# ── Accent colours ─────────────────────────────────────────────────────────────
PRIMARY   = "#1a5276"    # dark navy blue — headers, tab selected
SECONDARY = "#2471a3"    # mid blue — hover states
SUCCESS   = "#1e8449"    # green — run buttons
WARNING   = "#d68910"    # amber
DANGER    = "#922b21"    # red — cancel / error
NEUTRAL   = "#717d7e"    # grey — secondary buttons
LIGHT_BG  = "#f4f6f7"    # very light grey — group box backgrounds
BORDER    = "#d5d8dc"    # subtle border

# ── Global QDialog stylesheet ──────────────────────────────────────────────────
DIALOG_STYLE = f"""
    QDialog, QWidget {{
        font-family: "Segoe UI", Arial, sans-serif;
        font-size: 10px;
        background: #ffffff;
        color: #1c2833;
    }}
    QGroupBox {{
        font-weight: bold;
        font-size: 10px;
        border: 1px solid {BORDER};
        border-radius: 4px;
        margin-top: 8px;
        padding-top: 6px;
        background: {LIGHT_BG};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 8px;
        padding: 0 4px;
        color: {PRIMARY};
        background: #ffffff;
    }}
    QTabWidget::pane {{
        border: 1px solid {BORDER};
        border-radius: 4px;
        background: #ffffff;
    }}
    QTabBar::tab {{
        background: #eaecee;
        color: #2c3e50;
        padding: 6px 14px;
        border: 1px solid {BORDER};
        border-bottom: none;
        border-radius: 4px 4px 0 0;
        font-size: 10px;
    }}
    QTabBar::tab:selected {{
        background: {PRIMARY};
        color: white;
        font-weight: bold;
    }}
    QTabBar::tab:hover:!selected {{
        background: #d6eaf8;
    }}
    QScrollArea {{
        border: none;
        background: #ffffff;
    }}
    QProgressBar {{
        border: 1px solid {BORDER};
        border-radius: 4px;
        text-align: center;
        height: 18px;
        background: #eaecee;
    }}
    QProgressBar::chunk {{
        background: {PRIMARY};
        border-radius: 3px;
    }}
    QTextEdit {{
        background: #1e1e1e;
        color: #d4d4d4;
        font-family: Consolas, "Courier New", monospace;
        font-size: 10px;
        border: 1px solid {BORDER};
        border-radius: 4px;
    }}
    QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{
        border: 1px solid {BORDER};
        border-radius: 3px;
        padding: 3px 6px;
        background: white;
        selection-background-color: {SECONDARY};
    }}
    QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
        border: 1px solid {SECONDARY};
    }}
    QCheckBox, QRadioButton {{
        font-size: 10px;
        spacing: 6px;
    }}
    QLabel {{
        font-size: 10px;
    }}
"""

# ── Reusable button styles ─────────────────────────────────────────────────────
def btn_primary(label=""):
    return (f"QPushButton{{background:{PRIMARY};color:white;padding:7px 16px;"
            f"border-radius:4px;font-weight:bold;font-size:10px;border:none;}}"
            f"QPushButton:hover{{background:{SECONDARY};}}"
            f"QPushButton:disabled{{background:#aab7b8;color:#fff;}}")

def btn_success(label=""):
    return (f"QPushButton{{background:{SUCCESS};color:white;padding:7px 16px;"
            f"border-radius:4px;font-weight:bold;font-size:10px;border:none;}}"
            f"QPushButton:hover{{background:#196f3d;}}"
            f"QPushButton:disabled{{background:#aab7b8;color:#fff;}}")

def btn_danger(label=""):
    return (f"QPushButton{{background:{DANGER};color:white;padding:7px 16px;"
            f"border-radius:4px;font-weight:bold;font-size:10px;border:none;}}"
            f"QPushButton:hover{{background:#7b241c;}}"
            f"QPushButton:disabled{{background:#aab7b8;color:#fff;}}")

def btn_neutral(label=""):
    return (f"QPushButton{{background:{NEUTRAL};color:white;padding:7px 16px;"
            f"border-radius:4px;font-weight:bold;font-size:10px;border:none;}}"
            f"QPushButton:hover{{background:#5d6d7e;}}"
            f"QPushButton:disabled{{background:#aab7b8;color:#fff;}}")

def btn_warning(label=""):
    return (f"QPushButton{{background:{WARNING};color:white;padding:7px 16px;"
            f"border-radius:4px;font-weight:bold;font-size:10px;border:none;}}"
            f"QPushButton:hover{{background:#b7770d;}}"
            f"QPushButton:disabled{{background:#aab7b8;color:#fff;}}")

# ── Header widget factory ──────────────────────────────────────────────────────
def make_header(title, subtitle="", parent=None):
    """Return a styled QFrame for the top of any QCivilTools dialog."""
    from qgis.PyQt.QtWidgets import QFrame, QVBoxLayout, QLabel
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QFont

    frame = QFrame(parent)
    frame.setStyleSheet(
        f"QFrame{{background:{PRIMARY};border-radius:5px;"
        f"padding:8px 12px;margin-bottom:4px;}}"
    )
    lay = QVBoxLayout(frame)
    lay.setSpacing(2); lay.setContentsMargins(8, 6, 8, 6)

    t_lbl = QLabel(title)
    f = QFont(); f.setPointSize(12); f.setBold(True)
    t_lbl.setFont(f)
    t_lbl.setAlignment(Qt.AlignCenter)
    t_lbl.setStyleSheet("color:white;background:transparent;")
    lay.addWidget(t_lbl)

    if subtitle:
        s_lbl = QLabel(subtitle)
        s_lbl.setAlignment(Qt.AlignCenter)
        s_lbl.setStyleSheet("color:rgba(255,255,255,0.75);font-size:9px;background:transparent;")
        lay.addWidget(s_lbl)

    return frame

# ── Info banner factory ────────────────────────────────────────────────────────
def info_banner(text, color=PRIMARY):
    """Return stylesheet string for an info/banner QLabel."""
    import colorsys
    return (
        f"background:#eaf4fb;border:1px solid {color};border-radius:4px;"
        f"padding:8px;color:#1a3a5c;font-size:10px;"
    )

def warn_banner():
    return (
        "background:#fef9e7;border:1px solid #f0c040;border-radius:4px;"
        "padding:8px;color:#664d03;font-size:10px;"
    )

def ok_banner():
    return (
        "background:#eafaf1;border:1px solid #27ae60;border-radius:4px;"
        "padding:8px;color:#1e8449;font-size:10px;"
    )
