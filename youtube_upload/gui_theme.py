"""Centralized styling for the GUI."""

from tkinter import ttk

PALETTE = {
    "bg": "#0f1115",
    "surface": "#1a1d24",
    "sunken": "#14171d",
    "fg": "#e6e9ef",
    "muted": "#9aa3b5",
    "accent": "#4ba3ff",
    "accent_alt": "#7fd1ff",
    "border": "#2b303b",
    "focus": "#6cb8ff",
    "danger": "#ff6b6b",
}


def apply_dark_theme(root):
    """Apply a reusable dark theme to the given root window."""
    palette = PALETTE
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        # Fallback to whatever is available
        available = style.theme_names()
        if available:
            style.theme_use(available[0])

    root.configure(bg=palette["bg"])

    style.configure(
        "TFrame",
        background=palette["bg"],
        borderwidth=0,
    )
    style.configure(
        "TLabelframe",
        background=palette["bg"],
        foreground=palette["fg"],
        bordercolor=palette["border"],
    )
    style.configure(
        "TLabel",
        background=palette["bg"],
        foreground=palette["fg"],
    )
    style.configure(
        "TButton",
        background=palette["surface"],
        foreground=palette["fg"],
        borderwidth=1,
        relief="flat",
        focusthickness=1,
        focuscolor=palette["focus"],
    )
    style.map(
        "TButton",
        background=[("active", palette["sunken"])],
        foreground=[("disabled", palette["muted"])],
    )
    style.configure(
        "TEntry",
        fieldbackground=palette["surface"],
        background=palette["surface"],
        foreground=palette["fg"],
        bordercolor=palette["border"],
        lightcolor=palette["focus"],
        darkcolor=palette["border"],
    )
    style.configure(
        "TCombobox",
        fieldbackground=palette["surface"],
        background=palette["surface"],
        foreground=palette["fg"],
        bordercolor=palette["border"],
        arrowcolor=palette["fg"],
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", palette["surface"])],
        foreground=[("readonly", palette["fg"])],
    )
    style.configure(
        "TCheckbutton",
        background=palette["bg"],
        foreground=palette["fg"],
    )
    style.configure(
        "Horizontal.TProgressbar",
        background=palette["accent"],
        troughcolor=palette["surface"],
        bordercolor=palette["border"],
        lightcolor=palette["accent"],
        darkcolor=palette["accent"],
    )

    return palette


def style_text_widget(widget, palette=None):
    """Apply dark theme colors to Text widgets."""
    colors = palette or PALETTE
    widget.configure(
        background=colors["surface"],
        foreground=colors["fg"],
        insertbackground=colors["accent"],
        highlightbackground=colors["border"],
        highlightcolor=colors["focus"],
        relief="flat",
        borderwidth=1,
        highlightthickness=1,
    )
