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


def test_task_presets_and_managed_style_help_are_present():
    page = (ROOT / "src/reticulum_bolt_on/templates/interface_form.html").read_text()
    for text in ("What are you trying to do?", "User Access TCP", "Trusted Backbone TCP", "Trusted TCP Client",
                 "Local AutoInterface", "RNode / Radio Interface", "Public TCP", "Trusted TCP", "Trusted Upstream",
                 "boundary", "4242", "4243", "TCP Server: let others connect inward", "Using Tailscale"):
        assert text in page
    assert "Advanced interface type and mode" in page


def test_ifac_enable_and_size_workflow_is_present():
    page = (ROOT / "src/reticulum_bolt_on/templates/interface_form.html").read_text()
    for text in ("Enable IFAC", "Use interface default", "Custom size in bits", "ifac_size_mode", "ifac-enabled"):
        assert text in page


def test_mode_selector_has_standard_options_and_preserves_legacy_values():
    page = (ROOT / "src/reticulum_bolt_on/templates/interface_form.html").read_text()
    assert '<select id="interface-mode" name="mode">' in page
    assert "{% for value,label in mode_options %}" in page
    assert "values.get('mode','')|lower == value" in page
    assert "Configured legacy value:" in page
    for value, label in (("full", "Full"), ("boundary", "Boundary"), ("internal", "Internal"),
                         ("gateway", "Gateway"), ("point_to_point", "Point-to-Point"),
                         ("access_point", "Access Point"), ("roaming", "Roaming")):
        assert f'("{value}", "{label}")' in (ROOT / "src/reticulum_bolt_on/app.py").read_text()


def test_mode_presets_choose_expected_defaults():
    page = (ROOT / "src/reticulum_bolt_on/templates/interface_form.html").read_text()
    assert '"user-access":{name:"Public TCP",type:"TCPServerInterface",mode:"boundary"' in page
    assert '"trusted-backbone":{name:"Trusted TCP",type:"TCPServerInterface",mode:"full"' in page
    assert '"trusted-client":{name:"Trusted Upstream",type:"TCPClientInterface",mode:"full"' in page
    assert "mode.value=p.mode" in page


def test_no_live_interface_lifecycle_routes_returned_to_navigation():
    source = (ROOT / "src/reticulum_bolt_on/app.py").read_text()
    assert '"/management/interface/review"' not in source
    assert '"attach_interface", "detach_interface", "reload_interface"' not in source
