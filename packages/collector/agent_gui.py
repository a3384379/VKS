"""Native Windows-first collector UI. No web page can request a local path."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog
import time
from .agent import AgentClient, LocalCollectorAgent, authorize_profile


class CollectorWindow:
    def __init__(self, root, directory=None):
        self.root = root
        root.title("VKS 2.0 · 本机只读采集")
        root.geometry("900x740")
        root.minsize(700, 650)
        self.agent = LocalCollectorAgent(directory or Path.home() / ".vks/collector")
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.client = None
        self.profile = None
        self.next_due = 0
        self.projects = []
        self.fields = {
            key: tk.StringVar()
            for key in (
                "base_url",
                "project_id",
                "project_key",
                "source_key",
                "provider",
                "root",
                "revision",
                "analysis_set_id",
                "token",
                "interval",
            )
        }
        self.fields["provider"].set("directory")
        self.fields["source_key"].set("repository")
        self.fields["interval"].set("60")
        self.consent = tk.BooleanVar(value=False)
        self.schedule = tk.BooleanVar(value=False)
        self.detect_changes = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="选择本机范围并授权，令牌仅保留在本次会话内。")
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="把已授权资料整理为可审核的知识草稿", font=("", 16, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 16)
        )
        labels = [
            ("base_url", "VKS 站点（远端需 HTTPS）"),
            ("project_id", "项目 ID"),
            ("project_key", "项目 key"),
            ("source_key", "稳定来源 key"),
            ("provider", "来源类型"),
            ("root", "授权目录 / SVN 地址"),
            ("revision", "Git ref / SVN 数字 revision"),
            ("analysis_set_id", "已有草稿 ID（选填）"),
            ("token", "专用项目令牌（不保存）"),
            ("interval", "本会话检查间隔（秒）"),
        ]
        for row, (key, label) in enumerate(labels, 1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 14))
            if key == "provider":
                widget = ttk.Combobox(
                    frame, textvariable=self.fields[key], values=("directory", "git", "svn"), state="readonly", name=key
                )
            else:
                widget = ttk.Entry(frame, textvariable=self.fields[key], show="•" if key == "token" else "", name=key)
            widget.grid(row=row, column=1, sticky="ew", pady=5)
            if key == "root":
                ttk.Button(frame, text="选择目录…", command=self.choose).grid(row=row, column=2, padx=(8, 0))
            if key == "base_url":
                ttk.Button(frame, text="读取可见项目", command=self.connect).grid(row=row, column=2, padx=(8, 0))
        ttk.Checkbutton(
            frame,
            text="授权只读采集所选来源；忽略链接、秘密与构建目录，资料仅发送到上述 VKS。",
            variable=self.consent,
            name="consent",
        ).grid(row=11, column=0, columnspan=3, sticky="w", pady=12)
        ttk.Checkbutton(
            frame,
            text="在本次打开期间周期检查；关闭后停止，重新打开需再次输入令牌与授权。",
            variable=self.schedule,
            command=self.toggle,
            name="schedule",
        ).grid(row=12, column=0, columnspan=3, sticky="w")
        buttons = ttk.Frame(frame)
        buttons.grid(row=13, column=0, columnspan=3, sticky="w", pady=18)
        self.buttons = [
            ttk.Button(buttons, text="预览只读采集", command=lambda: self.start(False), name="preview"),
            ttk.Button(buttons, text="同步到 VKS / 重试", command=lambda: self.start(True), name="sync"),
            ttk.Button(buttons, text="新建授权配置", command=self.new_profile, name="new_profile"),
        ]
        for button in self.buttons:
            button.pack(side="left", padx=(0, 10))
        ttk.Label(frame, textvariable=self.status, wraplength=810, name="status").grid(
            row=14, column=0, columnspan=3, sticky="nw", pady=10
        )
        ttk.Label(
            frame,
            text="每次固定来源版本。游标仅在服务器任务成功后推进；断网保留回执。仅生成待审草稿，不批准或发布。",
            wraplength=810,
        ).grid(row=15, column=0, columnspan=3, sticky="w", pady=12)
        self.profiles = self.agent.load_profiles()
        if self.profiles:
            names = [f"{row.project_key} · {row.source_key} · {row.id[:8]}" for row in self.profiles]
            selected = ttk.Combobox(frame, values=names, state="readonly", name="profiles")
            selected.grid(row=16, column=0, columnspan=3, sticky="ew")
            selected.bind("<<ComboboxSelected>>", lambda e: self.load(self.profiles[selected.current()]))
        self.project_selector = ttk.Combobox(frame, state="readonly", name="visible_projects")
        self.project_selector.grid(row=17, column=0, columnspan=3, sticky="ew")
        self.project_selector.bind("<<ComboboxSelected>>", self.select_project)
        ttk.Checkbutton(
            frame,
            text="同步成功后建立变更影响候选（令牌需具备分析权限）",
            variable=self.detect_changes,
            name="detect_changes",
        ).grid(row=18, column=0, columnspan=3, sticky="w", pady=6)
        self.detect_changes.trace_add("write", lambda *_: self.edited("detect_changes"))
        for key, value in self.fields.items():
            value.trace_add("write", lambda *_, key=key: self.edited(key))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(200, self.tick)

    def choose(self):
        directory = filedialog.askdirectory(parent=self.root, title="选择本次授权的只读来源目录", mustexist=True)
        if directory:
            self.fields["root"].set(directory)
            self.consent.set(False)
            self.schedule.set(False)

    def edited(self, key):
        self.schedule.set(False)
        if key not in ("interval", "token"):
            self.consent.set(False)
        if key == "base_url":
            self.fields["token"].set("")

    def select_project(self, event=None):
        index = self.project_selector.current()
        if index >= 0:
            row = self.projects[index]
            self.fields["project_id"].set(row["id"])
            self.fields["project_key"].set(row["key"])
            self.fields["analysis_set_id"].set("")

    def connect(self):
        if self.future is not None:
            return
        try:
            if self.client:
                self.client.close()
            self.client = AgentClient(self.fields["base_url"].get().strip(), self.fields["token"].get())

            def read():
                rows = []
                cursor = None
                for _ in range(10):
                    params = {"limit": 100}
                    if cursor:
                        params["cursor"] = cursor
                    page = self.client.request("GET", "/api/v1/projects", params=params, envelope=True)
                    rows.extend(page["data"])
                    cursor = page.get("meta", {}).get("next_cursor")
                    if not cursor:
                        break
                return dict(state="projects_loaded", projects=rows, truncated=bool(cursor))

            self.future = self.pool.submit(read)
            self.status.set("正在读取令牌允许的项目…")
        except Exception as exc:
            self.status.set(str(exc))

    def load(self, profile):
        self.profile = profile
        for key in self.fields:
            if hasattr(profile, key):
                self.fields[key].set(getattr(profile, key) or "")
        self.fields["interval"].set(str(profile.interval_seconds or 60))
        self.fields["token"].set("")
        self.consent.set(False)
        self.schedule.set(False)
        self.detect_changes.set(profile.detect_changes)

    def new_profile(self):
        self.profile = None
        self.consent.set(False)
        self.schedule.set(False)
        self.status.set("下次授权会建立独立配置，旧配置与未完成回执继续保留。")

    def configure(self):
        values = {k: v.get().strip() for k, v in self.fields.items()}
        candidate = authorize_profile(
            root=values["root"],
            provider=values["provider"],
            project_id=values["project_id"],
            project_key=values["project_key"],
            source_key=values["source_key"],
            base_url=values["base_url"],
            consent=self.consent.get(),
            revision=values["revision"],
            analysis_set_id=values["analysis_set_id"] or None,
            interval_seconds=int(values["interval"]) if self.schedule.get() else 0,
            detect_changes=self.detect_changes.get(),
        )
        if self.profile and self.profile.consent_sha256 == candidate.consent_sha256:
            candidate = replace(candidate, id=self.profile.id)
        self.profile = candidate
        self.agent.save_profile(candidate)
        return candidate

    def start(self, upload, scheduled=False):
        if self.future is not None:
            return
        try:
            profile = self.configure()
            if upload:
                if self.client:
                    self.client.close()
                self.client = AgentClient(profile.base_url, self.fields["token"].get())
            self.future = (
                self.pool.submit(self.agent.sync_once, profile, self.client, retry_failed=not scheduled)
                if upload
                else self.pool.submit(self.agent.preview, profile)
            )
            for button in self.buttons:
                button.configure(state="disabled")
            self.status.set("正在只读处理；原项目文件保持不变…")
        except Exception as exc:
            self.schedule.set(False)
            self.status.set(str(exc))

    def toggle(self):
        if self.schedule.get():
            self.start(True)
            self.next_due = time.monotonic() + (
                self.profile.interval_seconds if self.profile and self.profile.interval_seconds else 60
            )

    def tick(self):
        if self.future is not None and self.future.done():
            try:
                result = self.future.result()
                delta = result.get("delta", {})
                if result["state"] == "projects_loaded":
                    self.projects = result["projects"]
                    self.project_selector.configure(values=[row["name"] + " · " + row["key"] for row in self.projects])
                    self.status.set(
                        f"已读取 {len(self.projects)} 个可见项目，请从下方选择。"
                        + ("仅显示前 1000 个，请在 VKS 查询后填写准确项目。" if result["truncated"] else "")
                    )
                else:
                    self.status.set(
                        f"状态：{result['state']}；文件 {delta.get('current_file_count', 0)}，新增 {len(delta.get('added', []))}，修改 {len(delta.get('modified', []))}，删除 {len(delta.get('removed', []))}，移动 {len(delta.get('moved', []))}。任务：{result.get('job_id', '待提交')}。"
                    )
                if result["state"] in ("failed", "cancelled"):
                    self.schedule.set(False)
            except Exception as exc:
                self.schedule.set(False)
                self.status.set(str(exc))
            self.future = None
            for button in self.buttons:
                button.configure(state="normal")
            self.next_due = time.monotonic() + (
                self.profile.interval_seconds if self.profile and self.profile.interval_seconds else 60
            )
        if self.schedule.get() and self.future is None and time.monotonic() >= self.next_due:
            self.start(True, scheduled=True)
        self.root.after(200, self.tick)

    def close(self):
        self.schedule.set(False)
        self.fields["token"].set("")
        if self.client:
            self.client.close()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main():
    root = tk.Tk()
    CollectorWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
