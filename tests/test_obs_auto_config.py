import importlib.util
from pathlib import Path
import sys

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.awsrequest import AWSResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s3_profiles import ProfileStoreError, validate_profile


def load_app_module():
    path = Path(__file__).resolve().parents[1] / "lxp-test.py"
    spec = importlib.util.spec_from_file_location("lxp_test_obs_auto", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def obs_profile(**overrides):
    profile = {
        "id": "obs-test",
        "name": "OBS 测试",
        "provider": "华为云OBS",
        "access_key_id": "example-access-key",
        "secret_access_key": "example-secret-key",
        "endpoint_url": "https://obs.cn-north-4.myhuaweicloud.com",
        "region_name": "cn-north-4",
        "bucket_name": "example-bucket",
        "addressing_style": "virtual",
    }
    profile.update(overrides)
    return profile


class ObsClient:
    def __init__(self, location="cn-east-3", location_error=None, list_error=None):
        self.location = location
        self.location_error = location_error
        self.list_error = list_error
        self.location_calls = []
        self.list_calls = []

    def get_bucket_location(self, **request):
        self.location_calls.append(request)
        if self.location_error:
            raise self.location_error
        return {"LocationConstraint": self.location}

    def list_objects_v2(self, **request):
        self.list_calls.append(request)
        if self.list_error:
            raise self.list_error
        return {"Contents": []}


def wrong_region_error():
    return ClientError({"Error": {"Code": "NoSuchBucket"}}, "ListObjectsV2")


def test_obs_endpoint_can_be_left_blank():
    profile = validate_profile(obs_profile(endpoint_url="", region_name="cn-east-3"))
    assert profile["endpoint_url"] == "https://obs.cn-east-3.myhuaweicloud.com"


def test_obs_unknown_region_uses_a_bootstrap_address():
    profile = validate_profile(obs_profile(endpoint_url="", region_name=""))
    assert profile["region_name"] == "cn-north-4"
    assert profile["endpoint_url"] == "https://obs.cn-north-4.myhuaweicloud.com"


def test_obs_region_can_be_inferred_from_an_existing_endpoint():
    profile = validate_profile(obs_profile(
        region_name="", endpoint_url="https://obs.cn-east-3.myhuaweicloud.com"
    ))
    assert profile["region_name"] == "cn-east-3"


def test_custom_s3_still_requires_an_endpoint():
    with pytest.raises(ProfileStoreError, match="Endpoint不能为空"):
        validate_profile(obs_profile(provider="自定义S3", endpoint_url=""))


def test_obs_uses_virtual_host_addressing():
    assert validate_profile(obs_profile(addressing_style="path"))["addressing_style"] == "virtual"


@pytest.mark.parametrize("body", [
    b'<Location xmlns="http://obs.myhuaweicloud.com/doc/2015-06-30/">cn-east-3</Location>',
    b'<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/">cn-east-3</LocationConstraint>',
    b'<CreateBucketConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><LocationConstraint>cn-east-3</LocationConstraint></CreateBucketConfiguration>',
])
def test_obs_location_xml_is_parsed_by_the_bundled_s3_client(monkeypatch, body):
    module = load_app_module()
    client = module.create_s3_client(obs_profile())
    requests = []

    class RawResponse:
        def stream(self, amt=None, decode_content=False):
            yield body

    def send(request):
        requests.append(request.url)
        return AWSResponse(request.url, 200, {"content-type": "application/xml"}, RawResponse())

    monkeypatch.setattr(client._endpoint.http_session, "send", send)
    try:
        response = client.get_bucket_location(Bucket="example-bucket")
        assert response["LocationConstraint"] == "cn-east-3"
        assert requests == ["https://example-bucket.obs.cn-north-4.myhuaweicloud.com/?location"]
    finally:
        client.close()


def test_obs_discovers_the_bucket_region_only_after_direct_connection_fails(monkeypatch):
    module = load_app_module()
    bootstrap = ObsClient(list_error=wrong_region_error())
    regional = ObsClient()
    profiles = []

    def create(profile):
        profiles.append(dict(profile))
        return bootstrap if profile["region_name"] == "cn-north-4" else regional

    monkeypatch.setattr(module, "create_s3_client", create)
    client, profile = module.create_verified_s3_client(
        obs_profile(endpoint_url="", region_name="")
    )
    assert client is regional
    assert profile["region_name"] == "cn-east-3"
    assert profile["endpoint_url"] == "https://obs.cn-east-3.myhuaweicloud.com"
    assert bootstrap.location_calls == [{"Bucket": "example-bucket"}]
    assert bootstrap.list_calls == [{"Bucket": "example-bucket", "MaxKeys": 1}]
    assert regional.list_calls == [{"Bucket": "example-bucket", "MaxKeys": 1}]
    assert profiles[1]["access_key_id"] == profiles[0]["access_key_id"]
    assert profiles[1]["secret_access_key"] == profiles[0]["secret_access_key"]


def test_obs_location_permission_is_not_required_for_an_existing_working_endpoint(monkeypatch):
    module = load_app_module()
    error = ClientError({"Error": {"Code": "AccessDenied"}}, "GetBucketLocation")
    client = ObsClient(location_error=error)
    monkeypatch.setattr(module, "create_s3_client", lambda _profile: client)
    verified, profile = module.create_verified_s3_client(obs_profile())
    assert verified is client
    assert profile["region_name"] == "cn-north-4"
    assert client.location_calls == []
    assert len(client.list_calls) == 1


@pytest.mark.parametrize("location", ["", "../evil", "cn-east-3.example.com", None])
def test_working_obs_connection_does_not_depend_on_location_query(monkeypatch, location):
    module = load_app_module()
    client = ObsClient(location=location)
    monkeypatch.setattr(module, "create_s3_client", lambda _profile: client)
    verified, profile = module.create_verified_s3_client(obs_profile())
    assert verified is client
    assert profile["region_name"] == "cn-north-4"
    assert client.location_calls == []
    assert len(client.list_calls) == 1


@pytest.mark.parametrize("location", ["", "../evil", "cn-east-3.example.com", None])
def test_invalid_discovery_keeps_the_direct_connection_error(monkeypatch, location):
    module = load_app_module()
    error = wrong_region_error()
    client = ObsClient(location=location, list_error=error)
    created = []

    def create(profile):
        created.append(profile)
        return client

    monkeypatch.setattr(module, "create_s3_client", create)
    with pytest.raises(ClientError) as raised:
        module.create_verified_s3_client(obs_profile())
    assert raised.value is error
    assert len(created) == 1
    assert len(client.list_calls) == 1


def test_obs_network_failure_does_not_claim_a_verified_connection(monkeypatch):
    module = load_app_module()
    error = EndpointConnectionError(endpoint_url="https://obs.cn-north-4.myhuaweicloud.com")
    direct_error = wrong_region_error()
    client = ObsClient(location_error=error, list_error=direct_error)
    monkeypatch.setattr(module, "create_s3_client", lambda _profile: client)
    with pytest.raises(ClientError) as raised:
        module.create_verified_s3_client(obs_profile())
    assert raised.value is direct_error
    assert len(client.list_calls) == 1


def test_direct_access_denied_is_not_hidden_by_region_discovery(monkeypatch):
    module = load_app_module()
    error = ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2")
    client = ObsClient(list_error=error)
    monkeypatch.setattr(module, "create_s3_client", lambda _profile: client)
    with pytest.raises(ClientError) as raised:
        module.create_verified_s3_client(obs_profile())
    assert raised.value is error
    assert client.location_calls == []


def test_region_lookup_access_denied_keeps_the_direct_connection_error(monkeypatch):
    module = load_app_module()
    error = wrong_region_error()
    denied = ClientError({"Error": {"Code": "AccessDenied"}}, "GetBucketLocation")
    client = ObsClient(list_error=error, location_error=denied)
    monkeypatch.setattr(module, "create_s3_client", lambda _profile: client)
    with pytest.raises(ClientError) as raised:
        module.create_verified_s3_client(obs_profile())
    assert raised.value is error


def test_explicit_custom_obs_endpoint_is_preserved(monkeypatch):
    module = load_app_module()
    client = ObsClient()
    monkeypatch.setattr(module, "create_s3_client", lambda _profile: client)
    endpoint = "https://obs-private.cn-north-4.myhuaweicloud.com"
    _, profile = module.create_verified_s3_client(obs_profile(endpoint_url=endpoint))
    assert profile["endpoint_url"] == endpoint
    assert client.location_calls == []
    assert len(client.list_calls) == 1


def test_new_profile_dialog_immediately_shows_a_default_endpoint(monkeypatch):
    module = load_app_module()
    root = module.tk.Tk()
    root.withdraw()
    original_toplevel = module.tk.Toplevel

    def hidden_toplevel(parent):
        window = original_toplevel(parent)
        window.withdraw()
        return window

    monkeypatch.setattr(module.tk, "Toplevel", hidden_toplevel)
    try:
        dialog = module.ProfileDialog(root)
        assert dialog.endpoint_var.get() == "https://obs.cn-north-4.myhuaweicloud.com"
    finally:
        root.destroy()
