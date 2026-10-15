import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import tkinter as tk

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vdelog_browser import (
    VDELOG_BUCKET, VDELOG_PREFIX, VdeLogRefreshDialog,
    is_vdelog_path, normalize_vin, find_vin_folder,
)

VIN = 'TEST0000000000001'


@pytest.mark.parametrize('configured,expected', [
    (None, 'example-vehicle-logs'),
    ('', 'example-vehicle-logs'),
    ('   ', 'example-vehicle-logs'),
    ('  custom-vehicle-logs  ', 'custom-vehicle-logs'),
])
def test_vehicle_log_bucket_can_be_configured_without_editing_source(configured, expected):
    # Each launch reads its own configuration without changing Tcl's process environment.
    environment = os.environ.copy()
    if configured is None:
        environment.pop('OSS_BROWSER_VEHICLE_LOG_BUCKET', None)
    else:
        environment['OSS_BROWSER_VEHICLE_LOG_BUCKET'] = configured
    result = subprocess.run(
        [sys.executable, '-c', 'from vdelog_browser import VDELOG_BUCKET; print(VDELOG_BUCKET)'],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == expected


@pytest.mark.parametrize('value', [VIN, VIN.lower(), '  ' + VIN + '  '])
def test_normalize_vin(value):
    assert normalize_vin(value) == VIN


@pytest.mark.parametrize('value', ['', VIN[:8], VIN + 'X', '../' + VIN, VIN[:-1] + '/', VIN[:-1] + 'I'])
def test_reject_invalid_vin(value):
    with pytest.raises(ValueError):
        normalize_vin(value)


def test_scope_is_only_this_huawei_bucket_and_directory():
    assert is_vdelog_path('华为云OBS', VDELOG_BUCKET, VDELOG_PREFIX)
    assert is_vdelog_path('华为云OBS', VDELOG_BUCKET, VDELOG_PREFIX.rstrip('/'))
    assert not is_vdelog_path('自定义S3', VDELOG_BUCKET, VDELOG_PREFIX)
    assert not is_vdelog_path('华为云OBS', 'other-bucket', VDELOG_PREFIX)
    assert not is_vdelog_path('华为云OBS', VDELOG_BUCKET, VDELOG_PREFIX + VIN + '/')


class Client:
    def __init__(self, response=None):
        self.response = response if response is not None else {'Contents': [{'Key': VDELOG_PREFIX + VIN + '/day/a.log'}]}
        self.calls = []

    def list_objects_v2(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    def download_file(self, *args, **kwargs):
        raise AssertionError('refresh must never download a file')


def test_vin_query_checks_exact_folder_with_one_object_only():
    client = Client()
    folders = find_vin_folder(client, VDELOG_BUCKET, VDELOG_PREFIX, VIN)
    assert client.calls == [{'Bucket': VDELOG_BUCKET, 'Prefix': VDELOG_PREFIX + VIN + '/', 'MaxKeys': 1}]
    assert len(folders) == 1
    assert folders[0]['name'] == VIN
    assert folders[0]['type'] == '文件夹'
    assert folders[0]['key'] == VDELOG_PREFIX + VIN + '/'


def test_nonexistent_vin_returns_empty_list():
    assert find_vin_folder(Client({}), VDELOG_BUCKET, VDELOG_PREFIX, VIN) == []


def test_empty_folder_marker_still_appears():
    client = Client({'Contents': [{'Key': VDELOG_PREFIX + VIN + '/'}]})
    assert find_vin_folder(client, VDELOG_BUCKET, VDELOG_PREFIX, VIN)[0]['name'] == VIN


def test_other_vin_in_response_cannot_be_shown():
    client = Client({'Contents': [{'Key': VDELOG_PREFIX + VIN + 'X/day/a.log'}]})
    with pytest.raises(ValueError):
        find_vin_folder(client, VDELOG_BUCKET, VDELOG_PREFIX, VIN)


@pytest.fixture(scope='module')
def ui():
    spec = importlib.util.spec_from_file_location('app_vdelog_tests', Path(__file__).resolve().parents[1] / 'lxp-test.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tk.Tk()
    root.withdraw()
    yield module, root
    root.destroy()


@pytest.fixture
def app(ui, monkeypatch):
    module, root = ui
    monkeypatch.setattr(module.ProfileStore, 'load', lambda self: ([], None))
    monkeypatch.setattr(module.S3BrowserTool, 'add_profile', lambda self: None)
    instance = module.S3BrowserTool(root)
    instance.profiles = [{'id': 'test', 'name': 'test', 'provider': '华为云OBS', 'region_name': 'test'}]
    instance.selected_profile_id = 'test'
    instance.current_bucket = VDELOG_BUCKET
    instance.current_prefix = VDELOG_PREFIX
    instance.s3_client = Client()
    yield instance
    instance._cancel_listing()
    instance.main_frame.destroy()


def choose(ui, monkeypatch, result):
    module, _ = ui
    class Dialog:
        def __init__(self, *args):
            pass
        def show(self):
            return result
    monkeypatch.setattr(module, 'VdeLogRefreshDialog', Dialog)


def finish(app):
    deadline = time.monotonic() + 3
    while app.loading and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    assert not app.loading


def test_vin_refresh_shows_vehicle_folder_at_vdelog_root(app, ui, monkeypatch):
    choose(ui, monkeypatch, ('vin', VIN))
    app.list_objects_threaded(VDELOG_BUCKET, VDELOG_PREFIX)
    finish(app)
    assert app.current_prefix == VDELOG_PREFIX
    assert app.full_data[0]['name'] == VIN
    assert app.tree.item(app.tree.get_children()[0], 'values')[0] == VIN
    assert 'VIN' in app.status_var.get()
    assert app.s3_client.calls[0]['Prefix'] == VDELOG_PREFIX + VIN + '/'


def test_all_refresh_lists_full_root(app, ui, monkeypatch):
    choose(ui, monkeypatch, ('all', ''))
    app.s3_client = Client({'CommonPrefixes': [{'Prefix': VDELOG_PREFIX + VIN + '/'}]})
    app.refresh_current_path()
    finish(app)
    assert app.s3_client.calls == [{'Bucket': VDELOG_BUCKET, 'Prefix': VDELOG_PREFIX, 'Delimiter': '/', 'MaxKeys': 1000}]


def test_missing_vin_never_falls_back_to_full_scan(app, ui, monkeypatch):
    choose(ui, monkeypatch, ('vin', VIN))
    app.s3_client = Client({})
    app.refresh_current_path()
    finish(app)
    assert app.full_data == []
    assert not app.tree.get_children()
    assert '未找到 VIN' in app.status_var.get()
    assert len(app.s3_client.calls) == 1
    assert app.s3_client.calls[0]['Prefix'] == VDELOG_PREFIX + VIN + '/'


def test_vin_network_error_is_reported_without_full_scan(app, ui, monkeypatch):
    choose(ui, monkeypatch, ('vin', VIN))
    messages = []
    monkeypatch.setattr(ui[0].messagebox, 'showerror', lambda *args: messages.append(args))
    class FailedClient(Client):
        def list_objects_v2(self, **kwargs):
            self.calls.append(kwargs)
            raise OSError('connection lost')
    app.s3_client = FailedClient()
    app.refresh_current_path()
    finish(app)
    assert app.full_data == []
    assert len(app.s3_client.calls) == 1
    assert 'connection lost' in app.status_var.get()
    assert messages


@pytest.mark.parametrize('provider,bucket,prefix', [
    ('自定义S3', VDELOG_BUCKET, VDELOG_PREFIX),
    ('华为云OBS', 'other-bucket', VDELOG_PREFIX),
    ('华为云OBS', VDELOG_BUCKET, 'data/other-logs/'),
])
def test_other_providers_and_paths_use_normal_browsing(app, ui, monkeypatch, provider, bucket, prefix):
    class UnexpectedDialog:
        def __init__(self, *args):
            raise AssertionError('unrelated browsing must not prompt')
    monkeypatch.setattr(ui[0], 'VdeLogRefreshDialog', UnexpectedDialog)
    app.profiles[0]['provider'] = provider
    app.s3_client = Client({})
    app.list_objects_threaded(bucket, prefix)
    finish(app)
    assert app.s3_client.calls == [{'Bucket': bucket, 'Prefix': prefix, 'Delimiter': '/', 'MaxKeys': 1000}]


def test_force_refresh_can_switch_back_to_all_vehicles(app, ui, monkeypatch):
    choose(ui, monkeypatch, ('vin', VIN))
    app.refresh_current_path()
    finish(app)
    choose(ui, monkeypatch, ('all', ''))
    app.s3_client = Client({'CommonPrefixes': [{'Prefix': VDELOG_PREFIX + 'ANOTHER-CAR/'}]})
    app.force_refresh()
    finish(app)
    assert [item['name'] for item in app.full_data] == ['ANOTHER-CAR']
    assert app.s3_client.calls[0]['Prefix'] == VDELOG_PREFIX


def test_full_listing_retains_pagination(app, ui, monkeypatch):
    choose(ui, monkeypatch, ('all', ''))
    class PagedClient(Client):
        def list_objects_v2(self, **kwargs):
            self.calls.append(kwargs)
            if 'ContinuationToken' not in kwargs:
                return {'CommonPrefixes': [{'Prefix': VDELOG_PREFIX + VIN + '/'}],
                        'IsTruncated': True, 'NextContinuationToken': 'page-2'}
            return {'CommonPrefixes': [{'Prefix': VDELOG_PREFIX + 'ANOTHER-CAR/'}]}
    app.s3_client = PagedClient()
    app.refresh_current_path()
    finish(app)
    assert len(app.full_data) == 2
    assert app.s3_client.calls[1]['ContinuationToken'] == 'page-2'


def test_malformed_pagination_stops_instead_of_looping(app, ui, monkeypatch):
    choose(ui, monkeypatch, ('all', ''))
    monkeypatch.setattr(ui[0].messagebox, 'showerror', lambda *args: None)
    app.s3_client = Client({'IsTruncated': True})
    app.refresh_current_path()
    finish(app)
    assert len(app.s3_client.calls) == 1
    assert '分页标记' in app.status_var.get()


def test_cancel_dialog_sends_no_request_and_clears_stale_parent_rows(app, ui, monkeypatch):
    choose(ui, monkeypatch, None)
    app.full_data = [{'name': 'parent-folder'}]
    app.tree.insert('', tk.END, values=('parent-folder', '文件夹'))
    app.list_objects_threaded(VDELOG_BUCKET, VDELOG_PREFIX)
    assert app.s3_client.calls == []
    assert app.full_data == []
    assert not app.tree.get_children()


def test_manual_refresh_can_change_vin_and_auto_refresh_reuses_choice(app, ui, monkeypatch):
    choose(ui, monkeypatch, ('vin', VIN))
    app.refresh_current_path()
    finish(app)
    class UnexpectedDialog:
        def __init__(self, *args):
            raise AssertionError('automatic refresh must not show another dialog')
    monkeypatch.setattr(ui[0], 'VdeLogRefreshDialog', UnexpectedDialog)
    app.refresh_current_path(prompt_vdelog=False)
    finish(app)
    assert len(app.s3_client.calls) == 2
    assert all(c['Prefix'] == VDELOG_PREFIX + VIN + '/' for c in app.s3_client.calls)


def test_return_from_vehicle_folder_prompts_and_keeps_filtered_root(app, ui, monkeypatch):
    choose(ui, monkeypatch, ('vin', VIN))
    app.current_prefix = VDELOG_PREFIX + VIN + '/'
    app.navigate_up()
    finish(app)
    assert app.current_prefix == VDELOG_PREFIX
    assert app.full_data[0]['name'] == VIN


def test_path_bar_navigation_also_prompts(app, ui, monkeypatch):
    choose(ui, monkeypatch, ('vin', VIN))
    app.path_var.set('obs://' + VDELOG_BUCKET + '/' + VDELOG_PREFIX)
    app.navigate_to_path()
    finish(app)
    assert app.full_data[0]['name'] == VIN


def test_double_clicking_vdelog_opens_scoped_refresh(app, ui, monkeypatch):
    choose(ui, monkeypatch, ('vin', VIN))
    app.current_prefix = 'data/'
    row = app.tree.insert('', tk.END, values=('vdelog', '文件夹'))
    monkeypatch.setattr(app.tree, 'identify_row', lambda y: row)
    app.on_item_double_click(type('Event', (), {'y': 1})())
    finish(app)
    assert app.current_prefix == VDELOG_PREFIX
    assert app.full_data[0]['name'] == VIN


def test_double_clicking_vin_browses_logs_without_prompt(app, ui, monkeypatch):
    row = app.tree.insert('', tk.END, values=(VIN, '文件夹'))
    monkeypatch.setattr(app.tree, 'identify_row', lambda y: row)
    class UnexpectedDialog:
        def __init__(self, *args):
            raise AssertionError('VIN child must use ordinary folder browsing')
    monkeypatch.setattr(ui[0], 'VdeLogRefreshDialog', UnexpectedDialog)
    app.s3_client = Client({'CommonPrefixes': [{'Prefix': VDELOG_PREFIX + VIN + '/day/'}]})
    app.on_item_double_click(type('Event', (), {'y': 1})())
    finish(app)
    assert app.current_prefix == VDELOG_PREFIX + VIN + '/'
    assert app.full_data[0]['name'] == 'day'


def test_old_full_scan_cannot_overwrite_new_vin_result(app, ui, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    class SlowClient(Client):
        def list_objects_v2(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs['Prefix'] == VDELOG_PREFIX:
                entered.set()
                release.wait(3)
                return {'CommonPrefixes': [{'Prefix': VDELOG_PREFIX + 'OLD-CAR/'}]}
            return self.response
    app.s3_client = SlowClient()
    choose(ui, monkeypatch, ('all', ''))
    app.refresh_current_path()
    assert entered.wait(1)
    choose(ui, monkeypatch, ('vin', VIN))
    app.refresh_current_path()
    finish(app)
    release.set()
    for _ in range(5):
        app.root.update()
        time.sleep(0.02)
    assert [item['name'] for item in app.full_data] == [VIN]


def test_real_dialog_has_both_modes_and_vin_input(ui):
    _, root = ui
    dialog = VdeLogRefreshDialog(root)
    assert dialog.mode_var.get() == 'vin'
    dialog.mode_var.set('all')
    dialog.update_mode()
    assert str(dialog.vin_entry['state']) == 'disabled'
    dialog.mode_var.set('vin')
    dialog.update_mode()
    dialog.vin_var.set(VIN.lower())
    root.after(0, dialog.confirm)
    assert dialog.show() == ('vin', VIN)


def test_real_dialog_cancel(ui):
    _, root = ui
    dialog = VdeLogRefreshDialog(root)
    root.after(0, dialog.cancel)
    assert dialog.show() is None
