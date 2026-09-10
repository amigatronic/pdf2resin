# TODO

## 1. Collapsible ("folder") sections for the transform panel

Currently every control (scale, rotation, flip, invert, crop, B/W threshold,
color filter) sits permanently visible in the transforms panel. As more
filters/options get added this becomes cluttered, and most of them are only
relevant to the user for a specific PDF, not every time.

The crop panel already implements the target pattern (`crop_toggle_btn` +
`crop_section`, folded by default, `on_crop_section_toggled`) — extend the
same pattern to the rest of the panel instead of inventing a new mechanism.

- [ ] Extract a small reusable helper (e.g. `make_foldable_section(title, widget)`
      returning `(toggle_btn, container)`) instead of duplicating the
      toggle-button + `QWidget` + `setVisible(False)` boilerplate per section.
- [ ] Group into their own foldable sections:
  - [ ] Scale / Rotate / Flip (geometric transforms)
  - [ ] Invert
  - [ ] Crop (already done — migrate to the shared helper)
  - [ ] B/W threshold
  - [ ] Color filter (target color + tolerance)
- [ ] Decide default expanded/folded state per section (likely: all folded
      except whichever the user last had open — persist via `QSettings`,
      same mechanism already used for tool paths/printer preset).
- [ ] Make sure `update_preview()` / `_update_crop_label()`-style live
      readouts still update correctly while their section is folded (state
      must keep updating in the background, only visibility changes).

## 2. PDF → PNG → STL (grayscale heightmap) → sliced output pipeline

Extend the tool beyond flat masked-exposure export (identical layers) into
an actual heightmap-based 3D slicing pipeline, reusing the existing
PDF→PNG rasterization (`pdf_to_highres_pil`) as the first stage.

- [ ] **PNG → heightmap → STL**
  - [ ] New "Z height (mm)" setting, user-configurable, default `10.0` mm.
  - [ ] Map grayscale value per pixel to Z: black (0) → Z = 0,
        white (255) → Z = configured max height, linear interpolation
        in between (reuse the existing B/W / grayscale conversion logic
        where possible, e.g. `apply_b_w` as a special case with hard
        Z = 0 / Z = max).
  - [ ] Generate a manifold, watertight mesh from the heightmap: top
        surface (2 triangles per pixel/cell), side walls around the
        perimeter, flat base at Z = 0. Decide a strategy to keep triangle
        count manageable on high-DPI renders (e.g. optional
        downsample/decimation step before meshing).
  - [ ] Export as binary STL.
- [ ] **Optional Netfabb pass**
  - [ ] Auto-detect a Netfabb CLI executable, following the same pattern
        already used for `pdftoppm` / `UVtoolsCmd` (PATH, common install
        dirs, "Choose app..." manual override, path persisted in
        `QSettings`).
  - [ ] Make the pass optional and skippable if Netfabb isn't available —
        the mesh generation step above should already produce a valid
        (if unoptimized) STL on its own.
  - [ ] Run repair/optimization on the generated STL, replace it with
        Netfabb's output before the slicing stage.
- [ ] **Slicing for the selected printer**
  - [ ] This is real slicing (per-layer masks that differ from each
        other), which is a different code path from the current
        identical-layers SL1 export — needs its own export function
        rather than reusing `build_sl1`.
  - [ ] Slice the (repaired) STL into layers at the configured layer
        height, using the selected printer preset's resolution/display
        size for the per-layer raster, and the existing exposure settings
        (normal/bottom exposure time, bottom layer count).
  - [ ] Investigate whether UVtools or another existing slicing engine
        can be driven from the STL directly (avoiding writing a slicer
        from scratch), vs. implementing planar-cut rasterization
        in-house.
  - [ ] Output through the existing multi-format export path (SL1 native
        + UVtools conversion to CTB/PHOTON/GOO/CBDDLP/PHZ) once the
        per-layer image stack is produced.
- [ ] Update `README.md`'s "Known Limitations" section once this ships,
      since "identical layers only / does not slice a 3D model" will no
      longer be accurate for this mode.
