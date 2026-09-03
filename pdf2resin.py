"""
PDF2Resin — Direct-to-Print Photolithography
=============================================

Version: v1.0.0

Converts a single-page vector PDF into a native resin-printer exposure
file (SL1, CTB, PHOTON, GOO, CBDDLP, PHZ), preserving real physical
dimensions on the build plate regardless of printer brand or LCD
resolution.

This tool targets flat masked-exposure workflows (photolithography,
PCB exposure, stencils, UV curing masks) where every output layer is
an identical copy of the same source image — it does not slice a 3D
model.

External dependencies:
  - pdftoppm (Poppler)   : high-resolution PDF rasterization
  - UVtoolsCmd (UVtools) : conversion from SL1 to any non-SL1 format
"""

import sys
import zipfile
import subprocess
import traceback
import os
import tempfile
from pathlib import Path
from datetime import datetime

try:
    from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, 
                                    QVBoxLayout, QHBoxLayout, QPushButton,
                                    QLabel, QFileDialog, QSpinBox, QComboBox,
                                    QLineEdit, QGroupBox, QMessageBox, QCheckBox,
                                    QSlider, QDoubleSpinBox, QScrollArea, 
                                    QFormLayout, QButtonGroup, QRadioButton,
                                    QTextEdit)
    from PySide6.QtGui import QPixmap, QImage
    from PySide6.QtCore import Qt, QSettings
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
    print("Install with: pip install PySide6 Pillow")
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

# --- MAP OUTPUT FORMAT TO ITS STRICT UVTOOLS ENCODER NAME ---
# UVtools requires the exact encoder name, not the file extension: several
# encoders can share the same extension (e.g. .ctb is used by both the
# "Chitubox" and "CTBEncrypted" encoders), so passing the extension itself
# is ambiguous and UVtools will reject it.
FORMAT_TO_UVTOOLS_ENCODER = {
    "SL1": "sl1",
    "CTB": "chitubox",     # "ctb" alone is ambiguous with CTBEncrypted
    "PHOTON": "chitubox",  # .photon is handled by the Chitubox encoder,
                            # NOT by AnycubicPhotonS (which only produces .photons)
    "GOO": "goov5",        # Goo format v5
    "CBDDLP": "chitubox",  # CBDDLP is also produced by the Chitubox encoder
    "PHZ": "phz"
}


def calculate_render_dpi(res_x: int, res_y: int, disp_w: float, disp_h: float,
                          min_dpi: int = 1200) -> int:
    """
    Compute the DPI to use when rasterizing the source PDF, scaled to the
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


def apply_transforms(img: Image.Image, scale: float, rotate: int, 
                     flip_h: bool, flip_v: bool, invert: bool,
                     b_w: bool = False, bw_threshold: int = 128) -> Image.Image:
    """Apply geometric and color transformations to the image (used for the live preview)."""
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
    
    if b_w:
        img = apply_b_w(img, threshold=bw_threshold)
        
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
    """Convert an SL1 file to the target format using the UVtools CLI with strict encoder names."""
    
    encoder = FORMAT_TO_UVTOOLS_ENCODER.get(fmt.upper())
    if encoder is None:
        raise RuntimeError(f"No UVtools encoder mapped for format '{fmt}'")
    
    # Pass arguments as a list (shell=False) rather than a shell string:
    # avoids shell-injection risk and handles paths containing spaces or
    # quotes correctly without manual escaping.
    cmd = [uvtools_exe, "convert", sl1_path, encoder, out_path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    
    if not Path(out_path).exists():
        raise RuntimeError(f"UVtools conversion failed:\n{result.stderr}\n\nSTDOUT:\n{result.stdout}")


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
            'b_w': False, 'bw_threshold': 128
        }

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)

        # --- TOP: PREVIEW + CONTROLS ---
        top_layout = QHBoxLayout()
        root_layout.addLayout(top_layout, stretch=1)

        # --- LEFT: PREVIEW AREA ---
        self.preview_label = QLabel("No PDF loaded")
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
        
        self.pdftoppm_path = QLineEdit(self.settings.value("pdftoppm_path", ""))
        self.pdftoppm_path.setPlaceholderText("C:\\poppler\\Library\\bin\\pdftoppm.exe")
        pdftoppm_row = QHBoxLayout()
        pdftoppm_row.addWidget(self.pdftoppm_path)
        pdftoppm_browse_btn = QPushButton("Choose app...")
        pdftoppm_browse_btn.clicked.connect(
            lambda: self.browse_for_tool(self.pdftoppm_path, "pdftoppm", "pdftoppm.exe pdftoppm")
        )
        pdftoppm_row.addWidget(pdftoppm_browse_btn)
        tools_layout.addRow("pdftoppm.exe:", pdftoppm_row)
        
        self.uvtools_path = QLineEdit(self.settings.value("uvtools_path", ""))
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
        self.scale_slider.setValue(100)
        self.scale_slider.valueChanged.connect(self.on_transform_change)
        self.scale_label = QLabel("Scale: 1.00x")
        trans_layout.addWidget(self.scale_label)
        trans_layout.addWidget(self.scale_slider)

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
        
        self.b_w_check = QCheckBox("B/W Conversion (Threshold)")
        self.b_w_check.toggled.connect(self.on_transform_change)
        trans_layout.addWidget(self.b_w_check)

        self.bw_threshold_slider = QSlider(Qt.Horizontal)
        self.bw_threshold_slider.setRange(10, 250)
        self.bw_threshold_slider.setValue(int(self.settings.value("bw_threshold", 128)))
        # Re-running the full-resolution B/W threshold pass on every single
        # pixel of slider travel is too slow to feel responsive, even on
        # fast hardware. valueChanged only updates the label/state (cheap);
        # the actual preview re-render is deferred to sliderReleased.
        self.bw_threshold_slider.valueChanged.connect(self.on_bw_threshold_value_changed)
        self.bw_threshold_slider.sliderReleased.connect(self.on_bw_threshold_released)
        self.bw_threshold_slider.setEnabled(self.b_w_check.isChecked())
        self.bw_threshold_label = QLabel(f"B/W Threshold: {self.bw_threshold_slider.value()}")
        self.bw_threshold_label.setEnabled(self.b_w_check.isChecked())
        trans_layout.addWidget(self.bw_threshold_label)
        trans_layout.addWidget(self.bw_threshold_slider)

        # The threshold slider is only meaningful while B/W conversion is
        # active, so its enabled state always follows the checkbox.
        self.b_w_check.toggled.connect(self.bw_threshold_slider.setEnabled)
        self.b_w_check.toggled.connect(self.bw_threshold_label.setEnabled)
        
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

        # --- STARTUP LOGIC ---
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

        self.log("PDF2Resin v1.0.0 started.")

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
                recommended_format = PRESET_FORMAT_MAP[preset_name].strip()
                self.format_combo.setCurrentText(recommended_format)
            
            self.format_combo.setStyleSheet("")

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

    def on_transform_change(self):
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
        self.current_transforms['b_w'] = self.b_w_check.isChecked()
        self.current_transforms['bw_threshold'] = self.bw_threshold_slider.value()
        self.bw_threshold_label.setText(f"B/W Threshold: {self.bw_threshold_slider.value()}")
        
        self.update_preview()

    def on_bw_threshold_value_changed(self, value: int):
        """Fires continuously while the slider handle moves. Only updates
        the label and stored value - the expensive preview re-render is
        deferred (see on_bw_threshold_released) to keep dragging smooth."""
        self.bw_threshold_label.setText(f"B/W Threshold: {value}")
        self.current_transforms['bw_threshold'] = value
        if not self.bw_threshold_slider.isSliderDown():
            # No drag in progress (e.g. keyboard arrow keys, or a click that
            # jumps the handle without a following release): refresh now,
            # since no sliderReleased signal will otherwise follow.
            self.refresh_preview_with_status()

    def on_bw_threshold_released(self):
        """Fires once the mouse button is released after dragging the slider."""
        self.refresh_preview_with_status()

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
            
        preview_img = apply_transforms(
            self.original_image.copy(), 
            **self.current_transforms
        )
        
        ui_size = self.preview_label.size()
        preview_img.thumbnail((ui_size.width() - 20, ui_size.height() - 20), Image.Resampling.LANCZOS)
        
        q_img = QImage(preview_img.tobytes(), preview_img.width, preview_img.height, preview_img.width * 3, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(q_img)
        
        self.preview_label.setPixmap(pixmap)
        self.preview_label.setStyleSheet("background: #1e1e1e; border: 2px solid #007acc;")

    def load_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select PDF", "", "PDF Files (*.pdf)")
        if not path:
            return
        
        self.pdf_path = path
        
        pdftoppm = self.pdftoppm_path.text()
        if not pdftoppm or not Path(pdftoppm).exists():
            QMessageBox.critical(self, "Error", "pdftoppm not configured.")
            self.log("Load aborted: pdftoppm not configured.", level="ERROR")
            return
        
        # Rasterize at a DPI scaled to the currently selected printer's
        # pixel density, so the source is always sampled finely enough
        # regardless of which preset is active.
        render_dpi = calculate_render_dpi(
            self.res_x_spin.value(), self.res_y_spin.value(),
            self.disp_w_spin.value(), self.disp_h_spin.value()
        )
        
        try:
            self.original_image = pdf_to_highres_pil(path, pdftoppm, dpi=render_dpi)
            self.render_dpi = render_dpi
            self.convert_btn.setEnabled(True)
            self.update_preview()
            self.log(f"PDF loaded: {path} -> {self.original_image.width}x{self.original_image.height}px "
                      f"(rendered at {render_dpi} DPI)")
        except subprocess.TimeoutExpired:
            QMessageBox.critical(self, "Error", "PDF rendering timed out.")
            self.log("PDF rendering timed out.", level="ERROR")
        except Exception as e:
            tb = traceback.format_exc()
            QMessageBox.critical(self, "PDF Error", str(e))
            self.log(f"Failed to load PDF: {e}\n{tb}", level="ERROR")

    def launch_uvtools_gui(self, file_path: str):
        uvtools_gui = Path(self.uvtools_path.text()).parent / "UVtools.exe"
        if uvtools_gui.exists():
            subprocess.Popen([str(uvtools_gui), file_path])

    def convert(self):
        if not self.convert_btn.isEnabled() or not self.original_image:
            return
        
        default_name = Path(self.pdf_path).stem + f".{self.format_combo.currentText().lower()}"
        default_dir = str(Path(self.pdf_path).parent / default_name)
        
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Save Printer File", 
            default_dir,
            f"{self.format_combo.currentText()} Files (*.{self.format_combo.currentText().lower()})"
        )
        if not out_path:
            return
        
        self.convert_btn.setEnabled(False)
        self.convert_btn.setText("Processing...")
        self.load_pdf_btn.setEnabled(False)
        self.log(f"Starting export to: {out_path}")
        
        tmp_png = Path(out_path).with_suffix(".render.png")
        tmp_sl1 = Path(out_path).with_suffix(".sl1")
        
        try:
            res_x = self.res_x_spin.value()
            res_y = self.res_y_spin.value()
            disp_w = self.disp_w_spin.value()
            disp_h = self.disp_h_spin.value()
            
            printer_ppm_x = res_x / disp_w
            printer_ppm_y = res_y / disp_h
            
            # Use the DPI the source image was actually rasterized at
            # (recorded by load_pdf), not a fixed constant, so the
            # physical size of the source PDF is computed correctly even
            # though the render DPI now varies by printer preset.
            pdf_dpi = self.render_dpi
            pdf_ppm = pdf_dpi / 25.4
            pdf_width_mm = self.original_image.width / pdf_ppm
            pdf_height_mm = self.original_image.height / pdf_ppm
            
            rotate_deg = self.current_transforms['rotate']
            if rotate_deg in (90, 270):
                pdf_width_mm, pdf_height_mm = pdf_height_mm, pdf_width_mm
            
            scaled_width_mm = pdf_width_mm * self.current_transforms['scale']
            scaled_height_mm = pdf_height_mm * self.current_transforms['scale']
            
            target_w_px = int(scaled_width_mm * printer_ppm_x)
            target_h_px = int(scaled_height_mm * printer_ppm_y)
            
            if target_w_px > res_x or target_h_px > res_y:
                ratio_w = res_x / target_w_px
                ratio_h = res_y / target_h_px
                fit_ratio = min(ratio_w, ratio_h)
                target_w_px = int(target_w_px * fit_ratio)
                target_h_px = int(target_h_px * fit_ratio)
                self.log("Requested size exceeds the printer's display area; auto-scaled down to fit.", level="WARN")
            
            final_img = self.original_image.copy()
            
            if rotate_deg == 90: 
                final_img = final_img.transpose(Image.Transpose.ROTATE_90)
            elif rotate_deg == 180: 
                final_img = final_img.transpose(Image.Transpose.ROTATE_180)
            elif rotate_deg == 270: 
                final_img = final_img.transpose(Image.Transpose.ROTATE_270)
                
            if self.current_transforms['flip_h']: 
                final_img = final_img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if self.current_transforms['flip_v']: 
                final_img = final_img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            
            final_img = final_img.resize((target_w_px, target_h_px), Image.Resampling.LANCZOS)
            
            if self.current_transforms['invert']:
                final_img = ImageOps.invert(final_img.convert("RGB"))
            
            canvas = Image.new("RGB", (res_x, res_y), "white")
            paste_x = (res_x - target_w_px) // 2
            paste_y = (res_y - target_h_px) // 2
            canvas.paste(final_img, (paste_x, paste_y))
            
            if self.b_w_check.isChecked():
                canvas = apply_b_w(canvas, threshold=self.bw_threshold_slider.value())
            
            canvas.save(tmp_png, optimize=False)
            
            fmt = self.format_combo.currentText().upper()
            
            # For SL1, write directly to out_path: routing it through a
            # temp .sl1 file first and renaming afterward can fail on
            # Windows with WinError 32 if any process still has the temp
            # file briefly locked (e.g. antivirus scan).
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
                    raise RuntimeError("UVtoolsCmd.exe not configured.")
                encoder = FORMAT_TO_UVTOOLS_ENCODER.get(fmt, "?")
                self.log(f"Converting via UVtools: format={fmt}, encoder={encoder}")
                convert_with_uvtools(uvtools_exe, str(tmp_sl1), out_path, fmt)
                
            if Path(out_path).exists():
                self.preview_label.setText(f"Success!\nSaved to:\n{out_path}")
                self.preview_label.setStyleSheet("background: #1e1e1e; border: 2px solid #00ff00; color: #fff;")
                self.log(f"Export successful: {out_path}")
                
                if self.verify_check.isChecked():
                    self.launch_uvtools_gui(out_path)
            else:
                raise RuntimeError(f"Output file was not created at: {out_path}")
                
        except subprocess.TimeoutExpired:
            QMessageBox.critical(self, "Error", "Operation timed out. The external tool may be hanging.")
            self.log("Operation timed out (external tool unresponsive).", level="ERROR")
        except Exception as e:
            tb = traceback.format_exc()
            QMessageBox.critical(self, "Error", f"{str(e)}\n\n{tb}")
            self.preview_label.setText("Error during export")
            self.log(f"Export failed: {e}\n{tb}", level="ERROR")
        finally:
            if tmp_png.exists() and tmp_png != Path(out_path):
                tmp_png.unlink(missing_ok=True)
            if tmp_sl1.exists() and tmp_sl1 != Path(out_path):
                tmp_sl1.unlink(missing_ok=True)
                
            self.convert_btn.setEnabled(True)
            self.convert_btn.setText("Generate & Export")
            self.load_pdf_btn.setEnabled(True)

    def closeEvent(self, event):
        # Persist every user-configurable setting, not just paths and format,
        # so the tool reopens in the exact state it was left in.
        self.settings.setValue("pdftoppm_path", self.pdftoppm_path.text())
        self.settings.setValue("uvtools_path", self.uvtools_path.text())
        self.settings.setValue("preset", self.preset_combo.currentText())
        self.settings.setValue("format", self.format_combo.currentText())
        
        self.settings.setValue("layer_height", self.layer_height_spin.value())
        self.settings.setValue("normal_exp", self.normal_exp_spin.value())
        self.settings.setValue("bottom_exp", self.bottom_exp_spin.value())
        self.settings.setValue("bottom_layers", self.bottom_layers_spin.value())
        self.settings.setValue("num_layers", self.num_layers_spin.value())
        self.settings.setValue("bw_threshold", self.bw_threshold_slider.value())
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
