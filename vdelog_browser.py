"""Huawei vehicle-log directory selection and an exact VIN folder lookup."""

import re
import tkinter as tk
from tkinter import ttk, messagebox


VDELOG_BUCKET = 'example-vehicle-logs'
VDELOG_PREFIX = 'data/vdelog/'


def is_vdelog_path(provider, bucket, prefix):
    return (provider == '华为云OBS' and bucket == VDELOG_BUCKET
            and str(prefix).rstrip('/') + '/' == VDELOG_PREFIX)


def normalize_vin(value):
    vin = str(value).strip().upper()
    if not re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', vin):
        raise ValueError('请输入完整的 17 位 VIN，由数字和英文字母组成，不包含 I、O、Q。')
    return vin


def find_vin_folder(client, bucket, base_prefix, vin):
    """Check existence with one key, without enumerating other vehicle folders."""
    vin = normalize_vin(vin)
    folder_prefix = base_prefix.rstrip('/') + '/' + vin + '/'
    response = client.list_objects_v2(Bucket=bucket, Prefix=folder_prefix, MaxKeys=1)
    contents = response.get('Contents', [])
    if not contents:
        return []
    if any(not item['Key'].startswith(folder_prefix) for item in contents):
        raise ValueError('服务端返回了指定 VIN 文件夹之外的对象，请重试。')
    return [{
        'name': vin, 'type': '文件夹', 'size': '-', 'modified': '-',
        'key': folder_prefix, 'storage_class': '-', 'etag': '-',
    }]


class VdeLogRefreshDialog:
    """Choose all vehicle folders or enter a VIN in the same modal dialog."""

    def __init__(self, parent, previous=None):
        self.result = None
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title('车辆日志刷新范围')
        self.window.transient(parent)
        self.window.resizable(False, False)
        self.window.protocol('WM_DELETE_WINDOW', self.cancel)
        self.window.bind('<Escape>', lambda _event: self.cancel())
        self.window.bind('<Return>', lambda _event: self.confirm())
        previous = previous or ('vin', '')
        self.mode_var = tk.StringVar(value=previous[0])
        self.vin_var = tk.StringVar(value=previous[1])
        body = ttk.Frame(self.window, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text='请选择车辆日志的刷新范围：', font=('', 11, 'bold')).pack(anchor=tk.W)
        ttk.Label(body, text='obs://' + VDELOG_BUCKET + '/' + VDELOG_PREFIX).pack(anchor=tk.W, pady=(8, 12))
        ttk.Radiobutton(body, text='刷新列出所有车辆日志', variable=self.mode_var,
                        value='all', command=self.update_mode).pack(anchor=tk.W)
        ttk.Label(body, text='车辆数量较多时，全量刷新可能需要较长时间。').pack(anchor=tk.W, padx=22, pady=(2, 12))
        ttk.Radiobutton(body, text='指定 VIN 刷新车辆日志', variable=self.mode_var,
                        value='vin', command=self.update_mode).pack(anchor=tk.W)
        row = ttk.Frame(body)
        row.pack(fill=tk.X, padx=(22, 0), pady=8)
        ttk.Label(row, text='VIN：').pack(side=tk.LEFT)
        self.vin_entry = ttk.Entry(row, textvariable=self.vin_var, width=35)
        self.vin_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(body, text='输入完整 VIN，列表只显示对应车辆的日志文件夹。').pack(anchor=tk.W, padx=22)
        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X, pady=(16, 0))
        ttk.Button(buttons, text='取消', command=self.cancel).pack(side=tk.RIGHT)
        ttk.Button(buttons, text='刷新列表', command=self.confirm).pack(side=tk.RIGHT, padx=8)
        self.update_mode()

    def update_mode(self):
        self.vin_entry.config(state=tk.NORMAL if self.mode_var.get() == 'vin' else tk.DISABLED)
        if self.mode_var.get() == 'vin':
            self.vin_entry.focus_set()

    def show(self):
        self.window.deiconify()
        self.window.grab_set()
        self.window.wait_window()
        return self.result

    def confirm(self):
        mode = self.mode_var.get()
        try:
            vin = normalize_vin(self.vin_var.get()) if mode == 'vin' else ''
        except ValueError as exc:
            messagebox.showwarning('VIN 无效', str(exc), parent=self.window)
            self.vin_entry.focus_set()
            return
        self.result = (mode, vin)
        self.window.destroy()

    def cancel(self):
        self.window.destroy()
