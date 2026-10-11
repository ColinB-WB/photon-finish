# Roadmap

This roadmap describes possible work after v1. It is direction, not a release
promise. Work earns priority through user evidence and the
[project tenets](TENETS.md), especially consistent quality and honest
uncertainty.

## Release status

v1.0.0, v1.1.0, and v1.1.1 shipped for macOS as signed, notarized applications,
each checked by installing the released app and comparing a real render against
the committed reference results. Windows runs from source
and its test suite runs on every change, but it has not been verified hands-on and
has no packaged application yet — see [Compatibility](compatibility.md).

## Likely post-v1 improvements

- A packaged Windows application, after the source workflow is proven with
  real users.
- Better installation and helper-tool discovery diagnostics.
- Calibrated background-chroma diagnostics.
- More representative validation captures across S30 and S30 Pro.
- Documentation examples and screenshots based on the released interface.

## Evidence-gated research

- Revisit frame-filling-nebula segmentation only with a new, falsifiable
  approach; the v1 measurement research correctly abstained.
- Evaluate whether category consensus in spatially ambiguous catalog
  neighborhoods recovers meaningful offline value without crossing the
  measurement-authority boundary.
- Explore multi-session integration as a separate source and provenance project.
- Consider whether inert footprint and morphology data earn a measured runtime
  use before broadening the catalog contract.

## Explicitly not a shortcut

Future work should not:

- add target-specific processing rules;
- bulk-import OpenNGC into runtime;
- make catalog identity a measurement authority;
- optimize to a single trophy image;
- silently weaken quality or source-safety gates; or
- treat generated image content as a substitute for captured photons.

The historical pre-v1 research record remains in the private maintainer
repository rather than the public project.
