"""`packaging/` is this repo's local tooling directory (release scripts + the
public-export scanner), which collides in name with the third-party PyPI
package `packaging` (a dependency of astropy/pip/setuptools/etc.).

Extend our `__path__` with `pkgutil` so this stays a valid namespace-style
package: local modules (e.g. `packaging.scan_personal_info`) import normally,
while submodule imports that fall through to the real PyPI package (e.g.
`packaging.version`, used by astropy) still resolve via the merged path
instead of breaking every other test in the suite.
"""

from __future__ import annotations

from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)
