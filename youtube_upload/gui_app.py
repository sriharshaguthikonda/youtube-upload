#!/usr/bin/env python
"""Tkinter GUI for the youtube-upload engine.

Threading model: Tk is not thread safe. The upload runs on a worker thread and
never touches a widget; it posts closures to a queue that the Tk main loop
drains every few milliseconds (``_post`` / ``_drain_ui_queue``).

Errors are never swallowed silently: they go to the Errors pane, the log pane,
a dialog when the user must act, and (for crashes) a log file in the per-user
application-data folder, because ``pythonw.exe`` has no console to print to.
"""
from __future__ import annotations

import json
import queue
import re
import sys
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    DND_FILES = None
    TkinterDnD = None

# Allow `python youtube_upload/gui_app.py` as well as `python -m youtube_upload.gui_app`.
if __package__ in (None, ""):
    _PARENT = str(Path(__file__).resolve().parent.parent)
    if _PARENT not in sys.path:
        sys.path.insert(0, _PARENT)

from youtube_upload import auth, gui_app_files, gui_theme, lib, paths  # noqa: E402
import youtube_upload.main as cli_main  # noqa: E402
from youtube_upload import report as report_mod  # noqa: E402
from youtube_upload.errors import UploadCancelled, UploadFailed, YoutubeUploadError  # noqa: E402

UI_POLL_MS = 40
MAX_LOG_LINES = 2000
CRASH_LOG_NAME = "gui_crash.log"


def append_crash_log(text):
    """Best-effort append to the crash log; returns the path or ``None``."""
    try:
        target = paths.app_home() / CRASH_LOG_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(str(target), "a", encoding="utf-8") as handle:
            handle.write("[{0}] {1}\n".format(datetime.now().isoformat(timespec="seconds"), lib.redact(text)))
        return target
    except OSError:
        return None


class UploadGUI:
    MIN_WIDTH = 640
    MIN_HEIGHT = 480

    def __init__(self, root):
        self.root = root
        self._main_thread = threading.current_thread()  # must exist before anything can log
        self._ui_queue: queue.Queue = queue.Queue()
        self._closing = False
        self.root.title("YouTube Upload GUI")
        self.root.minsize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.root.report_callback_exception = self._on_tk_exception
        self.theme_var = tk.StringVar(value="dark")
        self.palette = gui_theme.apply_theme(self.root, mode=self.theme_var.get())

        self.video_paths: list[str] = []
        self.thumbnail_path: str | None = None
        self.progress_bars: dict[str, ttk.Progressbar] = {}
        self.status_labels: dict[str, ttk.Label] = {}
        self._uploading = False
        self._scanning = False         # a folder is being searched for videos (off the UI thread)
        self._scan_token = 0           # bumped to cancel/replace a running scan
        self.cancel_event = threading.Event()
        self._log_lines = 0
        self._error_lines = 0
        self._loading_settings = False
        self._last_loaded_accounts_dir: str | None = None
        self._last_save_error: str | None = None
        self._status_job = None
        self._auto_playlist = ""      # the last value this window filled in itself
        self._status_notes_seen: set[str] = set()
        self.accounts_dir_history: list[str] = []

        self._build_form()
        self._load_settings()
        self._setup_drag_and_drop()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_job = self.root.after(UI_POLL_MS, self._drain_ui_queue)
        self._refresh_account_status()

    def shutdown(self):
        """Stop the timers this window owns (a pending ``after`` outlives ``destroy()``)."""
        self._closing = True
        for job in (getattr(self, "_poll_job", None), getattr(self, "_status_job", None)):
            if job:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass

    # ------------------------------------------------------------------ threading

    def _post(self, func, *args, **kwargs):
        """Run ``func`` on the Tk thread (immediately if already on it)."""
        if threading.current_thread() is self._main_thread:
            func(*args, **kwargs)
        else:
            self._ui_queue.put((func, args, kwargs))

    # kept for older callers
    _run_on_ui_thread = _post

    def _drain_ui_queue(self):
        try:
            while True:
                func, args, kwargs = self._ui_queue.get_nowait()
                try:
                    func(*args, **kwargs)
                except tk.TclError as exc:
                    # widget destroyed while a late message arrived: nothing to show it on
                    append_crash_log("late UI update ignored: {0}".format(exc))
                except Exception as exc:  # a bug in a UI callback must be visible
                    self._report_internal_error("UI update failed", exc)
        except queue.Empty:
            pass
        if not self._closing:
            try:
                self._poll_job = self.root.after(UI_POLL_MS, self._drain_ui_queue)
            except tk.TclError:
                pass

    def _on_tk_exception(self, exc_type, exc, tb):
        """Tk prints callback errors to stderr, which does not exist under pythonw."""
        self._report_internal_error("Unexpected error in the window", exc, tb)

    def _report_internal_error(self, what, exc, tb=None):
        detail = "".join(traceback.format_exception(type(exc), exc, tb or exc.__traceback__))
        where = append_crash_log("{0}: {1}\n{2}".format(what, exc, detail))
        try:
            self._log_error("{0}: {1}{2}".format(
                what, exc, " (details in {0})".format(where) if where else ""))
        except (tk.TclError, AttributeError):
            pass
        if sys.stderr is not None:
            sys.stderr.write(detail)

    # ------------------------------------------------------------------- dialogs
    # Thin wrappers so tests can replace them without patching tkinter.

    def _info(self, title, message):
        messagebox.showinfo(title, message, parent=self.root)

    def _error(self, title, message):
        messagebox.showerror(title, message, parent=self.root)

    def _confirm(self, title, message):
        return messagebox.askyesno(title, message, parent=self.root)

    # --------------------------------------------------------------------- layout

    def _build_form(self):
        # Scrollable container to keep logs/errors reachable when many uploads are listed
        container = ttk.Frame(self.root)
        container.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        canvas = tk.Canvas(container, highlightthickness=0, background=self.palette["bg"])
        self._canvas = canvas
        vscroll = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        frame = ttk.Frame(canvas, padding=12)
        self._content_frame = frame
        self._content_window = canvas.create_window((0, 0), window=frame, anchor="nw")

        def _update_scroll_region(event=None):
            canvas.update_idletasks()
            canvas.configure(scrollregion=canvas.bbox("all"))
            content_height = frame.winfo_reqheight()
            target_height = max(content_height, canvas.winfo_height())
            canvas.itemconfigure(
                self._content_window,
                width=canvas.winfo_width(),
                height=target_height,
            )

        # The content height is pinned to max(requested, window) below, so adding rows later
        # does not trigger <Configure>; callers re-run this explicitly (see _reset_progress_bars).
        self._update_scroll_region = _update_scroll_region
        frame.bind("<Configure>", _update_scroll_region)
        canvas.bind("<Configure>", _update_scroll_region)

        def _on_mousewheel(event):
            delta = -1 * int(event.delta / 120) if event.delta else 0
            canvas.yview_scroll(delta, "units")

        # Enable mouse wheel scrolling (Windows / most platforms)
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        row = 0

        def label(text, r, sticky="w"):
            ttk.Label(frame, text=text).grid(row=r, column=0, sticky=sticky)

        # Title (optional; defaults to filename)
        label("Title (optional)", row)
        self.title_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.title_var, width=50).grid(row=row, column=1, sticky="ew")
        row += 1

        label("Description", row, "nw")
        self.description_text = tk.Text(frame, width=50, height=4)
        self.description_text.grid(row=row, column=1, sticky="ew")
        gui_theme.style_text_widget(self.description_text, self.palette)
        row += 1

        label("Tags (comma separated)", row)
        self.tags_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.tags_var).grid(row=row, column=1, sticky="ew")
        row += 1

        label("Category", row)
        self.category_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.category_var).grid(row=row, column=1, sticky="ew")
        row += 1

        label("Playlist", row)
        self.playlist_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.playlist_var).grid(row=row, column=1, sticky="ew")
        row += 1

        label("Privacy", row)
        self.privacy_var = tk.StringVar(value="public")
        ttk.Combobox(frame, textvariable=self.privacy_var, values=["public", "unlisted", "private"],
                     state="readonly").grid(row=row, column=1, sticky="w")
        row += 1

        label("Publish at (ISO 8601)", row)
        self.publish_at_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.publish_at_var).grid(row=row, column=1, sticky="ew")
        row += 1

        label("Skip if exists", row)
        self.skip_if_exists_var = tk.StringVar(value="hash")
        ttk.Combobox(
            frame,
            textvariable=self.skip_if_exists_var,
            values=["none", "title", "hash"],
            state="readonly",
        ).grid(row=row, column=1, sticky="w")
        row += 1

        # Thumbnail
        thumb_frame = ttk.Frame(frame)
        thumb_frame.grid(row=row, column=1, sticky="w")
        ttk.Button(thumb_frame, text="Choose Thumbnail", command=self._choose_thumbnail).grid(row=0, column=0, sticky="w")
        self.thumb_label = ttk.Label(thumb_frame, text="No file selected")
        self.thumb_label.grid(row=0, column=1, sticky="w", padx=6)
        label("Thumbnail", row)
        row += 1

        # Client secrets (optional; auto-resolves from accounts dir if set)
        label("Client secrets (optional)", row)
        secrets_frame = ttk.Frame(frame)
        secrets_frame.grid(row=row, column=1, sticky="ew")
        self.secrets_var = tk.StringVar()
        ttk.Entry(secrets_frame, textvariable=self.secrets_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(secrets_frame, text="Browse", command=self._choose_client_secrets).grid(row=0, column=1, padx=6)
        secrets_frame.columnconfigure(0, weight=1)
        row += 1

        # Credentials (optional)
        label("Credentials file (optional)", row)
        creds_frame = ttk.Frame(frame)
        creds_frame.grid(row=row, column=1, sticky="ew")
        self.credentials_var = tk.StringVar()
        ttk.Entry(creds_frame, textvariable=self.credentials_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(creds_frame, text="Browse", command=self._choose_credentials).grid(row=0, column=1, padx=6)
        creds_frame.columnconfigure(0, weight=1)
        row += 1

        # Account selection, with sign in / sign out
        label("Account label", row)
        account_frame = ttk.Frame(frame)
        account_frame.grid(row=row, column=1, sticky="ew")
        self.account_var = tk.StringVar()
        self.account_entry = ttk.Entry(account_frame, textvariable=self.account_var)
        self.account_entry.grid(row=0, column=0, sticky="ew")
        self.sign_in_button = ttk.Button(account_frame, text="Sign in", command=self._sign_in)
        self.sign_in_button.grid(row=0, column=1, padx=(6, 0))
        self.sign_out_button = ttk.Button(account_frame, text="Sign out", command=self._sign_out)
        self.sign_out_button.grid(row=0, column=2, padx=(6, 0))
        account_frame.columnconfigure(0, weight=1)
        row += 1

        label("Sign-in status", row)
        self.account_status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.account_status_var).grid(row=row, column=1, sticky="w")
        row += 1

        label("Accounts (comma separated)", row)
        self.accounts_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.accounts_var).grid(row=row, column=1, sticky="ew")
        row += 1

        # Accounts dir
        label("Accounts dir (optional)", row)
        accounts_dir_frame = ttk.Frame(frame)
        accounts_dir_frame.grid(row=row, column=1, sticky="ew")
        self.accounts_dir_var = tk.StringVar()
        self.accounts_dir_combo = ttk.Combobox(
            accounts_dir_frame,
            textvariable=self.accounts_dir_var,
            values=self.accounts_dir_history,
        )
        self.accounts_dir_combo.grid(row=0, column=0, sticky="ew")
        ttk.Button(accounts_dir_frame, text="Browse", command=self._choose_accounts_dir).grid(row=0, column=1, padx=6)
        accounts_dir_frame.columnconfigure(0, weight=1)
        row += 1

        # Checkboxes
        self.open_browser_var = tk.BooleanVar(value=True)
        self.open_link_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Open the browser automatically to sign in", variable=self.open_browser_var).grid(row=row, column=1, sticky="w")
        row += 1
        ttk.Checkbutton(frame, text="Open uploaded video link after upload", variable=self.open_link_var).grid(row=row, column=1, sticky="w")
        row += 1
        self.debug_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Enable debug logging", variable=self.debug_var).grid(row=row, column=1, sticky="w")
        row += 1
        self.always_on_top_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Keep window on top", variable=self.always_on_top_var, command=self._apply_topmost).grid(row=row, column=1, sticky="w")
        row += 1

        # Theme toggle
        label("Theme", row)
        ttk.Combobox(
            frame,
            textvariable=self.theme_var,
            values=["dark", "light"],
            state="readonly",
        ).grid(row=row, column=1, sticky="w")
        row += 1

        # Video selector
        label("Choose Video(s)*", row)
        videos_frame = ttk.Frame(frame)
        videos_frame.grid(row=row, column=1, sticky="ew")
        self.video_path_var = tk.StringVar()
        ttk.Entry(videos_frame, textvariable=self.video_path_var, width=40).grid(row=0, column=0, sticky="ew")
        ttk.Button(videos_frame, text="Browse Files", command=self._choose_videos).grid(row=0, column=1, padx=6)
        ttk.Button(videos_frame, text="Browse Folder", command=self._choose_video_folder).grid(row=0, column=2, padx=6)
        self.videos_label = ttk.Label(videos_frame, text="No videos selected")
        self.videos_label.grid(row=0, column=3, padx=6, sticky="w")
        videos_frame.columnconfigure(0, weight=1)
        # React to manual typing/pasting of video paths
        self.video_path_var.trace_add("write", self._on_video_path_change)
        row += 1

        # Upload/cancel buttons
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=1, sticky="e", pady=8)
        self.upload_button = ttk.Button(buttons, text="Upload", command=self._upload)
        self.upload_button.grid(row=0, column=0, padx=(0, 6))
        ttk.Button(buttons, text="Save settings", command=self._manual_save_settings).grid(row=0, column=1, padx=(0, 6))
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self._cancel_upload, state="disabled")
        self.cancel_button.grid(row=0, column=2)
        row += 1

        # Progress area
        ttk.Separator(frame, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        row += 1
        ttk.Label(frame, text="Upload Progress").grid(row=row, column=0, sticky="nw")
        self.progress_container = ttk.Frame(frame)
        self.progress_container.grid(row=row, column=1, sticky="nsew")
        self.progress_container.columnconfigure(1, weight=1)
        weighted_rows = [row]
        row += 1

        # Log pane
        ttk.Separator(frame, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        row += 1
        ttk.Label(frame, text="Status / Log").grid(row=row, column=0, sticky="nw")
        log_frame = ttk.Frame(frame)
        log_frame.grid(row=row, column=1, sticky="nsew")
        self.log_text = tk.Text(log_frame, width=60, height=8, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        gui_theme.style_text_widget(self.log_text, self.palette)
        weighted_rows.append(row)
        row += 1

        # Errors pane
        ttk.Separator(frame, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        row += 1
        ttk.Label(frame, text="Errors (skipped files)").grid(row=row, column=0, sticky="nw")
        errors_frame = ttk.Frame(frame)
        errors_frame.grid(row=row, column=1, sticky="nsew")
        self.errors_text = tk.Text(errors_frame, width=60, height=6, wrap="word", state="disabled")
        errors_scroll = ttk.Scrollbar(errors_frame, orient="vertical", command=self.errors_text.yview)
        self.errors_text.configure(yscrollcommand=errors_scroll.set)
        self.errors_text.grid(row=0, column=0, sticky="nsew")
        errors_scroll.grid(row=0, column=1, sticky="ns")
        errors_frame.columnconfigure(0, weight=1)
        errors_frame.rowconfigure(0, weight=1)
        self._style_text_panes()
        weighted_rows.append(row)
        row += 1

        for i in range(0, row):
            frame.rowconfigure(i, pad=4)
        frame.columnconfigure(1, weight=1)
        for weighted in weighted_rows:
            frame.rowconfigure(weighted, weight=1)

        # Loading an accounts folder resets the whole form, so it must happen on a deliberate act
        # (Enter, leaving the field, picking from the list, Browse) and not on every keystroke:
        # typing "C:\\Users\\me\\channel" used to wipe the form and write settings into every
        # folder the path passed through.
        for sequence in ("<Return>", "<FocusOut>", "<<ComboboxSelected>>"):
            self.accounts_dir_combo.bind(sequence, self._commit_accounts_dir)
        # React to theme changes
        self.theme_var.trace_add("write", self._on_theme_change)
        # Keep the sign-in status line current
        for var in (self.account_var, self.accounts_var, self.secrets_var, self.credentials_var):
            var.trace_add("write", self._schedule_account_status)

    def _style_text_panes(self):
        for widget in (self.description_text, self.log_text, self.errors_text):
            gui_theme.style_text_widget(widget, self.palette)
        self.errors_text.configure(foreground=self.palette["danger"])

    # ------------------------------------------------------------------- settings

    def _settings_path_for_accounts_dir(self, accounts_dir):
        if accounts_dir:
            return Path(accounts_dir) / "gui_settings.json"
        return (paths.home_dir() or paths.app_home()) / ".youtube_upload_gui_settings.json"

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
        self.accounts_dir_var.set("")
        self.open_browser_var.set(True)
        self.open_link_var.set(False)
        self.debug_var.set(False)
        self.always_on_top_var.set(False)
        self.description_text.delete("1.0", "end")
        self.thumbnail_path = None
        self.thumb_label.config(text="No file selected")
        self.video_paths = []
        self.video_path_var.set("")
        self.videos_label.config(text="No videos selected")

    def _apply_account_folder_defaults(self, accounts_dir):
        if not accounts_dir:
            return
        base = Path(accounts_dir)
        try:
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
        except OSError as exc:
            self._log_error("Could not look through the accounts folder {0}: {1}".format(base, exc))
        if not self.account_var.get():
            self.account_var.set(base.name)

    def _read_settings_file(self, settings_path):
        """Return the saved settings dict, or ``None`` (missing or unusable, and say why)."""
        try:
            text = settings_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._log_error("Cannot read the settings file {0}: {1}".format(settings_path, exc))
            return None
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data
        except ValueError as exc:
            aside = settings_path.with_name("{0}.corrupt-{1}".format(settings_path.name, int(time.time())))
            try:
                settings_path.replace(aside)
                moved = "kept as {0}".format(aside.name)
            except OSError:
                moved = "could not be moved aside"
            self._log_error("The settings file {0} was damaged ({1}) and was not used ({2}). "
                            "Defaults are in effect.".format(settings_path, exc, moved))
            return None

    def _load_settings(self, accounts_dir=None, clear_current=False, apply_account_defaults=False):
        # Prevent recursive trace-triggered loads
        if self._loading_settings:
            return
        target_dir = accounts_dir or self.accounts_dir_var.get().strip() or None
        settings_path = self._settings_path_for_accounts_dir(target_dir)
        data = self._read_settings_file(settings_path)

        if data is not None and accounts_dir is None:
            pointed = data.get("accounts_dir")
            if pointed:
                alt_path = self._settings_path_for_accounts_dir(pointed)
                if alt_path != settings_path and alt_path.exists():
                    return self._load_settings(
                        accounts_dir=pointed, clear_current=clear_current,
                        apply_account_defaults=apply_account_defaults)

        self._loading_settings = True
        try:
            if clear_current:
                self._reset_settings_fields()
                # Resetting blanked the field: put the chosen folder back so settings
                # are saved next to it, not in the home-folder pointer file.
                self.accounts_dir_var.set(target_dir or "")
            if data is None:
                if apply_account_defaults:
                    self._apply_account_folder_defaults(target_dir)
                self._last_loaded_accounts_dir = target_dir
                self._set_accounts_dir_history([])
                if target_dir:
                    self._add_to_accounts_dir_history(target_dir)
            else:
                self._apply_settings_data(data, target_dir, apply_account_defaults)
        finally:
            self._loading_settings = False
        if apply_account_defaults:
            self._save_settings()
        # Apply after loading; run once immediately and once after idle so Tk has realized.
        self._apply_topmost()
        self.root.after_idle(self._apply_topmost)
        self._refresh_account_status()

    def _apply_settings_data(self, data, target_dir, apply_account_defaults):
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
        self.accounts_dir_var.set(data.get("accounts_dir", target_dir or ""))
        self._set_accounts_dir_history(data.get("accounts_dir_history", []))
        # "auth_browser" (older versions) meant something else; the new key defaults to on.
        self.open_browser_var.set(bool(data.get("open_browser", True)))
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
            self.video_paths = [v for v in videos if isinstance(v, str)]
            self.video_path_var.set(gui_app_files.join_video_paths(self.video_paths))
            self.videos_label.config(text="{0} file(s) selected".format(len(self.video_paths)))
        first_parent = Path(self.video_paths[0]).parent.name if self.video_paths else ""
        # A saved playlist equal to the folder name was a suggestion, not something typed.
        self._auto_playlist = first_parent if first_parent and self.playlist_var.get() == first_parent else ""
        self._ensure_playlist_default()

        geometry = data.get("geometry")
        if geometry:
            try:
                self.root.geometry(self._normalize_geometry(geometry))
            except tk.TclError as exc:
                self._log_error("Could not restore the window position ({0}).".format(exc))
        if apply_account_defaults:
            self._apply_account_folder_defaults(target_dir)
        self._apply_theme_from_var()
        self._last_loaded_accounts_dir = target_dir
        if target_dir:
            self._add_to_accounts_dir_history(target_dir)

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
            "accounts_dir_history": self.accounts_dir_history,
            "open_browser": bool(self.open_browser_var.get()),
            "open_link": bool(self.open_link_var.get()),
            "debug": bool(self.debug_var.get()),
            "always_on_top": bool(self.always_on_top_var.get()),
            "description": self.description_text.get("1.0", "end").strip(),
            "thumbnail_path": self.thumbnail_path,
            "video_paths": self.video_paths if self.video_paths else [],
            "geometry": self._normalize_geometry(self.root.winfo_geometry()),
        }
        try:
            # Atomic: a crash or full disk can no longer leave half a JSON file behind.
            paths.write_private_json(self._settings_path, data)
            # Also persist a global pointer to the last used accounts dir so startup can reload it
            accounts_dir = self.accounts_dir_var.get().strip()
            if accounts_dir:
                paths.write_private_json(self._settings_path_for_accounts_dir(None), {
                    "accounts_dir": accounts_dir,
                    "accounts_dir_history": self.accounts_dir_history,
                })
            self._last_save_error = None
            return True
        except OSError as exc:
            message = "Could not save settings to {0}: {1}".format(self._settings_path, exc)
            if message != self._last_save_error:      # say it once, not on every keystroke
                self._last_save_error = message
                self._log_error(message)
            return False

    def _normalize_geometry(self, geometry: str) -> str:
        """
        Clamp geometry to a sensible minimum size and keep it within visible bounds.
        This avoids restoring windows off-screen when monitor layouts change.
        """
        match = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geometry)
        if not match:
            return geometry

        width, height, x_pos, y_pos = map(int, match.groups())
        width = max(width, self.MIN_WIDTH)
        height = max(height, self.MIN_HEIGHT)

        screen_w = max(self.root.winfo_screenwidth(), width)
        screen_h = max(self.root.winfo_screenheight(), height)

        x_pos = max(0, min(x_pos, screen_w - width))
        y_pos = max(0, min(y_pos, screen_h - height))
        return "{0}x{1}+{2}+{3}".format(width, height, x_pos, y_pos)

    def _manual_save_settings(self):
        success = self._save_settings()
        if success:
            self._log("Settings saved.")
            self._info("Settings saved", "Settings have been saved.")
        else:
            self._error("Save failed", "Could not save settings. Check file permissions or path.\n\n"
                        "{0}".format(self._last_save_error or ""))

    def _on_close(self):
        if self._uploading:
            if not self._confirm("Quit?", "An operation is still running. Stop it and quit?\n\n"
                                 "An interrupted upload can be resumed the next time."):
                return
            self.cancel_event.set()
        self._save_settings()
        self.shutdown()
        self.root.destroy()

    # --------------------------------------------------------------- video choosing

    def _set_video_selection(self, paths_list, log_unsupported=True, update_entry=True):
        supported, unsupported = gui_app_files.filter_supported_video_paths(paths_list)
        if unsupported and log_unsupported:
            self._log("Ignoring unsupported files: {0}".format(", ".join(Path(p).name for p in unsupported)))
        self.video_paths = list(supported)
        if update_entry:
            self._loading_settings = True       # the entry's trace must not re-parse what we just wrote
            try:
                self.video_path_var.set(gui_app_files.join_video_paths(self.video_paths))
            finally:
                self._loading_settings = False
        if supported:
            self.videos_label.config(text="{0} supported file(s) selected".format(len(supported)))
        else:
            self.videos_label.config(text="No supported videos selected")
        self._reset_progress_bars()
        self._ensure_playlist_default()
        self._save_settings()
        return supported, unsupported

    def _on_video_path_change(self, *_):
        if self._loading_settings:
            return
        parts = gui_app_files.parse_video_paths(self.video_path_var.get())
        self._set_video_selection(parts, update_entry=False)

    def _apply_theme_from_var(self, *_):
        mode = self.theme_var.get() or "dark"
        self.palette = gui_theme.apply_theme(self.root, mode=mode)
        if hasattr(self, "_canvas"):
            self._canvas.configure(background=self.palette["bg"])
        self._style_text_panes()

    def _on_theme_change(self, *_):
        if self._loading_settings:
            return
        self._apply_theme_from_var()

    def _cancel_scan(self):
        """Forget a folder scan in progress: its result will be discarded."""
        self._scan_token += 1
        if self._scanning:
            self._scanning = False
            if not self._uploading:
                self.upload_button.state(["!disabled"])

    def _clear_video_selection(self):
        self._cancel_scan()
        self.video_paths = []
        self.video_path_var.set("")
        self.videos_label.config(text="No videos selected")
        self._reset_progress_bars()

    def _ensure_playlist_default(self):
        """Suggest the folder name of the first video as the playlist.

        Only while the field is empty or still holds the value suggested earlier:
        a playlist the user typed is never overwritten (it used to be, on every upload).
        """
        current = self.playlist_var.get()
        if not self.video_paths:
            if current == self._auto_playlist:
                self.playlist_var.set("")
            self._auto_playlist = ""
            return
        parent_name = Path(self.video_paths[0]).parent.name or "Uploads"
        if current in ("", self._auto_playlist):
            self.playlist_var.set(parent_name)
            self._auto_playlist = parent_name

    def _choose_videos(self):
        selected = filedialog.askopenfilenames(title="Select video files")
        if selected:
            self._set_video_selection(list(selected))

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
            # Always reset video selection when switching accounts
            self._clear_video_selection()

    def _scan_in_background(self, folders, loose_files=()):
        """Find the videos under ``folders`` without freezing the window.

        The search runs on a worker thread that touches no widget; its result comes back through
        ``_post`` and is dropped if the user chose something else in the meantime.
        """
        self._cancel_scan()
        token = self._scan_token
        self._scanning = True
        self.upload_button.state(["disabled"])
        self.videos_label.config(text="Scanning folder...")
        threading.Thread(target=self._scan_worker, args=(token, list(folders), list(loose_files)),
                         daemon=True).start()

    def _scan_worker(self, token, folders, loose_files):
        def stale():
            return token != self._scan_token or self._closing
        found, ignored, errors = list(loose_files), 0, []
        try:
            for folder in folders:
                result = gui_app_files.scan_folder(folder, cancelled=stale)
                found.extend(result.videos)
                ignored += result.ignored
                errors.extend(result.errors)
                if stale():
                    return
        except Exception as exc:        # a worker must never die silently
            errors.append("{0}: {1}".format(type(exc).__name__, exc))
        self._post(self._finish_scan, token, found, ignored, errors)

    def _finish_scan(self, token, found, ignored, errors):
        if token != self._scan_token:
            return                       # superseded
        self._scanning = False
        if not self._uploading:
            self.upload_button.state(["!disabled"])
        for message in errors:
            self._log_error("Cannot read part of the folder: {0}".format(message))
        if ignored:
            self._log("{0} other file(s) ignored (not a supported video type).".format(ignored))
        supported, _ = gui_app_files.filter_supported_video_paths(found)
        if not supported:
            self._log("No supported videos found in the selected folder(s).")
            self._set_video_selection(self.video_paths, log_unsupported=False)      # restore the label
            return
        self._set_video_selection(found)

    def _choose_video_folder(self):
        path = filedialog.askdirectory(title="Select folder containing videos")
        if path:
            self._scan_in_background([path])

    def _commit_accounts_dir(self, *_):
        path = (self.accounts_dir_var.get() or "").strip()
        if not path:
            return
        # Avoid reloading the same folder repeatedly or re-entrancy
        if self._loading_settings or path == self._last_loaded_accounts_dir:
            return
        if not Path(path).is_dir():
            return
        self._load_settings(accounts_dir=path, clear_current=True, apply_account_defaults=True)
        self._add_to_accounts_dir_history(path)

    def _set_accounts_dir_history(self, paths_list):
        self.accounts_dir_history = [p for p in paths_list if p]
        self.accounts_dir_combo["values"] = self.accounts_dir_history

    def _add_to_accounts_dir_history(self, path, max_items=5):
        if not path:
            return
        normalized = str(Path(path))
        deduped = [p for p in self.accounts_dir_history if Path(p) != Path(normalized)]
        deduped.insert(0, normalized)
        self.accounts_dir_history = deduped[:max_items]
        self.accounts_dir_combo["values"] = self.accounts_dir_history

    def _setup_drag_and_drop(self):
        if not (TkinterDnD and DND_FILES):
            self._log("Drag-and-drop is off (optional package 'tkinterdnd2' is not installed).")
            return
        try:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop)
        except (tk.TclError, AttributeError, RuntimeError) as exc:   # best effort by design
            self._log("Drag-and-drop is unavailable: {0}".format(exc))

    def _on_drop(self, event):
        raw = event.data or ""
        # Use Tcl list splitter to handle braces/quoted paths from TkinterDnD.
        try:
            dropped = list(self.root.tk.splitlist(raw))
        except tk.TclError:
            dropped = re.findall(r"{[^}]+}|[^\s]+", raw.strip())
        folders: list[str] = []
        files: list[str] = []
        for item in dropped:
            path = Path(item.strip("{}"))
            if path.is_dir():
                folders.append(str(path))
            elif path.is_file():
                files.append(str(path))
        if not folders and not files:
            self._log("No files found in dropped items.")
            return
        if folders:
            self._scan_in_background(folders, files)
            return
        supported, _ = self._set_video_selection(files)
        if supported:
            self._log("Added {0} file(s) via drag-and-drop.".format(len(supported)))
        else:
            self._log("No supported videos found in dropped items.")

    # ------------------------------------------------------------------- log panes

    def _append_text(self, widget, text, counter_attr):
        count = getattr(self, counter_attr) + 1
        setattr(self, counter_attr, count)
        widget.configure(state="normal")
        widget.insert("end", "[{0:03d}] {1}\n".format(count, lib.redact(text)))
        lines = int(widget.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            widget.delete("1.0", "{0}.0".format(lines - MAX_LOG_LINES))
        widget.see("end")
        widget.configure(state="disabled")

    def _log(self, message):
        text = str(message)
        self._post(self._append_text, self.log_text, text, "_log_lines")

    def _log_error(self, message):
        text = str(message)
        self._post(self._append_text, self.errors_text, text, "_error_lines")

    def _clear_errors(self):
        self._error_lines = 0
        self.errors_text.configure(state="normal")
        self.errors_text.delete("1.0", "end")
        self.errors_text.configure(state="disabled")

    # --------------------------------------------------------------------- progress

    def _reset_progress_bars(self):
        # Never rebuild the rows while an operation is feeding them.
        if self._uploading:
            return
        for child in self.progress_container.winfo_children():
            child.destroy()
        self.progress_bars = {}
        self.status_labels = {}
        for row, path in enumerate(self.video_paths):
            ttk.Label(self.progress_container, text=Path(path).name).grid(row=row, column=0, sticky="w", padx=(0, 6))
            bar = ttk.Progressbar(self.progress_container, mode="determinate", length=240, maximum=100)
            bar.grid(row=row, column=1, sticky="ew", pady=2)
            status = ttk.Label(self.progress_container, text="")
            status.grid(row=row, column=2, sticky="w", padx=(6, 0))
            self.progress_bars[path] = bar
            self.status_labels[path] = status
        # Make room for the new rows (and show the scrollbar) instead of squeezing them.
        self.root.after_idle(self._update_scroll_region)

    def _set_progress(self, video_path, total_size, completed):
        bar = self.progress_bars.get(video_path)
        if bar is None or not bar.winfo_exists():
            return
        bar.configure(maximum=max(total_size, 1))
        bar["value"] = completed
        label = self.status_labels.get(video_path)
        if label is not None and label.winfo_exists():
            label.configure(text="{0:.0f}%".format(100.0 * completed / max(total_size, 1)))

    def _progress_factory(self, video_path):
        """Called on the worker thread: it must only post to the queue."""
        state = {"last_logged": -10}
        name = Path(video_path).name

        def _callback(total_size, completed):
            if self.cancel_event.is_set():
                raise UploadCancelled("Cancelled by user")
            percent = int(100.0 * completed / total_size) if total_size else 100
            self._post(self._set_progress, video_path, total_size, completed)
            if percent >= state["last_logged"] + 10 or percent >= 100:
                state["last_logged"] = percent
                self._log("{0}: {1}% ({2}/{3} bytes)".format(name, percent, completed, total_size))

        return cli_main.ProgressInfo(callback=_callback, finish=lambda: None)

    def _apply_result(self, result):
        """Show one file's outcome (runs on the Tk thread)."""
        label = self.status_labels.get(result.path)
        bar = self.progress_bars.get(result.path)
        if result.status in (report_mod.UPLOADED, report_mod.SKIPPED):
            text = "Done" if result.status == report_mod.UPLOADED else "Already on channel"
            if result.problems:
                text += " (with problems)"
            if bar is not None and bar.winfo_exists():
                bar["value"] = float(bar.cget("maximum"))
        elif result.status == report_mod.CANCELLED:
            text = "Cancelled"
        elif result.status == report_mod.NOT_ATTEMPTED:
            text = "Not attempted"
        else:
            text = "Failed"
        if label is not None and label.winfo_exists():
            label.configure(text=text)
        name = Path(result.path).name
        if result.status in (report_mod.FAILED, report_mod.NOT_ATTEMPTED):
            self._log_error("{0}: {1}".format(name, result.message or text))
        for problem in result.problems:
            self._log_error("{0}: {1}".format(name, problem))
        for warning in result.warnings:
            self._log("{0}: {1}".format(name, warning))
        if result.status == report_mod.UPLOADED and result.url:
            self._log("{0} uploaded: {1}".format(name, result.url))

    # ------------------------------------------------------------------ validation

    def _validate_fields(self):
        errors = []
        if not self.video_paths:
            errors.append("Select at least one supported video file.")
            return errors
        missing = [p for p in self.video_paths if not Path(p).exists()]
        if missing:
            self._log("Skipping missing files: " + ", ".join(missing[:5]))
            self._set_video_selection([p for p in self.video_paths if p not in missing],
                                      log_unsupported=False)
            if not self.video_paths:
                errors.append("All selected video files are missing. Please choose at least one existing file.")
        publish_at = self.publish_at_var.get().strip()
        if publish_at:
            try:
                cli_main.normalize_datetime(publish_at, "Publish at", warn=lambda m: None)
            except YoutubeUploadError as exc:
                errors.append(str(exc))
        thumb = self.thumbnail_path
        if thumb:
            try:
                cli_main.validate_thumbnail(thumb)
            except YoutubeUploadError as exc:
                errors.append(str(exc))
        for optional_path, name in [
            (self.secrets_var.get().strip(), "Client secrets"),
            (self.credentials_var.get().strip(), "Credentials file"),
            (self.accounts_dir_var.get().strip(), "Accounts directory"),
        ]:
            if optional_path and not Path(optional_path).exists():
                errors.append("{0} does not exist: {1}".format(name, optional_path))
        return errors

    # -------------------------------------------------------------------- accounts

    def _account_args(self):
        args = []
        if self.secrets_var.get().strip():
            args += ["--client-secrets", self.secrets_var.get().strip()]
        several = [a for a in self.accounts_var.get().split(",") if a.strip()]
        if self.credentials_var.get().strip():
            if len(several) > 1:
                # one credentials file for every listed account would send all uploads to one channel
                self._note_once("The credentials file is ignored because several accounts are listed; "
                                "each account uses <accounts folder>/<label>.json.")
            else:
                args += ["--credentials-file", self.credentials_var.get().strip()]
        if self.account_var.get().strip():
            args += ["--account", self.account_var.get().strip()]
        if self.accounts_var.get().strip():
            args += ["--accounts", self.accounts_var.get().strip()]
        if self.accounts_dir_var.get().strip():
            args += ["--accounts-dir", self.accounts_dir_var.get().strip()]
        if not self.open_browser_var.get():
            args.append("--no-browser")
        return args

    def _schedule_account_status(self, *_):
        if self._loading_settings or self._closing:
            return
        if getattr(self, "_status_job", None):
            try:
                self.root.after_cancel(self._status_job)
            except tk.TclError:
                pass
        self._status_job = self.root.after(400, self._refresh_account_status)

    def _refresh_account_status(self):
        """Show whether the chosen account is signed in (reads a file, no network)."""
        self._status_job = None
        try:
            options, _ = cli_main.build_parser().parse_args(
                [a for a in self._account_args() if a != "--no-browser"])
            labels = cli_main.account_labels(options)
            parts = []
            for label in labels:
                context = cli_main.resolve_account(options, label, self._note_once)
                state = auth.credentials_status(context.credentials)
                parts.append("{0}: {1}".format(context.label, {
                    "signed-in": "signed in", "signed-out": "not signed in",
                    "unreadable": "saved sign-in is unreadable - sign in again"}[state]))
            self.account_status_var.set("; ".join(parts))
        except YoutubeUploadError as exc:
            self.account_status_var.set(str(exc).split(". ")[0][:160])
        except OSError as exc:      # unmounted drive, a file in the path, permission denied ...
            self.account_status_var.set("Accounts folder not usable: {0}".format(exc.strerror or exc))
        except SystemExit:
            self.account_status_var.set("")

    def _note_once(self, message):
        """Log a notice from the (frequent) status refresh only the first time."""
        if message not in self._status_notes_seen:
            self._status_notes_seen.add(message)
            self._log(message)

    def _sign_in(self):
        if self._uploading:
            self._info("Busy", "Another operation is running.")
            return
        problems = self._validate_fields_for_account()
        if problems:
            self._error("Cannot sign in", "\n\n".join(problems))
            return
        self._start_worker("login", ["--login"] + self._account_args())

    def _validate_fields_for_account(self):
        errors = []
        for optional_path, name in [
            (self.secrets_var.get().strip(), "Client secrets"),
            (self.credentials_var.get().strip(), "Credentials file"),
            (self.accounts_dir_var.get().strip(), "Accounts directory"),
        ]:
            if optional_path and not Path(optional_path).exists():
                errors.append("{0} does not exist: {1}".format(name, optional_path))
        return errors

    def _sign_out(self):
        if self._uploading:
            self._info("Busy", "Another operation is running.")
            return
        problems = self._validate_fields_for_account()
        if problems:
            self._error("Cannot sign out", "\n\n".join(problems))
            return
        who = self.account_var.get().strip() or self.accounts_var.get().strip() or "the default account"
        if not self._confirm("Sign out", "Sign out {0}?\n\nThis revokes the app's access at Google and deletes "
                             "the saved sign-in on this computer.".format(who)):
            return
        self._start_worker("logout", ["--logout"] + self._account_args())

    # ------------------------------------------------------------------ the worker

    def _cancel_upload(self):
        if self._uploading:
            self.cancel_event.set()
            self._log("Cancellation requested.")
            self.cancel_button.state(["disabled"])

    def _apply_topmost(self):
        try:
            self.root.wm_attributes("-topmost", bool(self.always_on_top_var.get()))
        except tk.TclError as exc:
            self._log_error("Could not change 'keep on top': {0}".format(exc))
        if not self._loading_settings:
            self._save_settings()

    def _build_upload_args(self):
        title = self.title_var.get().strip()
        description = self.description_text.get("1.0", "end").strip()
        args = []
        # Only pass an explicit title if the user provided one; otherwise the engine
        # derives a distinct title from each file name.
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

        args += self._account_args()
        if self.open_link_var.get():
            args.append("--open-link")
        if self.debug_var.get():
            args.append("--debug")
        skip_if_exists = self.skip_if_exists_var.get().strip()
        if skip_if_exists and skip_if_exists != "none":
            args += ["--skip-if-exists", skip_if_exists]
        return args + self.video_paths

    def _upload(self):
        if self._uploading:
            self._info("Upload in progress", "An upload is already running.")
            return
        if self._scanning:
            self._info("Still searching", "Wait until the folder has been searched for videos.")
            return
        self._clear_errors()
        errors = self._validate_fields()
        if errors:
            self._error("Invalid fields", "\n\n".join(errors))
            return
        # Ensure playlist gets populated before constructing CLI args
        self._ensure_playlist_default()
        self._reset_progress_bars()
        self._log("Starting upload of {0} file(s).".format(len(self.video_paths)))
        self._start_worker("upload", self._build_upload_args())

    def _start_worker(self, kind, argv):
        self._uploading = True
        self.cancel_event.clear()
        for button in (self.upload_button, self.sign_in_button, self.sign_out_button):
            button.state(["disabled"])
        self.cancel_button.state(["!disabled"])
        threading.Thread(target=self._run_worker, args=(kind, argv), daemon=True).start()

    def _run_worker(self, kind, argv):
        """Worker thread. Touches no widget: everything goes through ``_post``."""
        try:
            result = cli_main.main(
                argv, progress_factory=self._progress_factory, cancel_check=self.cancel_event.is_set,
                notify=self._log, log=self._log, output=None,
                on_result=lambda r: self._post(self._apply_result, r),
                # A worker thread must never wait on input(): when the GUI is started from a
                # terminal the default prompt blocked it for good and Cancel could not help.
                confirm_create_playlist=lambda title: True)
            self._post(self._finished_ok, kind, result)
        except UploadFailed as exc:
            self._post(self._finished_failed, exc)
        except UploadCancelled:
            self._log("Cancelled by user.")
            self._post(self._info, "Cancelled", "The operation was cancelled. A partly uploaded "
                       "video can be continued by uploading it again.")
        except YoutubeUploadError as exc:
            self._log_error("{0}: {1}".format(type(exc).__name__, exc))
            self._post(self._error, "Cannot continue", str(exc))
        except SystemExit as exc:
            self._log_error("Invalid options (exit code {0}).".format(exc.code))
            self._post(self._error, "Invalid options", "The request was rejected (exit code {0}).".format(exc.code))
        except Exception as exc:  # a bug: surface it fully rather than leave a frozen window
            detail = traceback.format_exc()
            where = append_crash_log("worker crashed: {0}\n{1}".format(exc, detail))
            self._log_error("Unexpected error: {0}: {1}{2}".format(
                type(exc).__name__, exc, " (details in {0})".format(where) if where else ""))
            self._post(self._error, "Unexpected error", "{0}: {1}".format(type(exc).__name__, exc))
        finally:
            self._post(self._set_idle)

    def _finished_ok(self, kind, result):
        if kind == "login":
            self._log("Sign-in complete.")
            self._info("Signed in", "Sign-in completed and was saved.")
            return
        if kind == "logout":
            self._log("Signed out.")
            self._info("Signed out", "The saved sign-in was removed. See the log for details.")
            return
        uploaded = result.count(report_mod.UPLOADED) if result else 0
        skipped = result.count(report_mod.SKIPPED) if result else 0
        self._log("Upload completed: {0} uploaded, {1} already on the channel.".format(uploaded, skipped))
        self._notify_completion()
        self._info("Upload complete", "{0} video(s) uploaded, {1} already on the channel.".format(uploaded, skipped))

    def _finished_failed(self, exc):
        run_report = exc.report
        for line in run_report.summary_lines():
            self._log(line)
        for label, (message, _) in run_report.account_errors.items():
            self._log_error("Account {0}: {1}".format(label, message))
        failed = sum(1 for r in run_report.results if not r.succeeded)
        total = len(run_report.results)
        done = total - failed
        head = "{0} of {1} file(s) did not complete ({2} succeeded).".format(failed, total, done)
        first = [m for m, _ in run_report.account_errors.values()][:1]
        first += [r.message for r in run_report.results if r.message and not r.succeeded][:2]
        self._notify_completion()
        self._error("Upload finished with problems", head + "\n\n" + "\n".join(first) +
                    "\n\nSee the Errors pane for every file.")

    def _set_idle(self):
        self._uploading = False
        for button in (self.upload_button, self.sign_in_button, self.sign_out_button):
            button.state(["!disabled"])
        if self._scanning:
            self.upload_button.state(["disabled"])        # a folder search is still running
        self.cancel_button.state(["disabled"])
        self._refresh_account_status()

    def _notify_completion(self):
        try:
            self.root.bell()
        except tk.TclError:
            pass  # the bell is a courtesy; there may be no sound device


def _fatal(message):
    """Last resort for startup failures under pythonw: log, print, and show a box."""
    where = append_crash_log(message)
    if sys.stderr is not None:
        sys.stderr.write(message + "\n")
    try:
        probe = tk.Tk()
        probe.withdraw()
        messagebox.showerror("YouTube Upload GUI", message + ("\n\nDetails: {0}".format(where) if where else ""))
        probe.destroy()
    except tk.TclError:
        pass


def main():
    try:
        root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    except tk.TclError as exc:
        _fatal("The window could not be opened (is there a display?): {0}".format(exc))
        return 1
    try:
        UploadGUI(root)
        root.mainloop()
    except Exception as exc:  # startup failure: never exit silently
        _fatal("The GUI crashed: {0}: {1}\n{2}".format(type(exc).__name__, exc, traceback.format_exc()))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
