"""photon-finish: target-agnostic processing for ZWO Seestar S30 / S30 Pro FITS.

Three layers (see CLAUDE.md):
  - engine   : content-adaptive stages, driven by measurement + filter header
  - profiles : purpose presets (showcase / natural / documentation / print)
  - forks    : composition options (star treatment, framing) within a profile
"""

__version__ = "1.1.0"
