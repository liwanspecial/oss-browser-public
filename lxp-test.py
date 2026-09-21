# Source Generated with Decompyle++
# File: lxp-test.pyc (Python 3.11)
# Optimized: extracted constants, merged duplicate methods, improved naming,
#             added comments, unified error handling, added build_obs_path().

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from datetime import datetime, timezone, timedelta
import os
import threading
import time
import re
import math
import csv
from collections import defaultdict
from pathlib import Path
import zipfile
import tarfile
import subprocess
import platform
import sys
import shutil
import uuid

from s3_profiles import (
    PROVIDERS,
    REGIONS_BY_PROVIDER,
    ProfileStore,
    ProfileStoreError,
    default_endpoint_for,
    profiles_for_provider,
    validate_profile,
)


def resolve_ca_bundle_path():
    """Return an existing CA bundle path for normal and PyInstaller runs."""
    candidates = []

    for env_name in ('AWS_CA_BUNDLE', 'REQUESTS_CA_BUNDLE', 'SSL_CERT_FILE'):
        env_value = os.environ.get(env_name)
        if env_value:
            candidates.append(env_value)

    try:
        import certifi
        candidates.append(certifi.where())
    except Exception:
        pass

    try:
        import botocore
        candidates.append(str(Path(botocore.__file__).with_name('cacert.pem')))
    except Exception:
        pass

    bundle_root = getattr(sys, '_MEIPASS', None)
    if bundle_root:
        candidates.extend([
            str(Path(bundle_root) / 'certifi' / 'cacert.pem'),
            str(Path(bundle_root) / 'botocore' / 'cacert.pem'),
        ])

    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    return None


def configure_ssl_certificate_environment():
    ca_bundle_path = resolve_ca_bundle_path()
    if not ca_bundle_path:
        return None

    for env_name in ('AWS_CA_BUNDLE', 'REQUESTS_CA_BUNDLE', 'SSL_CERT_FILE'):
        env_value = os.environ.get(env_name)
        if not env_value or not Path(env_value).is_file():
            os.environ[env_name] = ca_bundle_path
    return ca_bundle_path


CA_BUNDLE_PATH = configure_ssl_certificate_environment()

WINDOWS_RESERVED_FILENAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
}


def sanitize_local_path_part(part):
    """Return a Windows-safe local path segment for an OBS key segment."""
    sanitized = re.sub(r'[<>:"\\|?*\x00-\x1f]', '_', str(part))
    sanitized = sanitized.rstrip(' .')
    if not sanitized or sanitized in ('.', '..'):
        sanitized = '_'

    stem = sanitized.split('.', 1)[0].upper()
    if stem in WINDOWS_RESERVED_FILENAMES:
        sanitized = f'_{sanitized}'
    return sanitized


def safe_join_download_path(base_path, relative_path):
    """Join a download root and OBS relative key using safe local segments."""
    safe_parts = [
        sanitize_local_path_part(part)
        for part in str(relative_path).split('/')
        if part and part != '.'
    ]
    return os.path.join(str(base_path), *safe_parts)


# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
# ── 华为云 OBS 配置 ──
# ═══════════════════════════════════════════════════════════

def create_s3_client(profile):
    """Create a hardened S3-compatible client from a validated profile."""
    profile = validate_profile(profile)
    kwargs = {
        'service_name': 's3',
        'aws_access_key_id': profile['access_key_id'],
        'aws_secret_access_key': profile['secret_access_key'],
        'endpoint_url': profile['endpoint_url'],
        'region_name': profile['region_name'],
        'config': boto3.session.Config(
            s3={'addressing_style': profile['addressing_style']},
            signature_version='s3v4',
            retries={'max_attempts': 5, 'mode': 'standard'},
            connect_timeout=8,
            read_timeout=30,
        ),
    }
    if CA_BUNDLE_PATH:
        kwargs['verify'] = CA_BUNDLE_PATH
    return boto3.client(**kwargs)


class ProfileDialog:
    """Modal editor for one encrypted connection profile."""

    def __init__(self, parent, initial_profile=None, test_callback=None):
        self.initial_profile = initial_profile or {}
        self.test_callback = test_callback
        self.result = None
        self.window = tk.Toplevel(parent)
        self.window.title('编辑云存储配置' if initial_profile else '新增云存储配置')
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.grab_set()

        provider = self.initial_profile.get('provider', '华为云OBS')
        region = self.initial_profile.get('region_name', 'cn-north-4')
        self.profile_id = self.initial_profile.get('id') or uuid.uuid4().hex
        self.name_var = tk.StringVar(value=self.initial_profile.get('name', ''))
        self.provider_var = tk.StringVar(value=provider)
        self.access_key_var = tk.StringVar(
            value=self.initial_profile.get('access_key_id', ''))
        self.secret_key_var = tk.StringVar(
            value=self.initial_profile.get('secret_access_key', ''))
        self.region_var = tk.StringVar(value=region)
        self.endpoint_var = tk.StringVar(
            value=self.initial_profile.get('endpoint_url', ''))
        self.bucket_var = tk.StringVar(
            value=self.initial_profile.get('bucket_name', ''))
        self.show_secret_var = tk.BooleanVar(value=False)
        self._last_suggested_endpoint = default_endpoint_for(provider, region)

        form = ttk.Frame(self.window, padding=12)
        form.grid(row=0, column=0, sticky='nsew')
        fields = (
            ('配置名称:', self.name_var),
            ('AccessKey ID:', self.access_key_var),
            ('AccessKey Secret:', self.secret_key_var),
            ('区域 Region:', self.region_var),
            ('Endpoint:', self.endpoint_var),
            ('Bucket:', self.bucket_var),
        )

        ttk.Label(form, text='服务提供商:').grid(
            row=0, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        self.provider_combo = ttk.Combobox(
            form,
            textvariable=self.provider_var,
            values=PROVIDERS,
            state='readonly',
            width=42,
        )
        self.provider_combo.grid(row=0, column=1, sticky='ew', pady=4)
        self.provider_combo.bind('<<ComboboxSelected>>', self._on_provider_change)

        self.entries = {}
        for row, (label, variable) in enumerate(fields, start=1):
            ttk.Label(form, text=label).grid(
                row=row, column=0, sticky=tk.W, padx=(0, 8), pady=4)
            if label == '区域 Region:':
                entry = ttk.Combobox(
                    form,
                    textvariable=variable,
                    values=REGIONS_BY_PROVIDER.get(provider, ()),
                    width=42,
                )
                entry.bind('<<ComboboxSelected>>', self._on_region_change)
                entry.bind('<FocusOut>', self._on_region_change)
            else:
                entry = ttk.Entry(form, textvariable=variable, width=45)
            if label == 'AccessKey Secret:':
                entry.configure(show='*')
                self.secret_entry = entry
            entry.grid(row=row, column=1, sticky='ew', pady=4)
            self.entries[label] = entry

        ttk.Checkbutton(
            form,
            text='显示 Secret',
            variable=self.show_secret_var,
            command=self._toggle_secret,
        ).grid(row=7, column=1, sticky=tk.W, pady=(0, 8))

        button_row = ttk.Frame(form)
        button_row.grid(row=8, column=0, columnspan=2, sticky=tk.E)
        ttk.Button(button_row, text='测试连接', command=self._test).pack(
            side=tk.LEFT, padx=3)
        ttk.Button(button_row, text='保存', command=self._save).pack(
            side=tk.LEFT, padx=3)
        ttk.Button(button_row, text='取消', command=self.window.destroy).pack(
            side=tk.LEFT, padx=3)

        self.window.protocol('WM_DELETE_WINDOW', self.window.destroy)
        self.window.bind('<Escape>', lambda _event: self.window.destroy())
        self.entries['配置名称:'].focus_set()

    def show(self):
        self.window.wait_window()
        return self.result

    def _toggle_secret(self):
        self.secret_entry.configure(show='' if self.show_secret_var.get() else '*')

    def _on_provider_change(self, _event=None):
        regions = REGIONS_BY_PROVIDER.get(self.provider_var.get(), ())
        region_combo = self.entries['区域 Region:']
        region_combo['values'] = regions
        if regions and self.region_var.get() not in regions:
            self.region_var.set(regions[0])
        self._update_suggested_endpoint()

    def _on_region_change(self, _event=None):
        self._update_suggested_endpoint()

    def _update_suggested_endpoint(self):
        current_endpoint = self.endpoint_var.get().strip()
        suggested = default_endpoint_for(
            self.provider_var.get(), self.region_var.get())
        if not current_endpoint or current_endpoint == self._last_suggested_endpoint:
            self.endpoint_var.set(suggested)
        self._last_suggested_endpoint = suggested

    def _collect_profile(self):
        return validate_profile({
            'id': self.profile_id,
            'name': self.name_var.get(),
            'provider': self.provider_var.get(),
            'access_key_id': self.access_key_var.get(),
            'secret_access_key': self.secret_key_var.get(),
            'endpoint_url': self.endpoint_var.get(),
            'region_name': self.region_var.get(),
            'bucket_name': self.bucket_var.get(),
            'addressing_style': self.initial_profile.get(
                'addressing_style', 'virtual'),
        })

    def _test(self):
        try:
            profile = self._collect_profile()
            if self.test_callback:
                self.test_callback(profile)
            messagebox.showinfo('连接成功', '凭据有效，Bucket 可以访问。',
                                parent=self.window)
        except Exception as exc:
            messagebox.showerror('连接失败', str(exc), parent=self.window)

    def _save(self):
        try:
            self.result = self._collect_profile()
        except ProfileStoreError as exc:
            messagebox.showwarning('配置不完整', str(exc), parent=self.window)
            return
        self.window.destroy()


class StartupLoginDialog:
    """Select a provider, auto-fill saved credentials, and log in explicitly."""

    NEW_PROFILE_LABEL = '＋ 新增配置'

    def __init__(self, parent, profiles, selected_profile_id=None,
                 test_callback=None):
        self.profiles = profiles
        self.test_callback = test_callback
        self.provider_profiles = []
        self.profile_id = None
        self.result = None

        selected_profile = next(
            (profile for profile in profiles
             if profile['id'] == selected_profile_id),
            None,
        )
        initial_provider = (
            selected_profile['provider'] if selected_profile else PROVIDERS[0])

        self.window = tk.Toplevel(parent)
        self.window.title('选择 OSS 并登录')
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.grab_set()

        self.provider_var = tk.StringVar(value=initial_provider)
        self.saved_profile_var = tk.StringVar()
        self.name_var = tk.StringVar()
        self.access_key_var = tk.StringVar()
        self.secret_key_var = tk.StringVar()
        self.region_var = tk.StringVar()
        self.endpoint_var = tk.StringVar()
        self.bucket_var = tk.StringVar()
        self.show_secret_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(
            value='选择服务商后，已保存的信息会自动补充。')
        self._last_suggested_endpoint = ''

        content = ttk.Frame(self.window, padding=16)
        content.grid(row=0, column=0, sticky='nsew')
        content.columnconfigure(1, weight=1)

        ttk.Label(
            content,
            text='1. 选择本次登录的对象存储服务商',
            font=('', 10, 'bold'),
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
        ttk.Label(content, text='服务提供商:').grid(
            row=1, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        self.provider_combo = ttk.Combobox(
            content,
            textvariable=self.provider_var,
            values=PROVIDERS,
            state='readonly',
            width=43,
        )
        self.provider_combo.grid(row=1, column=1, sticky='ew', pady=4)
        self.provider_combo.bind(
            '<<ComboboxSelected>>', self._on_provider_change)

        ttk.Label(content, text='已保存配置:').grid(
            row=2, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        self.saved_profile_combo = ttk.Combobox(
            content,
            textvariable=self.saved_profile_var,
            state='readonly',
            width=43,
        )
        self.saved_profile_combo.grid(row=2, column=1, sticky='ew', pady=4)
        self.saved_profile_combo.bind(
            '<<ComboboxSelected>>', self._on_saved_profile_change)

        ttk.Separator(content).grid(
            row=3, column=0, columnspan=2, sticky='ew', pady=10)
        ttk.Label(
            content,
            text='2. 确认或填写登录信息',
            font=('', 10, 'bold'),
        ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(0, 6))

        fields = (
            ('配置名称:', self.name_var),
            ('AccessKey ID:', self.access_key_var),
            ('AccessKey Secret:', self.secret_key_var),
            ('区域 Region:', self.region_var),
            ('Endpoint:', self.endpoint_var),
            ('Bucket:', self.bucket_var),
        )
        self.entries = {}
        for row, (label, variable) in enumerate(fields, start=5):
            ttk.Label(content, text=label).grid(
                row=row, column=0, sticky=tk.W, padx=(0, 8), pady=4)
            if label == '区域 Region:':
                entry = ttk.Combobox(
                    content, textvariable=variable, width=43)
                entry.bind('<<ComboboxSelected>>', self._on_region_change)
                entry.bind('<FocusOut>', self._on_region_change)
                self.region_combo = entry
            else:
                entry = ttk.Entry(content, textvariable=variable, width=46)
            if label == 'AccessKey Secret:':
                entry.configure(show='*')
                self.secret_entry = entry
            entry.grid(row=row, column=1, sticky='ew', pady=4)
            self.entries[label] = entry

        ttk.Checkbutton(
            content,
            text='显示 Secret',
            variable=self.show_secret_var,
            command=self._toggle_secret,
        ).grid(row=11, column=1, sticky=tk.W)
        ttk.Label(
            content,
            textvariable=self.status_var,
            foreground='#555555',
            wraplength=420,
        ).grid(row=12, column=0, columnspan=2, sticky=tk.W, pady=(8, 12))

        button_row = ttk.Frame(content)
        button_row.grid(row=13, column=0, columnspan=2, sticky=tk.E)
        ttk.Button(
            button_row, text='登录并保存', command=self._login).pack(
                side=tk.LEFT, padx=3)
        ttk.Button(button_row, text='退出软件', command=self._cancel).pack(
            side=tk.LEFT, padx=3)

        self.window.protocol('WM_DELETE_WINDOW', self._cancel)
        self.window.bind('<Escape>', lambda _event: self._cancel())
        self._refresh_saved_profiles(
            preferred_profile_id=selected_profile_id)
        self.provider_combo.focus_set()

    def show(self):
        self.window.wait_window()
        return self.result

    def _on_provider_change(self, _event=None):
        self._refresh_saved_profiles()

    def _refresh_saved_profiles(self, preferred_profile_id=None):
        provider = self.provider_var.get()
        self.provider_profiles = profiles_for_provider(self.profiles, provider)
        values = [profile['name'] for profile in self.provider_profiles]
        values.append(self.NEW_PROFILE_LABEL)
        self.saved_profile_combo['values'] = values

        selected_index = None
        if preferred_profile_id:
            for index, profile in enumerate(self.provider_profiles):
                if profile['id'] == preferred_profile_id:
                    selected_index = index
                    break
        if selected_index is None:
            selected_index = 0 if self.provider_profiles else len(values) - 1
        self.saved_profile_combo.current(selected_index)
        self._on_saved_profile_change()

    def _on_saved_profile_change(self, _event=None):
        index = self.saved_profile_combo.current()
        if 0 <= index < len(self.provider_profiles):
            self._load_profile(self.provider_profiles[index])
        else:
            self._prepare_new_profile()

    def _load_profile(self, profile):
        self.profile_id = profile['id']
        self.name_var.set(profile['name'])
        self.access_key_var.set(profile['access_key_id'])
        self.secret_key_var.set(profile['secret_access_key'])
        self.region_var.set(profile['region_name'])
        self.endpoint_var.set(profile['endpoint_url'])
        self.bucket_var.set(profile['bucket_name'])
        self.region_combo['values'] = REGIONS_BY_PROVIDER.get(
            profile['provider'], ())
        self._last_suggested_endpoint = default_endpoint_for(
            profile['provider'], profile['region_name'])
        self.status_var.set('已自动补充本机保存的登录信息，请确认后登录。')

    def _prepare_new_profile(self):
        provider = self.provider_var.get()
        regions = REGIONS_BY_PROVIDER.get(provider, ())
        region = regions[0] if regions else ''
        self.profile_id = None
        self.name_var.set('')
        self.access_key_var.set('')
        self.secret_key_var.set('')
        self.region_var.set(region)
        self.region_combo['values'] = regions
        self.endpoint_var.set(default_endpoint_for(provider, region))
        self.bucket_var.set('')
        self._last_suggested_endpoint = self.endpoint_var.get()
        self.status_var.set('该服务商没有选中的已保存配置，请填写后登录。')

    def _toggle_secret(self):
        self.secret_entry.configure(
            show='' if self.show_secret_var.get() else '*')

    def _on_region_change(self, _event=None):
        current_endpoint = self.endpoint_var.get().strip()
        suggested = default_endpoint_for(
            self.provider_var.get(), self.region_var.get())
        if not current_endpoint or current_endpoint == self._last_suggested_endpoint:
            self.endpoint_var.set(suggested)
        self._last_suggested_endpoint = suggested

    def _collect_profile(self):
        provider = self.provider_var.get()
        bucket = self.bucket_var.get().strip()
        name = self.name_var.get().strip()
        if not name and bucket:
            name = f'{provider}-{bucket}'
        profile = validate_profile({
            'id': self.profile_id or uuid.uuid4().hex,
            'name': name,
            'provider': provider,
            'access_key_id': self.access_key_var.get(),
            'secret_access_key': self.secret_key_var.get(),
            'endpoint_url': self.endpoint_var.get(),
            'region_name': self.region_var.get(),
            'bucket_name': bucket,
            'addressing_style': 'virtual',
        })
        for existing in self.profiles:
            if (existing['id'] != profile['id']
                    and existing['name'].casefold() == profile['name'].casefold()):
                raise ProfileStoreError('配置名称不能重复')
        return profile

    def _login(self):
        try:
            profile = self._collect_profile()
            self.status_var.set('正在验证连接，请稍候...')
            self.window.update_idletasks()
            if self.test_callback:
                self.test_callback(profile)
            self.result = profile
            self.window.destroy()
        except Exception as exc:
            self.status_var.set('连接失败，请检查填写的信息。')
            messagebox.showerror('登录失败', str(exc), parent=self.window)

    def _cancel(self):
        self.result = None
        self.window.destroy()


class S3BrowserTool:

    # ── 类常量 ──

    # 文件类型映射表（扩展名 → 中文描述）
    FILE_TYPE_MAP = {
        '.txt': '文本文件',
        '.pdf': 'PDF文档',
        '.jpg': 'JPEG图像',
        '.jpeg': 'JPEG图像',
        '.png': 'PNG图像',
        '.zip': '压缩文件',
        '.rar': '压缩文件',
        '.7z': '压缩文件',
        '.doc': 'Word文档',
        '.docx': 'Word文档',
        '.xls': 'Excel文档',
        '.xlsx': 'Excel文档',
        '.ppt': 'PowerPoint文档',
        '.pptx': 'PowerPoint文档',
        '.mp3': '音频文件',
        '.mp4': '视频文件',
        '.avi': '视频文件',
        '.mov': '视频文件',
        '.exe': '可执行文件',
        '.dll': '动态链接库',
        '.py': 'Python脚本',
        '.js': 'JavaScript文件',
        '.html': '网页文件',
        '.css': '样式表文件',
        '.xml': 'XML文件',
        '.json': 'JSON文件',
    }

    # 文件大小单位换算表（单位字符串 → 字节数）
    SIZE_UNITS = {
        'B': 1,
        'KB': 1024,
        'MB': 1048576,          # 1024 ** 2
        'GB': 1073741824,       # 1024 ** 3
        'TB': 1099511627776,    # 1024 ** 4
    }

    # 压缩文件扩展名集合
    ARCHIVE_EXTENSIONS = {
        '.tar', '.rar', '.tar.bz2', '.tbz2',
        '.zip', '.tar.gz', '.7z', '.gz', '.bz2', '.tgz',
    }

    MAX_KEYS = 1000  # S3 list_objects_v2 单次请求最大返回对象数

    # ═══════════════════════════════════════════════════════════
    # ── 初始化 ──
    # ═══════════════════════════════════════════════════════════

    def __init__(self, root):
        self.root = root
        self.root.title('lxp-test')
        self.root.geometry('1400x800')
        self.profile_store = ProfileStore()
        self.profiles = []
        self.selected_profile_id = None
        self.profile_load_error = None
        try:
            self.profiles, self.selected_profile_id = self.profile_store.load()
        except ProfileStoreError as exc:
            self.profile_load_error = exc
        self.s3_client = None
        self.endpoint_url = ''
        self.current_region = ''
        self.current_bucket = ''
        self.current_prefix = ''
        self.local_timezone = timezone(timedelta(hours=8))
        self.use_local_time = True
        self.loading = False
        self.cancel_loading = False
        self.object_cache = defaultdict(dict)
        self.metadata_cache = defaultdict(lambda: defaultdict(dict))
        self.request_count = 0
        self.total_objects = 0
        self.scan_start_time = 0
        self._loading_batch_count = 0
        self.visible_items = 100
        self.current_page = 0
        self.total_pages = 0
        self.full_data = []
        self.search_results = []
        self.is_searching = False
        self.sort_column = 'name'
        self.sort_ascending = True
        self.default_download_path = str(Path.home() / 'Downloads')
        self.auto_extract = True
        self.auto_open = True
        self.status_var = tk.StringVar(value='就绪')
        self.setup_ui()
        self.root.protocol('WM_DELETE_WINDOW', self.on_closing)
        self.reload_profile_controls()
        if self.profile_load_error:
            self.status_var.set('本地配置读取失败')
            self.root.after(100, lambda: messagebox.showerror(
                '配置读取失败',
                f'{self.profile_load_error}\n\n配置文件未被覆盖：\n{self.profile_store.path}',
            ))
        elif self.profiles:
            self.status_var.set('请选择本次登录的云存储配置')
            self.root.after(100, self.prompt_startup_profile)
        else:
            self.status_var.set('尚未配置云存储，请新增配置')
            self.root.after(100, self.add_profile)

    # ═══════════════════════════════════════════════════════════
    # ── UI 构建 ──
    # ═══════════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════
    # ── AWS 连接管理 ──
    # ═══════════════════════════════════════════════════════════

    def get_current_config(self):
        """Return the currently selected decrypted local profile."""
        for profile in self.profiles:
            if profile['id'] == self.selected_profile_id:
                return profile
        return self.profiles[0] if self.profiles else None

    @staticmethod
    def test_profile_connection(profile):
        """Verify credentials using the configured bucket, not account-wide APIs."""
        client = create_s3_client(profile)
        client.list_objects_v2(Bucket=profile['bucket_name'], MaxKeys=1)

    def setup_aws_connection(self, show_error=True):
        """Connect using the selected profile and verify its configured bucket."""
        cfg = self.get_current_config()
        if not cfg:
            self.s3_client = None
            self.status_var.set('尚未配置云存储')
            return False
        try:
            client = create_s3_client(cfg)
            client.list_objects_v2(Bucket=cfg['bucket_name'], MaxKeys=1)
            self.s3_client = client
            self.endpoint_url = cfg['endpoint_url']
            self.current_region = cfg['region_name']
            self.current_bucket = cfg['bucket_name']
            self.status_var.set(
                f"已连接 {cfg['provider']} — {cfg['bucket_name']}")
            return True
        except Exception as exc:
            self.s3_client = None
            self.status_var.set(f'连接失败: {exc}')
            if show_error:
                messagebox.showwarning(
                    '连接失败', f"无法连接配置“{cfg['name']}”：\n{exc}")
            return False

    def connect_current_profile(self):
        self.cancel_loading = True
        self.object_cache.clear()
        self.metadata_cache.clear()
        self.current_prefix = ''
        if not self.setup_aws_connection():
            return
        self.refresh_buckets()
        self.path_var.set(self.build_obs_path(prefix=''))
        self.list_objects_threaded(self.current_bucket, '')

    def prompt_startup_profile(self):
        """Select provider, auto-fill saved login data, then connect explicitly."""
        profile = StartupLoginDialog(
            self.root,
            self.profiles,
            selected_profile_id=self.selected_profile_id,
            test_callback=self.test_profile_connection,
        ).show()
        if profile is None:
            self.on_closing()
            return
        previous_profiles = list(self.profiles)
        previous_selected = self.selected_profile_id
        existing_index = next(
            (index for index, existing in enumerate(self.profiles)
             if existing['id'] == profile['id']),
            None,
        )
        if existing_index is None:
            self.profiles.append(profile)
        else:
            self.profiles[existing_index] = profile
        self.selected_profile_id = profile['id']
        if not self._persist_profiles():
            self.profiles = previous_profiles
            self.selected_profile_id = previous_selected
            return
        self.reload_profile_controls()
        self.connect_current_profile()

    def on_config_change(self, _event=None):
        """Persist the selection and reconnect without asking for credentials."""
        idx = self.config_combo.current()
        if idx < 0 or idx >= len(self.profiles):
            return
        self.selected_profile_id = self.profiles[idx]['id']
        if self._persist_profiles():
            self.reload_profile_controls()
            self.connect_current_profile()

    def reload_profile_controls(self):
        names = [profile['name'] for profile in self.profiles]
        self.config_combo['values'] = names
        cfg = self.get_current_config()
        if not cfg:
            self.config_var.set('')
            self.provider_var.set('')
            self.region_var.set('')
            return
        self.selected_profile_id = cfg['id']
        self.config_var.set(cfg['name'])
        self.provider_var.set(cfg['provider'])
        self.region_var.set(cfg['region_name'])
        self.config_combo.current(self.profiles.index(cfg))

    def _persist_profiles(self):
        if self.profile_load_error:
            messagebox.showerror(
                '无法保存配置',
                '现有配置文件无法解密，为避免覆盖数据，本次没有写入。\n'
                f'请先备份或移走：\n{self.profile_store.path}',
            )
            return False
        try:
            self.profile_store.save(self.profiles, self.selected_profile_id)
            return True
        except ProfileStoreError as exc:
            messagebox.showerror('配置保存失败', str(exc))
            return False

    def add_profile(self):
        if self.profile_load_error:
            self._persist_profiles()
            return
        profile = ProfileDialog(
            self.root, test_callback=self.test_profile_connection).show()
        if not profile:
            return
        previous_selected = self.selected_profile_id
        self.profiles.append(profile)
        self.selected_profile_id = profile['id']
        if self._persist_profiles():
            self.reload_profile_controls()
            self.connect_current_profile()
        else:
            self.profiles.pop()
            self.selected_profile_id = previous_selected

    def edit_profile(self):
        current = self.get_current_config()
        if not current:
            messagebox.showwarning('提示', '请先新增一个配置')
            return
        updated = ProfileDialog(
            self.root,
            initial_profile=current,
            test_callback=self.test_profile_connection,
        ).show()
        if not updated:
            return
        index = self.profiles.index(current)
        previous = self.profiles[index]
        self.profiles[index] = updated
        if self._persist_profiles():
            self.reload_profile_controls()
            self.connect_current_profile()
        else:
            self.profiles[index] = previous

    def delete_profile(self):
        current = self.get_current_config()
        if not current:
            return
        if not messagebox.askyesno(
                '删除配置',
                f"确定删除配置“{current['name']}”吗？\n"
                '只删除本机配置，不会删除云端 Bucket 或文件。'):
            return
        previous_profiles = list(self.profiles)
        previous_selected = self.selected_profile_id
        self.profiles = [
            profile for profile in self.profiles if profile['id'] != current['id']
        ]
        self.selected_profile_id = (
            self.profiles[0]['id'] if self.profiles else None)
        if not self._persist_profiles():
            self.profiles = previous_profiles
            self.selected_profile_id = previous_selected
            return
        self.s3_client = None
        self.current_bucket = ''
        self.current_prefix = ''
        self.reload_profile_controls()
        if self.profiles:
            self.connect_current_profile()
        else:
            self.bucket_combo['values'] = []
            self.bucket_var.set('')
            self.path_var.set('')
            self.status_var.set('配置已删除，请新增配置')


    def setup_ui(self):
        """初始化用户界面 — 三栏布局：左侧面板 + 中央主内容区 + 底部状态栏"""
        # ── 主容器 ──
        self.main_frame = ttk.Frame(self.root)
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        # ── 内容区（左侧面板 + 中央区域 水平排列）──
        self.content_frame = ttk.Frame(self.main_frame)
        self.content_frame.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)

        # ─────────── 左侧面板 ───────────
        self.left_panel = ttk.Frame(self.content_frame, width=280)
        self.left_panel.pack(side=tk.LEFT, fill=tk.Y)
        self.left_panel.pack_propagate(False)

        # === 控制面板 LabelFrame ===
        self.control_group = ttk.LabelFrame(self.left_panel, text='控制面板')
        self.control_group.pack(fill=tk.X, padx=2, pady=2)

        # --- 云服务配置 子区域 ---
        ttk.Label(self.control_group, text='服务提供商:', font=('', 9)).pack(
            anchor=tk.W, pady=(4, 0))
        self.provider_var = tk.StringVar()
        self.provider_combo = ttk.Combobox(
            self.control_group, textvariable=self.provider_var,
            values=PROVIDERS,
            state='disabled',
        )
        self.provider_combo.pack(fill=tk.X, pady=(0, 2))

        ttk.Label(self.control_group, text='配置切换:', font=('', 9)).pack(anchor=tk.W, pady=(4, 0))
        self.config_var = tk.StringVar()
        self.config_combo = ttk.Combobox(
            self.control_group, textvariable=self.config_var, state='readonly')
        self.config_combo.pack(fill=tk.X, pady=(0, 2))
        self.config_combo.bind('<<ComboboxSelected>>', self.on_config_change)

        config_button_row = ttk.Frame(self.control_group)
        config_button_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(config_button_row, text='新增', command=self.add_profile).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 1))
        ttk.Button(config_button_row, text='编辑', command=self.edit_profile).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=1)
        ttk.Button(config_button_row, text='删除', command=self.delete_profile).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(1, 0))

        ttk.Label(self.control_group, text='区域:', font=('', 9)).pack(anchor=tk.W)
        self.region_var = tk.StringVar()
        self.region_combo = ttk.Combobox(
            self.control_group, textvariable=self.region_var,
            state='disabled',
        )
        self.region_combo.pack(fill=tk.X, pady=(0, 6))

        # --- 操作 子区域 ---
        self.action_group = ttk.LabelFrame(self.control_group, text='操作')
        self.action_group.pack(fill=tk.X, pady=2)
        ttk.Button(self.action_group, text='上传文件',
                   command=self.upload_file).pack(fill=tk.X, pady=1)
        ttk.Button(self.action_group, text='上传文件夹',
                   command=self.upload_folder).pack(fill=tk.X, pady=1)
        ttk.Button(self.action_group, text='下载文件',
                   command=self.download_files).pack(fill=tk.X, pady=1)
        ttk.Button(self.action_group, text='新建文件夹',
                   command=self.create_folder).pack(fill=tk.X, pady=1)
        ttk.Button(self.action_group, text='删除对象',
                   command=self.delete_items).pack(fill=tk.X, pady=1)
        ttk.Button(self.action_group, text='复制对象',
                   command=self.copy_items).pack(fill=tk.X, pady=1)
        ttk.Button(self.action_group, text='重命名对象',
                   command=self.rename_item).pack(fill=tk.X, pady=1)
        ttk.Button(self.action_group, text='强制刷新',
                   command=self.force_refresh).pack(fill=tk.X, pady=1)

        # --- 下载设置 LabelFrame ---
        self.download_group = ttk.LabelFrame(self.control_group, text='下载设置')
        self.download_group.pack(fill=tk.X, pady=2)

        self.path_row = ttk.Frame(self.download_group)
        self.path_row.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(self.path_row, text='默认下载路径:').pack(side=tk.LEFT)
        self.download_path_var = tk.StringVar(value=self.default_download_path)
        self.download_path_entry = ttk.Entry(
            self.path_row, textvariable=self.download_path_var, width=16)
        self.download_path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        ttk.Button(self.path_row, text='浏览',
                   command=self.browse_download_path).pack(side=tk.RIGHT)

        self.auto_extract_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.download_group, text='双击自动解压压缩文件',
                        variable=self.auto_extract_var).pack(anchor=tk.W)
        self.auto_open_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.download_group, text='双击自动打开文件',
                        variable=self.auto_open_var).pack(anchor=tk.W)

        # --- 性能设置 LabelFrame ---
        self.perf_group = ttk.LabelFrame(self.control_group, text='性能设置')
        self.perf_group.pack(fill=tk.X, pady=2)

        ttk.Label(self.perf_group, text='每页显示:').pack(anchor=tk.W)
        self.page_size_var = tk.StringVar(value='100')
        self.page_spin = ttk.Spinbox(
            self.perf_group, from_=10, to=1000, increment=10,
            textvariable=self.page_size_var, width=6,
            command=self.on_page_size_change,
        )
        self.page_spin.pack(fill=tk.X)

        ttk.Label(self.perf_group, text='最大线程数:').pack(anchor=tk.W)
        self.threads_var = tk.StringVar(value='10')
        self.threads_spin = ttk.Spinbox(
            self.perf_group, from_=1, to=20,
            textvariable=self.threads_var, width=6,
        )
        self.threads_spin.pack(fill=tk.X)

        ttk.Label(self.perf_group, text='时间显示:').pack(anchor=tk.W)
        self.time_display_var = tk.StringVar(value='默认')
        self.time_combo = ttk.Combobox(
            self.perf_group, textvariable=self.time_display_var,
            values=['默认', 'UTC', '本地时间'], width=8,
        )
        self.time_combo.pack(fill=tk.X)
        self.time_combo.bind('<<ComboboxSelected>>', self.on_time_display_change)

        # ─────────── 中央主内容区 ───────────
        self.center_panel = ttk.Frame(self.content_frame)
        self.center_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(3, 0))

        # --- 顶部工具栏 ---
        self.toolbar_frame = ttk.Frame(self.center_panel)
        self.toolbar_frame.pack(fill=tk.X, pady=(0, 2))

        # 第一行：桶选择
        bucket_row = ttk.Frame(self.toolbar_frame)
        bucket_row.pack(fill=tk.X)
        ttk.Label(bucket_row, text='桶:').pack(side=tk.LEFT)
        self.bucket_var = tk.StringVar()
        self.bucket_combo = ttk.Combobox(
            bucket_row, textvariable=self.bucket_var, width=25, state='readonly',
        )
        self.bucket_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        self.bucket_combo.bind('<<ComboboxSelected>>', self.on_bucket_change)
        ttk.Button(bucket_row, text='刷新桶',
                   command=self.refresh_buckets).pack(side=tk.LEFT)

        # 第二行：导航栏
        self.nav_row = ttk.Frame(self.toolbar_frame)
        self.nav_row.pack(fill=tk.X, pady=(2, 0))
        ttk.Button(self.nav_row, text='刷新',
                   command=self.refresh_current_path).pack(side=tk.LEFT)
        self.back_btn = ttk.Button(self.nav_row, text='返回',
                                   command=self.navigate_up)
        self.back_btn.pack(side=tk.LEFT, padx=1)

        self.path_var = tk.StringVar()
        self.path_entry = ttk.Entry(
            self.nav_row, textvariable=self.path_var)
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        self.path_entry.bind('<Return>', self.navigate_to_path)
        self.go_btn = ttk.Button(
            self.nav_row, text='转到',
            command=lambda: self.navigate_to_path(None))
        self.go_btn.pack(side=tk.LEFT)

        # 搜索和筛选区域
        self.search_filter_group = tk.LabelFrame(
            self.toolbar_frame, text=' 搜索和筛选 ',
            font=('Microsoft YaHei', 9), padx=6, pady=4,
        )
        self.search_filter_group.pack(fill=tk.X, pady=(4, 0))

        sf_row1 = ttk.Frame(self.search_filter_group)
        sf_row1.pack(fill=tk.X, pady=(1, 0))
        ttk.Label(sf_row1, text='名称搜索:').pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(sf_row1, textvariable=self.search_var)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        self.search_entry.bind('<KeyRelease>', self.on_search_change)
        ttk.Button(sf_row1, text='搜索',
                   command=self.search_objects).pack(side=tk.LEFT, padx=1)
        ttk.Button(sf_row1, text='清除',
                   command=self.clear_search).pack(side=tk.LEFT)

        sf_row2 = ttk.Frame(self.search_filter_group)
        sf_row2.pack(fill=tk.X, pady=(1, 2))
        ttk.Label(sf_row2, text='时间范围:').pack(side=tk.LEFT)
        ttk.Label(sf_row2, text='从').pack(side=tk.LEFT, padx=(2, 0))
        self.start_date_var = tk.StringVar()
        self.start_date_entry = ttk.Entry(
            sf_row2, textvariable=self.start_date_var, width=14)
        self.start_date_entry.pack(side=tk.LEFT, padx=2)
        self.start_date_entry.insert(0, 'YYYY-MM-DD')
        self.start_date_entry.bind(
            '<FocusIn>',
            lambda e: self.start_date_entry.delete(0, tk.END)
            if self.start_date_var.get() == 'YYYY-MM-DD' else None,
        )
        ttk.Label(sf_row2, text='到').pack(side=tk.LEFT)
        self.end_date_var = tk.StringVar()
        self.end_date_entry = ttk.Entry(
            sf_row2, textvariable=self.end_date_var, width=14)
        self.end_date_entry.pack(side=tk.LEFT, padx=2)
        self.end_date_entry.insert(0, 'YYYY-MM-DD')
        self.end_date_entry.bind(
            '<FocusIn>',
            lambda e: self.end_date_entry.delete(0, tk.END)
            if self.end_date_var.get() == 'YYYY-MM-DD' else None,
        )
        ttk.Button(sf_row2, text='时间筛选',
                   command=self.filter_by_time).pack(side=tk.LEFT, padx=2)
        ttk.Button(sf_row2, text='清除时间',
                   command=self.clear_time_filter).pack(side=tk.LEFT)
        ttk.Button(sf_row2, text='导出列表',
                   command=self.export_list).pack(side=tk.RIGHT, padx=2)

        # --- 文件列表 Treeview（6 列）---
        self.tree_frame = ttk.Frame(self.center_panel)
        self.tree_frame.pack(fill=tk.BOTH, expand=True)

        self.scroll_y = ttk.Scrollbar(self.tree_frame, orient=tk.VERTICAL)
        self.scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        self.scroll_x = ttk.Scrollbar(self.tree_frame, orient=tk.HORIZONTAL)
        self.scroll_x.pack(side=tk.BOTTOM, fill=tk.X)

        self.columns = ('名称', '类型', '大小', '修改时间', '存储类型', 'ETag')
        self.tree = ttk.Treeview(
            self.tree_frame, columns=self.columns, show='headings',
            yscrollcommand=self.scroll_y.set, xscrollcommand=self.scroll_x.set,
        )
        self.scroll_y.config(command=self.tree.yview)
        self.scroll_x.config(command=self.tree.xview)
        self.tree.pack(fill=tk.BOTH, expand=True)

        col_config = [
            ('名称',     '名称',     300, tk.W),
            ('类型',     '类型',     120, tk.CENTER),
            ('大小',     '大小',     100, tk.E),
            ('修改时间', '修改时间',  150, tk.CENTER),
            ('存储类型', '存储类型',  100, tk.CENTER),
            ('ETag',     'ETag',     200, tk.CENTER),
        ]
        for col, text, width, anchor in col_config:
            self.tree.heading(
                col, text=text, anchor=anchor,
                command=lambda c=col: self.sort_treeview(c))
            self.tree.column(col, width=width, anchor=anchor)

        self.tree.bind('<Double-1>', self.on_item_double_click)
        self.tree.bind('<<TreeviewSelect>>', self.on_item_selected)
        self.tree.bind('<Button-3>', self.on_right_click)

        # ─────────── 底部状态栏 ───────────
        self.bottom_frame = ttk.Frame(self.main_frame)
        self.bottom_frame.pack(fill=tk.X, padx=5, pady=(2, 0))

        # 左侧：连接状态
        self.status_label = ttk.Label(
            self.bottom_frame, textvariable=self.status_var,
            relief=tk.SUNKEN, anchor=tk.W)
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 右侧：分页控件
        self.page_ctrl_frame = ttk.Frame(self.bottom_frame)
        self.page_ctrl_frame.pack(side=tk.RIGHT)

        self.prev_btn = ttk.Button(
            self.page_ctrl_frame, text='上一页',
            command=self.prev_page, state=tk.DISABLED)
        self.prev_btn.pack(side=tk.LEFT, padx=1)

        self.page_info = ttk.Label(
            self.page_ctrl_frame, text='第 0 页 / 共 0 页')
        self.page_info.pack(side=tk.LEFT, padx=5)

        self.next_btn = ttk.Button(
            self.page_ctrl_frame, text='下一页',
            command=self.next_page, state=tk.DISABLED)
        self.next_btn.pack(side=tk.LEFT, padx=1)

        ttk.Label(self.page_ctrl_frame, text='跳转到:').pack(
            side=tk.LEFT, padx=5)
        self.jump_page_var = tk.StringVar()
        self.jump_page_entry = ttk.Entry(
            self.page_ctrl_frame, textvariable=self.jump_page_var, width=5)
        self.jump_page_entry.pack(side=tk.LEFT)
        self.jump_page_entry.bind('<Return>', self.jump_to_page)
        ttk.Button(self.page_ctrl_frame, text='跳转',
                   command=lambda: self.jump_to_page(None)).pack(
                       side=tk.LEFT, padx=2)

        # 进度条（全局）
        self.progress = ttk.Progressbar(self.main_frame, mode='indeterminate')
        self.progress.pack(fill=tk.X, padx=5)

    # ═══════════════════════════════════════════════════════════
    # ── 路径辅助 ──
    # ═══════════════════════════════════════════════════════════

    def build_obs_path(self, bucket=None, prefix=None):
        """Build a provider-appropriate display path.

        Args:
            bucket: 桶名；为 None 时使用 self.current_bucket。
            prefix: 前缀路径；为 None 时使用 self.current_prefix。

        Returns:
            str: 形如 'obs://bucket/prefix/' 或 'oss://bucket/prefix/'。
        """
        b = bucket if bucket is not None else self.current_bucket
        p = prefix if prefix is not None else self.current_prefix
        cfg = self.get_current_config() or {}
        scheme = {
            '华为云OBS': 'obs',
            '阿里云OSS': 'oss',
        }.get(cfg.get('provider'), 's3')
        return f'{scheme}://{b}/{p}'

    # ═══════════════════════════════════════════════════════════
    # ── 文件工具 ──
    # ═══════════════════════════════════════════════════════════

    def browse_download_path(self):
        """浏览选择默认下载路径"""
        path = filedialog.askdirectory(
            title='选择默认下载路径', initialdir=self.default_download_path)
        if path:
            self.default_download_path = path
            self.download_path_var.set(path)
        return None

    def is_archive_file(self, filename):
        """判断文件是否为压缩文件"""
        filename_lower = filename.lower()
        is_archive = any(
            filename_lower.endswith(ext) for ext in self.ARCHIVE_EXTENSIONS)
        print(f'文件 {filename} 是否为压缩文件: {is_archive}')
        return is_archive

    def extract_archive(self, archive_path, extract_to):
        """解压文件（支持 zip / tar.gz / tar.bz2 / tar）"""
        try:
            print(f'开始解压: {archive_path} -> {extract_to}')
            if not os.path.exists(archive_path):
                print(f'文件不存在: {archive_path}')
                return False
            if archive_path.endswith('.zip'):
                with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_to)
                print(f'ZIP解压成功: {archive_path}')
                return True
            elif archive_path.endswith('.tar.gz') or archive_path.endswith('.tgz'):
                with tarfile.open(archive_path, 'r:gz') as tar_ref:
                    tar_ref.extractall(extract_to)
                print(f'TAR.GZ解压成功: {archive_path}')
                return True
            elif archive_path.endswith('.tar.bz2') or archive_path.endswith('.tbz2'):
                with tarfile.open(archive_path, 'r:bz2') as tar_ref:
                    tar_ref.extractall(extract_to)
                print(f'TAR.BZ2解压成功: {archive_path}')
                return True
            elif archive_path.endswith('.tar'):
                with tarfile.open(archive_path, 'r') as tar_ref:
                    tar_ref.extractall(extract_to)
                print(f'TAR解压成功: {archive_path}')
                return True
            else:
                print(f'不支持的压缩格式: {archive_path}')
                return False
        except Exception as e:
            print(f'解压失败: {e}')
            import traceback
            traceback.print_exc()
            return False

    def open_file(self, file_path):
        """使用系统默认程序打开文件或目录"""
        try:
            print(f'尝试打开: {file_path}')
            if not os.path.exists(file_path):
                print(f'路径不存在: {file_path}')
                return False
            if platform.system() == 'Windows':
                os.startfile(file_path)
            elif platform.system() == 'Darwin':
                subprocess.run(['open', file_path])
            else:
                subprocess.run(['xdg-open', file_path])
            print(f'打开成功: {file_path}')
            return True
        except Exception as e:
            print(f'打开文件失败: {e}')
            return False

    def get_extracted_files(self, extract_dir):
        """递归获取解压目录中的所有文件路径列表"""
        try:
            all_files = []
            for root, _dirs, files in os.walk(extract_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    if os.path.isfile(file_path):
                        all_files.append(file_path)
            return all_files
        except Exception as e:
            print(f'获取解压文件列表失败: {e}')
            return []

    def open_extracted_content(self, extract_dir, extracted_files):
        """根据解压结果自动打开文件或目录"""
        try:
            if len(extracted_files) == 1:
                single_file = extracted_files[0]
                print(f'解压后只有一个文件，直接打开: {single_file}')
                self.open_file(single_file)
            elif len(extracted_files) > 1:
                print(f'解压后有多个文件，打开目录: {extract_dir}')
                self.open_file(extract_dir)
            else:
                print(f'解压后没有文件，打开目录: {extract_dir}')
                self.open_file(extract_dir)
        except Exception as e:
            print(f'打开解压内容失败: {e}')
            self.open_file(extract_dir)

    # ═══════════════════════════════════════════════════════════
    # ── 下载与解压 ──
    # ═══════════════════════════════════════════════════════════

    def download_and_open_file(self, filename):
        """下载文件到默认路径，若需解压则自动解压，然后打开"""
        if not self.s3_client:
            messagebox.showwarning('警告', '请先连接S3服务')
            return
        download_path = self.download_path_var.get()
        if not download_path:
            download_path = self.default_download_path
            self.download_path_var.set(download_path)
        os.makedirs(download_path, exist_ok=True)
        file_path = safe_join_download_path(download_path, filename)
        object_key = self.current_prefix + filename
        self.status_var.set(f'正在下载: {filename}')
        try:
            self.s3_client.download_file(
                self.current_bucket, object_key, file_path)
            self.status_var.set(f'下载完成: {filename}')
            if self.is_archive_file(filename) and self.auto_extract_var.get():
                self.status_var.set(f'正在解压: {filename}')
                extract_dir = os.path.splitext(file_path)[0]
                os.makedirs(extract_dir, exist_ok=True)
                shutil.unpack_archive(file_path, extract_dir)
                self.status_var.set(f'解压完成: {filename}')
                if self.auto_open_var.get():
                    extracted_files = self.get_extracted_files(extract_dir)
                    if extracted_files:
                        main_file = os.path.join(
                            extract_dir, extracted_files[0])
                        self.status_var.set(
                            f'正在打开: {extracted_files[0]}')
                        os.startfile(main_file)
            elif self.auto_open_var.get():
                self.status_var.set(f'正在打开: {filename}')
                os.startfile(file_path)
        except Exception as e:
            self.status_var.set(f'下载失败: {str(e)}')
            messagebox.showerror('错误', f'下载文件失败:\n{str(e)}')

    # ═══════════════════════════════════════════════════════════
    # ── 搜索与筛选 ──
    # ═══════════════════════════════════════════════════════════

    def filter_by_time(self):
        """按时间范围筛选对象列表"""
        start_date_str = self.start_date_var.get().strip()
        end_date_str = self.end_date_var.get().strip()
        if not start_date_str and not end_date_str:
            messagebox.showwarning('警告', '请输入开始时间或结束时间')
            return
        try:
            start_date = None
            end_date = None
            if start_date_str:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
                if self.use_local_time:
                    start_date = start_date.replace(tzinfo=self.local_timezone)
                else:
                    start_date = start_date.replace(tzinfo=timezone.utc)
            if end_date_str:
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
                end_date = end_date.replace(hour=23, minute=59, second=59)
                if self.use_local_time:
                    end_date = end_date.replace(tzinfo=self.local_timezone)
                else:
                    end_date = end_date.replace(tzinfo=timezone.utc)
        except ValueError:
            messagebox.showerror('错误', '日期格式不正确，请使用 YYYY-MM-DD 格式')
            return

        filtered = []
        for item in self.full_data:
            time_str = item.get('modified', '')
            try:
                if self.use_local_time:
                    item_time = datetime.strptime(
                        time_str, '%Y-%m-%d %H:%M:%S').replace(
                            tzinfo=self.local_timezone)
                else:
                    item_time = datetime.strptime(
                        time_str, '%Y-%m-%d %H:%M:%S').replace(
                            tzinfo=timezone.utc)
            except ValueError:
                continue
            if start_date and item_time < start_date:
                continue
            if end_date and item_time > end_date:
                continue
            filtered.append(item)

        self.search_results = filtered
        total_pages = max(1, math.ceil(len(filtered) / self.visible_items))
        self._update_display(filtered, 0, total_pages)
        self.status_var.set(f'时间筛选完成 | 找到 {len(filtered)} 个匹配项')

    def clear_time_filter(self):
        """清除时间筛选，恢复完整列表显示"""
        self.start_date_var.set('')
        self.end_date_var.set('')
        self.search_results = []
        total_pages = max(1, math.ceil(len(self.full_data) / self.visible_items))
        self._update_display(self.full_data, 0, total_pages)
        self.status_var.set('时间筛选已清除')

    def export_list(self):
        """导出当前列表数据到 CSV 文件（含所有页的全部数据）"""
        is_filtered = bool(
            self.search_var.get().strip() or
            self.start_date_var.get().strip() or
            self.end_date_var.get().strip()
        )
        data = self.search_results if (is_filtered and self.search_results) else self.full_data
        if not data:
            messagebox.showwarning('提示', '没有可导出的数据')
            return

        file_path = filedialog.asksaveasfilename(
            title='导出列表',
            defaultextension='.csv',
            filetypes=[('CSV 文件', '*.csv'), ('所有文件', '*.*')],
            initialfile='obs_list_export.csv',
        )
        if not file_path:
            return

        try:
            with open(file_path, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(['名称', '类型', '大小', '修改时间', '存储类型', 'ETag'])
                for item in data:
                    writer.writerow([
                        item.get('name', ''),
                        item.get('type', ''),
                        item.get('size', ''),
                        str(item.get('modified', '')),
                        item.get('storage_class', '-'),
                        item.get('etag', '-'),
                    ])
            self.status_var.set(f'导出完成 — {os.path.basename(file_path)} ({len(data)} 条)')
            messagebox.showinfo('导出完成', f'已导出 {len(data)} 条记录到:\n{file_path}')
        except Exception as e:
            messagebox.showerror('导出失败', f'写入文件失败:\n{str(e)}')

    # ═══════════════════════════════════════════════════════════
    # ── 设置与配置 ──
    # ═══════════════════════════════════════════════════════════

    def on_time_display_change(self, _event=None):
        """处理时间显示方式更改（本地时间 / UTC）"""
        self.use_local_time = self.time_display_var.get() == '本地时间 (UTC+8)'
        self.refresh_current_path()

    def on_auto_refresh_change(self, _event=None):
        """处理自动刷新间隔更改"""
        try:
            interval = int(self.auto_refresh_var.get())
            if interval > 0:
                self.setup_auto_refresh(interval)
                return None
            if hasattr(self, 'auto_refresh_id'):
                self.root.after_cancel(self.auto_refresh_id)
                del self.auto_refresh_id
            return None
        except ValueError:
            self.auto_refresh_var.set('0')
            return None

    def setup_auto_refresh(self, interval):
        """设置自动刷新定时器"""
        if hasattr(self, 'auto_refresh_id'):
            self.root.after_cancel(self.auto_refresh_id)

        def refresh_task():
            if not self.loading and self.current_bucket:
                self.status_var.set(
                    f'自动刷新中... '
                    f'({datetime.now().strftime("%H:%M:%S")})')
                self.refresh_current_path()
            self.auto_refresh_id = self.root.after(
                interval * 1000, refresh_task)

        self.auto_refresh_id = self.root.after(interval * 1000, refresh_task)

    def on_closing(self):
        """处理窗口关闭事件，取消未完成的自动刷新定时器"""
        if hasattr(self, 'auto_refresh_id'):
            self.root.after_cancel(self.auto_refresh_id)
        self.root.destroy()

    # ═══════════════════════════════════════════════════════════
    # ── 刷新与缓存 ──
    # ═══════════════════════════════════════════════════════════

    def force_refresh(self):
        """强制刷新当前路径，绕过缓存"""
        if (self.current_bucket in self.object_cache and
                self.current_prefix in self.object_cache[self.current_bucket]):
            del self.object_cache[self.current_bucket][self.current_prefix]
        self.refresh_current_path()

    # ═══════════════════════════════════════════════════════════
    # ── 分页控制 ──
    # ═══════════════════════════════════════════════════════════

    def jump_to_page(self, _event=None):
        """跳转到用户指定的页码"""
        try:
            page_num = int(self.jump_page_var.get()) - 1
            if page_num < 0 or page_num >= self.total_pages:
                messagebox.showerror(
                    '错误', f'请输入有效的页码 (1-{self.total_pages})')
                return None
            if (self.search_var.get().strip()
                    or self.start_date_var.get().strip()
                    or self.end_date_var.get().strip()):
                current_data = self.search_results
            else:
                current_data = self.full_data
            self._update_display(current_data, page_num, self.total_pages)
            return None
        except ValueError:
            messagebox.showerror('错误', '请输入有效的页码')
            return None

    def select_all_items(self, _event):
        """选中列表中的所有项目（Ctrl+A 回调）"""
        items = self.tree.get_children()
        self.tree.selection_set(items)
        return 'break'

    def on_page_size_change(self, _event=None):
        """处理每页显示数量变更"""
        try:
            new_size = int(self.page_size_var.get())
            new_size = min(max(new_size, 10), 500)
            self.visible_items = new_size
            self.page_size_var.set(str(new_size))
            self.refresh_display_with_new_page_size()
        except ValueError:
            self.page_size_var.set(str(self.visible_items))

    def refresh_display_with_new_page_size(self):
        """使用新的每页数量重新计算分页并刷新显示"""
        if not hasattr(self, 'full_data') or not self.full_data:
            return
        if (self.search_var.get().strip()
                or self.start_date_var.get().strip()
                or self.end_date_var.get().strip()):
            current_data = self.search_results
        else:
            current_data = self.full_data
        total_pages = max(1, math.ceil(
            len(current_data) / self.visible_items))
        if self.current_page >= total_pages:
            self.current_page = max(0, total_pages - 1)
        self._update_display(current_data, self.current_page, total_pages)

    # ═══════════════════════════════════════════════════════════
    # ── S3 连接与配置 ──
    # ═══════════════════════════════════════════════════════════

    def on_bucket_change(self, _event):
        """处理桶下拉框变更，进入所选桶的根目录"""
        self.current_bucket = self.bucket_var.get()
        self.current_prefix = ''
        self.path_var.set(self.build_obs_path(prefix=''))
        self.list_objects_threaded(self.current_bucket, '')

    # ═══════════════════════════════════════════════════════════
    # ── 搜索 ──
    # ═══════════════════════════════════════════════════════════

    def on_search_change(self, _event=None):
        """搜索框内容变化时触发延迟搜索（300ms 防抖）"""
        if self.is_searching:
            return
        search_text = self.search_var.get().strip().lower()
        if not search_text:
            self.clear_search()
            return
        self.root.after(300, self.perform_search)

    def perform_search(self):
        """执行名称模糊搜索，结果写入 self.search_results"""
        search_text = self.search_var.get().strip().lower()
        if not search_text:
            self.clear_search()
            return
        self.is_searching = True
        matched = []
        for item in self.full_data:
            if search_text in item.get('name', '').lower():
                matched.append(item)
        self.search_results = matched
        total_pages = max(1, math.ceil(len(matched) / self.visible_items))
        self._update_display(matched, 0, total_pages)
        self.status_var.set(f'搜索完成 | 找到 {len(matched)} 个匹配项')
        self.is_searching = False

    def search_objects(self):
        """搜索按钮回调：执行搜索"""
        search_text = self.search_var.get().strip().lower()
        if not search_text:
            self.clear_search()
            return
        self.perform_search()

    def clear_search(self):
        """清除搜索条件，恢复显示完整对象列表"""
        self.search_var.set('')
        self.search_results = []
        total_pages = max(1, math.ceil(len(self.full_data) / self.visible_items))
        self._update_display(self.full_data, 0, total_pages)
        self.status_var.set('搜索已清除')

    # ═══════════════════════════════════════════════════════════
    # ── S3 操作：列出对象 ──
    # ═══════════════════════════════════════════════════════════


    def refresh_buckets(self):
        """Refresh visible buckets and support least-privilege single-bucket users."""
        if not self.s3_client:
            messagebox.showwarning('提示', '请先新增或选择一个有效配置')
            return
        try:
            response = self.s3_client.list_buckets()
            buckets = [bucket['Name'] for bucket in response['Buckets']]
            if self.current_bucket and self.current_bucket not in buckets:
                buckets.append(self.current_bucket)
            self.bucket_combo['values'] = buckets
            if buckets:
                if self.current_bucket and self.current_bucket in buckets:
                    self.bucket_var.set(self.current_bucket)
                else:
                    self.current_bucket = buckets[0]
                    self.bucket_var.set(self.current_bucket)
                self.path_var.set(self.build_obs_path(prefix=''))
        except ClientError as e:
            error_code = str(e.response.get('Error', {}).get('Code', ''))
            if error_code.lower() in {
                    'accessdenied', '403', 'unauthorized', 'forbidden'}:
                self.bucket_combo['values'] = [self.current_bucket]
                self.bucket_var.set(self.current_bucket)
                self.path_var.set(self.build_obs_path(prefix=''))
                self.status_var.set(
                    f'已连接 — 当前凭据仅显示配置的 Bucket：{self.current_bucket}')
                return
            error_msg = e.response.get('Error', {}).get('Message', str(e))
            messagebox.showerror(
                '错误', f'获取桶列表失败: {error_code} - {error_msg}')
        except Exception as e:
            messagebox.showerror('错误', f'获取桶列表时发生意外错误: {str(e)}')

    def list_objects_threaded(self, bucket, prefix):
        """在后台线程中列出 S3 对象，支持增量加载：每批 API 结果到达即刷新 UI。"""

        self.loading = True
        self.cancel_loading = False
        self.full_data = []
        self._loading_batch_count = 0
        self.tree.delete(*self.tree.get_children())
        self.current_page = 0
        self.total_pages = 0
        self.progress.start()
        self.status_var.set('正在加载... 第 1 批')
        self.prev_btn.config(state=tk.DISABLED)
        self.next_btn.config(state=tk.DISABLED)
        self.page_info.config(text='第 0 页 / 共 0 页')
        self.jump_page_var.set('')

        thread = threading.Thread(
            target=lambda: self._s3_list_objects_incremental(bucket, prefix),
            daemon=True,
        )
        thread.start()

    def _on_list_complete(self, objects):
        """列表加载完成回调（主线程执行）"""
        self.loading = False
        self.cancel_loading = False
        self.progress.stop()
        self.full_data = objects
        self.total_objects = len(objects)
        total_pages = max(1, math.ceil(len(objects) / self.visible_items))
        self._update_display(objects, 0, total_pages)

    def _on_list_error(self, error_msg):
        """列表加载失败回调（主线程执行）"""
        self.loading = False
        self.cancel_loading = False
        self.progress.stop()
        self.full_data = []
        self.status_var.set(f'加载失败: {error_msg}')
        messagebox.showerror('错误', f'加载对象列表失败: {error_msg}')

    def _s3_list_objects(self, bucket, prefix):
        """使用 S3 ListObjectsV2 API 分页列出指定桶和前缀下的所有对象。

        逻辑说明：
        1. 使用 Delimiter='/' 实现文件夹式浏览：
           - CommonPrefixes 返回当前层级下的子文件夹。
           - Contents 返回当前前缀下的直接文件（不含子目录内的文件）。
        2. 分页处理：
           - 每次请求最多返回 MAX_KEYS（1000）个条目。
           - 通过 IsTruncated 判断是否还有更多数据。
           - 通过 NextContinuationToken 实现翻页。
        3. 过滤规则：
           - 跳过 Key 等于 prefix 的"文件夹占位对象"（零大小）。
           - 跳过文件名中含 '/' 的条目（已由 Delimiter 避免，此处防御）。
        4. 返回结构：
           每个对象为一个 dict，包含 name / type / size / modified / key /
           storage_class / etag 字段，统一文件夹和文件的表示形式。
        """
        objects = []
        continuation_token = None
        prefix = prefix or ''

        while True:
            params = {
                'Bucket': bucket,
                'Prefix': prefix,
                'Delimiter': '/',
                'MaxKeys': self.MAX_KEYS,
            }
            if continuation_token:
                params['ContinuationToken'] = continuation_token

            response = self.s3_client.list_objects_v2(**params)

            # 处理子文件夹（CommonPrefixes）
            if 'CommonPrefixes' in response:
                for common_prefix in response['CommonPrefixes']:
                    folder_name = common_prefix['Prefix'][len(prefix):].rstrip(
                        '/')
                    if folder_name:
                        objects.append({
                            'name': folder_name,
                            'type': '文件夹',
                            'size': '-',
                            'modified': '-',
                            'key': common_prefix['Prefix'],
                            'storage_class': '-',
                            'etag': '-',
                        })

            # 处理文件对象（Contents）
            if 'Contents' in response:
                for obj in response['Contents']:
                    # 跳过占位对象（key 等于当前 prefix 的零大小条目）
                    if obj['Key'] == prefix:
                        continue
                    file_name = obj['Key'][len(prefix):]
                    # 子文件已在 CommonPrefixes 体现，此防御性过滤保留
                    if '/' in file_name:
                        continue
                    objects.append({
                        'name': file_name,
                        'type': self.get_file_type(file_name),
                        'size': self.format_size(obj['Size']),
                        'modified': obj['LastModified'].astimezone(
                            timezone(timedelta(hours=8))),
                        'key': obj['Key'],
                        'storage_class': obj.get('StorageClass', 'STANDARD'),
                        'etag': obj.get('ETag', '').strip('"'),
                    })

            if not response.get('IsTruncated'):
                break
            continuation_token = response.get('NextContinuationToken')

        return objects

    def _s3_list_objects_incremental(self, bucket, prefix):
        """后台线程中分页拉取 S3 对象，每批结果通过回调增量追加到 UI。

        与 _s3_list_objects 拉取逻辑一致，区别在于：
        - 每拉取完一页 API 数据（最多 1000 条），立刻通过 after(0) 回调
          将当前批次追加到 full_data 和 Treeview。
        - 全部拉取完毕后通过 after(0) 调用 _on_incremental_done 收尾。
        """
        continuation_token = None
        prefix = prefix or ''

        try:
            while True:
                if self.cancel_loading:
                    break

                params = {
                    'Bucket': bucket,
                    'Prefix': prefix,
                    'Delimiter': '/',
                    'MaxKeys': self.MAX_KEYS,
                }
                if continuation_token:
                    params['ContinuationToken'] = continuation_token

                response = self.s3_client.list_objects_v2(**params)
                batch = []

                # 处理子文件夹（CommonPrefixes）
                if 'CommonPrefixes' in response:
                    for common_prefix in response['CommonPrefixes']:
                        folder_name = common_prefix['Prefix'][len(prefix):].rstrip('/')
                        if folder_name:
                            batch.append({
                                'name': folder_name,
                                'type': '文件夹',
                                'size': '-',
                                'modified': '-',
                                'key': common_prefix['Prefix'],
                                'storage_class': '-',
                                'etag': '-',
                            })

                # 处理文件对象（Contents）
                if 'Contents' in response:
                    for obj in response['Contents']:
                        if obj['Key'] == prefix:
                            continue
                        file_name = obj['Key'][len(prefix):]
                        if '/' in file_name:
                            continue
                        batch.append({
                            'name': file_name,
                            'type': self.get_file_type(file_name),
                            'size': self.format_size(obj['Size']),
                            'modified': obj['LastModified'].astimezone(
                                    timezone(timedelta(hours=8))),
                            'key': obj['Key'],
                            'storage_class': obj.get('StorageClass', 'STANDARD'),
                            'etag': obj.get('ETag', '').strip('"'),
                        })

                if batch:
                    # 通过默认参数捕获当前 batch，避免闭包延迟绑定问题
                    self.root.after(0, lambda b=batch: self._append_batch(b))

                if not response.get('IsTruncated'):
                    break
                continuation_token = response.get('NextContinuationToken')

            self.root.after(0, self._on_incremental_done)
        except Exception as e:
            self.root.after(0, lambda err=str(e): self._on_list_error(err))

    def _append_batch(self, batch_data):
        """将一批 S3 数据增量追加到 full_data 和 Treeview（主线程执行）。

        规则：
        - 始终追加到 self.full_data 并更新分页信息。
        - 仅当 current_page == 0 且 Treeview 中行数未达 visible_items 时，
          才会向 Treeview 追加新行，避免 UI 卡顿。
        """
        self._loading_batch_count += 1
        self.full_data.extend(batch_data)

        total_pages = max(1, math.ceil(len(self.full_data) / self.visible_items))
        self.total_pages = total_pages

        # 只在第一页时追加到可见行，且不超过 visible_items
        if self.current_page == 0:
            current_visible = len(self.tree.get_children())
            remaining = self.visible_items - current_visible
            if remaining > 0:
                to_add = batch_data[:remaining]
                for item in to_add:
                    self.tree.insert('', tk.END, values=(
                        item['name'],
                        item['type'],
                        item['size'],
                        item['modified'],
                        item.get('storage_class', '-'),
                        item.get('etag', '-'),
                    ))

        self.status_var.set(
            f'正在加载... 第 {self._loading_batch_count} 批，'
            f'已加载 {len(self.full_data)} 个对象')
        self.page_info.config(
            text=f'第 1 页 / 共 {total_pages} 页')

    def _on_incremental_done(self):
        """增量加载完成回调（主线程执行）：停止进度条，刷新最终状态。"""
        self.loading = False
        self.cancel_loading = False
        self.progress.stop()

        total_pages = max(1, math.ceil(len(self.full_data) / self.visible_items))
        self.total_pages = total_pages
        self.total_objects = len(self.full_data)

        # 用最终完整数据重新渲染第一页，确保显示准确
        self._update_display(self.full_data, 0, total_pages)
        self.status_var.set(f'就绪 | 共 {len(self.full_data)} 个对象')

    # ═══════════════════════════════════════════════════════════
    # ── 显示更新 ──
    # ═══════════════════════════════════════════════════════════

    def _update_display(self, data, current_page, total_pages):
        """将指定页的数据渲染到 Treeview 中，并更新分页控件状态"""
        self.tree.delete(*self.tree.get_children())
        start_idx = current_page * self.visible_items
        end_idx = min(start_idx + self.visible_items, len(data))
        page_data = data[start_idx:end_idx]
        for item in page_data:
            self.tree.insert(
                '', tk.END,
                values=(
                    item['name'],
                    item['type'],
                    item['size'],
                    item['modified'],
                    item.get('storage_class', '-'),
                    item.get('etag', '-'),
                ),
            )
        self.current_page = current_page
        self.page_info.config(
            text=f'第 {current_page + 1} 页 / 共 {total_pages} 页')
        self.prev_btn.config(
            state=tk.NORMAL if current_page > 0 else tk.DISABLED)
        self.next_btn.config(
            state=tk.NORMAL if current_page < total_pages - 1 else tk.DISABLED)
        total_objects = f' (共 {len(data)} 个对象)' if data else ''
        self.status_var.set(f'加载完成{total_objects}')

    # ═══════════════════════════════════════════════════════════
    # ── 导航与刷新 ──
    # ═══════════════════════════════════════════════════════════

    def navigate_to_path(self, _event=None):
        """Parse an obs://, oss://, or s3:// path and navigate to it."""
        path = self.path_var.get().strip()
        if not path:
            return
        path = re.sub(r'^(?:obs|oss|s3)://', '', path, flags=re.IGNORECASE)
        parts = path.split('/', 1)
        bucket = parts[0]
        prefix = parts[1] if len(parts) > 1 else ''
        if prefix and not prefix.endswith('/'):
            prefix += '/'
        self.current_bucket = bucket
        self.current_prefix = prefix
        self.bucket_var.set(bucket)
        self.path_var.set(self.build_obs_path())
        self.list_objects_threaded(bucket, prefix)

    def navigate_up(self):
        """返回上级目录"""
        if not self.current_bucket or self.loading:
            return
        if self.current_prefix:
            parts = self.current_prefix.rstrip('/').split('/')
            if len(parts) > 1:
                new_prefix = '/'.join(parts[:-1]) + '/'
            else:
                new_prefix = ''
            self.current_prefix = new_prefix
            self.path_var.set(self.build_obs_path())
            self.list_objects_threaded(self.current_bucket, new_prefix)

    def refresh_current_path(self):
        """刷新当前路径下的对象列表"""
        if self.current_bucket and not self.loading:
            self.list_objects_threaded(
                self.current_bucket, self.current_prefix)

    def clear_cache(self):
        """清除内存中的对象和元数据缓存，然后刷新当前路径"""
        self.object_cache.clear()
        self.metadata_cache.clear()
        messagebox.showinfo('提示', '缓存已清除')
        self.refresh_current_path()

    # ═══════════════════════════════════════════════════════════
    # ── 上传操作 ──
    # ═══════════════════════════════════════════════════════════

    def upload_file(self):
        """上传单个或多个文件到当前路径"""
        if not self.s3_client:
            messagebox.showwarning('警告', '请先连接S3服务')
            return
        files = filedialog.askopenfilenames(title='选择要上传的文件')
        if not files:
            return
        self.status_var.set(f'正在上传 {len(files)} 个文件...')
        self.progress.start()
        success, failed = 0, 0
        for file_path in files:
            try:
                filename = os.path.basename(file_path)
                object_key = self.current_prefix + filename
                self.s3_client.upload_file(
                    file_path, self.current_bucket, object_key)
                success += 1
            except Exception as e:
                failed += 1
                print(f'上传失败 {file_path}: {e}')
        self.progress.stop()
        self._show_operation_result(success, failed, '上传')
        self.force_refresh()

    def upload_folder(self):
        """递归上传整个文件夹到当前路径下"""
        if not self.s3_client:
            messagebox.showwarning('警告', '请先连接S3服务')
            return
        folder = filedialog.askdirectory(title='选择要上传的文件夹')
        if not folder:
            return
        self.status_var.set(f'正在上传文件夹: {os.path.basename(folder)}')
        self.progress.start()
        success, failed = 0, 0
        folder_name = os.path.basename(folder)
        base_prefix = self.current_prefix + folder_name + '/'
        for root_dir, _dirs, files in os.walk(folder):
            for filename in files:
                file_path = os.path.join(root_dir, filename)
                rel_path = os.path.relpath(file_path, folder).replace(
                    '\\', '/')
                object_key = base_prefix + rel_path
                try:
                    self.s3_client.upload_file(
                        file_path, self.current_bucket, object_key)
                    success += 1
                except Exception as e:
                    failed += 1
                    print(f'上传失败 {file_path}: {e}')
        self.progress.stop()
        self._show_operation_result(success, failed, '上传')
        self.force_refresh()

    # ═══════════════════════════════════════════════════════════
    # ── 下载操作 ──
    # ═══════════════════════════════════════════════════════════

    def download_files(self):
        """下载选中的文件或文件夹到默认下载路径"""
        if not self.s3_client:
            messagebox.showwarning('警告', '请先连接S3服务')
            return
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning('警告', '请先选择要下载的文件或文件夹')
            return
        download_path = self.download_path_var.get()
        if not download_path:
            download_path = self.default_download_path
            self.download_path_var.set(download_path)
        os.makedirs(download_path, exist_ok=True)
        success, failed = 0, 0
        for item in selected:
            values = self.tree.item(item, 'values')
            name = values[0] if values else ''
            item_type = values[1] if len(values) > 1 else ''
            try:
                if item_type == '文件夹':
                    folder_prefix = self.current_prefix + name + '/'
                    self._download_folder(
                        folder_prefix, safe_join_download_path(download_path, name))
                    success += 1
                else:
                    object_key = self.current_prefix + name
                    file_path = safe_join_download_path(download_path, name)
                    self.s3_client.download_file(
                        self.current_bucket, object_key, file_path)
                    success += 1
                    if (self.is_archive_file(name)
                            and self.auto_extract_var.get()):
                        extract_dir = os.path.splitext(file_path)[0]
                        os.makedirs(extract_dir, exist_ok=True)
                        shutil.unpack_archive(file_path, extract_dir)
            except Exception as e:
                failed += 1
                print(f'下载失败 {name}: {e}')
        self._show_operation_result(success, failed, '下载', refresh=False)

    def _download_folder(self, folder_prefix, local_path):
        """递归下载文件夹中的所有对象到本地路径"""
        os.makedirs(local_path, exist_ok=True)
        continuation_token = None
        while True:
            params = {
                'Bucket': self.current_bucket,
                'Prefix': folder_prefix,
                'MaxKeys': self.MAX_KEYS,
            }
            if continuation_token:
                params['ContinuationToken'] = continuation_token
            response = self.s3_client.list_objects_v2(**params)
            if 'Contents' in response:
                for obj in response['Contents']:
                    key = obj['Key']
                    rel_path = key[len(folder_prefix):]
                    if not rel_path:
                        continue
                    file_path = safe_join_download_path(local_path, rel_path)
                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                    self.s3_client.download_file(
                        self.current_bucket, key, file_path)
            if not response.get('IsTruncated'):
                break
            continuation_token = response.get('NextContinuationToken')

    # ═══════════════════════════════════════════════════════════
    # ── 文件操作（新建 / 删除 / 复制 / 重命名）──
    # ═══════════════════════════════════════════════════════════

    def create_folder(self):
        """在当前路径下新建文件夹（S3 零大小占位对象）"""
        if not self.s3_client:
            messagebox.showwarning('警告', '请先连接S3服务')
            return
        folder_name = simpledialog.askstring('新建文件夹', '请输入文件夹名称:')
        if not folder_name:
            return
        folder_name = folder_name.strip()
        if not folder_name:
            return
        try:
            object_key = self.current_prefix + folder_name + '/'
            self.s3_client.put_object(
                Bucket=self.current_bucket, Key=object_key, Body='')
            self.status_var.set(f'文件夹已创建: {folder_name}')
            self.force_refresh()
        except Exception as e:
            messagebox.showerror('错误', f'创建文件夹失败:\n{str(e)}')

    def delete_items(self):
        """删除选中的一个或多个对象（含文件夹递归删除）"""
        if not self.s3_client:
            messagebox.showwarning('警告', '请先连接S3服务')
            return
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning('警告', '请先选择要删除的对象')
            return
        names = []
        for item in selected:
            values = self.tree.item(item, 'values')
            name = values[0] if values else ''
            if name:
                names.append(name)
        if not messagebox.askyesno(
                '确认删除',
                f'确定要删除以下 {len(names)} 个对象吗?\n\n'
                + '\n'.join(names)):
            return
        for i, item in enumerate(selected):
            name = names[i]
            try:
                values = self.tree.item(item, 'values')
                item_type = values[1] if len(values) > 1 else ''
                if item_type == '文件夹':
                    self._delete_folder_contents(
                        self.current_prefix + name + '/')
                else:
                    object_key = self.current_prefix + name
                    self.s3_client.delete_object(
                        Bucket=self.current_bucket, Key=object_key)
            except Exception as e:
                print(f'删除失败 {name}: {e}')
        self.status_var.set(f'已删除 {len(names)} 个对象')
        self.force_refresh()

    def _delete_folder_contents(self, folder_prefix):
        """使用批量删除 API 递归删除文件夹下的所有对象"""
        paginator = self.s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(
            Bucket=self.current_bucket, Prefix=folder_prefix)
        delete_keys = {'Objects': []}
        for page in pages:
            if 'Contents' in page:
                for obj in page['Contents']:
                    delete_keys['Objects'].append({'Key': obj['Key']})
                    if len(delete_keys['Objects']) >= 1000:
                        self.s3_client.delete_objects(
                            Bucket=self.current_bucket, Delete=delete_keys)
                        delete_keys = {'Objects': []}
        if delete_keys['Objects']:
            self.s3_client.delete_objects(
                Bucket=self.current_bucket, Delete=delete_keys)

    def copy_items(self):
        """将选中的对象复制到指定目标路径"""
        if not self.s3_client:
            messagebox.showwarning('警告', '请先连接S3服务')
            return
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning('警告', '请先选择要复制的对象')
            return
        dest_path = simpledialog.askstring(
            '复制到', '请输入目标路径 (如 folder/subfolder/):')
        if dest_path is None:
            return
        dest_path = dest_path.strip()
        success, failed = 0, 0
        for item in selected:
            values = self.tree.item(item, 'values')
            name = values[0] if values else ''
            if not name:
                continue
            try:
                source_key = self.current_prefix + name
                dest_key = dest_path + name
                copy_source = {
                    'Bucket': self.current_bucket,
                    'Key': source_key,
                }
                self.s3_client.copy_object(
                    Bucket=self.current_bucket, Key=dest_key,
                    CopySource=copy_source)
                success += 1
            except Exception as e:
                failed += 1
                print(f'复制失败 {name}: {e}')
        self._show_operation_result(success, failed, '复制')
        self.force_refresh()

    def rename_item(self):
        """重命名选中的单个对象（Copy + Delete 实现）"""
        if not self.s3_client:
            messagebox.showwarning('警告', '请先连接S3服务')
            return
        selected = self.tree.selection()
        if not selected or len(selected) != 1:
            messagebox.showwarning('警告', '请选择单个对象进行重命名')
            return
        values = self.tree.item(selected[0], 'values')
        old_name = values[0] if values else ''
        if not old_name:
            return
        new_name = simpledialog.askstring(
            '重命名', f'请输入新名称:', initialvalue=old_name)
        if not new_name or new_name == old_name:
            return
        try:
            old_key = self.current_prefix + old_name
            new_key = self.current_prefix + new_name
            copy_source = {
                'Bucket': self.current_bucket,
                'Key': old_key,
            }
            self.s3_client.copy_object(
                Bucket=self.current_bucket, Key=new_key,
                CopySource=copy_source)
            self.s3_client.delete_object(
                Bucket=self.current_bucket, Key=old_key)
            self.status_var.set(f'已重命名: {old_name} -> {new_name}')
            self.force_refresh()
        except Exception as e:
            messagebox.showerror('错误', f'重命名失败:\n{str(e)}')

    # ═══════════════════════════════════════════════════════════
    # ── 结果显示（合并后的统一方法）──
    # ═══════════════════════════════════════════════════════════

    def _show_operation_result(self, success, failed, operation_name,
                               error_details=None, refresh=True):
        """统一显示上传/下载/复制等操作的结果弹窗。

        Args:
            success:        成功数量（int）。
            failed:         失败数量（int）。
            operation_name: 操作名称，用于弹窗文案（如 '上传'、'下载'、'复制'）。
            error_details:  可选，失败详情字符串列表；为 None 时仅显示计数。
            refresh:        是否在操作结束后刷新当前路径列表。
        """
        self.loading = False
        self.cancel_loading = False
        self.progress.stop()
        self.progress.config(value=0)

        if failed == 0:
            messagebox.showinfo(
                '成功', f'{operation_name}完成，成功 {success} 个文件')
        else:
            # 若有详细错误信息则展示，否则仅展示失败计数
            if error_details:
                detail_text = '\n'.join(error_details[:10])
                more = (
                    f'\n...（共 {len(error_details)} 条）'
                    if len(error_details) > 10 else '')
                messagebox.showwarning(
                    '完成',
                    f'{operation_name}完成，成功 {success} 个，'
                    f'失败 {failed} 个\n\n{detail_text}{more}')
            else:
                messagebox.showwarning(
                    '完成',
                    f'{operation_name}完成，成功 {success} 个，'
                    f'失败 {failed} 个')

        if refresh:
            self.refresh_current_path()

    # ═══════════════════════════════════════════════════════════
    # ── 事件处理 ──
    # ═══════════════════════════════════════════════════════════

    def on_item_double_click(self, event):
        """双击 Treeview 项目：文件夹则进入，文件则下载并打开"""
        try:
            item = self.tree.identify_row(event.y)
            if not item:
                print('双击事件: 未找到项目')
                return
            self.tree.selection_set(item)
            item_data = self.tree.item(item)
            values = item_data['values']
            if not values or len(values) < 2:
                print('双击事件: 项目数据不完整')
                return
            name = values[0]
            item_type = values[1]
            print(f'双击项目: {name}, 类型: {item_type}')
            if item_type == '文件夹':
                print(f'进入文件夹: {name}')
                new_prefix = f'{self.current_prefix}{name}/'
                self.current_prefix = new_prefix
                self.path_var.set(self.build_obs_path())
                self.list_objects_threaded(self.current_bucket, new_prefix)
                return
            print(f'开始下载文件: {name}')
            self.download_and_open_file(name)
        except Exception as e:
            print(f'双击事件处理错误: {e}')
            import traceback
            traceback.print_exc()

    def on_item_selected(self, _event):
        """Treeview 选择变更事件：更新底部状态栏的选中信息"""
        selected = self.tree.selection()
        count = len(selected)
        if count > 0:
            try:
                first_item = self.tree.item(selected[0])
                values = first_item['values']
                if values and len(values) >= 2:
                    name = values[0]
                    name_str = str(name) if name is not None else ''
                    display_name = (
                        name_str[:20] + '...'
                        if len(name_str) > 20 else name_str)
                    info = f'已选择 {count} 个项目 | 第一个: {display_name}'
                    self.status_var.set(info)
            except Exception as e:
                print(f'选择事件处理错误: {e}')

    def on_right_click(self, event):
        """右键菜单：下载 / 重命名 / 复制 / 删除 / 属性"""
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.tree.selection_set(item)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label='下载', command=self.download_files)
        menu.add_command(label='重命名', command=self.rename_item)
        menu.add_command(label='复制', command=self.copy_items)
        menu.add_command(label='删除', command=self.delete_items)
        menu.add_separator()
        menu.add_command(label='属性', command=self.show_properties)
        menu.post(event.x_root, event.y_root)

    # ═══════════════════════════════════════════════════════════
    # ── 属性展示 ──
    # ═══════════════════════════════════════════════════════════

    def show_properties(self):
        """展示选中对象的详细属性弹窗"""
        selected = self.tree.selection()
        if not selected or len(selected) != 1:
            messagebox.showwarning('警告', '请选择单个对象查看属性')
            return
        values = self.tree.item(selected[0], 'values')
        if not values:
            return
        name, file_type, size, modified, storage_class, etag = values
        info = '对象属性:\n'
        info += f'名称: {name}\n'
        info += f'类型: {file_type}\n'
        info += f'大小: {size}\n'
        info += f'修改时间: {modified}\n'
        info += f'路径: {self.current_prefix}{name}\n'
        info += f'桶: {self.current_bucket}'
        if file_type == '文件夹':
            try:
                folder_prefix = self.current_prefix + name + '/'
                response = self.s3_client.list_objects_v2(
                    Bucket=self.current_bucket,
                    Prefix=folder_prefix, MaxKeys=1)
                info += '\n包含内容: ' + (
                    '是' if response.get('KeyCount', 0) > 0 else '空文件夹')
            except Exception:
                pass
        messagebox.showinfo('属性', info)

    # ═══════════════════════════════════════════════════════════
    # ── 状态完成 ──
    # ═══════════════════════════════════════════════════════════

    def finish_loading(self):
        """更新状态栏，展示加载完成的统计信息（请求数、耗时、速率）"""
        self.loading = False
        self.cancel_loading = False
        elapsed = time.time() - self.scan_start_time
        req_rate = self.request_count / elapsed if elapsed > 0 else 0
        self.status_var.set(
            f'就绪 | 对象: {self.total_objects}'
            f' | 请求: {self.request_count}'
            f' | 耗时: {elapsed:.1f}s'
            f' | 速率: {req_rate:.1f} req/s')
        self.progress['value'] = 100

    # ═══════════════════════════════════════════════════════════
    # ── 格式化方法 ──
    # ═══════════════════════════════════════════════════════════

    def format_size(self, size):
        """将字节数格式化为人类可读的字符串（B / KB / MB / GB / TB）"""
        if size == 0:
            return '0 B'
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024:
                return f'{size:.1f} {unit}'
            size /= 1024
        return f'{size:.1f} TB'

    def format_date(self, date_obj):
        """将 datetime 对象格式化为 'YYYY-MM-DD HH:MM:SS' 字符串"""
        if isinstance(date_obj, datetime):
            return date_obj.strftime('%Y-%m-%d %H:%M:%S')
        return str(date_obj)

    def get_file_type(self, filename):
        """根据文件扩展名返回中文类型描述"""
        ext = os.path.splitext(filename)[1].lower()
        return self.FILE_TYPE_MAP.get(
            ext, ext.upper() + '文件' if ext else '未知类型')

    # ═══════════════════════════════════════════════════════════
    # ── 排序 ──
    # ═══════════════════════════════════════════════════════════

    def sort_treeview(self, col):
        """点击列标题时对该列进行排序（切换升序/降序）"""
        reverse = False
        if col == self.sort_column:
            self.sort_ascending = not self.sort_ascending
            reverse = not self.sort_ascending
        else:
            self.sort_column = col
            self.sort_ascending = True
            reverse = False

        # 清除所有列标题的排序箭头
        for column in self.tree['columns']:
            heading = self.tree.heading(column, 'text')
            self.tree.heading(column, text=heading.rstrip(' \u2193\u2191'))

        # 添加当前排序列的箭头
        arrow = ' \u2191' if self.sort_ascending else ' \u2193'
        new_heading = self.tree.heading(col, 'text') + arrow
        self.tree.heading(col, text=new_heading)

        # 收集数据并按列排序
        data = [(self.tree.set(child, col), child)
                for child in self.tree.get_children('')]
        if col == '大小':
            data.sort(key=lambda x: self.parse_size(x[0]), reverse=reverse)
        elif col == '修改时间':
            data.sort(key=lambda x: self.parse_date(x[0]) or 0,
                      reverse=reverse)
        else:
            data.sort(reverse=reverse)

        for idx, (_, child) in enumerate(data):
            self.tree.move(child, '', idx)

    def parse_size(self, size_str):
        """解析人类可读大小字符串（如 '1.5 MB'）为字节数；失败返回 0"""
        if not size_str or size_str == '-':
            return 0
        try:
            match = re.match(r'([\d.]+)\s*(\w*)', size_str)
            if match:
                size, unit = match.groups()
                size = float(size)
                unit = unit.upper() if unit else 'B'
                return size * self.SIZE_UNITS.get(unit, 1)
            return 0
        except Exception:
            return 0

    def parse_date(self, date_str):
        """解析 'YYYY-MM-DD HH:MM:SS' 为 Unix 时间戳；失败返回 0"""
        try:
            return datetime.strptime(
                date_str, '%Y-%m-%d %H:%M:%S').timestamp()
        except Exception:
            return 0

    # ═══════════════════════════════════════════════════════════
    # ── 分页导航 ──
    # ═══════════════════════════════════════════════════════════

    def next_page(self):
        """切换到下一页"""
        if (self.search_var.get().strip()
                or self.start_date_var.get().strip()
                or self.end_date_var.get().strip()):
            current_data = self.search_results
        else:
            current_data = self.full_data
        total_pages = max(1, math.ceil(
            len(current_data) / self.visible_items))
        if self.current_page < total_pages - 1:
            self._update_display(
                current_data, self.current_page + 1, total_pages)

    def prev_page(self):
        """切换到上一页"""
        if (self.search_var.get().strip()
                or self.start_date_var.get().strip()
                or self.end_date_var.get().strip()):
            current_data = self.search_results
        else:
            current_data = self.full_data
        total_pages = max(1, math.ceil(
            len(current_data) / self.visible_items))
        if self.current_page > 0:
            self._update_display(
                current_data, self.current_page - 1, total_pages)


# ═══════════════════════════════════════════════════════════════
# ── 程序入口 ──
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    root = tk.Tk()
    app = S3BrowserTool(root)
    root.mainloop()
