"""Layer laws (spec sections 2 and 4): core purity by import ALLOWLIST (an
allowlist sweep beats a forbidden-list sweep), one dependency direction."""

import ast
import glob
import os
import re

SEESTAR = os.path.join(os.path.dirname(__file__), "..", "seestar")
CORE = os.path.join(SEESTAR, "core")

CORE_ALLOWED = {
    "__future__",
    "dataclasses",
    "enum",
    "math",
    "typing",
    "collections",
    "collections.abc",
    "functools",
    "itertools",
}


def _imports(path):
    """Yield (module_name, level) for every import in the file, including
    function-local ones (a lazy import is still an import)."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, 0
        elif isinstance(node, ast.ImportFrom):
            yield node.module or "", node.level


def _core_files():
    files = glob.glob(os.path.join(CORE, "*.py"))
    assert files, "seestar/core/ missing?"
    return files


def test_core_imports_only_allowlisted_stdlib():
    bad = []
    for f in _core_files():
        for mod, level in _imports(f):
            if level == 1:  # from . import X: intra-core, fine
                continue
            if level >= 2:  # from .. import X escapes the core package
                bad.append(f"{os.path.basename(f)}: relative parent import ({mod})")
                continue
            root = mod.split(".")[0]
            if mod.startswith("seestar.core"):
                continue
            if root == "seestar" or (mod not in CORE_ALLOWED and root not in CORE_ALLOWED):
                bad.append(f"{os.path.basename(f)}: import {mod}")
    assert not bad, "core purity violations:\n" + "\n".join(bad)


def test_adapters_import_core_only():
    adir = os.path.join(SEESTAR, "adapters")
    if not os.path.isdir(adir):  # ratchet: applies once adapters exist
        return
    bad = []
    for f in glob.glob(os.path.join(adir, "*.py")):
        for mod, level in _imports(f):
            if mod.startswith("seestar") and not mod.startswith("seestar.core"):
                bad.append(f"{os.path.basename(f)}: import {mod}")
            if level == 2:  # from .. import X — resolve manually
                bad.append(f"{os.path.basename(f)}: relative parent import ({mod})")
    assert not bad, "adapter layer violations (adapters import core types only):\n" + "\n".join(bad)


def _bound_subprocess_names(tree: ast.AST) -> tuple[set, set]:
    """Names in a module's namespace that resolve to `subprocess.run`/
    `subprocess.Popen` -- either via `subprocess.run(...)` (a plain or aliased
    `import subprocess`) or `from subprocess import run`/`Popen` (any alias)."""
    module_aliases: set[str] = set()
    direct_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "subprocess":
                    module_aliases.add(a.asname or a.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for a in node.names:
                if a.name in ("run", "Popen"):
                    direct_names.add(a.asname or a.name)
    return module_aliases, direct_names


def _direct_subprocess_calls(path: str) -> list[str]:
    """AST-based (not a text grep) scan for `subprocess.run`/`subprocess.Popen`
    calls reachable from this file's own imports, however the name got bound --
    a text grep would miss a renamed import or `from subprocess import run as r`."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    module_aliases, direct_names = _bound_subprocess_names(tree)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in ("run", "Popen")
            and isinstance(func.value, ast.Name)
            and func.value.id in module_aliases
        ):
            hits.append(f"{func.value.id}.{func.attr}")
        elif isinstance(func, ast.Name) and func.id in direct_names:
            hits.append(func.id)
    return hits


def test_processing_adapters_own_subprocess_only_through_the_bounded_runner():
    """H1 (Task 4): the four concrete tool-shelling adapters must launch every
    process through `_external_process.run_external`'s bounded deadline, never a
    bare `subprocess.run`/`subprocess.Popen` directly -- that runner is the ONE
    place in the adapter layer allowed to own a subprocess call (a hung/slow tool
    call must always be killed by the deadline, never able to hang the pipeline
    by bypassing it). AST-based, not a text grep, so a renamed import or an
    aliased `from subprocess import run as r` can't slip past undetected."""
    adapters_dir = os.path.join(SEESTAR, "adapters")
    concrete = ("siril.py", "graxpert.py", "starnet.py", "exiftool.py")
    bad = {}
    for name in concrete:
        hits = _direct_subprocess_calls(os.path.join(adapters_dir, name))
        if hits:
            bad[name] = hits
    assert not bad, f"direct subprocess call outside _external_process.py: {bad}"


def test_core_has_no_presentation_hooks():
    for f in _core_files():
        with open(f, encoding="utf-8") as fh:
            src = fh.read()
        assert "_STAGE_INFO" not in src, f"{f}: display table leaked into core"
        assert "presentation" not in src, f"{f}: core must not know the presentation layer"


def test_no_target_name_conditionals_in_processing_source():
    """Processing modules must not branch on object identity. presentation.py
    (display copy keyed by pipeline STAGE, not by object) stays exempt by design.

    metadata.py joined the sweep on 2026-08-20, when its six-entry `_COMMON`
    name table was replaced by the offline catalog's spatially-matched
    `common_name` — the last object-name literals in shipped code.

    Moved here from tests/test_profiles.py (Task 23: policy.py/profiles.py/pipeline.py/
    trail.py/annotate.py deleted -- this sweep test must survive the deletions with no
    gap, so it was relocated FIRST, ahead of any of those files' removal). Task 24
    extended the sweep to workspace.py and every seestar/core/*.py module (the pure
    functional core landed in Tasks 17-23 and had never been swept before)."""
    processing = (
        "run.py",
        "orchestrate.py",
        "execute.py",
        "io.py",
        "measure.py",
        "prefs.py",
        "qa.py",
        "framing.py",
        "recipe.py",
        "inspect.py",
        "validate.py",
        "workspace.py",
        "metadata.py",
    )
    # Task 13 moved the tool-calling implementations into seestar/adapters/ — the
    # moved logic stays swept. exiftool.py builds display metadata from a card the
    # shell already composed, but even it must contain no target-name CONDITIONALS
    # (prose comments citing M8/M81 as evidence are fine: comments/docstrings are
    # stripped below before matching).
    adapter_processing = (
        "siril.py",
        "graxpert.py",
        "starnet.py",
        "builtin.py",
        "exiftool.py",
        "_trail_impl.py",
        "_annotate_impl.py",
        "_external_process.py",
    )
    # seestar/core/* is the pure functional core (types + decisions, no I/O) --
    # target-name conditionals have zero business being here, so unlike the
    # `processing`/`adapter_processing` shell lists above (legitimately curated --
    # not every shell file is processing source), the core half buys nothing from
    # curation and everything from drift-proofing. Known-targets Task 9 review
    # finding: a hand-picked `core_processing` tuple + a ONE-DIRECTIONAL
    # completeness check (`len(core_files) == len(core_processing)`) lets a
    # newly-added seestar/core/*.py module go UNSCANNED as long as nobody also
    # remembers to add it to the tuple -- exactly how qa_rules.py/recovery.py fell
    # out of coverage before this fix. Sweeping ALL of `_core_files()` (minus the
    # docstring-only `__init__.py` stub) makes the list self-maintaining: every
    # core file is always scanned, full stop, no tuple to fall out of sync.
    root = os.path.join(os.path.dirname(__file__), "..", "seestar")
    forbidden = re.compile(r"""["'](?:M\s?\d{1,3}|NGC\s?\d{2,4}|IC\s?\d{2,4}|Lagoon|Omega|Hercules|Centaurus)["']""")
    name_branch = re.compile(r"""\btarget\b[^\n=]*(?:==|\bin\b)\s*["(\[]""")
    files = [f for f in glob.glob(os.path.join(root, "*.py")) if os.path.basename(f) in processing]
    adapter_files = [
        f for f in glob.glob(os.path.join(root, "adapters", "*.py")) if os.path.basename(f) in adapter_processing
    ]
    core_files = [f for f in _core_files() if os.path.basename(f) != "__init__.py"]
    # seestar/gui/* is swept in full (self-maintaining, like core): the GUI is shell
    # code, but "category from measurement, never the target's name" binds it too.
    gui_files = list(glob.glob(os.path.join(root, "gui", "*.py")))
    assert gui_files, "seestar/gui package missing from the sweep"
    assert len(files) == len(processing), "processing sweep list out of sync with seestar/*.py"
    assert len(adapter_files) == len(adapter_processing), "adapter sweep list out of sync with seestar/adapters/"
    for f in files + adapter_files + core_files + gui_files:
        src = open(f, encoding="utf-8").read()
        # strip comments/docstrings so prose mentioning an object as evidence is fine
        code = "\n".join(re.sub(r"#.*", "", line) for line in re.sub(r'"""[\s\S]*?"""', "", src).splitlines())
        assert not forbidden.search(code), f"object-name literal in processing module {os.path.basename(f)}"
        assert not name_branch.search(code), f"branch on target name in {os.path.basename(f)}"


def test_no_footprint_or_morphology_reference_in_core_source():
    """Known-targets spec sec5.4/sec7.10 -- INERTNESS is enforced structurally by the
    TYPE boundary (`core.types.PriorObservation` carries `category` only, no
    footprint/morphology field at all -- the type-shape guard). This test is the
    COMPLEMENTARY structural guard: no seestar/core/*.py module's SOURCE may
    reference a footprint/morphology field name at all, so even a future edit to
    `core/planning.py` or `core/policy.py` cannot wire in a footprint/morphology
    consumer without also editing the type it would have to smuggle the value
    through (a `PriorObservation`-shaped value physically cannot carry one). D2b
    (spec sec10, deferred, out of scope) is exactly the consumer this guards
    against landing in the core before its own separate design review.

    Mirrors `test_no_target_name_conditionals_in_processing_source`'s sweep
    structure (glob the module set, strip prose, scan the remainder) but scans
    ALL of `seestar/core/*.py` (`_core_files()`), not a hand-picked subset, and
    uses the stricter `_strip_true_docstrings` AST-based strip (only a real
    docstring is blanked, not every triple-quoted span) -- the same rigor as
    `test_no_display_text_literals_in_core` below, because this is checking for a
    live code reference, not merely a target-name string literal, and a blanket
    triple-quote strip could hide a real leak sitting in a non-docstring
    triple-quoted string (see `_strip_true_docstrings`'s own docstring for the
    precedent that bit the display-literal check)."""
    bad = [os.path.basename(f) for f in _core_files() if _has_footprint_or_morphology_reference(_read(f))]
    assert not bad, "footprint/morphology reference in core source (inertness violation): " + ", ".join(bad)


def test_footprint_morphology_scanner_is_not_vacuous():
    """Non-vacuity proof for the inertness sweep above, run against the exact same
    detector function (`_has_footprint_or_morphology_reference`) the sweep uses --
    not a mutate-real-source-then-revert dance (extra risk of leaving the repo
    dirty for no extra rigor), but a positive/negative control pair proving the
    detector (a) DOES flag a live code reference to a footprint/morphology field
    (so the sweep above isn't vacuously passing because the regex or the
    docstring/comment strip is broken or overly permissive), and (b) does NOT flag
    prose that merely talks ABOUT the invariant (so a future core docstring or
    comment explaining *why* no footprint field exists -- exactly the kind of
    sentence this task's own commit message and docstrings contain -- can't itself
    trip the real sweep)."""
    planted_violation = "def f(state):\n    return state.search_footprints  # a planted violation\n"
    assert _has_footprint_or_morphology_reference(planted_violation)

    planted_violation_2 = 'MORPHOLOGY_TRAITS = obs["morphology_traits"]\n'
    assert _has_footprint_or_morphology_reference(planted_violation_2)

    prose_only = (
        "# This module intentionally carries no footprint or morphology field --\n"
        '"""PriorObservation has no morphology attribute by design."""\n'
        "def f():\n    return None\n"
    )
    assert not _has_footprint_or_morphology_reference(prose_only)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


_FOOTPRINT_MORPHOLOGY_TOKEN = re.compile(
    r"\b(?:search_footprints?|morphology_traits?|footprint|morphology)\w*", re.IGNORECASE
)


def _has_footprint_or_morphology_reference(src: str) -> bool:
    """True if CODE (comments and true docstrings stripped) references a
    footprint/morphology field/identifier. Shared by the sweep test and its
    non-vacuity proof so both exercise identical detection logic."""
    no_docstrings = _strip_true_docstrings(src)
    code = "\n".join(re.sub(r"#.*", "", line) for line in no_docstrings.splitlines())
    return bool(_FOOTPRINT_MORPHOLOGY_TOKEN.search(code))


def _strip_true_docstrings(src: str) -> str:
    """Blank out ONLY true docstrings (the first statement of a Module/ClassDef/
    FunctionDef body, when it's a bare string-literal Expr) by line range, via ast
    -- NOT a blanket triple-quote regex strip. A blanket regex removes every
    triple-quoted span, docstring or not, which would also hide a real leak written
    as a triple-quoted module-level constant, or a triple-quoted return value inside
    a function (Task 24 review finding: both independently confirmed to slip past
    the blanket-regex version and NOT fail the check). Scoped to
    test_no_display_text_literals_in_core ONLY -- the target-name sweep above keeps
    its existing (deliberately docstring-tolerant, comment-tolerant) strip
    untouched; it wants prose evidence like "M8 bp law" to stay exempt."""
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    blank_lines: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            blank_lines.update(range(first.lineno, first.end_lineno + 1))
    return "".join("\n" if i + 1 in blank_lines else line for i, line in enumerate(lines))


def test_no_display_text_literals_in_core():
    """seestar.presentation.STAGE_PRESENTATION is THE display-copy table (novice
    stage labels + tool names). None of those strings may appear as CODE literals in
    seestar/core/*.py -- the pure functional core must not know display copy at all
    (test_core_has_no_presentation_hooks already checks the module name isn't
    imported; this checks the copy itself hasn't leaked in as a bare string, e.g. a
    duplicated label used to build a message).

    Scanned against comment/TRUE-docstring-stripped source, not raw source: a first
    pass at raw-source scanning found "GraXpert" and "StarNet2" (tool VALUES from
    STAGE_PRESENTATION) inside one-line class docstrings in types.py ("GraXpert
    background extraction.", "StarNet2 star/starless separation.") -- these name the
    real-world tool an Operation dataclass maps to, which is legitimate architecture
    documentation, not a display-copy leak (the core still never returns or brands
    that text; an adapter's presentation, not the core's, decides what the user
    sees). A code literal (return value, default, dict entry, f-string, or a
    triple-quoted CONSTANT masquerading as a docstring) is the real defect this test
    targets, so only true docstrings are stripped (`_strip_true_docstrings`, ast-based
    -- a blanket triple-quote regex was tried first and missed exactly that last
    case, see its docstring), and the mutation tests below prove this still has
    teeth against all three shapes of leak (bare code literal, triple-quoted module
    constant, triple-quoted return value)."""
    from seestar.presentation import STAGE_PRESENTATION

    literals = set()
    for label, tool in STAGE_PRESENTATION.values():
        if label:
            literals.add(label)
        if tool:
            literals.add(tool)
    assert literals, "STAGE_PRESENTATION produced no literals to check against"

    core_dir = os.path.join(os.path.dirname(__file__), "..", "seestar", "core")
    bad = []
    for f in glob.glob(os.path.join(core_dir, "*.py")):
        src = open(f, encoding="utf-8").read()
        no_docstrings = _strip_true_docstrings(src)
        code = "\n".join(re.sub(r"#.*", "", line) for line in no_docstrings.splitlines())
        for literal in literals:
            if literal in code:
                bad.append(f"{os.path.basename(f)}: contains display literal {literal!r}")
    assert not bad, "display copy leaked into core:\n" + "\n".join(bad)
