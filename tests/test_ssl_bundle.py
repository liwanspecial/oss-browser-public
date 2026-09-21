import importlib.util
from pathlib import Path


def load_app_module():
    module_path = Path(__file__).resolve().parents[1] / "lxp-test.py"
    spec = importlib.util.spec_from_file_location("lxp_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolve_ca_bundle_path_returns_existing_file():
    module = load_app_module()

    ca_bundle = module.resolve_ca_bundle_path()

    assert ca_bundle is not None
    assert Path(ca_bundle).is_file()


def test_sanitize_local_path_part_replaces_windows_forbidden_chars():
    module = load_app_module()

    assert (
        module.sanitize_local_path_part(
            "DMDb_108_2026-07-28-14:28:51.519_2026-07-28-23:15:02.058.tar.gz"
        )
        == "DMDb_108_2026-07-28-14_28_51.519_2026-07-28-23_15_02.058.tar.gz"
    )


def test_safe_join_download_path_sanitizes_each_relative_segment(tmp_path):
    module = load_app_module()

    safe_path = module.safe_join_download_path(
        tmp_path,
        "TEST0000000000001/DMDb_108_2026-07-28-14:28:51.519.tar.gz",
    )

    assert safe_path == str(
        tmp_path
        / "TEST0000000000001"
        / "DMDb_108_2026-07-28-14_28_51.519.tar.gz"
    )
