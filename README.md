# PDF2Resin

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)](https://github.com/amigatronic/pdf2resin)

**Direct-to-Print Photolithography Tool**

A lightweight PySide6 desktop application that converts a single-page vector PDF into a native resin-printer exposure file (`.sl1`, `.ctb`, `.photon`, `.goo`, `.cbddlp`, `.phz`), **preserving real physical dimensions**. A 40 mm circle in the PDF will be rendered as a 40 mm circle (a 40 mm cylinder, since the output is a stack of identical layers) on the build plate, independent of the printer brand or LCD resolution.

This tool is specifically designed for **flat masked-exposure workflows** (PCB exposure, stencils, UV curing masks, resin test patterns) where every layer of the output is the same image repeated *N* times, rather than a sliced 3D model.

![Main Window](screenshots/monoscope.jpg)

## ✨ New in v1.3.4

- **Crop tool**: draw a rectangle directly on the preview to select a
  portion of the source PDF. A live readout reports the crop's real size in
  millimeters — both the raw source size and the size that will actually
  land on the build plate, computed with the exact same DPI, scale,
  rotation, and printer pixel-grid math used at export time (so what you
  read is what gets printed, not an estimate).
- **Foldable transform panel**: the crop controls (and, going forward,
  the other transform sections — see `TODO.md`) are now collapsed by
  default behind a `▸ Crop` toggle, keeping the UI uncluttered when those
  options aren't needed for a given job.

![Main Window](screenshots/snapshot_landscape.jpg) 

## ✨ New in v1.3.11

- **Native STL export**: the final rendered image can now be converted
  directly into a binary STL heightmap without requiring UVtools.
- **Grayscale-to-height mapping**: the grayscale value of each image sample
  determines its Z height. Black represents the lowest surface level and white
  represents the maximum height.
- **Configurable STL base**: the Bottom Layers setting is also used to define
  the solid STL base thickness. This keeps the physical STL model consistent
  with the configured resin-print layer plan.
- **Preserved image proportions**: STL generation uses the final image geometry
  and preserves its aspect ratio and physical XY dimensions, avoiding distortion
  when the source image does not match the printer's full LCD aspect ratio.
- **Efficient heightmap sampling**: large raster images are automatically
  downsampled to a maximum grid size of 512 nodes per axis while preserving
  the original grayscale samples.
- **Closed STL geometry**: the generated STL includes the heightmap surface,
  base, side walls, and bottom surface to produce a closed solid suitable
  for standard 3D-printing software.

## 🛠️ How It Works

1. **High-Resolution Rasterization**: Renders the first page of the input PDF to a high-resolution raster image using `pdftoppm` (from Poppler). The rendering DPI is dynamically calculated based on the target printer's pixel density to prevent aliasing.
2. **Physical Scaling**: Computes the exact pixel size needed on the target printer's LCD from the printer's real display dimensions (`disp_w` / `disp_h` in mm) and resolution (`res_x` / `res_y` in px). Scale, rotation, flipping, inversion, cropping, and B/W thresholding are applied in physical units, not arbitrary pixels.
3. **SL1 Archive Generation**: Centers the result on a canvas matching the printer's native resolution and builds a valid `.sl1` archive. Layer height, normal/bottom exposure times, bottom layer count, and the total number of repeated layers are fully configurable.
4. **Format Conversion (Optional)**: If the target format isn't `.sl1`, the tool hands the generated `.sl1` file off to [UVtools](https://github.com/sn4k3/UVtools) (`UVtoolsCmd`) to produce the printer-native file.
5. **Native STL Heightmap Generation (Optional)**: The final processed raster
   image can also be converted directly into a binary STL heightmap. Grayscale
   values are mapped to Z height, while the configured Bottom Layers define the
   solid base thickness. The STL uses the actual cropped/transformed image
   dimensions rather than the full printer canvas, preserving the physical
   proportions of the exported object.

---

## 📦 Requirements

- **Python 3.9+**
- **Python Libraries**: [PySide6](https://pypi.org/project/PySide6/), [Pillow](https://pypi.org/project/Pillow/), [numpy](https://pypi.org/project/numpy/)
  (used by the color filter and native STL heightmap generation)
  `pip install PySide6 Pillow numpy`
- **Poppler** (`pdftoppm` executable): Used for high-DPI PDF rasterization.
  * *Windows*: Download a Poppler build (e.g., from [oschwartz10612/poppler-windows](https://github.com/oschwartz10612/poppler-windows/releases)) and point the app to `pdftoppm.exe`.
  * *Linux*: `sudo apt install poppler-utils`
  * *macOS*: `brew install poppler`
- **UVtools** (`UVtoolsCmd` / `UVtoolsCmd.exe`): Required *only* if exporting to a format other than `.sl1` (CTB, PHOTON, GOO, CBDDLP, PHZ). Not needed if you only ever export `.sl1`.

> **Note:** Both external tool paths are set once in the GUI and persisted between sessions.

---

## 🚀 Usage

1. Launch the app: `python pdf2resin.py`
2. Point it to your `pdftoppm` and (optionally) `UVtoolsCmd` executables.
3. Pick a printer preset (or enter custom resolution/display size).
4. Load your PDF.
5. Adjust scale, rotate, flip, invert, and B/W threshold as needed. Expand the **Crop** section if you need to isolate a portion of the design — the panel shows its exact size in mm as you draw.
6. Set exposure and layer parameters (Bottom Layers, Total Layers, etc.).
7. Choose the output format and click **Generate & Export**.
8. Choose **STL** to generate a native heightmap-based 3D model instead of
   a resin-printer exposure file.

---

## 📋 Supported Output Formats & Encoders

![UVtools conversion](screenshots/uvtools_AMY501.jpg)

| Format     | UVtools Strict Encoder | Notes                                                                                           |
| ---------- | ---------------------- | ----------------------------------------------------------------------------------------------- |
| **SL1**    | `sl1`                  | Written directly. No UVtools call needed.                                                       |
| **CTB**    | `chitubox`             | `.ctb` is shared by multiple encoders in UVtools; the strict name must be used.                 |
| **PHOTON** | `chitubox`             | `.photon` belongs to the Chitubox encoder, *not* `AnycubicPhotonS` (which produces `.photons`). |
| **GOO**    | `goov5`                | Goo format v5.                                                                                  |
| **CBDDLP** | `chitubox`             | Handled by the Chitubox encoder.                                                                |
| **PHZ**    | `phz`                  | Phrozen format.                                                                                 |

> ⚠️ **Important:** If you add support for another format, verify the exact strict encoder name by running `UVtoolsCmd convert` with no arguments. It lists all valid encoder names and the extensions each one accepts. Never assume the encoder name matches the file extension.

## 🧊 Native STL Export

PDF2Resin can generate a binary STL directly from the final processed image,
without using UVtools for STL generation.

The STL is created as a **grayscale heightmap**:

- Black pixels produce the lowest surface height.
- White pixels produce the maximum height.
- Intermediate grayscale values produce proportional intermediate heights.
- The total STL height is determined by the configured number of layers and
  layer height.
- The configured Bottom Layers define the solid base thickness.
- The final cropped and transformed image is used for the STL geometry,
  preserving its physical aspect ratio.
- Large source images are automatically sampled to keep the generated mesh
  manageable.
- The generated STL is a closed solid with a bottom surface, side walls, base,
  and heightmap top surface.

This makes STL export useful for workflows where the same artwork needs to be
converted into a conventional 3D-printable model, for example for FDM/PLA
printing or further processing in a conventional slicer.

> **Important:** STL height is derived from image luminance. A grayscale image
> represents surface height; it does not represent photographic depth.
> Therefore, a normal photograph should not be interpreted as a true 3D depth
> map.

![UVtools conversion, monoscope](screenshots/uvtools_monoscope.jpg)

---

![Main Window, full](screenshots/Main_Window.jpg) 
![Main Window, full](screenshots/snapshot_landscape_cyan.jpg)
## 📏 XY Calibration (Crucial for Photolithography)

This tool computes pixel sizes from the *nominal* display dimensions of your printer (from the preset or your custom values). It **cannot** know:

1. The real, as-manufactured size of your specific LCD panel (datasheet values have manufacturing tolerances, typically ±0.1–0.5%).
2. Your resin's UV light-bleed / overcure margin, which depends on resin, exposure time, and layer height, and always grows the cured part slightly beyond the mask.

Therefore, a 40 mm circle in the source PDF will not automatically print as a physically exact 40 mm cylinder. It will be offset by whatever your printer + resin + exposure settings add or remove.

The included `calibration_pattern.pdf` exists to measure and correct that offset:

1. Load `calibration_pattern.pdf` in the app, using your real printer preset and exposure settings.
2. Export and print it as-is (100% scale, no auto-fit/auto-center).
3. Measure the printed shapes with a caliper (the 100 mm line and the graduated ruler are the most sensitive to read).
4. Compute `correction = measured_mm / nominal_mm`.
5. Multiply your printer preset's `disp_w` / `disp_h` by that factor (increase them if the print came out larger than nominal, decrease if smaller) and save it as a **Custom** preset.
6. Repeat whenever you change resin brand or exposure profile, since light-bleed compensation is resin- and settings-dependent.

---

![Main Window, AMY501](screenshots/AMY501.jpg)
![Main Window, AMY501](screenshots/landscape_uvtools.jpg)

## ⚠️ Known Limitations

- **Single-page PDFs only**: Only the first page of the input PDF is processed.
- **Identical resin layers**: Resin-printer output consists of identical copies
  of the same rendered image. PDF2Resin does not slice the source PDF as a
  conventional 3D model.
- **STL heightmap limitation**: Native STL export interprets grayscale as
  surface height. It does not perform photographic depth estimation and does
  not infer the real-world 3D structure of objects in an image.
- **GUI Thread Blocking**: Conversion runs on the main GUI thread. For very large files or high layer counts on 12K+ printers, the window may become briefly unresponsive during export.

---

## 📄 License

This project is licensed under the **GNU General Public License v3.0** (GPL-3.0).
See the [LICENSE](LICENSE) file for details.

---

## 🤝 Contributing

Contributions, issues, and feature requests are welcome! Feel free to check the [issues page](https://github.com/amigatronic/pdf2resin/issues). Planned work is tracked in `TODO.md`.

## 🙏 Acknowledgments

- Special thanks to [sn4k3](https://github.com/sn4k3) and the [UVtools](https://github.com/sn4k3/UVtools) project for making multi-format slicer file conversion possible.
- Thanks to the [Poppler](https://gitlab.freedesktop.org/poppler/poppler) project for `pdftoppm`, used for high-resolution PDF rasterization.
- Thanks to [oschwartz10612](https://github.com/oschwartz10612) for maintaining [poppler-windows](https://github.com/oschwartz10612/poppler-windows), the prebuilt Windows binaries this project relies on for `pdftoppm`.
