import importlib.util
from pathlib import Path
import sys

from botocore.exceptions import ClientError


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_app_module():
    module_path = Path(__file__).resolve().parents[1] / "lxp-test.py"
    spec = importlib.util.spec_from_file_location("lxp_test_bucket_routing", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_profile(**overrides):
    profile = {
        "id": "aliyun-source",
        "name": "阿里云-杭州",
        "provider": "阿里云OSS",
        "access_key_id": "example-access-key",
        "secret_access_key": "example-secret-key",
        "endpoint_url": "https://oss-cn-hangzhou.aliyuncs.com",
        "region_name": "cn-hangzhou",
        "bucket_name": "hangzhou-bucket",
        "addressing_style": "virtual",
    }
    profile.update(overrides)
    return profile


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def list_objects_v2(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"Contents": []}


class FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def test_verified_client_follows_bucket_endpoint_redirect(monkeypatch):
    module = load_app_module()
    redirect = ClientError(
        {
            "Error": {
                "Code": "PermanentRedirect",
                "Message": "Use the specified endpoint",
                "Endpoint": "oss-cn-wuhan-lr.aliyuncs.com",
            }
        },
        "ListObjectsV2",
    )
    wrong_client = FakeClient(redirect)
    right_client = FakeClient()

    def fake_create(profile):
        if profile["endpoint_url"].endswith("oss-cn-wuhan-lr.aliyuncs.com"):
            return right_client
        return wrong_client

    monkeypatch.setattr(module, "create_s3_client", fake_create)

    client, corrected = module.create_verified_s3_client(
        make_profile(bucket_name="vdctesttest")
    )

    assert client is right_client
    assert corrected["endpoint_url"] == (
        "https://oss-cn-wuhan-lr.aliyuncs.com"
    )
    assert corrected["region_name"] == "cn-wuhan-lr"
    assert wrong_client.calls == [{"Bucket": "vdctesttest", "MaxKeys": 1}]
    assert right_client.calls == [{"Bucket": "vdctesttest", "MaxKeys": 1}]


def test_verified_client_does_not_follow_untrusted_redirect(monkeypatch):
    module = load_app_module()
    redirect = ClientError(
        {
            "Error": {
                "Code": "PermanentRedirect",
                "Endpoint": "oss-cn-wuhan-lr.example.com",
            }
        },
        "ListObjectsV2",
    )
    wrong_client = FakeClient(redirect)
    monkeypatch.setattr(module, "create_s3_client", lambda _profile: wrong_client)

    try:
        module.create_verified_s3_client(
            make_profile(bucket_name="vdctesttest")
        )
    except ClientError as exc:
        assert exc is redirect
    else:
        raise AssertionError("untrusted redirect must not be followed")


def test_bucket_selection_switches_to_matching_saved_profile():
    module = load_app_module()
    source = make_profile()
    target = make_profile(
        id="wuhan-profile",
        name="阿里云-武汉",
        bucket_name="vdctesttest",
        endpoint_url="https://oss-cn-wuhan-lr.aliyuncs.com",
        region_name="cn-wuhan-lr",
    )
    app = module.S3BrowserTool.__new__(module.S3BrowserTool)
    app.profiles = [source, target]
    app.selected_profile_id = source["id"]
    app.current_bucket = source["bucket_name"]
    app.bucket_var = FakeVar("vdctesttest")
    app.get_current_config = lambda: source
    app._persist_profiles = lambda: True
    events = []
    app.reload_profile_controls = lambda: events.append("reload")
    app.connect_current_profile = lambda: events.append("connect")

    app.on_bucket_change(None)

    assert app.selected_profile_id == target["id"]
    assert events == ["reload", "connect"]


def test_bucket_selection_creates_profile_when_no_match_exists():
    module = load_app_module()
    source = make_profile()
    app = module.S3BrowserTool.__new__(module.S3BrowserTool)
    app.profiles = [source]
    app.selected_profile_id = source["id"]
    app.current_bucket = source["bucket_name"]
    app.bucket_var = FakeVar("vdctesttest")
    app.get_current_config = lambda: source
    created = []
    app._create_bucket_profile = (
        lambda profile, bucket: created.append((profile, bucket)) or True
    )

    app.on_bucket_change(None)

    assert created == [(source, "vdctesttest")]
