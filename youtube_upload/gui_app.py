#!/usr/bin/env python
"""Simple Tkinter GUI wrapper around the existing youtube-upload CLI."""

import json
import shlex
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    DND_FILES = None
    TkinterDnD = None

# Ensure local package has priority whether run as module or script
SCRIPT_DIR = Path(__file__).resolve().parent
PARENT = SCRIPT_DIR.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))
import youtube_upload.main as cli_main


class UploadGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("YouTube Upload GUI")

        self.video_paths = []
        self.thumbnail_path = None
        self.progress_bars = {}
        self._uploading = False
        self.cancel_event = threading.Event()
        self._log_lines = 0

        self._build_form()
        self._load_settings()
        self._setup_drag_and_drop()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_form(self):
        frame = ttk.Frame(self.root, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        # Title
        ttk.Label(frame, text="Title*").grid(row=0, column=0, sticky="w")
        self.title_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.title_var, width=50).grid(row=0, column=1, sticky="ew")

        # Description
        ttk.Label(frame, text="Description").grid(row=1, column=0, sticky="nw")
        self.description_text = tk.Text(frame, width=50, height=4)
        self.description_text.grid(row=1, column=1, sticky="ew")

        # Tags
        ttk.Label(frame, text="Tags (comma separated)").grid(row=2, column=0, sticky="w")
        self.tags_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.tags_var).grid(row=2, column=1, sticky="ew")

        # Category
        ttk.Label(frame, text="Category").grid(row=3, column=0, sticky="w")
        self.category_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.category_var).grid(row=3, column=1, sticky="ew")

        # Playlist
        ttk.Label(frame, text="Playlist").grid(row=4, column=0, sticky="w")
        self.playlist_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.playlist_var).grid(row=4, column=1, sticky="ew")

        # Privacy
        ttk.Label(frame, text="Privacy").grid(row=5, column=0, sticky="w")
        self.privacy_var = tk.StringVar(value="public")
        ttk.Combobox(frame, textvariable=self.privacy_var, values=["public", "unlisted", "private"], state="readonly").grid(row=5, column=1, sticky="w")

        # Publish at
        ttk.Label(frame, text="Publish at (ISO 8601)").grid(row=6, column=0, sticky="w")
        self.publish_at_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.publish_at_var).grid(row=6, column=1, sticky="ew")

        # Skip if exists
        ttk.Label(frame, text="Skip if exists").grid(row=7, column=0, sticky="w")
        self.skip_if_exists_var = tk.StringVar(value="hash")
        ttk.Combobox(
            frame,
            textvariable=self.skip_if_exists_var,
            values=["none", "title", "hash"],
            state="readonly",
        ).grid(row=7, column=1, sticky="w")

        # Thumbnail
        thumb_frame = ttk.Frame(frame)
        thumb_frame.grid(row=8, column=1, sticky="w")
        ttk.Button(thumb_frame, text="Choose Thumbnail", command=self._choose_thumbnail).grid(row=0, column=0, sticky="w")
        self.thumb_label = ttk.Label(thumb_frame, text="No file selected")
        self.thumb_label.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(frame, text="Thumbnail").grid(row=8, column=0, sticky="w")

        # Client secrets (optional; auto-resolves from accounts dir if set)
        ttk.Label(frame, text="Client secrets (optional)").grid(row=9, column=0, sticky="w")
        secrets_frame = ttk.Frame(frame)
        secrets_frame.grid(row=9, column=1, sticky="ew")
        self.secrets_var = tk.StringVar()
        ttk.Entry(secrets_frame, textvariable=self.secrets_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(secrets_frame, text="Browse", command=self._choose_client_secrets).grid(row=0, column=1, padx=6)
        secrets_frame.columnconfigure(0, weight=1)

        # Credentials (optional)
        ttk.Label(frame, text="Credentials file (optional)").grid(row=10, column=0, sticky="w")
        creds_frame = ttk.Frame(frame)
        creds_frame.grid(row=10, column=1, sticky="ew")
        self.credentials_var = tk.StringVar()
        ttk.Entry(creds_frame, textvariable=self.credentials_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(creds_frame, text="Browse", command=self._choose_credentials).grid(row=0, column=1, padx=6)
        creds_frame.columnconfigure(0, weight=1)

        # Account selection
        ttk.Label(frame, text="Account label").grid(row=11, column=0, sticky="w")
        self.account_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.account_var).grid(row=11, column=1, sticky="ew")

        ttk.Label(frame, text="Accounts (comma separated)").grid(row=12, column=0, sticky="w")
        self.accounts_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.accounts_var).grid(row=12, column=1, sticky="ew")

        # Accounts dir
        ttk.Label(frame, text="Accounts dir (optional)").grid(row=13, column=0, sticky="w")
        accounts_dir_frame = ttk.Frame(frame)
        accounts_dir_frame.grid(row=13, column=1, sticky="ew")
        self.accounts_dir_var = tk.StringVar()
        ttk.Entry(accounts_dir_frame, textvariable=self.accounts_dir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(accounts_dir_frame, text="Browse", command=self._choose_accounts_dir).grid(row=0, column=1, padx=6)
        accounts_dir_frame.columnconfigure(0, weight=1)

        # Checkboxes
        # Default to console-based auth to avoid missing Qt/PySide dependencies
        self.auth_browser_var = tk.BooleanVar(value=False)
        self.open_link_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Authenticate with browser (GUI)", variable=self.auth_browser_var).grid(row=14, column=1, sticky="w")
        ttk.Checkbutton(frame, text="Open uploaded video link after upload", variable=self.open_link_var).grid(row=15, column=1, sticky="w")
        self.debug_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Enable debug logging", variable=self.debug_var).grid(row=16, column=1, sticky="w")

        # Video selector
        ttk.Label(frame, text="Choose Video(s)*").grid(row=17, column=0, sticky="w")
        videos_frame = ttk.Frame(frame)
        videos_frame.grid(row=17, column=1, sticky="ew")
        self.video_path_var = tk.StringVar()
        ttk.Entry(videos_frame, textvariable=self.video_path_var, width=40).grid(row=0, column=0, sticky="ew")
        ttk.Button(videos_frame, text="Browse", command=self._choose_videos).grid(row=0, column=1, padx=6)
        self.videos_label = ttk.Label(videos_frame, text="No videos selected")
        self.videos_label.grid(row=0, column=2, padx=6, sticky="w")
        videos_frame.columnconfigure(0, weight=1)

        # Upload/cancel buttons
        buttons = ttk.Frame(frame)
        buttons.grid(row=18, column=1, sticky="e", pady=8)
        self.upload_button = ttk.Button(buttons, text="Upload", command=self._upload)
        self.upload_button.grid(row=0, column=0, padx=(0, 6))
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self._cancel_upload, state="disabled")
        self.cancel_button.grid(row=0, column=1)

        # Progress area
        ttk.Separator(frame, orient="horizontal").grid(row=19, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        ttk.Label(frame, text="Upload Progress").grid(row=20, column=0, sticky="nw")
        self.progress_container = ttk.Frame(frame)
        self.progress_container.grid(row=20, column=1, sticky="nsew")
        self.progress_container.columnconfigure(1, weight=1)

        # Log pane
        ttk.Separator(frame, orient="horizontal").grid(row=21, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        ttk.Label(frame, text="Status / Log").grid(row=22, column=0, sticky="nw")
        log_frame = ttk.Frame(frame)
        log_frame.grid(row=22, column=1, sticky="nsew")
        self.log_text = tk.Text(log_frame, width=60, height=8, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        for i in range(0, 23):
            frame.rowconfigure(i, pad=4)
        frame.columnconfigure(1, weight=1)

    @property
    def _settings_path(self):
        return Path.home() / ".youtube_upload_gui_settings.json"

    def _load_settings(self):
        try:
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            # Ignore malformed settings to avoid blocking startup
            return

        self.category_var.set(data.get("category", ""))
        self.privacy_var.set(data.get("privacy", "public"))
        self.publish_at_var.set(data.get("publish_at", ""))
        self.skip_if_exists_var.set(data.get("skip_if_exists", "hash"))
        self.secrets_var.set(data.get("secrets_path", ""))
        self.credentials_var.set(data.get("credentials_path", ""))
        self.account_var.set(data.get("account", ""))
        self.accounts_var.set(data.get("accounts", ""))
        self.accounts_dir_var.set(data.get("accounts_dir", ""))
        self.auth_browser_var.set(bool(data.get("auth_browser", False)))
        self.open_link_var.set(bool(data.get("open_link", False)))
        self.debug_var.set(bool(data.get("debug", False)))

        description = data.get("description", "")
        if description:
            self.description_text.delete("1.0", "end")
            self.description_text.insert("1.0", description)

        thumb = data.get("thumbnail_path")
        if thumb:
            self.thumbnail_path = thumb
            self.thumb_label.config(text=Path(thumb).name)

        videos = data.get("video_paths") or []
        if videos:
            self.video_paths = videos
            self.video_path_var.set("; ".join(self.video_paths))
            self.videos_label.config(text=f"{len(self.video_paths)} file(s) selected")

        geometry = data.get("geometry")
        if geometry:
            try:
                self.root.geometry(geometry)
            except Exception:
                pass

    def _save_settings(self):
        data = {
            "category": self.category_var.get(),

            "privacy": self.privacy_var.get(),
            "publish_at": self.publish_at_var.get(),
            "skip_if_exists": self.skip_if_exists_var.get(),
            "secrets_path": self.secrets_var.get(),
            "credentials_path": self.credentials_var.get(),
            "account": self.account_var.get(),
            "accounts": self.accounts_var.get(),
            "accounts_dir": self.accounts_dir_var.get(),
            "auth_browser": bool(self.auth_browser_var.get()),
            "open_link": bool(self.open_link_var.get()),
            "debug": bool(self.debug_var.get()),
            "thumbnail_path": self.thumbnail_path,
            "video_paths": self.video_paths if self.video_paths else [],
            "geometry": self.root.winfo_geometry(),

        }
        try:
            self._settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            # Failing to save settings should not crash the app
            pass

    def _on_close(self):
        self._save_settings()
        self.root.destroy()

    def _choose_videos(self):
        paths = filedialog.askopenfilenames(title="Select video files")
        if paths:
            self.video_paths = list(paths)
            self.videos_label.config(text=f"{len(self.video_paths)} file(s) selected")
            self._reset_progress_bars()

    def _choose_thumbnail(self):
        path = filedialog.askopenfilename(title="Select thumbnail (JPEG/PNG)")
        if path:
            self.thumbnail_path = path
            self.thumb_label.config(text=Path(path).name)

    def _choose_client_secrets(self):
        path = filedialog.askopenfilename(title="Select client_secrets.json")
        if path:
            self.secrets_var.set(path)

    def _choose_credentials(self):
        path = filedialog.askopenfilename(title="Select credentials file")
        if path:
            self.credentials_var.set(path)

    def _choose_accounts_dir(self):
        path = filedialog.askdirectory(title="Select accounts directory")
        if path:
            self.accounts_dir_var.set(path)

    def _setup_drag_and_drop(self):
        if TkinterDnD and DND_FILES:
            try:
                self.root.drop_target_register(DND_FILES)
                self.root.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

    def _on_drop(self, event):
        raw = event.data or ""
        try:
            dropped = shlex.split(raw)
        except ValueError:
            dropped = raw.split()
        files = []
        for item in dropped:
            path = Path(item)
            if path.is_dir():
                for sub in path.rglob("*"):
                    if sub.is_file():
                        files.append(str(sub))
            elif path.is_file():
                files.append(str(path))
        if files:
            self.video_paths = files
            self.video_path_var.set("; ".join(self.video_paths))
            self.videos_label.config(text=f"{len(self.video_paths)} file(s) selected")
            self._reset_progress_bars()
            self._log(f"Added {len(files)} file(s) via drag-and-drop.")

    def _log(self, message):
        self._log_lines += 1
        prefix = f"[{self._log_lines:03d}] "
        self.log_text.configure(state="normal")
        self.log_text.insert("end", prefix + str(message) + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _reset_progress_bars(self):
        # Clear previous progress widgets
        for child in self.progress_container.winfo_children():
            child.destroy()
        self.progress_bars = {}
        for row, path in enumerate(self.video_paths):
            ttk.Label(self.progress_container, text=Path(path).name).grid(row=row, column=0, sticky="w", padx=(0, 6))
            bar = ttk.Progressbar(self.progress_container, mode="determinate", length=240, maximum=100)
            bar.grid(row=row, column=1, sticky="ew", pady=2)
            self.progress_bars[path] = bar

    def _progress_factory(self, video_path):
        bar = self.progress_bars.get(video_path)

        def _callback(total_size, completed):
            if self.cancel_event.is_set():
                raise cli_main.UploadCancelled("Cancelled by user")
            if not bar:
                return
            def _update():
                bar.configure(maximum=max(total_size, 1))
                bar["value"] = completed
            self._log(f"{Path(video_path).name}: {completed}/{total_size} bytes")
            self.root.after(0, _update)

        def _finish():
            if not bar:
                return
            self.root.after(0, lambda: bar.configure(value=bar.cget("maximum")))

        return cli_main.struct("ProgressInfo", ["callback", "finish"])(callback=_callback, finish=_finish)

    def _validate_fields(self):
        errors = []
        if not self.title_var.get().strip():
            errors.append("Title is required.")
        if not self.video_paths:
            errors.append("Select at least one video file.")
        missing = [p for p in self.video_paths if not Path(p).exists()]
        if missing:
            errors.append("Missing files:\n" + "\n".join(missing[:5]))
        publish_at = self.publish_at_var.get().strip()
        if publish_at:
            try:
                parsed = publish_at.replace("Z", "+00:00")
                datetime.fromisoformat(parsed)
            except ValueError:
                errors.append("Publish at must be ISO 8601 (e.g., 2024-01-31T12:30:00Z).")
        thumb = self.thumbnail_path
        if thumb and not Path(thumb).exists():
            errors.append("Thumbnail path does not exist.")
        for optional_path, label in [
            (self.secrets_var.get().strip(), "Client secrets"),
            (self.credentials_var.get().strip(), "Credentials file"),
            (self.accounts_dir_var.get().strip(), "Accounts directory"),
        ]:
            if optional_path and not Path(optional_path).exists():
                errors.append(f"{label} does not exist: {optional_path}")
        unsupported = []
        for p in self.video_paths:
            suffix = Path(p).suffix.lower().lstrip(".")
            if suffix and suffix not in cli_main.SUPPORTED_VIDEO_EXTENSIONS:
                unsupported.append(p)
        if unsupported:
            errors.append("Unsupported formats:\n" + "\n".join(unsupported[:5]))
        return errors

    def _cancel_upload(self):
        if self._uploading:
            self.cancel_event.set()
            self._log("Cancellation requested.")
            self.cancel_button.state(["disabled"])

    def _upload(self):
        if self._uploading:
            messagebox.showinfo("Upload in progress", "An upload is already running.")
            return
        errors = self._validate_fields()
        if errors:
            messagebox.showerror("Invalid fields", "\n\n".join(errors))
            return
        title = self.title_var.get().strip()
        self._reset_progress_bars()

        description = self.description_text.get("1.0", "end").strip()
        args = []
        # Only pass an explicit title if the user provided one; otherwise let the CLI
        # derive a per-file title from each video path.
        if title:
            args += ["--title", title]

        if description:
            args += ["--description", description]
        if self.tags_var.get().strip():
            args += ["--tags", self.tags_var.get().strip()]
        if self.category_var.get().strip():
            args += ["--category", self.category_var.get().strip()]
        if self.playlist_var.get().strip():
            args += ["--playlist", self.playlist_var.get().strip()]

        # Avoid auto-appending counters like "[2/15]" when uploading multiple files.
        # If the user wants a custom template they can still set it via CLI manually.
        if len(self.video_paths) > 1:
            args += ["--title-template", "{title}"]

        privacy = self.privacy_var.get().strip()
        if privacy and privacy != "public":
            args += ["--privacy", privacy]

        if self.publish_at_var.get().strip():
            args += ["--publish-at", self.publish_at_var.get().strip()]

        if self.thumbnail_path:
            args += ["--thumbnail", self.thumbnail_path]

        # Prefer explicit client secrets; otherwise CLI will resolve from accounts dir or defaults
        if self.secrets_var.get().strip():
            args += ["--client-secrets", self.secrets_var.get().strip()]
        if self.credentials_var.get().strip():
            args += ["--credentials-file", self.credentials_var.get().strip()]

        if self.account_var.get().strip():
            args += ["--account", self.account_var.get().strip()]
        if self.accounts_var.get().strip():
            args += ["--accounts", self.accounts_var.get().strip()]
        if self.accounts_dir_var.get().strip():
            args += ["--accounts-dir", self.accounts_dir_var.get().strip()]

        if self.auth_browser_var.get():
            args.append("--auth-browser")
        if self.open_link_var.get():
            args.append("--open-link")
        if self.debug_var.get():
            args.append("--debug")
        skip_if_exists = self.skip_if_exists_var.get().strip()
        if skip_if_exists and skip_if_exists != "none":
            args += ["--skip-if-exists", skip_if_exists]

        full_args = args + self.video_paths

        self._uploading = True
        self.cancel_event.clear()
        cli_main.set_progress_factory(self._progress_factory)
        self.upload_button.state(["disabled"])
        self.cancel_button.state(["!disabled"])
        self._log(f"Starting upload of {len(self.video_paths)} file(s).")

        def _worker():
            try:
                cli_main.main(full_args)
                self.root.after(0, lambda: messagebox.showinfo("Upload complete", "Upload finished without error."))
                self._log("Upload completed successfully.")
            except SystemExit as exc:  # in case cli_main.run() is used accidentally
                if exc.code not in (None, 0):
                    self.root.after(0, lambda exc=exc: messagebox.showerror("Upload error", f"Exited with code {exc.code}"))
                    self._log(f"Upload exited with code {exc.code}")
            except cli_main.UploadCancelled:
                self.root.after(0, lambda: messagebox.showinfo("Upload cancelled", "Upload was cancelled."))
                self._log("Upload cancelled by user.")
            except Exception as exc:  # broad to surface any CLI errors to the user
                self.root.after(0, lambda exc=exc: messagebox.showerror("Upload failed", str(exc)))
                self._log(f"Upload failed: {exc}")
            finally:
                self._uploading = False
                self.upload_button.state(["!disabled"])
                self.cancel_button.state(["disabled"])

        threading.Thread(target=_worker, daemon=True).start()


def main():
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    UploadGUI(root)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
