# Acknowledgements

This project exists because other people made astrophotography unusually
approachable and shared powerful tools with the community.

## ZWO Seestar

The Seestar S30 and S30 Pro make it possible to set up quickly, find a target,
capture many short exposures, and receive both an immediate JPG and a
data-rich stacked FITS. That combination is the foundation of this project.

This processor is an independent hobby project and is not affiliated with or
endorsed by ZWO. Its aim is simply to offer Seestar owners another way to finish
the excellent data their telescopes can capture.

## Open-source processing tools

- **[GraXpert](https://graxpert.com)** makes sophisticated gradient removal and
  denoising accessible to astrophotographers.
- **[Siril](https://siril.org)** provides a deep, capable environment for
  astronomical image development and analysis.
- **[StarNet2](https://www.starnetastro.com)** enables thoughtful control of how
  stars and extended subjects share the frame.
- **[ExifTool](https://exiftool.org)** provides exceptionally reliable metadata
  support across image formats.

These tools are not incidental dependencies. They perform the specialist work;
this project coordinates them. Users who enjoy manual editing should explore
the projects directly—their capabilities go far beyond the subset automated
here.

## Astronomical data

The bundled known-target catalog is derived from
[OpenNGC](https://github.com/mattiaverga/OpenNGC), whose contributors assemble
and maintain open NGC/IC data. The derived catalog retains CC-BY-SA-4.0
attribution and provenance. OpenNGC incorporates information from additional
astronomical sources documented by that project.

## Python community

The application also depends on Astropy, NumPy, Pillow, tifffile, PyYAML,
FastAPI, Uvicorn, pywebview, and their transitive dependencies. Their
maintainers provide the scientific and application foundations on which this
small project is built.

License details and project links are collected in `THIRD_PARTY_NOTICES.md` at
the repository root.
