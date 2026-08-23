from pathlib import Path

from tools.check_docs import find_broken_links


def test_current_documentation_has_no_broken_local_links():
    assert find_broken_links() == []


def test_readme_states_the_public_audience_dependencies_and_limits():
    text = Path("README.md").read_text(encoding="utf-8")
    for required in (
        "hobby project",
        "GraXpert",
        "Siril",
        "StarNet2",
        "ExifTool",
        "stacked color FITS",
        "raw subframes",
        "never edits or deletes",
    ):
        assert required in text


def test_installation_uses_the_standalone_starnet_cli_route():
    text = Path("docs/manual/installation.md").read_text(encoding="utf-8")
    for required in (
        "https://starnetastro.com/cli-tools/starnet/",
        "StarNet2 command-line (CLI) package",
        "PixInsight module",
        "starnet2.exe",
        "C:\\Program Files\\StarNet2",
        "starnet2 --version",
    ):
        assert required in text
