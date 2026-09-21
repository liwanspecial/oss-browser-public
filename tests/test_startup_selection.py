import importlib.util
import inspect
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_app_module():
    module_path = Path(__file__).resolve().parents[1] / "lxp-test.py"
    spec = importlib.util.spec_from_file_location("lxp_test_startup", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_profiles_require_explicit_startup_selection():
    module = load_app_module()

    initializer_source = inspect.getsource(module.S3BrowserTool.__init__)

    assert "self.prompt_startup_profile" in initializer_source
    assert "self.root.after(100, self.connect_current_profile)" not in initializer_source


def test_startup_dialog_selects_provider_and_contains_login_fields():
    source = (
        Path(__file__).resolve().parents[1] / "lxp-test.py"
    ).read_text(encoding="utf-8")
    dialog_source = source.split("class StartupLoginDialog:", 1)[1].split(
        "class S3BrowserTool:", 1
    )[0]

    assert "values=PROVIDERS" in dialog_source
    assert "profiles_for_provider" in dialog_source
    assert "AccessKey ID" in dialog_source
    assert "AccessKey Secret" in dialog_source
    assert "Endpoint" in dialog_source
    assert "Bucket" in dialog_source
