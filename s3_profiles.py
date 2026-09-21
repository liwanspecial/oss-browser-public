"""User-scoped, encrypted connection profiles for the S3 browser."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
from urllib.parse import urlparse


PROFILE_VERSION = 1
APPLICATION_DIRECTORY = "LxpS3Browser"
PROFILE_FILENAME = "profiles.json"
PROVIDERS = (
    "华为云OBS",
    "阿里云OSS",
    "腾讯云COS",
    "AWS S3",
    "自定义S3",
)
REGIONS_BY_PROVIDER = {
    "华为云OBS": (
        "cn-north-4",
        "cn-north-1",
        "cn-east-2",
        "cn-east-3",
        "cn-south-1",
    ),
    "阿里云OSS": (
        "cn-hangzhou",
        "cn-shanghai",
        "cn-beijing",
        "cn-wuhan-lr",
    ),
    "腾讯云COS": ("ap-guangzhou", "ap-shanghai", "ap-beijing"),
    "AWS S3": ("us-east-1", "us-west-2", "eu-west-1"),
    "自定义S3": (),
}

PUBLIC_FIELDS = (
    "id",
    "name",
    "provider",
    "endpoint_url",
    "region_name",
    "bucket_name",
    "addressing_style",
)
CREDENTIAL_FIELDS = ("access_key_id", "secret_access_key")


class ProfileStoreError(RuntimeError):
    """Raised when profile validation, encryption, or persistence fails."""


def default_config_path() -> Path:
    """Return a stable per-user path that survives executable upgrades."""
    app_data = os.environ.get("APPDATA")
    base_path = Path(app_data) if app_data else Path.home() / ".config"
    return base_path / APPLICATION_DIRECTORY / PROFILE_FILENAME


def default_endpoint_for(provider: str, region: str) -> str:
    region = region.strip()
    if not region:
        return ""
    if provider == "华为云OBS":
        return f"https://obs.{region}.myhuaweicloud.com"
    if provider == "阿里云OSS":
        return f"https://oss-{region}.aliyuncs.com"
    if provider == "腾讯云COS":
        return f"https://cos.{region}.myqcloud.com"
    if provider == "AWS S3":
        if region == "us-east-1":
            return "https://s3.amazonaws.com"
        return f"https://s3.{region}.amazonaws.com"
    return ""


def profiles_for_provider(profiles, provider):
    """Return saved logins for one provider without changing their order."""
    return [profile for profile in profiles if profile.get("provider") == provider]


def find_profile_for_bucket(profiles, source_profile, bucket_name):
    """Find a saved profile for the same provider/account and bucket."""
    provider = str(source_profile.get("provider", "")).strip()
    access_key_id = str(source_profile.get("access_key_id", "")).strip()
    bucket_name = str(bucket_name).strip()
    for profile in profiles:
        if (
            str(profile.get("provider", "")).strip() == provider
            and str(profile.get("access_key_id", "")).strip() == access_key_id
            and str(profile.get("bucket_name", "")).strip() == bucket_name
        ):
            return profile
    return None


def endpoint_url_from_error_response(response):
    """Extract a safe HTTPS endpoint from an S3-compatible error response."""
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    endpoint = str(error.get("Endpoint", "")).strip()
    if not endpoint:
        return None
    if "://" not in endpoint:
        endpoint = f"https://{endpoint}"
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return None
    return f"https://{parsed.netloc}"


def endpoint_matches_provider(provider, endpoint_url):
    """Return whether an auto-discovered endpoint belongs to the provider."""
    parsed = urlparse(str(endpoint_url).strip())
    hostname = (parsed.hostname or "").lower()
    suffixes = {
        "阿里云OSS": ".aliyuncs.com",
        "华为云OBS": ".myhuaweicloud.com",
        "腾讯云COS": ".myqcloud.com",
        "AWS S3": ".amazonaws.com",
    }
    suffix = suffixes.get(provider)
    return bool(
        suffix
        and parsed.scheme == "https"
        and hostname.endswith(suffix)
        and hostname != suffix.lstrip(".")
    )


def region_from_endpoint(provider, endpoint_url):
    """Infer a provider region from a standard public or internal endpoint."""
    parsed = urlparse(str(endpoint_url).strip())
    hostname = (parsed.hostname or "").lower()
    labels = hostname.split(".")

    if provider == "阿里云OSS":
        for label in labels:
            if label.startswith("oss-"):
                region = label[len("oss-"):]
                if region.endswith("-internal"):
                    region = region[:-len("-internal")]
                return region
    if provider == "华为云OBS" and "obs" in labels:
        index = labels.index("obs")
        if index + 1 < len(labels):
            return labels[index + 1]
    if provider == "腾讯云COS" and "cos" in labels:
        index = labels.index("cos")
        if index + 1 < len(labels):
            return labels[index + 1]
    if provider == "AWS S3":
        if hostname == "s3.amazonaws.com":
            return "us-east-1"
        for index, label in enumerate(labels):
            if label == "s3" and index + 1 < len(labels):
                return labels[index + 1]
    return ""


def validate_profile(profile: dict) -> dict:
    """Validate and normalize a decrypted connection profile."""
    if not isinstance(profile, dict):
        raise ProfileStoreError("配置格式无效")

    normalized = {}
    for field in (*PUBLIC_FIELDS, *CREDENTIAL_FIELDS):
        value = profile.get(field, "")
        normalized[field] = str(value).strip()

    normalized["addressing_style"] = (
        normalized["addressing_style"] or "virtual"
    )

    labels = {
        "id": "配置 ID",
        "name": "配置名称",
        "provider": "服务提供商",
        "access_key_id": "AccessKey ID",
        "secret_access_key": "AccessKey Secret",
        "endpoint_url": "Endpoint",
        "region_name": "区域",
        "bucket_name": "Bucket",
    }
    for field, label in labels.items():
        if not normalized[field]:
            raise ProfileStoreError(f"{label}不能为空")

    if normalized["provider"] not in PROVIDERS:
        raise ProfileStoreError("不支持的服务提供商")
    if normalized["addressing_style"] not in ("virtual", "path"):
        raise ProfileStoreError("寻址方式必须是 virtual 或 path")
    if normalized["provider"] == "阿里云OSS":
        normalized["addressing_style"] = "virtual"

    endpoint = normalized["endpoint_url"].rstrip("/")
    parsed = urlparse(endpoint)
    if not parsed.netloc or parsed.path not in ("", "/"):
        raise ProfileStoreError("Endpoint 格式无效")
    if normalized["provider"] != "自定义S3" and parsed.scheme != "https":
        raise ProfileStoreError("Endpoint 必须使用 https")
    if parsed.scheme not in ("http", "https"):
        raise ProfileStoreError("Endpoint 必须以 http:// 或 https:// 开头")
    normalized["endpoint_url"] = endpoint
    return normalized


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class WindowsDpapiProtector:
    """Encrypt values for the current Windows user with DPAPI."""

    CRYPTPROTECT_UI_FORBIDDEN = 0x01

    def __init__(self):
        if os.name != "nt":
            raise ProfileStoreError("Windows DPAPI 仅能在 Windows 上使用")
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    @staticmethod
    def _blob_from_bytes(data: bytes):
        buffer = ctypes.create_string_buffer(data)
        blob = _DataBlob(
            len(data),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        return blob, buffer

    def protect(self, plaintext: str) -> str:
        input_blob, input_buffer = self._blob_from_bytes(
            plaintext.encode("utf-8")
        )
        output_blob = _DataBlob()
        if not self._crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "LxpS3Browser credentials",
            None,
            None,
            None,
            self.CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        ):
            error_code = ctypes.get_last_error()
            raise ProfileStoreError(f"Windows 凭据加密失败（错误 {error_code}）")
        try:
            encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
            return base64.b64encode(encrypted).decode("ascii")
        finally:
            self._kernel32.LocalFree(output_blob.pbData)

    def unprotect(self, protected_text: str) -> str:
        try:
            encrypted = base64.b64decode(protected_text, validate=True)
        except Exception as exc:
            raise ProfileStoreError("加密凭据格式无效") from exc

        input_blob, input_buffer = self._blob_from_bytes(encrypted)
        output_blob = _DataBlob()
        description = wintypes.LPWSTR()
        if not self._crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            ctypes.byref(description),
            None,
            None,
            None,
            self.CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        ):
            error_code = ctypes.get_last_error()
            raise ProfileStoreError(
                f"无法解密本机凭据（错误 {error_code}），请重新录入配置"
            )
        try:
            plaintext = ctypes.string_at(
                output_blob.pbData, output_blob.cbData
            )
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProfileStoreError("解密后的凭据格式无效") from exc
        finally:
            if description:
                self._kernel32.LocalFree(description)
            self._kernel32.LocalFree(output_blob.pbData)


class ProfileStore:
    """Persist profiles atomically while encrypting all credential fields."""

    def __init__(self, path=None, protector=None):
        self.path = Path(path) if path is not None else default_config_path()
        self.protector = protector or WindowsDpapiProtector()

    def load(self):
        if not self.path.exists():
            return [], None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileStoreError(f"配置文件损坏或无法读取：{self.path}") from exc

        if payload.get("version") != PROFILE_VERSION:
            raise ProfileStoreError("不支持的配置文件版本")
        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, list):
            raise ProfileStoreError("配置文件损坏：profiles 字段无效")

        profiles = []
        try:
            for raw_profile in raw_profiles:
                credential_text = self.protector.unprotect(
                    raw_profile["credential_blob"]
                )
                credentials = json.loads(credential_text)
                profile = {
                    field: raw_profile.get(field, "") for field in PUBLIC_FIELDS
                }
                for field in CREDENTIAL_FIELDS:
                    profile[field] = credentials.get(field, "")
                profiles.append(validate_profile(profile))
        except ProfileStoreError:
            raise
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ProfileStoreError("配置文件损坏：凭据字段无效") from exc

        self._validate_unique_profiles(profiles)
        selected_id = payload.get("selected_profile_id")
        profile_ids = {profile["id"] for profile in profiles}
        if selected_id not in profile_ids:
            selected_id = profiles[0]["id"] if profiles else None
        return profiles, selected_id

    def save(self, profiles, selected_profile_id=None):
        normalized_profiles = [validate_profile(profile) for profile in profiles]
        self._validate_unique_profiles(normalized_profiles)
        profile_ids = {profile["id"] for profile in normalized_profiles}
        if normalized_profiles and selected_profile_id not in profile_ids:
            raise ProfileStoreError("当前选中的配置不存在")

        stored_profiles = []
        for profile in normalized_profiles:
            stored = {field: profile[field] for field in PUBLIC_FIELDS}
            credential_payload = {
                field: profile[field] for field in CREDENTIAL_FIELDS
            }
            stored["credential_blob"] = self.protector.protect(
                json.dumps(
                    credential_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            stored_profiles.append(stored)

        payload = {
            "version": PROFILE_VERSION,
            "selected_profile_id": selected_profile_id,
            "profiles": stored_profiles,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary_path, self.path)
        except OSError as exc:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise ProfileStoreError(f"无法保存配置文件：{self.path}") from exc

    @staticmethod
    def _validate_unique_profiles(profiles):
        profile_ids = [profile["id"] for profile in profiles]
        profile_names = [profile["name"].casefold() for profile in profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise ProfileStoreError("配置 ID 不能重复")
        if len(profile_names) != len(set(profile_names)):
            raise ProfileStoreError("配置名称不能重复")
