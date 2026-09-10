"""
PDF2Resin — Direct-to-Print Photolithography
Version: v1.3.4
Converts a single-page vector PDF into a native resin-printer exposure
file (SL1, CTB, PHOTON, GOO, CBDDLP, PHZ), preserving real physical
dimensions on the build plate regardless of printer brand or LCD resolution.

This tool targets flat masked-exposure workflows (photolithography,
PCB exposure, stencils, UV curing masks) where every output layer is
an identical copy of the same source image — it does not slice a 3D model.

External dependencies:
  - pdftoppm (Poppler)   : high-resolution PDF rasterization
  - UVtoolsCmd (UVtools) : conversion from SL1 to any non-SL1 format
Both tool paths can be located automatically on first launch (see
find_executable) or set manually via the "Choose app..." buttons.
"""

import sys
import zipfile
import subprocess
import traceback
import os
import tempfile
import shutil
import platform
import numpy as np
from pathlib import Path
from datetime import datetime

try:
    from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget,
                                    QVBoxLayout, QHBoxLayout, QPushButton,
                                    QLabel, QFileDialog, QSpinBox, QComboBox,
                                    QLineEdit, QGroupBox, QCheckBox,
                                    QSlider, QDoubleSpinBox, QScrollArea,
                                    QFormLayout, QButtonGroup, QRadioButton,
                                    QTextEdit)
    from PySide6.QtGui import QPixmap, QImage, QPainter, QPen
    from PySide6.QtCore import Qt, QSettings, QPoint, QRect
    from PIL import Image, ImageOps
    # The dynamic render DPI (see calculate_render_dpi) can legitimately
    # produce very large raster images on high-density printer presets
    # (e.g. a 12K printer on an A4-sized PDF renders well above Pillow's
    # default decompression-bomb pixel limit). The source is a PDF the
    # user opened themselves, not untrusted third-party input, so the
    # check is disabled rather than silently failing on valid use cases.
    Image.MAX_IMAGE_PIXELS = None
except ImportError as e:
    print(f"Missing dependencies: {e}")
    print("Install with: pip install PySide6 Pillow numpy")
    sys.exit(1)

# --- PRESETS FOR COMMON RESIN PRINTERS ---
# Each entry: (resolution_x_px, resolution_y_px, display_width_mm, display_height_mm)
DISPLAY_PRESETS = {
    "Prusa SL1 (2K)": (2560, 1440, 120.0, 68.0),
    "Prusa SL1S (4K)": (3840, 2160, 120.0, 68.0),
    "Elegoo Saturn 3 (12K)": (7680, 4320, 196.8, 110.5),
    "Anycubic Photon Mono X (4K)": (3840, 2400, 192.0, 120.0),
    "Phrozen Sonic Mini 8K": (7500, 3240, 165.0, 72.0),
    "Custom": (2560, 1440, 120.0, 68.0)
}

# --- MAP EACH PRESET TO ITS RECOMMENDED OUTPUT FORMAT ---
PRESET_FORMAT_MAP = {
    "Prusa SL1 (2K)": "SL1",
    "Prusa SL1S (4K)": "SL1",
    "Elegoo Saturn 3 (12K)": "CTB",
    "Anycubic Photon Mono X (4K)": "PHOTON",
    "Phrozen Sonic Mini 8K": "PHZ",
    "Custom": "SL1"
}

# --- MAP OUTPUT FORMAT TO UVTOOLS TARGET TYPE ---
# UVtools accepts a known target type or the corresponding file extension.
# Using the explicit target extension keeps this mapping simple and avoids
# relying on internal encoder names that may change between UVtools versions.
FORMAT_TO_UVTOOLS_ENCODER = {
    "SL1": "sl1",
    "CTB": "ctb",
    "PHOTON": "photon",
    "GOO": "goo",
    "CBDDLP": "cbddlp",
    "PHZ": "phz"
}

# --- COLOR FILTER TARGETS ---
# Reference RGB values for each named color, used by apply_color_filter
# to compute Euclidean distance from each pixel.
COLOR_FILTER_TARGETS = {
    "Red":     (255, 0, 0),
    "Green":   (0, 255, 0),
    "Blue":    (0, 0, 255),
    "Cyan":    (0, 255, 255),
    "Magenta": (255, 0, 255),
    "Yellow":  (255, 255, 0),
    "Black":   (0, 0, 0),
    "White":   (255, 255, 255),
}


def find_executable(name: str, win_reg_key: str = None) -> str:
    """Try to locate an external tool executable automatically."""
    # First try PATH
    path = shutil.which(name)
    if path:
        return path

    system = platform.system()
    is_uvtools = name.lower().startswith("uvtools")

    if system == "Windows":
        # Try registry for UVtools
        if win_reg_key and is_uvtools:
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, win_reg_key) as key:
                    val, _ = winreg.QueryValueEx(key, "InstallDir")
                    candidate = Path(val) / f"{name}.exe"
                    if candidate.exists():
                        return str(candidate)
            except Exception:
                pass
        
        # Common install locations
        if is_uvtools:
            common_paths = [
                Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "UVtools" / f"{name}.exe",
                Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")) / "UVtools" / f"{name}.exe",
                Path(os.environ.get("LocalAppData", "")) / "Programs" / "UVtools" / f"{name}.exe",
                Path("C:\\UVtools") / f"{name}.exe",
            ]
        else:
            common_paths = [
                Path("C:\\poppler\\Library\\bin") / f"{name}.exe",
                Path("C:\\poppler\\bin") / f"{name}.exe",
                Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "poppler" / "Library" / "bin" / f"{name}.exe",
            ]
        
        for p in common_paths:
            if p.exists():
                return str(p)

    elif system == "Darwin":
        if is_uvtools:
            mac_path = Path("/Applications/UVtools.app/Contents/MacOS") / name
            if mac_path.exists():
                return str(mac_path)
        else:
            for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
                candidate = Path(prefix) / name
                if candidate.exists():
                    return str(candidate)

    elif system == "Linux":
        home = Path.home()
        linux_paths = [
            home / ".local" / "bin" / name,
            Path("/usr/local/bin") / name,
            Path("/usr/bin") / name,
        ]
        for p in linux_paths:
            if p.exists():
                return str(p)

    return ""


def calculate_render_dpi(res_x: int, res_y: int, disp_w: float, disp_h: float,
                         min_dpi: int = 1200) -> int:
    """Compute the DPI to use when rasterizing the source PDF, scaled to the
    target printer's pixel density.

    Rendering at a fixed DPI regardless of the printer is wasteful on
    low-density panels (unnecessary CPU/RAM for no visual benefit) and
    risks under-sampling on very high-density panels (e.g. 12K printers),
    where a fixed 1200 DPI source can end up close to or below the
    printer's own pixel density, producing a soft/aliased result after
    the final resize.

    The target DPI is set to twice the printer's native pixel density,
    which leaves enough headroom for a clean high-quality downscale.
    A floor of `min_dpi` is always enforced.
    """
    ppm_x = res_x / disp_w
    ppm_y = res_y / disp_h
    max_ppm = max(ppm_x, ppm_y)
    target_dpi = int(max_ppm * 25.4 * 2)
    return max(min_dpi, target_dpi)


def pdf_to_highres_pil(pdf_path: str, pdftoppm_exe: str, dpi: int = 1200) -> Image.Image:
    """Render the first page of a PDF to a high-resolution PIL Image for precise physical scaling."""
    # Render into the system temp directory rather than next to the source
    # PDF: the PDF's folder may be read-only (network share, mounted USB).
    fd, temp_path = tempfile.mkstemp(suffix=".png", dir=tempfile.gettempdir())
    os.close(fd)
    temp_png = Path(temp_path)
    output_prefix = str(temp_png.with_suffix(""))
    try:
        cmd = [pdftoppm_exe, "-png", "-r", str(dpi), "-f", "1", "-l", "1", "-singlefile", pdf_path, output_prefix]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0 or not temp_png.exists():
            raise RuntimeError(f"pdftoppm failed: {result.stderr}")
        img = Image.open(temp_png).convert("RGB")
        return img
    finally:
        temp_png.unlink(missing_ok=True)


def apply_b_w(img: Image.Image, threshold: int = 128) -> Image.Image:
    """Convert image to pure black and white using a configurable threshold."""
    gray = img.convert("L")
    bw = gray.point(lambda x: 0 if x < threshold else 255, mode="1")
    return bw.convert("RGB")


def apply_color_filter(img: Image.Image, target_color: str, tolerance: int = 50) -> Image.Image:
    """Filter image to show only pixels matching the target color.

    Pixels within `tolerance` (Euclidean RGB distance) of the target color
    become WHITE (exposed), everything else becomes BLACK (not exposed).
    This is consistent with the B/W threshold logic where the "active"
    areas are rendered white for resin photolithography.
    """
    if target_color not in COLOR_FILTER_TARGETS:
        return img

    target_rgb = COLOR_FILTER_TARGETS[target_color]
    img_rgb = img.convert("RGB")
    img_array = np.array(img_rgb)

    # Compute Euclidean distance from target color for every pixel
    distance = np.sqrt(
        np.sum((img_array.astype(float) - np.array(target_rgb)) ** 2, axis=2)
    )

    # Mask of pixels within tolerance
    mask = distance <= tolerance

    # Output: black background, white where target color was detected
    output = np.zeros_like(img_array)
    output[mask] = [255, 255, 255]

    return Image.fromarray(output.astype(np.uint8))


def apply_transforms(img: Image.Image, scale: float, rotate: int,
                     flip_h: bool, flip_v: bool, invert: bool,
                     filter_mode: str = "none", bw_threshold: int = 128,
                     color_target: str = "Red", color_tolerance: int = 50) -> Image.Image:
    """Apply geometric and color transformations to the image (used for the live preview).

    filter_mode:
      - "none"  : keep original colors (no filter applied)
      - "bw"    : apply B/W threshold using bw_threshold
      - "color" : apply color filter using color_target and color_tolerance
    """
    w, h = img.size
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))

    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    if rotate == 90:
        img = img.transpose(Image.Transpose.ROTATE_90)
    elif rotate == 180:
        img = img.transpose(Image.Transpose.ROTATE_180)
    elif rotate == 270:
        img = img.transpose(Image.Transpose.ROTATE_270)

    if flip_h:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if flip_v:
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)

    if invert:
        img = ImageOps.invert(img)

    # Apply the selected filter
    if filter_mode == "bw":
        img = apply_b_w(img, threshold=bw_threshold)
    elif filter_mode == "color":
        img = apply_color_filter(img, target_color=color_target, tolerance=color_tolerance)
    # "none" mode: no filter applied, keep original colors

    return img


def build_sl1(png_path: str, out_sl1: str, width_px: int, height_px: int,
              res_x: int, res_y: int, display_w_mm: float, display_h_mm: float,
              layer_height: float, normal_exp: float, bottom_exp: float,
              bottom_layers: int, num_layers: int) -> None:
    """Generate an SL1 archive with multiple identical layers for photolithography."""
    actual_bottom_layers = min(bottom_layers, num_layers)
    actual_normal_layers = max(0, num_layers - actual_bottom_layers)
    total_time = int(actual_bottom_layers * bottom_exp + actual_normal_layers * normal_exp)

    config_ini = f"""action = print
layer_height = {layer_height}
num_fast = {actual_bottom_layers}
num_slow = {actual_normal_layers}
num_fade = 0
print_time = {total_time}
used_material = 0.0
printer_model = SL1
exp_time = {normal_exp}
exp_time_first = {bottom_exp}
"""
    for i in range(num_layers):
        exp_time = bottom_exp if i < actual_bottom_layers else normal_exp
        config_ini += f"""
[layer_{i}]
file = slice_{i:06d}.png
exposure_time = {exp_time}
layer_height = {layer_height}
island_count = 0
"""

    prusaslicer_ini = f"""display_width = {display_w_mm}
display_height = {display_h_mm}
display_pixels_x = {res_x}
display_pixels_y = {res_y}
printer_model = SL1
printer_technology = SLA
"""

    with open(png_path, "rb") as f:
        png_data = f.read()

    with zipfile.ZipFile(out_sl1, "w", zipfile.ZIP_DEFLATED) as zf:
        # Every layer is the exact same PNG. Store it uncompressed (PNG is
        # already compressed) instead of re-running DEFLATE on identical
        # bytes for every single layer: this avoids needless CPU work that
        # scales with layer count and sidesteps double-compression quirks
        # some SL1 parsers have with re-deflated PNG streams.
        for i in range(num_layers):
            info = zipfile.ZipInfo(f"slice_{i:06d}.png")
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, png_data)
        zf.writestr("config.ini", config_ini)
        zf.writestr("prusaslicer.ini", prusaslicer_ini)


def convert_with_uvtools(uvtools_exe: str, sl1_path: str, out_path: str, fmt: str) -> None:
    """Convert an SL1 file to the target format using the UVtools CLI."""
    target_type = FORMAT_TO_UVTOOLS_ENCODER.get(fmt.upper())
    if target_type is None:
        raise RuntimeError(f"No UVtools target type mapped for format '{fmt}'")

    # Pass arguments as a list (shell=False) rather than a shell string:
    # avoids shell-injection risk and handles paths containing spaces or
    # quotes correctly without manual escaping.
    cmd = [uvtools_exe, "convert", sl1_path, target_type, out_path]
    # Let subprocess.TimeoutExpired propagate uncaught, so the GUI can show
    # its specific timeout message instead of a generic error dialog.
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    # Some UVtools versions can return a non-zero exit code even when the
    # conversion has completed and the target file was created successfully.
    # The output path is unique for each export, so an existing non-empty
    # file is a reliable success indicator here.
    output_file = Path(out_path)
    if not output_file.exists() or output_file.stat().st_size == 0:
        raise RuntimeError(
            f"UVtools conversion failed (exit code {result.returncode}):\n"
            f"{result.stderr}\n\nSTDOUT:\n{result.stdout}"
        )



class CropPreviewLabel(QLabel):
    """Preview label with an interactive crop overlay."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.crop_enabled = False
        self.crop_rect = None
        self.image_rect = QRect()
        self.aspect_ratio = None
        self._drag_mode = None
        self._drag_start = None
        self._drag_rect = None
        self.setMouseTracking(True)

    def set_image_rect(self, rect: QRect):
        self.image_rect = QRect(rect)
        self.update()

    def set_crop_rect(self, rect):
        self.crop_rect = QRect(rect) if rect is not None else None
        self.update()
        self.repaint()

    def set_crop_enabled(self, enabled: bool):
        self.crop_enabled = enabled
        self.setCursor(Qt.CrossCursor if enabled else Qt.ArrowCursor)
        self.update()

    def set_aspect_ratio(self, ratio):
        """Set the aspect ratio used for constrained crop selection."""
        self.aspect_ratio = ratio if ratio and ratio > 0 else None
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.crop_enabled or self.crop_rect is None or self.image_rect.isEmpty():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)

        crop = self.crop_rect.intersected(self.image_rect)
        if crop.isEmpty():
            painter.end()
            return

        # Dim the area outside the selected crop.
        painter.setOpacity(0.55)
        painter.fillRect(
            QRect(self.image_rect.left(), self.image_rect.top(),
                  self.image_rect.width(), max(0, crop.top() - self.image_rect.top())),
            Qt.black
        )
        painter.fillRect(
            QRect(self.image_rect.left(), crop.bottom() + 1,
                  self.image_rect.width(),
                  max(0, self.image_rect.bottom() - crop.bottom())),
            Qt.black
        )
        painter.fillRect(
            QRect(self.image_rect.left(), crop.top(),
                  max(0, crop.left() - self.image_rect.left()), crop.height()),
            Qt.black
        )
        painter.fillRect(
            QRect(crop.right() + 1, crop.top(),
                  max(0, self.image_rect.right() - crop.right()), crop.height()),
            Qt.black
        )
        painter.setOpacity(1.0)

        pen = QPen(Qt.white)
        pen.setWidth(2)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawRect(crop)

        # Corner handles make the selected area easy to resize.
        painter.setPen(Qt.NoPen)
        painter.setBrush(Qt.white)
        handle_size = 8
        for x, y in (
            (crop.left(), crop.top()),
            (crop.right(), crop.top()),
            (crop.left(), crop.bottom()),
            (crop.right(), crop.bottom()),
        ):
            painter.drawRect(
                x - handle_size // 2, y - handle_size // 2,
                handle_size, handle_size
            )

        painter.end()

    def _handle_at(self, pos):
        if self.crop_rect is None:
            return None
        r = self.crop_rect
        hs = 10
        handles = {
            "tl": QRect(r.left() - hs, r.top() - hs, hs * 2, hs * 2),
            "tr": QRect(r.right() - hs, r.top() - hs, hs * 2, hs * 2),
            "bl": QRect(r.left() - hs, r.bottom() - hs, hs * 2, hs * 2),
            "br": QRect(r.right() - hs, r.bottom() - hs, hs * 2, hs * 2),
        }
        for name, rect in handles.items():
            if rect.contains(pos):
                return name
        return None

    def mousePressEvent(self, event):
        if not self.crop_enabled or event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)

        pos = event.position().toPoint()
        if not self.image_rect.contains(pos):
            return

        handle = self._handle_at(pos)
        if handle:
            self._drag_mode = handle
            self._drag_start = pos
            self._drag_rect = QRect(self.crop_rect)
        elif self.crop_rect is not None and self.crop_rect.contains(pos):
            self._drag_mode = "move"
            self._drag_start = pos
            self._drag_rect = QRect(self.crop_rect)
        else:
            self._drag_mode = "draw"
            self._drag_start = pos
            self._drag_rect = QRect(pos, pos)

        self.grabMouse()

    def mouseMoveEvent(self, event):
        if self._drag_mode is None:
            if self.crop_enabled:
                handle = self._handle_at(event.position().toPoint())
                if handle in ("tl", "br"):
                    self.setCursor(Qt.SizeFDiagCursor)
                elif handle in ("tr", "bl"):
                    self.setCursor(Qt.SizeBDiagCursor)
                elif self.crop_rect is not None and self.crop_rect.contains(event.position().toPoint()):
                    self.setCursor(Qt.SizeAllCursor)
                else:
                    self.setCursor(Qt.CrossCursor)
            return super().mouseMoveEvent(event)

        pos = event.position().toPoint()
        # Crop corners are free by default. Hold Shift to constrain the
        # selection to the active printer aspect ratio.
        free_ratio = not bool(event.modifiers() & Qt.ShiftModifier)

        if self._drag_mode == "move":
            dx = pos.x() - self._drag_start.x()
            dy = pos.y() - self._drag_start.y()
            rect = QRect(self._drag_rect)
            rect.translate(dx, dy)
            self.crop_rect = self._clamp_rect_to_image(rect)
        elif self._drag_mode == "draw":
            self.crop_rect = self._make_draw_rect(self._drag_start, pos, free_ratio)
        else:
            self.crop_rect = self._make_resize_rect(
                self._drag_rect, self._drag_mode, pos, free_ratio
            )

        self.update()
        self._emit_crop_changed()

    def mouseReleaseEvent(self, event):
        if self._drag_mode is None:
            return super().mouseReleaseEvent(event)

        if event.button() == Qt.LeftButton:
            self.releaseMouse()
            self._emit_crop_changed(finished=True)
            self._drag_mode = None
            self._drag_start = None
            self._drag_rect = None
            self.setCursor(Qt.CrossCursor)

    def _emit_crop_changed(self, finished=False):
        parent = self.parent()
        while parent is not None and not hasattr(parent, "set_crop_from_preview_rect"):
            parent = parent.parent()
        if parent is not None:
            parent.set_crop_from_preview_rect(self.crop_rect, finished=finished)

    def _clamp_rect_to_image(self, rect):
        if self.image_rect.isEmpty():
            return rect
        width = min(max(1, rect.width()), self.image_rect.width())
        height = min(max(1, rect.height()), self.image_rect.height())
        x = min(max(rect.x(), self.image_rect.left()),
                self.image_rect.right() - width + 1)
        y = min(max(rect.y(), self.image_rect.top()),
                self.image_rect.bottom() - height + 1)
        return QRect(x, y, width, height)

    def _make_draw_rect(self, start, current, free_ratio):
        bounds = self.image_rect
        x1 = min(max(start.x(), bounds.left()), bounds.right())
        y1 = min(max(start.y(), bounds.top()), bounds.bottom())
        x2 = min(max(current.x(), bounds.left()), bounds.right())
        y2 = min(max(current.y(), bounds.top()), bounds.bottom())

        if free_ratio or not self.aspect_ratio:
            return QRect(QPoint(x1, y1), QPoint(x2, y2)).normalized()

        dx = x2 - x1
        dy = y2 - y1
        width = max(1, abs(dx))
        height = max(1, abs(dy))

        if width / height > self.aspect_ratio:
            height = max(1, int(round(width / self.aspect_ratio)))
        else:
            width = max(1, int(round(height * self.aspect_ratio)))

        sign_x = 1 if dx >= 0 else -1
        sign_y = 1 if dy >= 0 else -1
        left = x1 if sign_x > 0 else x1 - width
        top = y1 if sign_y > 0 else y1 - height
        rect = QRect(left, top, width, height).normalized()
        return self._clamp_rect_to_image(rect)

    def _make_resize_rect(self, original, handle, current, free_ratio):
        bounds = self.image_rect
        left, top = original.left(), original.top()
        right, bottom = original.right(), original.bottom()

        if handle in ("tl", "bl"):
            left = min(max(current.x(), bounds.left()), right - 1)
        else:
            right = max(min(current.x(), bounds.right()), left + 1)

        if handle in ("tl", "tr"):
            top = min(max(current.y(), bounds.top()), bottom - 1)
        else:
            bottom = max(min(current.y(), bounds.bottom()), top + 1)

        if free_ratio or not self.aspect_ratio:
            return QRect(QPoint(left, top), QPoint(right, bottom)).normalized()

        width = max(1, right - left)
        height = max(1, bottom - top)

        if width / height > self.aspect_ratio:
            height = max(1, int(round(width / self.aspect_ratio)))
            if handle in ("tl", "tr"):
                top = bottom - height
            else:
                bottom = top + height
        else:
            width = max(1, int(round(height * self.aspect_ratio)))
            if handle in ("tl", "bl"):
                left = right - width
            else:
                right = left + width

        return self._clamp_rect_to_image(
            QRect(QPoint(left, top), QPoint(right, bottom)).normalized()
        )

class PDF2ResinGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PDF2Resin — Direct-to-Print Photolithography")
        # Initial window size is computed at the end of __init__, once every
        # control (including the log panel) has been created, so it can be
        # sized exactly to fit its content - see the auto-sizing block below.
        self.settings = QSettings("PDF2Resin", "PDF2Resin")

        self.original_image = None
        self.pdf_path = None
        self.render_dpi = None  # DPI actually used to rasterize original_image, set by load_pdf()
        self.log_file_path = Path(__file__).resolve().parent / "pdf2resin.log"
        self.current_transforms = {
            'scale': 1.0, 'rotate': 0, 'flip_h': False, 'flip_v': False, 'invert': False,
            'filter_mode': 'none', 'bw_threshold': 128,
            'color_target': 'Red', 'color_tolerance': 50
        }
        self.crop_box = None  # Normalized source-image coordinates: (x0, y0, x1, y1)

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)

        # --- TOP: PREVIEW + CONTROLS ---
        top_layout = QHBoxLayout()
        root_layout.addLayout(top_layout, stretch=1)

        # --- LEFT: PREVIEW AREA ---
        self.preview_label = CropPreviewLabel()
        self.preview_label.setText("No PDF loaded")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setStyleSheet("background: #2b2b2b; border: 2px solid #555; color: #aaa;")
        self.preview_label.setMinimumSize(600, 600)
        top_layout.addWidget(self.preview_label, stretch=3)

        # --- RIGHT: CONTROLS (Scrollable) ---
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(350)
        controls_widget = QWidget()
        scroll.setWidget(controls_widget)
        ctrl_layout = QVBoxLayout(controls_widget)

        # 1. External Tools
        tools_group = QGroupBox("External Tools")
        tools_layout = QFormLayout()

        self.pdftoppm_path = QLineEdit()
        self.pdftoppm_path.setPlaceholderText("C:\\poppler\\Library\\bin\\pdftoppm.exe")
        pdftoppm_row = QHBoxLayout()
        pdftoppm_row.addWidget(self.pdftoppm_path)
        pdftoppm_browse_btn = QPushButton("Choose app...")
        pdftoppm_browse_btn.clicked.connect(
            lambda: self.browse_for_tool(self.pdftoppm_path, "pdftoppm", "pdftoppm.exe pdftoppm")
        )
        pdftoppm_row.addWidget(pdftoppm_browse_btn)
        tools_layout.addRow("pdftoppm.exe:", pdftoppm_row)

        self.uvtools_path = QLineEdit()
        self.uvtools_path.setPlaceholderText("C:\\UVtools\\UVtoolsCmd.exe")
        uvtools_row = QHBoxLayout()
        uvtools_row.addWidget(self.uvtools_path)
        uvtools_browse_btn = QPushButton("Choose app...")
        uvtools_browse_btn.clicked.connect(
            lambda: self.browse_for_tool(self.uvtools_path, "UVtoolsCmd", "UVtoolsCmd.exe UVtoolsCmd")
        )
        uvtools_row.addWidget(uvtools_browse_btn)
        tools_layout.addRow("UVtoolsCmd.exe:", uvtools_row)

        tools_group.setLayout(tools_layout)
        ctrl_layout.addWidget(tools_group)

        # 2. Display Settings
        disp_group = QGroupBox("Display Settings (Printer)")
        disp_layout = QFormLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(DISPLAY_PRESETS.keys()))
        self.preset_combo.currentTextChanged.connect(self.apply_preset)
        disp_layout.addRow("Preset:", self.preset_combo)

        self.res_x_spin = QSpinBox(); self.res_x_spin.setRange(100, 16000); self.res_x_spin.setValue(2560)
        self.res_y_spin = QSpinBox(); self.res_y_spin.setRange(100, 16000); self.res_y_spin.setValue(1440)
        self.disp_w_spin = QDoubleSpinBox(); self.disp_w_spin.setRange(10, 500); self.disp_w_spin.setValue(120.0); self.disp_w_spin.setSuffix(" mm")
        self.disp_h_spin = QDoubleSpinBox(); self.disp_h_spin.setRange(10, 500); self.disp_h_spin.setValue(68.0); self.disp_h_spin.setSuffix(" mm")

        disp_layout.addRow("Resolution X:", self.res_x_spin)
        disp_layout.addRow("Resolution Y:", self.res_y_spin)
        disp_layout.addRow("Width (mm):", self.disp_w_spin)
        disp_layout.addRow("Height (mm):", self.disp_h_spin)
        disp_group.setLayout(disp_layout)
        ctrl_layout.addWidget(disp_group)

        # 3. Transformations
        trans_group = QGroupBox("Transformations & Preview")
        trans_layout = QVBoxLayout()

        self.scale_slider = QSlider(Qt.Horizontal)
        self.scale_slider.setRange(10, 500)
        # Restore saved scale value
        saved_scale = int(self.settings.value("scale", 100))
        self.scale_slider.setValue(saved_scale)
        
        # Lightweight label update during drag, heavy rendering only on release
        self.scale_slider.valueChanged.connect(self.on_scale_slider_changed)
        self.scale_slider.sliderReleased.connect(self.refresh_preview_with_status)
        
        self.scale_label = QLabel(f"Scale: {saved_scale / 100.0:.2f}x")
        trans_layout.addWidget(self.scale_label)
        trans_layout.addWidget(self.scale_slider)
        
        # Initialize the scale in the transform dictionary
        self.current_transforms['scale'] = saved_scale / 100.0

        # Crop controls — folded by default (a UI convenience only; whether
        # cropping is actually enabled is tracked separately by crop_check).
        self.crop_toggle_btn = QPushButton("\u25b8 Crop")
        self.crop_toggle_btn.setCheckable(True)
        self.crop_toggle_btn.setChecked(False)
        self.crop_toggle_btn.setStyleSheet(
            "QPushButton { text-align: left; border: none; font-weight: bold; }"
        )
        self.crop_toggle_btn.toggled.connect(self.on_crop_section_toggled)
        trans_layout.addWidget(self.crop_toggle_btn)

        self.crop_section = QWidget()
        crop_section_layout = QVBoxLayout()
        crop_section_layout.setContentsMargins(0, 0, 0, 0)

        crop_layout = QHBoxLayout()
        self.crop_check = QCheckBox("Enable Crop")
        self.crop_check.setChecked(
            self.settings.value("crop_enabled", "false") == "true"
        )
        self.crop_check.toggled.connect(self.on_crop_toggled)
        crop_layout.addWidget(self.crop_check)

        self.crop_reset_btn = QPushButton("Reset Crop")
        self.crop_reset_btn.clicked.connect(self.reset_crop)
        self.crop_reset_btn.setEnabled(self.crop_check.isChecked())
        crop_layout.addWidget(self.crop_reset_btn)
        crop_section_layout.addLayout(crop_layout)

        self.crop_label = QLabel("Crop: Full Image")
        self.crop_label.setWordWrap(True)
        crop_section_layout.addWidget(self.crop_label)

        self.crop_section.setLayout(crop_section_layout)
        self.crop_section.setVisible(False)  # folded by default
        trans_layout.addWidget(self.crop_section)

        rot_layout = QHBoxLayout()
        self.rot_0 = QRadioButton("0°"); self.rot_0.setChecked(True)
        self.rot_90 = QRadioButton("90°")
        self.rot_180 = QRadioButton("180°")
        self.rot_270 = QRadioButton("270°")
        self.rot_group = QButtonGroup()
        for btn in [self.rot_0, self.rot_90, self.rot_180, self.rot_270]:
            self.rot_group.addButton(btn)
            rot_layout.addWidget(btn)
            btn.toggled.connect(self.on_transform_change)
        trans_layout.addLayout(rot_layout)

        flip_layout = QHBoxLayout()
        self.flip_h_check = QCheckBox("Flip H"); self.flip_h_check.toggled.connect(self.on_transform_change)
        self.flip_v_check = QCheckBox("Flip V"); self.flip_v_check.toggled.connect(self.on_transform_change)
        self.invert_check = QCheckBox("Negative/Invert"); self.invert_check.toggled.connect(self.on_transform_change)
        flip_layout.addWidget(self.flip_h_check)
        flip_layout.addWidget(self.flip_v_check)
        flip_layout.addWidget(self.invert_check)
        trans_layout.addLayout(flip_layout)

        # Filter mode selection: three mutually exclusive radio buttons.
        # "None" keeps original colors, "B/W Threshold" applies grayscale
        # thresholding, "Color Filter" isolates a single target color.
        filter_mode_layout = QVBoxLayout()
        self.filter_mode_group = QButtonGroup()

        self.none_radio = QRadioButton("None (Original Colors)")
        self.none_radio.setChecked(True)
        self.filter_mode_group.addButton(self.none_radio)
        filter_mode_layout.addWidget(self.none_radio)

        self.bw_radio = QRadioButton("B/W Threshold")
        self.filter_mode_group.addButton(self.bw_radio)
        filter_mode_layout.addWidget(self.bw_radio)

        self.color_radio = QRadioButton("Color Filter")
        self.filter_mode_group.addButton(self.color_radio)
        filter_mode_layout.addWidget(self.color_radio)

        # Color target combo: visible only when "Color Filter" is active.
        self.color_target_combo = QComboBox()
        self.color_target_combo.addItems(list(COLOR_FILTER_TARGETS.keys()))
        saved_color_target = self.settings.value("color_target", "Red")
        if saved_color_target in COLOR_FILTER_TARGETS:
            self.color_target_combo.setCurrentText(saved_color_target)
        self.color_target_combo.currentTextChanged.connect(self.on_transform_change)
        self.color_target_combo.setVisible(False)
        filter_mode_layout.addWidget(self.color_target_combo)

        trans_layout.addLayout(filter_mode_layout)

        # Connect radio buttons to the filter mode handler
        self.none_radio.toggled.connect(self.on_filter_mode_changed)
        self.bw_radio.toggled.connect(self.on_filter_mode_changed)
        self.color_radio.toggled.connect(self.on_filter_mode_changed)

        # Shared slider: acts as "Threshold" in B/W mode and as
        # "Tolerance" in Color Filter mode. Disabled by default because
        # "None" is the initial filter mode.
        self.filter_slider = QSlider(Qt.Horizontal)
        self.filter_slider.setRange(10, 250)
        saved_threshold = int(self.settings.value("bw_threshold", 128))
        self.filter_slider.setValue(saved_threshold)
        self.filter_slider.valueChanged.connect(self.on_filter_slider_changed)
        self.filter_slider.sliderReleased.connect(self.on_filter_slider_released)
        self.filter_slider.setEnabled(False)

        self.filter_slider_label = QLabel("No filter")
        self.filter_slider_label.setEnabled(False)
        trans_layout.addWidget(self.filter_slider_label)
        trans_layout.addWidget(self.filter_slider)

        trans_group.setLayout(trans_layout)
        ctrl_layout.addWidget(trans_group)

        # 4. Exposure Settings
        exp_group = QGroupBox("Exposure Settings")
        exp_layout = QFormLayout()
        self.layer_height_spin = QDoubleSpinBox(); self.layer_height_spin.setRange(0.01, 0.5); self.layer_height_spin.setValue(float(self.settings.value("layer_height", 0.05))); self.layer_height_spin.setSuffix(" mm")
        self.normal_exp_spin = QDoubleSpinBox(); self.normal_exp_spin.setRange(0.1, 120); self.normal_exp_spin.setValue(float(self.settings.value("normal_exp", 8.0))); self.normal_exp_spin.setSuffix(" s")
        self.bottom_exp_spin = QDoubleSpinBox(); self.bottom_exp_spin.setRange(0.1, 300); self.bottom_exp_spin.setValue(float(self.settings.value("bottom_exp", 40.0))); self.bottom_exp_spin.setSuffix(" s")
        self.bottom_layers_spin = QSpinBox(); self.bottom_layers_spin.setRange(0, 50); self.bottom_layers_spin.setValue(int(self.settings.value("bottom_layers", 5)))
        self.num_layers_spin = QSpinBox(); self.num_layers_spin.setRange(1, 200); self.num_layers_spin.setValue(int(self.settings.value("num_layers", 10)))

        exp_layout.addRow("Layer Height:", self.layer_height_spin)
        exp_layout.addRow("Normal Exp:", self.normal_exp_spin)
        exp_layout.addRow("Bottom Exp:", self.bottom_exp_spin)
        exp_layout.addRow("Bottom Layers:", self.bottom_layers_spin)
        exp_layout.addRow("Total Layers:", self.num_layers_spin)
        exp_group.setLayout(exp_layout)
        ctrl_layout.addWidget(exp_group)

        # 5. Output Format
        fmt_group = QGroupBox("Output Format")
        fmt_layout = QFormLayout()
        self.format_combo = QComboBox()
        self.format_combo.addItems(["SL1", "CTB", "PHOTON", "GOO", "CBDDLP", "PHZ"])
        fmt_layout.addRow("Format:", self.format_combo)
        # Warn the user if the selected format does not match the preset's
        # recommended format (still allowed, since it may be intentional).
        self.format_combo.currentTextChanged.connect(self.on_format_change)

        self.verify_check = QCheckBox("Launch UVtools GUI after export")
        self.verify_check.setChecked(False)
        fmt_layout.addRow(self.verify_check)
        fmt_group.setLayout(fmt_layout)
        ctrl_layout.addWidget(fmt_group)

        # Action Buttons
        btn_layout = QHBoxLayout()
        self.load_pdf_btn = QPushButton("Load PDF")
        self.load_pdf_btn.clicked.connect(self.load_pdf)
        btn_layout.addWidget(self.load_pdf_btn)

        self.convert_btn = QPushButton("Generate & Export")
        self.convert_btn.clicked.connect(self.convert)
        self.convert_btn.setEnabled(False)
        btn_layout.addWidget(self.convert_btn)
        ctrl_layout.addLayout(btn_layout)

        # --- LOG PANEL ---
        # Small log console directly under the action buttons, with a
        # checkbox immediately to its left that toggles whether log lines
        # are also appended to a log file on disk.
        log_layout = QHBoxLayout()
        self.log_to_file_check = QCheckBox("Save log\nto file")
        self.log_to_file_check.setChecked(self.settings.value("log_to_file", "false") == "true")
        log_layout.addWidget(self.log_to_file_check)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("background: #1e1e1e; color: #d0d0d0; font-family: Consolas, monospace; font-size: 10px;")
        self.log_text.setFixedHeight(90)
        log_layout.addWidget(self.log_text, stretch=1)
        ctrl_layout.addLayout(log_layout)

        ctrl_layout.addStretch()
        top_layout.addWidget(scroll, stretch=1)

        # --- STARTUP LOGIC & AUTO-DETECTION ---
        saved_preset = self.settings.value("preset", "Prusa SL1 (2K)")
        if saved_preset in DISPLAY_PRESETS:
            self.preset_combo.setCurrentText(saved_preset)
        else:
            self.preset_combo.setCurrentText("Prusa SL1 (2K)")

        # Apply preset FIRST (sets resolution + recommended format)
        self.apply_preset(self.preset_combo.currentText())

        # Then restore saved format, only if one was explicitly saved before
        saved_format = self.settings.value("format", "")
        if saved_format:
            self.format_combo.setCurrentText(saved_format.strip())

        # Trigger the format/preset mismatch warning for the initial state
        self.on_format_change(self.format_combo.currentText())

        # Auto-detect external tool paths if none was saved from a previous
        # session: saves the user from having to browse manually on a
        # fresh install if pdftoppm/UVtoolsCmd are in a common location.
        saved_pdftoppm = self.settings.value("pdftoppm_path", "")
        self.pdftoppm_path.setText(saved_pdftoppm or find_executable("pdftoppm") or "")

        saved_uvtools = self.settings.value("uvtools_path", "")
        self.uvtools_path.setText(saved_uvtools or find_executable("UVtoolsCmd", r"SOFTWARE\UVtools") or "")

        # Restore saved filter mode and parameters directly into current_transforms
        # so they are ready and active as soon as a PDF is loaded.
        saved_filter_mode = self.settings.value("filter_mode", "none")
        saved_threshold = int(self.settings.value("bw_threshold", 128))
        saved_color_target = self.settings.value("color_target", "Red")

        if saved_filter_mode == "bw":
            self.bw_radio.setChecked(True)
            self.current_transforms['filter_mode'] = 'bw'
            self.current_transforms['bw_threshold'] = saved_threshold
        elif saved_filter_mode == "color":
            self.color_radio.setChecked(True)
            self.current_transforms['filter_mode'] = 'color'
            self.current_transforms['color_target'] = saved_color_target
            self.current_transforms['color_tolerance'] = saved_threshold
        else:
            self.none_radio.setChecked(True)
            self.current_transforms['filter_mode'] = 'none'

        # Initialize UI state (slider visibility, labels) based on the restored mode.
        # Note: on_transform_change() will safely return early here since 
        # original_image is None, but the UI elements will be set up correctly.
        self.on_filter_mode_changed()

        # --- RESTORE TRANSFORM SETTINGS (Bug #7 Fix) ---
        saved_scale = int(self.settings.value("scale", 100))
        self.scale_slider.setValue(saved_scale)
        
        saved_rotation = int(self.settings.value("rotation", 0))
        if saved_rotation == 90:
            self.rot_90.setChecked(True)
        elif saved_rotation == 180:
            self.rot_180.setChecked(True)
        elif saved_rotation == 270:
            self.rot_270.setChecked(True)
        else:
            self.rot_0.setChecked(True)
            
        self.current_transforms['flip_h'] = self.settings.value("flip_h", "false") == "true"
        self.flip_h_check.setChecked(self.current_transforms['flip_h'])
        
        self.current_transforms['flip_v'] = self.settings.value("flip_v", "false") == "true"
        self.flip_v_check.setChecked(self.current_transforms['flip_v'])
        
        self.current_transforms['invert'] = self.settings.value("invert", "false") == "true"
        self.invert_check.setChecked(self.current_transforms['invert'])

        # Initialize UI state (slider visibility, labels) based on the restored mode.
        # Note: on_transform_change() will safely return early here since 
        # original_image is None, but the UI elements will be set up correctly.
        self.on_filter_mode_changed()

        saved_crop_box = self.settings.value("crop_box", "")
        if saved_crop_box:
            try:
                values = [float(v) for v in str(saved_crop_box).split(",")]
                if len(values) == 4:
                    self.crop_box = tuple(max(0.0, min(1.0, v)) for v in values)
            except (TypeError, ValueError):
                self.crop_box = None
        self.preview_label.set_crop_enabled(self.crop_check.isChecked())

        self.log("PDF2Resin v1.3.3 started.")

        # Auto-size the window tall enough to show every control, including
        # the log panel, without the right-hand panel needing to scroll.
        # The QScrollArea is kept as a safety net only (e.g. very small
        # screens), where scrolling will still kick in as a fallback.
        content_height = controls_widget.sizeHint().height()
        preview_height = self.preview_label.minimumHeight()
        window_chrome_margin = 60  # layout spacing, margins, title bar allowance
        ideal_height = max(content_height, preview_height) + window_chrome_margin

        screen = QApplication.primaryScreen()
        if screen is not None:
            max_available_height = screen.availableGeometry().height() - 40
            ideal_height = min(ideal_height, max_available_height)

        self.resize(1200, ideal_height)

    def on_filter_mode_changed(self):
        """Handle filter mode radio button changes.

        Updates the slider label text, enables/disables the slider and
        the color target combo, and triggers a preview refresh.
        """
        if self.none_radio.isChecked():
            self.filter_slider.setEnabled(False)
            self.filter_slider_label.setEnabled(False)
            self.filter_slider_label.setText("No filter")
            self.color_target_combo.setVisible(False)
        elif self.bw_radio.isChecked():
            self.filter_slider.setEnabled(True)
            self.filter_slider_label.setEnabled(True)
            self.filter_slider_label.setText(f"Threshold: {self.filter_slider.value()}")
            self.color_target_combo.setVisible(False)
        else:  # color_radio
            self.filter_slider.setEnabled(True)
            self.filter_slider_label.setEnabled(True)
            self.filter_slider_label.setText(f"Tolerance: {self.filter_slider.value()}")
            self.color_target_combo.setVisible(True)
        self.on_transform_change()

    def on_scale_slider_changed(self, value: int):
        """Lightweight update for the scale slider during drag.
        Only updates the label and stored value; the expensive preview
        re-render is deferred to sliderReleased to keep dragging smooth."""
        scale_val = value / 100.0
        self.scale_label.setText(f"Scale: {scale_val:.2f}x")
        self.current_transforms['scale'] = scale_val
        # Cheap (no re-render), so keep the mm readout live during drag too.
        self._update_crop_label()

        if not self.scale_slider.isSliderDown():
            # No drag in progress (e.g. keyboard arrow keys, or a click that
            # jumps the handle without a following release): refresh now,
            # since no sliderReleased signal will otherwise follow.
            self.refresh_preview_with_status()
    
    
    
    
    
    def on_filter_slider_changed(self, value: int):
        """Fires continuously while the slider handle moves. Only updates
        the label and stored value - the expensive preview re-render is
        deferred (see on_filter_slider_released) to keep dragging smooth."""
        if self.bw_radio.isChecked():
            self.filter_slider_label.setText(f"Threshold: {value}")
            self.current_transforms['bw_threshold'] = value
        elif self.color_radio.isChecked():
            self.filter_slider_label.setText(f"Tolerance: {value}")
            self.current_transforms['color_tolerance'] = value

        if not self.filter_slider.isSliderDown():
            # No drag in progress (e.g. keyboard arrow keys, or a click that
            # jumps the handle without a following release): refresh now,
            # since no sliderReleased signal will otherwise follow.
            self.refresh_preview_with_status()

    def on_filter_slider_released(self):
        """Fires once the mouse button is released after dragging the slider."""
        self.refresh_preview_with_status()

    def log(self, message: str, level: str = "INFO"):
        """Append a timestamped line to the on-screen log, and to the log file if enabled."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] [{level}] {message}"
        self.log_text.append(line)
        if self.log_to_file_check.isChecked():
            try:
                with open(self.log_file_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                # A logging failure must never interrupt the actual conversion workflow.
                pass

    def browse_for_tool(self, target_field: QLineEdit, tool_display_name: str, filter_pattern: str):
        """Open a native file picker to locate an external tool executable on disk,
        instead of requiring the user to type the full path manually."""
        start_dir = str(Path(target_field.text()).parent) if target_field.text() else ""
        path, _ = QFileDialog.getOpenFileName(
            self, f"Locate {tool_display_name}", start_dir,
            f"{tool_display_name} ({filter_pattern});;All Files (*)"
        )
        if path:
            target_field.setText(path)
            self.log(f"{tool_display_name} path set to: {path}")

    def apply_preset(self, preset_name):
        """Apply printer preset and auto-select its recommended output format."""
        if preset_name in DISPLAY_PRESETS:
            rx, ry, dw, dh = DISPLAY_PRESETS[preset_name]
            self.res_x_spin.setValue(rx)
            self.res_y_spin.setValue(ry)
            self.disp_w_spin.setValue(dw)
            self.disp_h_spin.setValue(dh)
            if preset_name in PRESET_FORMAT_MAP:
                self.format_combo.setCurrentText(PRESET_FORMAT_MAP[preset_name].strip())
            self.format_combo.setStyleSheet("")

            # The PDF source is rasterized according to the active printer
            # density. Re-render an already loaded PDF when the preset changes
            # so the source resolution always matches the current printer.
            # Do not refresh the preview before rendering: that would briefly
            # display the old source using the new printer geometry and causes
            # the visible glitch during preset changes.
            if self.pdf_path and self.original_image:
                self.log(
                    f"Printer preset changed to '{preset_name}'. "
                    "Re-rendering loaded PDF..."
                )
                if self.render_loaded_pdf():
                    self.log(
                        f"Loaded PDF re-rendered successfully for '{preset_name}'."
                    )
            elif self.original_image:
                self.update_preview()

    def on_format_change(self, new_format):
        """Warn the user if the selected format does not match the current printer preset."""
        current_preset = self.preset_combo.currentText()
        recommended = PRESET_FORMAT_MAP.get(current_preset, "SL1").strip()
        if new_format != recommended and current_preset != "Custom":
            self.format_combo.setStyleSheet(
                "QComboBox { border: 2px solid #ff9900; background: #fff3cd; }"
            )
        else:
            self.format_combo.setStyleSheet("")

    def get_crop_aspect_ratio(self):
        """Return the printer display aspect ratio in the current output orientation."""
        width = self.disp_w_spin.value()
        height = self.disp_h_spin.value()
        rotate = self.current_transforms.get('rotate', 0)
        if rotate in (90, 270):
            width, height = height, width
        return width / height if height else None

    def _update_crop_label(self):
        """Show the crop area in physical mm, accounting for the current
        scale and the selected printer's pixel pitch. Mirrors exactly the
        math used at export time (see convert_and_export) so the figures
        shown here match the real printed output, not just the source PDF."""
        if not self.crop_check.isChecked():
            self.crop_label.setText("Crop: Disabled")
            return
        if self.crop_box is None or not self.original_image or not self.render_dpi:
            self.crop_label.setText("Crop: Draw a rectangle on the preview")
            return

        x0, y0, x1, y1 = self.crop_box
        pdf_ppm = self.render_dpi / 25.4
        crop_w_mm = (x1 - x0) * self.original_image.width / pdf_ppm
        crop_h_mm = (y1 - y0) * self.original_image.height / pdf_ppm

        scale = self.current_transforms.get('scale', 1.0)
        plate_w_mm = crop_w_mm * scale
        plate_h_mm = crop_h_mm * scale

        if self.current_transforms.get('rotate', 0) in (90, 270):
            plate_w_mm, plate_h_mm = plate_h_mm, plate_w_mm

        # Quantize to the target printer's pixel grid using the exact same
        # int()-truncation and auto-fit logic as convert_and_export, so the
        # "on plate" figure reflects the real printed size, not an estimate.
        res_x = self.res_x_spin.value()
        res_y = self.res_y_spin.value()
        disp_w = self.disp_w_spin.value()
        disp_h = self.disp_h_spin.value()
        printer_ppm_x = res_x / disp_w if disp_w else 0
        printer_ppm_y = res_y / disp_h if disp_h else 0

        target_w_px = max(1, int(plate_w_mm * printer_ppm_x)) if printer_ppm_x else 0
        target_h_px = max(1, int(plate_h_mm * printer_ppm_y)) if printer_ppm_y else 0

        fit_note = ""
        if printer_ppm_x and printer_ppm_y and (target_w_px > res_x or target_h_px > res_y):
            fit_ratio = min(res_x / target_w_px, res_y / target_h_px)
            target_w_px = max(1, int(target_w_px * fit_ratio))
            target_h_px = max(1, int(target_h_px * fit_ratio))
            fit_note = " [exceeds plate, auto-scaled down]"

        printed_w_mm = target_w_px / printer_ppm_x if printer_ppm_x else 0
        printed_h_mm = target_h_px / printer_ppm_y if printer_ppm_y else 0

        self.crop_label.setText(
            f"Crop: {crop_w_mm:.2f} x {crop_h_mm:.2f} mm source\n"
            f"\u2192 {printed_w_mm:.2f} x {printed_h_mm:.2f} mm on plate "
            f"({target_w_px}x{target_h_px}px, {scale:.2f}x){fit_note}"
        )

    def _source_to_display_normalized(self, x, y):
        """Map normalized source coordinates through rotation and flips."""
        rotate = self.current_transforms.get('rotate', 0)
        if rotate == 90:
            x, y = y, 1.0 - x
        elif rotate == 180:
            x, y = 1.0 - x, 1.0 - y
        elif rotate == 270:
            x, y = 1.0 - y, x
        if self.current_transforms.get('flip_h'):
            x = 1.0 - x
        if self.current_transforms.get('flip_v'):
            y = 1.0 - y
        return x, y

    def _display_to_source_normalized(self, x, y):
        """Map normalized preview coordinates back to source-image coordinates."""
        if self.current_transforms.get('flip_v'):
            y = 1.0 - y
        if self.current_transforms.get('flip_h'):
            x = 1.0 - x

        rotate = self.current_transforms.get('rotate', 0)
        if rotate == 90:
            x, y = 1.0 - y, x
        elif rotate == 180:
            x, y = 1.0 - x, 1.0 - y
        elif rotate == 270:
            x, y = y, 1.0 - x
        return x, y

    def _source_crop_to_preview_rect(self):
        """Convert the normalized source crop to preview-label coordinates."""
        if not self.original_image or not self.crop_box:
            return None

        x0, y0, x1, y1 = self.crop_box
        points = [
            self._source_to_display_normalized(x0, y0),
            self._source_to_display_normalized(x1, y0),
            self._source_to_display_normalized(x0, y1),
            self._source_to_display_normalized(x1, y1),
        ]
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        rect = self.preview_label.image_rect
        left = rect.left() + int(round(min(xs) * (rect.width() - 1)))
        right = rect.left() + int(round(max(xs) * (rect.width() - 1)))
        top = rect.top() + int(round(min(ys) * (rect.height() - 1)))
        bottom = rect.top() + int(round(max(ys) * (rect.height() - 1)))
        return QRect(QPoint(left, top), QPoint(right, bottom)).normalized()

    def _preview_rect_to_source_crop(self, rect):
        """Convert a preview rectangle back into normalized source-image coordinates."""
        if not self.original_image or rect is None or self.preview_label.image_rect.isEmpty():
            return None

        image_rect = self.preview_label.image_rect
        x0 = (rect.left() - image_rect.left()) / max(1, image_rect.width() - 1)
        y0 = (rect.top() - image_rect.top()) / max(1, image_rect.height() - 1)
        x1 = (rect.right() - image_rect.left()) / max(1, image_rect.width() - 1)
        y1 = (rect.bottom() - image_rect.top()) / max(1, image_rect.height() - 1)

        points = [
            self._display_to_source_normalized(x0, y0),
            self._display_to_source_normalized(x1, y0),
            self._display_to_source_normalized(x0, y1),
            self._display_to_source_normalized(x1, y1),
        ]
        sx = [min(1.0, max(0.0, p[0])) for p in points]
        sy = [min(1.0, max(0.0, p[1])) for p in points]
        return (min(sx), min(sy), max(sx), max(sy))

    def set_crop_from_preview_rect(self, rect, finished=False):
        """Store the crop selection in source-image coordinates."""
        crop = self._preview_rect_to_source_crop(rect)
        if crop is None:
            return

        x0, y0, x1, y1 = crop
        if x1 - x0 < 0.005 or y1 - y0 < 0.005:
            return

        self.crop_box = crop
        self.crop_check.setChecked(True)
        self.crop_reset_btn.setEnabled(True)
        self.update_preview()

    def on_crop_section_toggled(self, expanded):
        """Fold/unfold the crop controls panel. Purely a UI visibility
        toggle — independent from whether cropping itself is enabled."""
        self.crop_section.setVisible(expanded)
        self.crop_toggle_btn.setText("\u25be Crop" if expanded else "\u25b8 Crop")

    def on_crop_toggled(self, enabled):
        """Enable or disable the crop overlay without changing the stored selection."""
        self.preview_label.set_crop_enabled(enabled)
        self.crop_reset_btn.setEnabled(enabled)
        self.update_preview()

    def reset_crop(self):
        """Reset the crop to the full source image."""
        self.crop_box = None
        self.crop_check.setChecked(True)
        self.update_preview()

    def on_transform_change(self):
        """Update current_transforms dict from UI controls and refresh the preview."""
        if not self.original_image:
            return
        self.current_transforms['scale'] = self.scale_slider.value() / 100.0
        self.scale_label.setText(f"Scale: {self.current_transforms['scale']:.2f}x")
        for btn in [self.rot_0, self.rot_90, self.rot_180, self.rot_270]:
            if btn.isChecked():
                self.current_transforms['rotate'] = int(btn.text().replace("°", ""))
                break
        self.current_transforms['flip_h'] = self.flip_h_check.isChecked()
        self.current_transforms['flip_v'] = self.flip_v_check.isChecked()
        self.current_transforms['invert'] = self.invert_check.isChecked()

        # Update filter mode and parameters based on the active radio button
        if self.none_radio.isChecked():
            self.current_transforms['filter_mode'] = 'none'
        elif self.bw_radio.isChecked():
            self.current_transforms['filter_mode'] = 'bw'
            self.current_transforms['bw_threshold'] = self.filter_slider.value()
        else:  # color_radio
            self.current_transforms['filter_mode'] = 'color'
            self.current_transforms['color_target'] = self.color_target_combo.currentText()
            self.current_transforms['color_tolerance'] = self.filter_slider.value()

        self.update_preview()

    def refresh_preview_with_status(self):
        """Re-render the preview, showing a brief 'Rendering...' placeholder
        first so the UI gives feedback during the (potentially slow) redraw."""
        if not self.original_image:
            return
        self.preview_label.setText("Rendering...")
        QApplication.processEvents()
        self.update_preview()

    def update_preview(self):
        if not self.original_image:
            return

        # PERFORMANCE FIX: Downscale the source image to fit the UI preview
        # area BEFORE applying transformations. Processing a 12K image for
        # a 600x600px label is the main cause of UI freezing.
        ui_size = self.preview_label.size()
        max_w = max(1, ui_size.width() - 40)
        max_h = max(1, ui_size.height() - 40)

        preview_img = self.original_image.copy()
        preview_img.thumbnail((max_w, max_h), Image.Resampling.BILINEAR)

        # Apply transformations to the small thumbnail (lightning fast).
        # Crop is intentionally not applied here: the full source remains
        # visible so the user can position the crop interactively.
        preview_img = apply_transforms(preview_img, **self.current_transforms)

        q_img = QImage(
            preview_img.tobytes(),
            preview_img.width,
            preview_img.height,
            preview_img.width * 3,
            QImage.Format.Format_RGB888
        )
        pixmap = QPixmap.fromImage(q_img)
        self.preview_label.setPixmap(pixmap)

        px = (self.preview_label.width() - pixmap.width()) // 2
        py = (self.preview_label.height() - pixmap.height()) // 2
        image_rect = QRect(px, py, pixmap.width(), pixmap.height())
        self.preview_label.set_image_rect(image_rect)
        self.preview_label.set_aspect_ratio(self.get_crop_aspect_ratio())

        if self.crop_check.isChecked() and self.crop_box is not None:
            self.preview_label.set_crop_rect(self._source_crop_to_preview_rect())
        else:
            self.preview_label.set_crop_rect(None)

        self._update_crop_label()

        self.preview_label.setStyleSheet(
            "background: #1e1e1e; border: 2px solid #007acc;"
        )

    def load_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select PDF", "", "PDF Files (*.pdf)")
        if not path:
            return

        self.pdf_path = path

        # A new source PDF starts with no crop selection. The user can draw
        # a new crop directly on the preview after the PDF has been rendered.
        self.crop_box = None
        if self.crop_check.isChecked():
            self.crop_label.setText("Crop: Draw a rectangle on the preview")

        self.render_loaded_pdf()

    def render_loaded_pdf(self):
        """Render the currently selected PDF at the active printer density."""
        if not self.pdf_path:
            return False

        pdftoppm = self.pdftoppm_path.text()
        if not pdftoppm or not Path(pdftoppm).exists():
            self.log("Load aborted: pdftoppm not configured.", level="ERROR")
            return False

        # Rasterize at a DPI scaled to the currently selected printer's
        # pixel density, so the source is always sampled finely enough
        # regardless of which preset is active.
        render_dpi = calculate_render_dpi(
            self.res_x_spin.value(), self.res_y_spin.value(),
            self.disp_w_spin.value(), self.disp_h_spin.value()
        )

        try:
            self.original_image = pdf_to_highres_pil(
                self.pdf_path, pdftoppm, dpi=render_dpi
            )
            self.render_dpi = render_dpi
            self.convert_btn.setEnabled(True)
            self.update_preview()
            self.log(
                f"PDF rendered: {self.pdf_path} -> "
                f"{self.original_image.width}x{self.original_image.height}px "
                f"(rendered at {render_dpi} DPI)"
            )
            return True

        except subprocess.TimeoutExpired:
            self.log("PDF rendering timed out.", level="ERROR")
        except Exception as e:
            tb = traceback.format_exc()
            self.log(f"Failed to load PDF: {e}\n{tb}", level="ERROR")

        return False

    def launch_uvtools_gui(self, file_path: str):
        """Launch UVtools GUI with the exported file.
        Assumes UVtools.exe is in the same directory as UVtoolsCmd.exe."""
        cmd_path = Path(self.uvtools_path.text())
        if not cmd_path.exists():
            self.log("Cannot launch UVtools GUI: path is empty or invalid.", level="WARN")
            return
        
        # The GUI executable is in the same folder as the CLI executable
        gui_path = cmd_path.parent / "UVtools.exe"
        
        if gui_path.exists():
            try:
                subprocess.Popen([str(gui_path), file_path])
                self.log(f"Launched UVtools GUI: {gui_path}")
            except Exception as e:
                self.log(f"Failed to launch UVtools GUI: {e}", level="ERROR")
        else:
            self.log(f"UVtools.exe not found next to UVtoolsCmd.exe at: {cmd_path.parent}", level="WARN")

    def convert(self):
        if not self.convert_btn.isEnabled() or not self.original_image:
            return

        default_name = Path(self.pdf_path).stem + f".{self.format_combo.currentText().lower()}"
        default_dir = str(Path(self.pdf_path).parent / default_name)
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Save Printer File", default_dir,
            f"{self.format_combo.currentText()} Files (*.{self.format_combo.currentText().lower()})"
        )
        if not out_path:
            return

        # Bug #5 Fix: Ensure the output path has the correct extension
        fmt = self.format_combo.currentText().upper()
        expected_ext = f".{fmt.lower()}"
        if not out_path.lower().endswith(expected_ext):
            out_path += expected_ext

        self.convert_btn.setEnabled(False)
        self.convert_btn.setText("Processing...")
        self.load_pdf_btn.setEnabled(False)
        QApplication.processEvents()
        self.log(f"Starting export to: {out_path}")

        # Use unique temporary files so an existing export cannot be mistaken
        # for a newly generated intermediate file.
        temp_dir = Path(tempfile.mkdtemp(prefix="pdf2resin_"))
        tmp_png = temp_dir / "render.png"
        tmp_sl1 = temp_dir / "source.sl1"
        tmp_output = temp_dir / Path(out_path).name

        try:
            res_x = self.res_x_spin.value()
            res_y = self.res_y_spin.value()
            disp_w = self.disp_w_spin.value()
            disp_h = self.disp_h_spin.value()

            printer_ppm_x = res_x / disp_w
            printer_ppm_y = res_y / disp_h

            recommended_dpi = calculate_render_dpi(res_x, res_y, disp_w, disp_h)
            if self.render_dpi != recommended_dpi:
                self.log(
                    f"Source render DPI ({self.render_dpi}) does not match the "
                    f"current printer settings ({recommended_dpi}). Re-rendering the PDF.",
                    level="WARN"
                )
                if not self.render_loaded_pdf() or self.render_dpi != recommended_dpi:
                    raise RuntimeError("PDF source could not be rendered at the current printer DPI.")

            pdf_dpi = self.render_dpi
            pdf_ppm = pdf_dpi / 25.4
            pdf_width_mm = self.original_image.width / pdf_ppm
            pdf_height_mm = self.original_image.height / pdf_ppm

            if self.crop_check.isChecked() and self.crop_box is not None:
                x0, y0, x1, y1 = self.crop_box
                pdf_width_mm *= max(0.0, x1 - x0)
                pdf_height_mm *= max(0.0, y1 - y0)

            rotate_deg = self.current_transforms['rotate']
            if rotate_deg in (90, 270):
                pdf_width_mm, pdf_height_mm = pdf_height_mm, pdf_width_mm

            scaled_width_mm = pdf_width_mm * self.current_transforms['scale']
            scaled_height_mm = pdf_height_mm * self.current_transforms['scale']

            target_w_px = int(scaled_width_mm * printer_ppm_x)
            target_h_px = int(scaled_height_mm * printer_ppm_y)

            target_w_px = max(1, target_w_px)
            target_h_px = max(1, target_h_px)

            if target_w_px > res_x or target_h_px > res_y:
                ratio_w = res_x / target_w_px
                ratio_h = res_y / target_h_px
                fit_ratio = min(ratio_w, ratio_h)
                target_w_px = int(target_w_px * fit_ratio)
                target_h_px = int(target_h_px * fit_ratio)
                self.log("Requested size exceeds the printer's display area; auto-scaled down to fit.", level="WARN")

            # Crop the source image before geometric transformations.
            # Crop coordinates are stored as normalized source-image coordinates
            # so they remain independent of preview size and printer resolution.
            source_img = self.original_image.copy()
            if self.crop_check.isChecked() and self.crop_box is not None:
                x0, y0, x1, y1 = self.crop_box
                crop_left = max(0, min(source_img.width - 1, int(round(x0 * source_img.width))))
                crop_top = max(0, min(source_img.height - 1, int(round(y0 * source_img.height))))
                crop_right = max(crop_left + 1, min(source_img.width, int(round(x1 * source_img.width))))
                crop_bottom = max(crop_top + 1, min(source_img.height, int(round(y1 * source_img.height))))
                source_img = source_img.crop(
                    (crop_left, crop_top, crop_right, crop_bottom)
                )

            # --- BUG #1 & #2 FIX: UNIFIED TRANSFORMATION PIPELINE ---
            # Use the exact same transformation pipeline as the preview to ensure
            # WYSIWYG and correctly apply all filters (B/W, Color, Invert, etc.).
            final_img = apply_transforms(
                source_img,
                scale=self.current_transforms['scale'],
                rotate=rotate_deg,
                flip_h=self.current_transforms['flip_h'],
                flip_v=self.current_transforms['flip_v'],
                invert=self.current_transforms['invert'],
                filter_mode=self.current_transforms['filter_mode'],
                bw_threshold=self.current_transforms['bw_threshold'],
                color_target=self.current_transforms['color_target'],
                color_tolerance=self.current_transforms['color_tolerance']
            )
            
            # Resize to the final calculated printer resolution
            final_img = final_img.resize((target_w_px, target_h_px), Image.Resampling.LANCZOS)
            
            # Background outside the design area must stay UNEXPOSED (black)
            canvas = Image.new("RGB", (res_x, res_y), "black")
            paste_x = (res_x - target_w_px) // 2
            paste_y = (res_y - target_h_px) // 2
            canvas.paste(final_img, (paste_x, paste_y))
            
            canvas.save(tmp_png, optimize=False)

            sl1_target = out_path if fmt == "SL1" else str(tmp_sl1)

            build_sl1(
                str(tmp_png), sl1_target, target_w_px, target_h_px,
                res_x, res_y, disp_w, disp_h,
                self.layer_height_spin.value(), self.normal_exp_spin.value(), 
                self.bottom_exp_spin.value(), self.bottom_layers_spin.value(),
                self.num_layers_spin.value()
            )
            self.log(f"SL1 built: {target_w_px}x{target_h_px}px, {self.num_layers_spin.value()} layers.")

            if fmt != "SL1":
                uvtools_exe = self.uvtools_path.text()
                if not uvtools_exe or not Path(uvtools_exe).exists():
                    auto_path = find_executable("UVtoolsCmd", r"SOFTWARE\UVtools")
                    if auto_path:
                        self.uvtools_path.setText(auto_path)
                        self.log(f"UVtools auto-detected and path updated: {auto_path}")
                        uvtools_exe = auto_path
                    else:
                        raise RuntimeError("UVtoolsCmd.exe not configured and auto-detection failed.")

                target_type = FORMAT_TO_UVTOOLS_ENCODER.get(fmt, "?")
                self.log(f"Converting via UVtools: format={fmt}, target={target_type}")
                convert_with_uvtools(uvtools_exe, str(tmp_sl1), str(tmp_output), fmt)
                os.replace(tmp_output, out_path)

            if Path(out_path).exists():
                self.preview_label.setText(f"Success!\nSaved to:\n{out_path}")
                self.preview_label.setStyleSheet("background: #1e1e1e; border: 2px solid #00ff00; color: #fff;")
                self.log(f"Export successful: {out_path}")
                if self.verify_check.isChecked():
                    self.launch_uvtools_gui(out_path)
            else:
                raise RuntimeError(f"Output file was not created at: {out_path}")

        except subprocess.TimeoutExpired:
            self.preview_label.setText("Export timed out")
            self.log("Operation timed out (external tool unresponsive).", level="ERROR")
        except Exception as e:
            tb = traceback.format_exc()
            self.preview_label.setText("Error during export")
            self.log(f"Export failed: {e}\n{tb}", level="ERROR")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            self.convert_btn.setEnabled(True)
            self.convert_btn.setText("Generate & Export")
            self.load_pdf_btn.setEnabled(True)

    def closeEvent(self, event):
        # Persist every user-configurable setting
        self.settings.setValue("pdftoppm_path", self.pdftoppm_path.text())
        self.settings.setValue("uvtools_path", self.uvtools_path.text())
        self.settings.setValue("preset", self.preset_combo.currentText())
        self.settings.setValue("format", self.format_combo.currentText())
        self.settings.setValue("layer_height", self.layer_height_spin.value())
        self.settings.setValue("normal_exp", self.normal_exp_spin.value())
        self.settings.setValue("bottom_exp", self.bottom_exp_spin.value())
        self.settings.setValue("bottom_layers", self.bottom_layers_spin.value())
        self.settings.setValue("num_layers", self.num_layers_spin.value())
        
        # Save scale and transform settings
        self.settings.setValue("scale", self.scale_slider.value())
        self.settings.setValue("rotation", self.current_transforms['rotate'])
        self.settings.setValue("flip_h", self.current_transforms['flip_h'])
        self.settings.setValue("flip_v", self.current_transforms['flip_v'])
        self.settings.setValue("invert", self.current_transforms['invert'])
        self.settings.setValue(
            "crop_enabled", "true" if self.crop_check.isChecked() else "false"
        )
        if self.crop_box is not None:
            self.settings.setValue(
                "crop_box", ",".join(f"{v:.8f}" for v in self.crop_box)
            )
        else:
            self.settings.remove("crop_box")

        # Save filter mode and related parameters
        if self.none_radio.isChecked():
            self.settings.setValue("filter_mode", "none")
        elif self.bw_radio.isChecked():
            self.settings.setValue("filter_mode", "bw")
        else:
            self.settings.setValue("filter_mode", "color")
            
        self.settings.setValue("bw_threshold", self.filter_slider.value())
        self.settings.setValue("color_target", self.color_target_combo.currentText())
        self.settings.setValue("log_to_file", "true" if self.log_to_file_check.isChecked() else "false")
        
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = PDF2ResinGUI()

    def log_uncaught_exception(exc_type, exc_value, exc_tb):
        """Route any exception the normal try/except blocks did not catch
        into the on-screen log too, so unexpected bugs remain visible for
        debugging instead of only appearing on the console (or nowhere,
        if launched without one)."""
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            window.log(f"Unhandled exception:\n{tb_text}", level="ERROR")
        except Exception:
            pass  # the log panel itself must never be the cause of a crash
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = log_uncaught_exception

    window.show()
    sys.exit(app.exec())
