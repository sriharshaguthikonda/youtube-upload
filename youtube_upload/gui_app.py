#!/usr/bin/env python
"""Simple Tkinter GUI wrapper around the existing youtube-upload CLI."""

import json
import re
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
from youtube_upload import gui_app_files, gui_theme  # ruff: noqa: E402
import youtube_upload.main as cli_main  # ruff: noqa: E402
import youtube_upload.content_validation as content_validation  # ruff: noqa: E402


class UploadGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("YouTube Upload GUI")
        self.theme_var = tk.StringVar(value="dark")
        self.palette = gui_theme.apply_theme(self.root, mode=self.theme_var.get())

        self.video_paths = []
        self.thumbnail_path = None
        self.progress_bars = {}
        self._uploading = False
        self.cancel_event = threading.Event()
        self._log_lines = 0
        self._error_lines = 0
        self._loading_settings = False
        self._last_loaded_accounts_dir: str | None = None

        self._build_form()
        self._load_settings()
        self._setup_drag_and_drop()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_form(self):
        # Scrollable container to keep logs/errors reachable when many uploads are listed
        container = ttk.Frame(self.root)
        container.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        canvas = tk.Canvas(container, highlightthickness=0, background=self.palette["bg"])
        vscroll = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        frame = ttk.Frame(canvas, padding=12)
        self._content_window = canvas.create_window((0, 0), window=frame, anchor="nw")

        def _update_scroll_region(event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(self._content_window, width=canvas.winfo_width())

        frame.bind("<Configure>", _update_scroll_region)
        canvas.bind("<Configure>", _update_scroll_region)

        def _on_mousewheel(event):
            delta = -1 * int(event.delta / 120) if event.delta else 0
            canvas.yview_scroll(delta, "units")

        # Enable mouse wheel scrolling (Windows / most platforms)
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # Title (optional; defaults to filename)
        ttk.Label(frame, text="Title (optional)").grid(row=0, column=0, sticky="w")
        self.title_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.title_var, width=50).grid(row=0, column=1, sticky="ew")

        # Description
        ttk.Label(frame, text="Description").grid(row=1, column=0, sticky="nw")
        self.description_text = tk.Text(frame, width=50, height=4)
        self.description_text.grid(row=1, column=1, sticky="ew")
        gui_theme.style_text_widget(self.description_text, self.palette)

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
        self.accounts_dir_entry = ttk.Entry(accounts_dir_frame, textvariable=self.accounts_dir_var)
        self.accounts_dir_entry.grid(row=0, column=0, sticky="ew")
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
        self.always_on_top_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Keep window on top", variable=self.always_on_top_var, command=self._apply_topmost).grid(row=17, column=1, sticky="w")

        # Theme toggle
        ttk.Label(frame, text="Theme").grid(row=18, column=0, sticky="w")
        ttk.Combobox(
            frame,
            textvariable=self.theme_var,
            values=["dark", "light"],
            state="readonly",
        ).grid(row=18, column=1, sticky="w")

        # Video selector
        ttk.Label(frame, text="Choose Video(s)*").grid(row=19, column=0, sticky="w")
        videos_frame = ttk.Frame(frame)
        videos_frame.grid(row=19, column=1, sticky="ew")
        self.video_path_var = tk.StringVar()
        ttk.Entry(videos_frame, textvariable=self.video_path_var, width=40).grid(row=0, column=0, sticky="ew")
        ttk.Button(videos_frame, text="Browse Files", command=self._choose_videos).grid(row=0, column=1, padx=6)
        ttk.Button(videos_frame, text="Browse Folder", command=self._choose_video_folder).grid(row=0, column=2, padx=6)
        self.videos_label = ttk.Label(videos_frame, text="No videos selected")
        self.videos_label.grid(row=0, column=3, padx=6, sticky="w")
        videos_frame.columnconfigure(0, weight=1)
        # React to manual typing/pasting of video paths
        self.video_path_var.trace_add("write", self._on_video_path_change)

        # Upload/cancel buttons
        buttons = ttk.Frame(frame)
        buttons.grid(row=20, column=1, sticky="e", pady=8)
        self.upload_button = ttk.Button(buttons, text="Upload", command=self._upload)
        self.upload_button.grid(row=0, column=0, padx=(0, 6))
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self._cancel_upload, state="disabled")
        self.cancel_button.grid(row=0, column=1)

        # Progress area
        ttk.Separator(frame, orient="horizontal").grid(row=21, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        ttk.Label(frame, text="Upload Progress").grid(row=22, column=0, sticky="nw")
        self.progress_container = ttk.Frame(frame)
        self.progress_container.grid(row=22, column=1, sticky="nsew")
        self.progress_container.columnconfigure(1, weight=1)

        # Log pane
        ttk.Separator(frame, orient="horizontal").grid(row=23, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        ttk.Label(frame, text="Status / Log").grid(row=24, column=0, sticky="nw")
        log_frame = ttk.Frame(frame)
        log_frame.grid(row=24, column=1, sticky="nsew")
        self.log_text = tk.Text(log_frame, width=60, height=8, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        gui_theme.style_text_widget(self.log_text, self.palette)

        # Errors pane
        ttk.Separator(frame, orient="horizontal").grid(row=25, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        ttk.Label(frame, text="Errors (skipped files)").grid(row=26, column=0, sticky="nw")
        errors_frame = ttk.Frame(frame)
        errors_frame.grid(row=26, column=1, sticky="nsew")
        self.errors_text = tk.Text(errors_frame, width=60, height=6, wrap="word", state="disabled", foreground="#ff8080")
        errors_scroll = ttk.Scrollbar(errors_frame, orient="vertical", command=self.errors_text.yview)
        self.errors_text.configure(yscrollcommand=errors_scroll.set)
        self.errors_text.grid(row=0, column=0, sticky="nsew")
        errors_scroll.grid(row=0, column=1, sticky="ns")
        errors_frame.columnconfigure(0, weight=1)
        errors_frame.rowconfigure(0, weight=1)
        gui_theme.style_text_widget(self.errors_text, self.palette)

        for i in range(0, 26):
            frame.rowconfigure(i, pad=4)
        frame.columnconfigure(1, weight=1)

        # React to accounts dir changes (typed or programmatic) by loading settings
        self.accounts_dir_var.trace_add("write", self._on_accounts_dir_change)
        # React to theme changes
        self.theme_var.trace_add("write", self._on_theme_change)

    def _settings_path_for_accounts_dir(self, accounts_dir: str | None):
        if accounts_dir:
            return Path(accounts_dir) / "gui_settings.json"
        return Path.home() / ".youtube_upload_gui_settings.json"

    @property
    def _settings_path(self):
        return self._settings_path_for_accounts_dir(self.accounts_dir_var.get().strip() or None)

    def _reset_settings_fields(self):
        self.category_var.set("")
        self.privacy_var.set("public")
        self.publish_at_var.set("")
        self.skip_if_exists_var.set("hash")
        self.theme_var.set("dark")
        self.playlist_var.set("")
        self.secrets_var.set("")
        self.credentials_var.set("")
        self.account_var.set("")
        self.accounts_var.set("")
        self.auth_browser_var.set(False)
        self.open_link_var.set(False)
        self.debug_var.set(False)
        self.always_on_top_var.set(False)
        self.description_text.delete("1.0", "end")
        self.thumbnail_path = None
        self.thumb_label.config(text="No file selected")
        self.video_paths = []
        self.video_path_var.set("")
        self.videos_label.config(text="No videos selected")

    def _apply_account_folder_defaults(self, accounts_dir: str | None):
        if not accounts_dir:
            return
        base = Path(accounts_dir)
        secret_candidates = [
            base / "client_secrets.json",
            *sorted(base.glob("client_secret*.json")),
            *sorted(base.glob("client_secrets*.json")),
        ]
        for candidate in secret_candidates:
            if candidate.exists():
                self.secrets_var.set(str(candidate))
                break

        cred_candidates = [
            base / ".youtube-upload-credentials.json",
            base / "credentials.json",
            *sorted(p for p in base.glob("*credentials*.json") if p.name != "gui_settings.json"),
        ]
        for candidate in cred_candidates:
            if candidate.exists():
                self.credentials_var.set(str(candidate))
                break
        if not self.account_var.get():
            self.account_var.set(base.name)

    def _load_settings(self, accounts_dir: str | None = None, clear_current: bool = False, apply_account_defaults: bool = False):
        target_dir = accounts_dir or self.accounts_dir_var.get().strip() or None
        # Prevent recursive trace-triggered loads
        if self._loading_settings:
            return
        self._loading_settings = True
        if clear_current:
            self._reset_settings_fields()
        settings_path = self._settings_path_for_accounts_dir(target_dir)
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if apply_account_defaults:
                self._apply_account_folder_defaults(target_dir)
            self._last_loaded_accounts_dir = target_dir
            if apply_account_defaults:
                # Persist discovered defaults (client_secrets/credentials) alongside the account dir
                self._save_settings()
            self._loading_settings = False
            return
        except Exception:
            # Ignore malformed settings to avoid blocking startup
            if apply_account_defaults:
                self._apply_account_folder_defaults(target_dir)
            self._last_loaded_accounts_dir = target_dir
            if apply_account_defaults:
                self._save_settings()
            self._loading_settings = False
            return

        data_accounts_dir = data.get("accounts_dir", target_dir or "")
        if accounts_dir is None and data_accounts_dir:
            alt_settings_path = self._settings_path_for_accounts_dir(data_accounts_dir)
            if alt_settings_path != settings_path and alt_settings_path.exists():
                self._loading_settings = False
                return self._load_settings(
                    accounts_dir=data_accounts_dir,
                    clear_current=clear_current,
                    apply_account_defaults=apply_account_defaults,
                )

        self.accounts_dir_var.set(data_accounts_dir)
        self.category_var.set(data.get("category", ""))
        self.privacy_var.set(data.get("privacy", "public"))
        self.publish_at_var.set(data.get("publish_at", ""))
        self.skip_if_exists_var.set(data.get("skip_if_exists", "hash"))
        self.theme_var.set(data.get("theme", "dark"))
        self.playlist_var.set(data.get("playlist", ""))
        self.secrets_var.set(data.get("secrets_path", ""))
        self.credentials_var.set(data.get("credentials_path", ""))
        self.account_var.set(data.get("account", ""))
        self.accounts_var.set(data.get("accounts", ""))
        self.accounts_dir_var.set(data.get("accounts_dir", ""))
        self.auth_browser_var.set(bool(data.get("auth_browser", False)))
        self.open_link_var.set(bool(data.get("open_link", False)))
        self.debug_var.set(bool(data.get("debug", False)))
        self.always_on_top_var.set(bool(data.get("always_on_top", False)))

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
        if apply_account_defaults:
            self._apply_account_folder_defaults(target_dir)
        self._apply_theme_from_var()
        self._last_loaded_accounts_dir = target_dir
        if apply_account_defaults and not self._loading_settings:
            self._save_settings()
        self._loading_settings = False
        # Apply after loading; run once immediately and once after idle so Tk has realized.
        self._apply_topmost()
        self.root.after_idle(self._apply_topmost)

    def _on_video_path_change(self, *_):
        if self._loading_settings:
            return
        raw = (self.video_path_var.get() or "").replace("\r", "")
        parts: list[str] = []
        for chunk in re.split(r"\n+", raw):
            chunk = chunk.strip()
            if not chunk:
                continue
            split_chunk = re.split(r";\s*(?=[A-Za-z]:\\)", chunk)
            parts.extend(p.strip() for p in split_chunk if p.strip())
        supported, unsupported = gui_app_files.filter_supported_video_paths(parts)
        self.video_paths = supported
        if unsupported:
            self._log(f"Ignoring unsupported files: {', '.join(Path(p).name for p in unsupported)}")
        if supported:
            self.videos_label.config(text=f"{len(supported)} supported file(s) selected")
        else:
            self.videos_label.config(text="No supported videos selected")
        self._reset_progress_bars()
        self._save_settings()
        self._ensure_playlist_default()

    def _apply_theme_from_var(self, *_):
        mode = self.theme_var.get() or "dark"
        self.palette = gui_theme.apply_theme(self.root, mode=mode)
        gui_theme.style_text_widget(self.description_text, self.palette)
        gui_theme.style_text_widget(self.log_text, self.palette)

    def _on_theme_change(self, *_):
        if self._loading_settings:
            return
        self._apply_theme_from_var()

    def _ensure_playlist_default(self):
        """If playlist is empty, default to folder name of first video (parent dir)."""
        if self.playlist_var.get().strip():
            return
        if not self.video_paths:
            return
        first = Path(self.video_paths[0])
        parent_name = first.parent.name
        if parent_name:
            self.playlist_var.set(parent_name)
        else:
            # Fallback default when no parent folder name is available
            self.playlist_var.set("Uploads")

    def _save_settings(self):
        data = {
            "category": self.category_var.get(),

            "privacy": self.privacy_var.get(),
            "publish_at": self.publish_at_var.get(),
            "skip_if_exists": self.skip_if_exists_var.get(),
            "theme": self.theme_var.get(),
            "playlist": self.playlist_var.get(),
            "secrets_path": self.secrets_var.get(),
            "credentials_path": self.credentials_var.get(),
            "account": self.account_var.get(),
            "accounts": self.accounts_var.get(),
            "accounts_dir": self.accounts_dir_var.get(),
            "auth_browser": bool(self.auth_browser_var.get()),
            "open_link": bool(self.open_link_var.get()),
            "debug": bool(self.debug_var.get()),
            "always_on_top": bool(self.always_on_top_var.get()),
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
            supported, unsupported = gui_app_files.filter_supported_video_paths(paths)
            self.video_paths = list(supported)
            if unsupported:
                self._log(f"Ignoring unsupported files: {', '.join(Path(p).name for p in unsupported)}")
            if supported:
                self.videos_label.config(text=f"{len(self.video_paths)} supported file(s) selected")
            else:
                self.videos_label.config(text="No supported videos selected")
            self._reset_progress_bars()
            self._ensure_playlist_default()

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
            # Load settings stored alongside the selected account folder
            self._load_settings(accounts_dir=path, clear_current=True, apply_account_defaults=True)

    def _choose_video_folder(self):
        path = filedialog.askdirectory(title="Select folder containing videos")
        if not path:
            return
        files = [str(p) for p in Path(path).rglob("*") if p.is_file()]
        if not files:
            self._log("No files found in the selected folder.")
            return
        supported, unsupported = gui_app_files.filter_supported_video_paths(files)
        self.video_paths = list(supported)
        if unsupported:
            self._log(f"Ignoring unsupported files: {', '.join(Path(p).name for p in unsupported)}")
        if supported:
            self.video_path_var.set("; ".join(self.video_paths))
            self.videos_label.config(text=f"{len(self.video_paths)} supported file(s) selected")
            if not self.playlist_var.get().strip():
                # Default playlist to the selected folder name for convenience
                self.playlist_var.set(Path(path).name)
        else:
            self.video_path_var.set("")
            self.videos_label.config(text="No supported videos selected")
        self._reset_progress_bars()
        self._ensure_playlist_default()

    def _on_accounts_dir_change(self, *_):
        path = (self.accounts_dir_var.get() or "").strip()
        if not path:
            return
        # Avoid reloading the same folder repeatedly or re-entrancy
        if self._loading_settings or path == self._last_loaded_accounts_dir:
            return
        if not Path(path).is_dir():
            return
        self._load_settings(accounts_dir=path, clear_current=True, apply_account_defaults=True)

    def _setup_drag_and_drop(self):
        if TkinterDnD and DND_FILES:
            try:
                self.root.drop_target_register(DND_FILES)
                self.root.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

    def _on_drop(self, event):
        raw = event.data or ""
        # Use Tcl list splitter to handle braces/quoted paths from TkinterDnD.
        try:
            dropped = list(self.root.tk.splitlist(raw))
        except Exception:
            dropped = re.findall(r"{[^}]+}|[^\s]+", raw.strip())
        files: list[str] = []
        for item in dropped:
            path = Path(item.strip("{}"))
            if path.is_dir():
                files.extend(str(sub) for sub in path.rglob("*") if sub.is_file())
            elif path.is_file():
                files.append(str(path))
        if not files:
            self._log("No files found in dropped items.")
            return
        supported, unsupported = gui_app_files.filter_supported_video_paths(files)
        self.video_paths = list(supported)
        if unsupported:
            self._log(f"Ignoring unsupported files: {', '.join(Path(p).name for p in unsupported)}")
        if supported:
            self.video_path_var.set("; ".join(self.video_paths))
            self.videos_label.config(text=f"{len(self.video_paths)} supported file(s) selected")
            self._log(f"Added {len(supported)} file(s) via drag-and-drop.")
        else:
            self.video_path_var.set("")
            self.videos_label.config(text="No supported videos selected")
            self._log("No supported videos found in dropped items.")
        self._reset_progress_bars()
        self._ensure_playlist_default()

    def _log(self, message):
        self._log_lines += 1
        prefix = f"[{self._log_lines:03d}] "
        self.log_text.configure(state="normal")
        self.log_text.insert("end", prefix + str(message) + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _validate_video_content_list(self) -> bool:
        """Validate per-file content; log and drop invalid files instead of aborting."""
        if not self.video_paths:
            return False
        invalid_paths: list[str] = []
        for path in list(self.video_paths):
            try:
                cli_main.validate_video_format(path)
                content_validation.validate_video_content(path)
            except Exception as exc:
                invalid_paths.append(path)
                self._log_error(f"{Path(path).name}: {exc}")
        if invalid_paths:
            self._log(f"Skipping {len(invalid_paths)} invalid file(s).")
            self.video_paths = [p for p in self.video_paths if p not in invalid_paths]
            if self.video_paths:
                self.video_path_var.set("; ".join(self.video_paths))
                self.videos_label.config(text=f"{len(self.video_paths)} supported file(s) selected")
                self._reset_progress_bars()
            else:
                self.video_path_var.set("")
                self.videos_label.config(text="No supported videos selected")
        return bool(self.video_paths)

    def _clear_errors(self):
        self._error_lines = 0
        self.errors_text.configure(state="normal")
        self.errors_text.delete("1.0", "end")
        self.errors_text.configure(state="disabled")

    def _log_error(self, message):
        self._error_lines += 1
        prefix = f"[{self._error_lines:03d}] "
        self.errors_text.configure(state="normal")
        self.errors_text.insert("end", prefix + str(message) + "\n")
        self.errors_text.see("end")
        self.errors_text.configure(state="disabled")

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

    def _notify_completion(self):
        try:
            self.root.bell()
        except Exception:
            pass

    def _apply_topmost(self):
        try:
            self.root.wm_attributes("-topmost", bool(self.always_on_top_var.get()))
        except Exception:
            pass
        if not self._loading_settings:
            self._save_settings()

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
        if not self.video_paths:
            errors.append("Select at least one supported video file.")
            return errors
        missing = [p for p in self.video_paths if not Path(p).exists()]
        if missing:
            self._log("Skipping missing files: " + ", ".join(missing[:5]))
            self.video_paths = [p for p in self.video_paths if p not in missing]
            if self.video_paths:
                self.video_path_var.set("; ".join(self.video_paths))
                self.videos_label.config(text=f"{len(self.video_paths)} supported file(s) selected")
                self._reset_progress_bars()
            else:
                errors.append("All selected video files are missing. Please choose at least one existing file.")
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
        return errors

    def _cancel_upload(self):
        if not self._uploading:
            self.cancel_button.state(["disabled"])
        self._reset_progress_bars()
        self._apply_topmost()
        if self._uploading:
            self.cancel_event.set()
            self._log("Cancellation requested.")
            self.cancel_button.state(["disabled"])

    def _upload(self):
        if self._uploading:
            messagebox.showinfo("Upload in progress", "An upload is already running.")
            return
        self._clear_errors()
        errors = self._validate_fields()
        if errors:
            messagebox.showerror("Invalid fields", "\n\n".join(errors))
            return
        if not self._validate_video_content_list():
            messagebox.showerror("Invalid videos", "All selected files were invalid or unsupported.")
            return
        # Ensure playlist gets populated before constructing CLI args
        self._ensure_playlist_default()
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
                self.root.after(0, self._notify_completion)
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
