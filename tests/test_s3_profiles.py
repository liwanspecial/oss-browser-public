import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s3_profiles import (
    ProfileStore,
    ProfileStoreError,
    WindowsDpapiProtector,
    default_config_path,
    default_endpoint_for,
    profiles_for_provider,
    validate_profile,
)


class ReversingProtector:
    """Deterministic test double that never writes credentials as plaintext."""

    def protect(self, plaintext):
        return "encrypted:" + plaintext[::-1]

    def unprotect(self, protected_text):
        assert protected_text.startswith("encrypted:")
        return protected_text[len("encrypted:"):][::-1]


def make_profile(**overrides):
    profile = {
        "id": "aliyun-test",
        "name": "阿里云-测试",
        "provider": "阿里云OSS",
        "access_key_id": "example-access-key",
        "secret_access_key": "example-secret-key",
        "endpoint_url": "https://oss-cn-hangzhou.aliyuncs.com",
        "region_name": "cn-hangzhou",
        "bucket_name": "example-bucket",
        "addressing_style": "virtual",
    }
    profile.update(overrides)
    return profile


def test_default_config_path_uses_roaming_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))

    assert default_config_path() == tmp_path / "LxpS3Browser" / "profiles.json"


def test_profile_store_round_trip_encrypts_credentials(tmp_path):
    path = tmp_path / "profiles.json"
    store = ProfileStore(path=path, protector=ReversingProtector())
    profile = make_profile()

    store.save([profile], selected_profile_id=profile["id"])

    raw_text = path.read_text(encoding="utf-8")
    assert profile["access_key_id"] not in raw_text
    assert profile["secret_access_key"] not in raw_text

    profiles, selected_id = store.load()
    assert profiles == [profile]
    assert selected_id == profile["id"]


def test_profile_store_rejects_duplicate_names(tmp_path):
    store = ProfileStore(
        path=tmp_path / "profiles.json",
        protector=ReversingProtector(),
    )

    with pytest.raises(ProfileStoreError, match="配置名称不能重复"):
        store.save(
            [make_profile(id="one"), make_profile(id="two")],
            selected_profile_id="one",
        )


def test_profile_store_reports_corrupted_file_without_overwriting(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text("not-json", encoding="utf-8")
    store = ProfileStore(path=path, protector=ReversingProtector())

    with pytest.raises(ProfileStoreError, match="配置文件损坏"):
        store.load()

    assert path.read_text(encoding="utf-8") == "not-json"


@pytest.mark.parametrize(
    ("provider", "region", "expected"),
    [
        (
            "华为云OBS",
            "cn-north-4",
            "https://obs.cn-north-4.myhuaweicloud.com",
        ),
        (
            "阿里云OSS",
            "cn-hangzhou",
            "https://oss-cn-hangzhou.aliyuncs.com",
        ),
    ],
)
def test_default_endpoint_for_supported_provider(provider, region, expected):
    assert default_endpoint_for(provider, region) == expected


def test_validate_profile_requires_https_endpoint():
    with pytest.raises(ProfileStoreError, match="Endpoint 必须使用 https"):
        validate_profile(make_profile(endpoint_url="http://example.com"))


def test_profiles_for_provider_only_returns_matching_saved_logins():
    profiles = [
        make_profile(id="ali-one", name="阿里一"),
        make_profile(
            id="huawei-one",
            name="华为一",
            provider="华为云OBS",
            region_name="cn-north-4",
            endpoint_url="https://obs.cn-north-4.myhuaweicloud.com",
        ),
        make_profile(id="ali-two", name="阿里二"),
    ]

    assert [
        profile["id"] for profile in profiles_for_provider(profiles, "阿里云OSS")
    ] == ["ali-one", "ali-two"]


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is Windows-only")
def test_windows_dpapi_round_trip_is_user_scoped():
    protector = WindowsDpapiProtector()
    plaintext = json.dumps(
        {"access_key_id": "ak-for-test", "secret_access_key": "sk-for-test"}
    )

    protected = protector.protect(plaintext)

    assert plaintext not in protected
    assert protector.unprotect(protected) == plaintext
