from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_interfaces_are_one_primary_workflow_with_runtime_split():
    page = (ROOT / "src/reticulum_bolt_on/templates/interfaces_overview.html").read_text()
    nav = (ROOT / "src/reticulum_bolt_on/templates/base.html").read_text()
    assert "Configured Interfaces" in page and "Runtime / System Interfaces" in page
    assert "+ Add Interface" in page and "Runtime / System Interface" not in nav
    assert "Interface Config" not in nav


def test_interface_forms_include_ifac_without_displaying_secret_values():
    page = (ROOT / "src/reticulum_bolt_on/templates/interface_form.html").read_text()
    assert "IFAC Connection Settings" in page and "New IFAC passphrase" in page
    assert "Present / Hidden" in page and "Create disabled backup clone" in page
    assert "Target Host" in page and "clone or create a replacement" in page


def test_no_live_interface_lifecycle_routes_returned_to_navigation():
    source = (ROOT / "src/reticulum_bolt_on/app.py").read_text()
    assert '"/management/interface/review"' not in source
    assert '"attach_interface", "detach_interface", "reload_interface"' not in source
