#!/usr/bin/env python
"""Simple Tkinter GUI wrapper around the existing youtube-upload CLI."""

import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# Allow running both as module (-m youtube_upload.gui_app) and as script
try:
    from . import main as cli_main
except ImportError:
    SCRIPT_DIR = Path(__file__).resolve().parent
    PARENT = SCRIPT_DIR.parent
    sys.path.insert(0, str(PARENT))
    import youtube_upload.main as cli_main


class UploadGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("YouTube Upload GUI")

        self.video_paths = []
        self.thumbnail_path = None

        self._build_form()

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

        # Thumbnail
        thumb_frame = ttk.Frame(frame)
        thumb_frame.grid(row=7, column=1, sticky="w")
        ttk.Button(thumb_frame, text="Choose Thumbnail", command=self._choose_thumbnail).grid(row=0, column=0, sticky="w")
        self.thumb_label = ttk.Label(thumb_frame, text="No file selected")
        self.thumb_label.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(frame, text="Thumbnail").grid(row=7, column=0, sticky="w")

        # Client secrets (optional)
        ttk.Label(frame, text="Client secrets (optional)").grid(row=8, column=0, sticky="w")
        secrets_frame = ttk.Frame(frame)
        secrets_frame.grid(row=8, column=1, sticky="ew")
        self.secrets_var = tk.StringVar()
        ttk.Entry(secrets_frame, textvariable=self.secrets_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(secrets_frame, text="Browse", command=self._choose_client_secrets).grid(row=0, column=1, padx=6)
        secrets_frame.columnconfigure(0, weight=1)

        # Credentials (optional)
        ttk.Label(frame, text="Credentials file (optional)").grid(row=9, column=0, sticky="w")
        creds_frame = ttk.Frame(frame)
        creds_frame.grid(row=9, column=1, sticky="ew")
        self.credentials_var = tk.StringVar()
        ttk.Entry(creds_frame, textvariable=self.credentials_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(creds_frame, text="Browse", command=self._choose_credentials).grid(row=0, column=1, padx=6)
        creds_frame.columnconfigure(0, weight=1)

        # Checkboxes
        self.auth_browser_var = tk.BooleanVar(value=True)
        self.open_link_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Authenticate with browser (GUI)", variable=self.auth_browser_var).grid(row=10, column=1, sticky="w")
        ttk.Checkbutton(frame, text="Open uploaded video link after upload", variable=self.open_link_var).grid(row=11, column=1, sticky="w")

        # Video selector
        videos_frame = ttk.Frame(frame)
        videos_frame.grid(row=12, column=1, sticky="w")
        ttk.Button(videos_frame, text="Choose Video(s)", command=self._choose_videos).grid(row=0, column=0, sticky="w")
        self.videos_label = ttk.Label(videos_frame, text="No videos selected")
        self.videos_label.grid(row=0, column=1, padx=6, sticky="w")
        ttk.Label(frame, text="Videos*").grid(row=12, column=0, sticky="w")

        # Upload button
        ttk.Button(frame, text="Upload", command=self._upload).grid(row=13, column=1, sticky="e", pady=8)

        for i in range(0, 14):
            frame.rowconfigure(i, pad=4)
        frame.columnconfigure(1, weight=1)

    def _choose_videos(self):
        paths = filedialog.askopenfilenames(title="Select video files")
        if paths:
            self.video_paths = list(paths)
            self.videos_label.config(text=f"{len(self.video_paths)} file(s) selected")

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

    def _upload(self):
        title = self.title_var.get().strip()
        if not self.video_paths:
            messagebox.showerror("Missing videos", "Please select at least one video file.")
            return
        # Default title from the first video filename if none provided
        if not title:
            title = cli_main.default_title_from_video_path(self.video_paths[0])
            self.title_var.set(title)

        description = self.description_text.get("1.0", "end").strip()
        args = ["--title", title]

        if description:
            args += ["--description", description]
        if self.tags_var.get().strip():
            args += ["--tags", self.tags_var.get().strip()]
        if self.category_var.get().strip():
            args += ["--category", self.category_var.get().strip()]
        if self.playlist_var.get().strip():
            args += ["--playlist", self.playlist_var.get().strip()]

        privacy = self.privacy_var.get().strip()
        if privacy and privacy != "public":
            args += ["--privacy", privacy]

        if self.publish_at_var.get().strip():
            args += ["--publish-at", self.publish_at_var.get().strip()]

        if self.thumbnail_path:
            args += ["--thumbnail", self.thumbnail_path]

        if self.secrets_var.get().strip():
            args += ["--client-secrets", self.secrets_var.get().strip()]
        if self.credentials_var.get().strip():
            args += ["--credentials-file", self.credentials_var.get().strip()]

        if self.auth_browser_var.get():
            args.append("--auth-browser")
        if self.open_link_var.get():
            args.append("--open-link")

        full_args = args + self.video_paths

        try:
            cli_main.main(full_args)
            messagebox.showinfo("Upload complete", "Upload finished without error.")
        except SystemExit as exc:  # in case cli_main.run() is used accidentally
            if exc.code not in (None, 0):
                messagebox.showerror("Upload error", f"Exited with code {exc.code}")
        except Exception as exc:  # broad to surface any CLI errors to the user
            messagebox.showerror("Upload failed", str(exc))


def main():
    root = tk.Tk()
    UploadGUI(root)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
