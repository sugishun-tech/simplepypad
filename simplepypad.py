#!/usr/bin/env python3
"""
SimplePyPad: a tiny Tkinter editor with Pygments highlighting and Python customization.

Run:
    python simplepypad.py [file]
    python simplepypad.py --nox [file]

Customize:
    Edit the user config file from Tools -> Open User Config.
"""

from __future__ import annotations

import argparse
import bisect
import datetime as _dt
import inspect
import locale
import os
import platform
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

try:
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, messagebox, simpledialog

    TK_AVAILABLE = True
    TK_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover - --nox can run without Tkinter
    TK_AVAILABLE = False
    TK_IMPORT_ERROR = exc
    tk = None  # type: ignore[assignment]
    filedialog = None  # type: ignore[assignment]
    tkfont = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    simpledialog = None  # type: ignore[assignment]

try:
    from pygments import lex
    from pygments.lexers import TextLexer, get_lexer_by_name, get_lexer_for_filename
    from pygments.styles import get_style_by_name
    from pygments.util import ClassNotFound

    PYGMENTS_AVAILABLE = True
except Exception:  # pragma: no cover - lets the editor still start without Pygments
    PYGMENTS_AVAILABLE = False
    lex = None  # type: ignore[assignment]
    TextLexer = None  # type: ignore[assignment]
    get_lexer_by_name = None  # type: ignore[assignment]
    get_lexer_for_filename = None  # type: ignore[assignment]
    get_style_by_name = None  # type: ignore[assignment]
    ClassNotFound = Exception  # type: ignore[assignment]


APP_NAME = "SimplePyPad"
APP_SLUG = "simplepypad"


@dataclass
class Options:
    font_family: str = "Consolas" if platform.system() == "Windows" else "Courier New"
    font_size: int = 11
    wrap: bool = False
    tab_width: int = 4
    indent_with_spaces: bool = True
    auto_indent: bool = True
    highlight: bool = True
    pygments_style: str = "default"
    max_highlight_chars: int = 300_000
    find_case_sensitive: bool = False
    default_encoding: str = "utf-8"


@dataclass(frozen=True)
class HighlightSpan:
    """A syntax-highlighted byte-independent character span."""

    start: int
    end: int
    token_type: Any


@dataclass
class HighlightResult:
    lexer_name: str
    spans: List[HighlightSpan]
    force_lexer_alias: Optional[str]


def get_shared_lexer(path: Optional[Path], force_lexer_alias: Optional[str]) -> tuple[Any, Optional[str]]:
    """Return a Pygments lexer for both Tk and --nox modes.

    ``force_lexer_alias`` is normalized to ``None`` when the alias is invalid,
    matching the old Tk-only behavior instead of letting the two frontends drift.
    """
    if not PYGMENTS_AVAILABLE:
        return None, force_lexer_alias

    alias = force_lexer_alias or None
    if alias:
        try:
            return get_lexer_by_name(alias, stripnl=False, stripall=False), alias  # type: ignore[misc]
        except ClassNotFound:
            alias = None

    filename = str(path) if path else "untitled.txt"
    try:
        return get_lexer_for_filename(filename, stripnl=False, stripall=False), alias  # type: ignore[misc]
    except ClassNotFound:
        return TextLexer(stripnl=False, stripall=False), alias  # type: ignore[operator]


def lexer_name_for_status(path: Optional[Path], force_lexer_alias: Optional[str]) -> tuple[str, Optional[str]]:
    try:
        lexer, alias = get_shared_lexer(path, force_lexer_alias)
        return (getattr(lexer, "name", "Text") or "Text"), alias
    except Exception:
        return "Text", None


def compute_highlight_spans(
    content: str,
    *,
    path: Optional[Path],
    options: Options,
    force_lexer_alias: Optional[str],
) -> HighlightResult:
    """Tokenize text once and let each frontend render it in its own way."""
    if not options.highlight:
        return HighlightResult("Highlight Off", [], force_lexer_alias)
    if not PYGMENTS_AVAILABLE:
        return HighlightResult("Text", [], force_lexer_alias)

    lexer, normalized_alias = get_shared_lexer(path, force_lexer_alias)
    lexer_name = getattr(lexer, "name", "Text") or "Text"

    if not content:
        return HighlightResult(lexer_name, [], normalized_alias)
    if len(content) > options.max_highlight_chars:
        return HighlightResult(f"Highlight skipped > {options.max_highlight_chars:,} chars", [], normalized_alias)

    spans: List[HighlightSpan] = []
    offset = 0
    for token_type, value in lex(content, lexer):  # type: ignore[misc]
        if not value:
            continue
        start = offset
        offset += len(value)
        if value.isspace():
            continue
        spans.append(HighlightSpan(start, offset, token_type))
    return HighlightResult(lexer_name, spans, normalized_alias)


def pygments_style_for_token(style_name: str, token_type: Any) -> Dict[str, Any]:
    if not PYGMENTS_AVAILABLE:
        return {}
    try:
        style_cls = get_style_by_name(style_name)  # type: ignore[misc]
    except Exception:
        style_cls = get_style_by_name("default")  # type: ignore[misc]
    return dict(style_cls.style_for_token(token_type))


class EditorAPI:
    """Small Python customization API exposed to init.py as ``api`` and ``editor``."""

    def __init__(self, app: "SimplePyPad") -> None:
        self._app = app

    @property
    def text_widget(self) -> tk.Text:
        """The raw Tk Text widget. Use when the high-level helpers are not enough."""
        return self._app.text

    @property
    def root(self) -> tk.Tk:
        return self._app.root

    @property
    def path(self) -> Optional[Path]:
        return self._app.current_path

    def get_text(self) -> str:
        return self._app.text.get("1.0", "end-1c")

    def set_text(self, value: str, *, dirty: bool = True) -> None:
        self._app.set_text_content(value, dirty=dirty)

    def insert(self, value: str, index: str = "insert") -> None:
        self._app.text.insert(index, value)
        self._app.schedule_highlight()

    def replace_selection(self, value: str) -> bool:
        try:
            start = self._app.text.index("sel.first")
            end = self._app.text.index("sel.last")
        except tk.TclError:
            return False
        self._app.text.delete(start, end)
        self._app.text.insert(start, value)
        self._app.schedule_highlight()
        return True

    def get_selection(self) -> str:
        try:
            return self._app.text.get("sel.first", "sel.last")
        except tk.TclError:
            return ""

    def set_option(self, name: str, value: Any) -> None:
        self._app.set_option(name, value)

    def get_option(self, name: str) -> Any:
        if not hasattr(self._app.options, name):
            raise AttributeError(f"unknown option: {name}")
        return getattr(self._app.options, name)

    def set_font(self, family: Optional[str] = None, size: Optional[int] = None) -> None:
        if family is not None:
            self.set_option("font_family", family)
        if size is not None:
            self.set_option("font_size", int(size))

    def set_theme(self, pygments_style: str) -> None:
        self.set_option("pygments_style", pygments_style)

    def set_language(self, alias: Optional[str]) -> None:
        """Force syntax by Pygments alias, e.g. 'python'. Pass None for auto-detect."""
        self._app.force_lexer_alias = alias or None
        self._app.schedule_highlight(immediate=True)

    def bind_key(self, sequence: str, callback: Callable[..., Any], *, widget: str = "text") -> None:
        """Bind a Tk key sequence. Callback receives (api, event) unless it accepts fewer args."""
        target: tk.Misc = self._app.text if widget == "text" else self._app.root

        def wrapper(event: tk.Event, cb: Callable[..., Any] = callback) -> Any:
            try:
                return _call_flexibly(cb, self, event)
            except Exception:
                self._app.report_exception("Customization key binding failed")
                return "break"

        target.bind(sequence, wrapper)

    def add_command(self, name: str, callback: Callable[..., Any]) -> None:
        self._app.add_command(name, callback, custom=True)

    def run_command(self, name: str) -> Any:
        return self._app.run_command(name)

    def commands(self) -> List[str]:
        return sorted(self._app.commands)

    def add_menu_item(
        self,
        menu: str,
        label: str,
        command: Union[str, Callable[..., Any]],
        *,
        accelerator: Optional[str] = None,
    ) -> None:
        self._app.add_menu_item(menu, label, command, accelerator=accelerator)

    def on(self, event_name: str, callback: Callable[..., Any]) -> None:
        self._app.hooks.setdefault(event_name, []).append(callback)

    def open_file(self, path: Union[str, os.PathLike[str]]) -> None:
        self._app.open_path(Path(path))

    def save_file(self, path: Optional[Union[str, os.PathLike[str]]] = None) -> bool:
        if path is not None:
            return self._app.save_path(Path(path))
        return self._app.save_file()

    def show_message(self, title: str, message: str) -> None:
        messagebox.showinfo(title, message, parent=self._app.root)

    def ask_string(self, title: str, prompt: str, initial: str = "") -> Optional[str]:
        return simpledialog.askstring(title, prompt, initialvalue=initial, parent=self._app.root)

    def status(self, message: str) -> None:
        self._app.set_status_message(message)


class SimplePyPad:
    def __init__(self, root: tk.Tk, *, config_paths: Optional[List[Path]] = None, load_user_config: bool = True) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.options = Options()
        self.current_path: Optional[Path] = None
        self.current_encoding: Optional[str] = None
        self.dirty = False
        self.highlight_after_id: Optional[str] = None
        self.force_lexer_alias: Optional[str] = None
        self.current_lexer_name = "Text"
        self.token_tags: set[str] = set()
        self.configured_token_tags: set[str] = set()
        self.token_font_cache: Dict[tuple[bool, bool], tkfont.Font] = {}
        self.commands: Dict[str, Callable[[], Any]] = {}
        self.custom_commands: set[str] = set()
        self.hooks: Dict[str, List[Callable[..., Any]]] = {}
        self.last_find = ""
        self._loading_file = False
        self._status_message = ""
        self.gui_prefix: Optional[str] = None
        self.gui_mark_index: Optional[str] = None
        self.gui_kill_ring = ""

        self.api = EditorAPI(self)

        self._build_widgets()
        self._register_builtin_commands()
        self._build_menus()
        self._bind_keys()
        self.apply_options()
        self.update_title()
        self.update_status()

        self.config_paths: List[Path] = []
        if load_user_config:
            self.config_paths.append(default_user_config_path())
        if config_paths:
            self.config_paths.extend(config_paths)
        self.load_customizations(silent=True)

        self.root.protocol("WM_DELETE_WINDOW", self.exit_app)

    # ----- UI construction -------------------------------------------------

    def _build_widgets(self) -> None:
        self.text_font = tkfont.Font(family=self.options.font_family, size=self.options.font_size)

        self.main = tk.Frame(self.root)
        self.main.pack(fill="both", expand=True)
        self.main.rowconfigure(0, weight=1)
        self.main.columnconfigure(0, weight=1)

        self.text = tk.Text(
            self.main,
            undo=True,
            maxundo=200,
            autoseparators=True,
            font=self.text_font,
            wrap="none",
            padx=4,
            pady=3,
            borderwidth=1,
            relief="sunken",
        )
        self.yscroll = tk.Scrollbar(self.main, orient="vertical", command=self.text.yview)
        self.xscroll = tk.Scrollbar(self.main, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=self.yscroll.set, xscrollcommand=self.xscroll.set)

        self.text.grid(row=0, column=0, sticky="nsew")
        self.yscroll.grid(row=0, column=1, sticky="ns")
        self.xscroll.grid(row=1, column=0, sticky="ew")

        self.status_var = tk.StringVar()
        self.status = tk.Label(self.root, textvariable=self.status_var, anchor="w", relief="sunken", padx=5)
        self.status.pack(side="bottom", fill="x")

        self.menubar = tk.Menu(self.root)
        self.root.configure(menu=self.menubar)
        self.menus: Dict[str, tk.Menu] = {}

    def _build_menus(self) -> None:
        file_menu = self.get_or_create_menu("File")
        self._menu_add(file_menu, "New", "file.new")
        self._menu_add(file_menu, "Find File / Open...", "find-file", "C-x C-f")
        self._menu_add(file_menu, "Save Buffer", "save-buffer", "C-x C-s")
        self._menu_add(file_menu, "Write File / Save As...", "write-file", "C-x C-w")
        file_menu.add_separator()
        self._menu_add(file_menu, "Reload From Disk", "file.reload")
        file_menu.add_separator()
        self._menu_add(file_menu, "Quit", "kill-emacs", "C-x C-c")

        edit_menu = self.get_or_create_menu("Edit")
        self._menu_add(edit_menu, "Undo", "undo", "C-/")
        edit_menu.add_separator()
        self._menu_add(edit_menu, "Kill Region", "kill-region", "C-w")
        self._menu_add(edit_menu, "Copy Region", "copy-region-as-kill", "M-w")
        self._menu_add(edit_menu, "Yank", "yank", "C-y")
        self._menu_add(edit_menu, "Delete Char", "edit.delete", "C-d")
        edit_menu.add_separator()
        self._menu_add(edit_menu, "Set Mark", "set-mark-command", "C-SPC")
        self._menu_add(edit_menu, "Mark Whole Buffer", "mark-whole-buffer", "C-x h")
        self._menu_add(edit_menu, "Exchange Point and Mark", "exchange-point-and-mark", "C-x C-x")
        edit_menu.add_separator()
        self._menu_add(edit_menu, "Search Forward...", "search-forward", "C-s")
        self._menu_add(edit_menu, "Search Backward...", "search-backward", "C-r")
        self._menu_add(edit_menu, "Find Next", "search.find_next", "F3")
        self._menu_add(edit_menu, "Go To Line...", "goto-line", "M-g g")

        view_menu = self.get_or_create_menu("View")
        self._menu_add(view_menu, "Toggle Word Wrap", "view.toggle_wrap")
        view_menu.add_separator()
        self._menu_add(view_menu, "Larger Font", "view.font_larger", "C-x +")
        self._menu_add(view_menu, "Smaller Font", "view.font_smaller", "C-x -")
        self._menu_add(view_menu, "Reset Font Size", "view.font_reset")
        view_menu.add_separator()
        self._menu_add(view_menu, "Toggle Highlight", "view.toggle_highlight")
        self._menu_add(view_menu, "Pygments Style...", "view.set_style")

        tools_menu = self.get_or_create_menu("Tools")
        self._menu_add(tools_menu, "M-x Command...", "execute-extended-command", "M-x")
        tools_menu.add_separator()
        self._menu_add(tools_menu, "Auto Detect Syntax", "syntax.auto")
        self._menu_add(tools_menu, "Set Syntax by Pygments Alias...", "syntax.set_alias")
        tools_menu.add_separator()
        self._menu_add(tools_menu, "Open User Config", "custom.open_config")
        self._menu_add(tools_menu, "Reload Customization", "custom.reload")
        self._menu_add(tools_menu, "Run Current Buffer as Customization", "custom.run_buffer")

        help_menu = self.get_or_create_menu("Help")
        self._menu_add(help_menu, "About", "help.about", "F1")

    def get_or_create_menu(self, name: str) -> tk.Menu:
        if name in self.menus:
            return self.menus[name]
        menu = tk.Menu(self.menubar, tearoff=0)
        self.menubar.add_cascade(label=name, menu=menu)
        self.menus[name] = menu
        return menu

    def _menu_add(self, menu: tk.Menu, label: str, command_name: str, accelerator: Optional[str] = None) -> None:
        menu.add_command(label=label, command=lambda name=command_name: self.run_command(name), accelerator=accelerator or "")

    def add_menu_item(
        self,
        menu: str,
        label: str,
        command: Union[str, Callable[..., Any]],
        *,
        accelerator: Optional[str] = None,
    ) -> None:
        target = self.get_or_create_menu(menu)
        if isinstance(command, str):
            target.add_command(label=label, command=lambda name=command: self.run_command(name), accelerator=accelerator or "")
        else:
            target.add_command(label=label, command=lambda cb=command: self._call_custom(cb), accelerator=accelerator or "")

    # ----- Commands and bindings ------------------------------------------

    def _register_builtin_commands(self) -> None:
        # The dotted names keep compatibility with old init.py files.  The
        # Emacs-style names mirror --nox so the same M-x command vocabulary works
        # in both front ends.
        builtin: Dict[str, Callable[[], Any]] = {
            "app.exit": self.exit_app,
            "backward-char": self.gui_backward_char,
            "backward-word": self.gui_backward_word,
            "beginning-of-buffer": self.gui_beginning_of_buffer,
            "beginning-of-line": self.gui_beginning_of_line,
            "copy-region-as-kill": self.gui_copy_region,
            "custom.open_config": self.open_user_config,
            "custom.reload": lambda: self.load_customizations(silent=False),
            "custom.run_buffer": self.run_buffer_as_customization,
            "delete-char": self.gui_delete_char,
            "display-line-numbers-mode": self.gui_display_line_numbers_mode,
            "edit.copy": self.gui_copy_region,
            "edit.cut": self.gui_kill_region,
            "edit.delete": self.gui_delete_char,
            "edit.paste": self.gui_yank,
            "edit.redo": lambda: self._event_generate("<<Redo>>"),
            "edit.select_all": self.mark_whole_buffer,
            "edit.undo": lambda: self._event_generate("<<Undo>>"),
            "end-of-buffer": self.gui_end_of_buffer,
            "end-of-line": self.gui_end_of_line,
            "exchange-point-and-mark": self.exchange_point_and_mark,
            "execute-extended-command": self.command_palette,
            "file.new": self.new_file,
            "file.open": self.open_file_dialog,
            "file.reload": self.reload_from_disk,
            "file.save": self.save_file,
            "file.save_as": self.save_as_dialog,
            "find-file": self.open_file_dialog,
            "forward-char": self.gui_forward_char,
            "forward-word": self.gui_forward_word,
            "goto-line": self.goto_line_dialog,
            "help": self.about_dialog,
            "help.about": self.about_dialog,
            "keyboard-quit": self.keyboard_quit,
            "kill-emacs": self.exit_app,
            "kill-line": self.gui_kill_line,
            "kill-region": self.gui_kill_region,
            "mark-whole-buffer": self.mark_whole_buffer,
            "next-line": self.gui_next_line,
            "open-line": self.gui_open_line,
            "previous-line": self.gui_previous_line,
            "recenter-top-bottom": self.gui_recenter,
            "save-buffer": self.save_file,
            "scroll-down-command": self.gui_page_up,
            "scroll-up-command": self.gui_page_down,
            "search-backward": self.search_backward_command,
            "search.find": self.search_forward_command,
            "search.find_next": self.find_next,
            "search.find_previous": self.find_previous,
            "search-forward": self.search_forward_command,
            "search.goto_line": self.goto_line_dialog,
            "search.replace_all": self.replace_all_dialog,
            "set-mark-command": self.set_mark_command,
            "syntax.auto": self.auto_detect_syntax,
            "syntax.set_alias": self.set_syntax_alias_dialog,
            "tools.command_palette": self.command_palette,
            "undo": lambda: self._event_generate("<<Undo>>"),
            "view.font_larger": lambda: self.change_font_size(+1),
            "view.font_reset": lambda: self.set_option("font_size", 11),
            "view.font_smaller": lambda: self.change_font_size(-1),
            "view.set_style": self.set_style_dialog,
            "view.toggle_highlight": self.toggle_highlight,
            "view.toggle_wrap": self.toggle_wrap,
            "write-file": self.save_as_dialog,
            "yank": self.gui_yank,
        }
        for name, fn in builtin.items():
            self.add_command(name, fn)

    def add_command(self, name: str, callback: Callable[..., Any], *, custom: bool = False) -> None:
        if custom:
            self.custom_commands.add(name)

        def wrapper(cb: Callable[..., Any] = callback) -> Any:
            return self._call_custom(cb) if custom else cb()

        self.commands[name] = wrapper

    def run_command(self, name: str) -> Any:
        fn = self.commands.get(name)
        if not fn:
            messagebox.showerror(APP_NAME, f"Unknown command: {name}", parent=self.root)
            return None
        try:
            return fn()
        except Exception:
            self.report_exception(f"Command failed: {name}")
            return None

    def _call_custom(self, callback: Callable[..., Any]) -> Any:
        return _call_flexibly(callback, self.api)

    def _bind_keys(self) -> None:
        # Use Emacs-style keys in the Tk front end too.  Ctrl-X is a prefix here,
        # not "cut"; kill-region is C-w, copy-region-as-kill is M-w, and yank is
        # C-y.  The old dotted command names are still available through init.py.
        self.text.bind("<KeyPress>", self._on_gui_keypress)
        self.root.bind("<F1>", lambda event: self._run_command_from_event("help"))
        self.text.bind("<<Modified>>", self._on_modified)
        self.text.bind("<KeyRelease>", lambda event: self.update_status())
        self.text.bind("<ButtonRelease>", lambda event: self.update_status())
        self.text.bind("<Tab>", self._on_tab)
        self.text.bind("<Return>", self._on_return)

    def _run_command_from_event(self, name: str) -> str:
        self.run_command(name)
        return "break"

    def _event_generate(self, virtual_event: str) -> None:
        self.text.event_generate(virtual_event)

    # ----- Emacs-style Tk key handling ------------------------------------

    def _gui_control_pressed(self, event: tk.Event) -> bool:
        char = event.char or ""
        if event.state & 0x0004:
            return True
        return len(char) == 1 and ord(char) < 32 and char not in {"\t", "\n", "\r", "\x1b"}

    def _gui_meta_pressed(self, event: tk.Event) -> bool:
        # Tk reports Alt/Meta differently across platforms.  Mod1 is common on
        # X11; the larger masks cover Meta variants seen by Tk on some systems.
        return bool(event.state & (0x0008 | 0x0080 | 0x20000))

    def _gui_ctrl_key(self, event: tk.Event, letter: str) -> bool:
        if not self._gui_control_pressed(event):
            return False
        key = (event.keysym or "").lower()
        if key == letter.lower():
            return True
        if len(letter) == 1 and event.char == chr(ord(letter.upper()) & 0x1F):
            return True
        return False

    def _gui_key_label(self, event: tk.Event) -> str:
        if self._gui_control_pressed(event) and event.keysym:
            return "C-" + event.keysym
        if self._gui_meta_pressed(event) and event.keysym:
            return "M-" + event.keysym
        return event.keysym or event.char or "?"

    def _on_gui_keypress(self, event: tk.Event) -> Optional[str]:
        if self.gui_prefix == "C-x":
            return self._handle_gui_c_x_key(event)
        if self.gui_prefix == "M-g":
            return self._handle_gui_m_g_key(event)
        if self.gui_prefix == "M":
            return self._handle_gui_meta_key(event)

        keysym = event.keysym or ""
        char = event.char or ""

        if keysym == "Escape" or char == "\x1b":
            self.gui_prefix = "M"
            self.set_status_message("M-")
            return "break"

        if self._gui_meta_pressed(event):
            return self._handle_gui_meta_key(event)

        if self._gui_ctrl_key(event, "x"):
            self.gui_prefix = "C-x"
            self.set_status_message("C-x")
            return "break"
        if self._gui_ctrl_key(event, "g"):
            self.keyboard_quit()
            return "break"
        if self._gui_ctrl_key(event, "a"):
            self.gui_beginning_of_line()
            return "break"
        if self._gui_ctrl_key(event, "e"):
            self.gui_end_of_line()
            return "break"
        if self._gui_ctrl_key(event, "f"):
            self.gui_forward_char()
            return "break"
        if self._gui_ctrl_key(event, "b"):
            self.gui_backward_char()
            return "break"
        if self._gui_ctrl_key(event, "n"):
            self.gui_next_line()
            return "break"
        if self._gui_ctrl_key(event, "p"):
            self.gui_previous_line()
            return "break"
        if self._gui_ctrl_key(event, "v"):
            self.gui_page_down()
            return "break"
        if self._gui_ctrl_key(event, "s"):
            self.search_forward_command()
            return "break"
        if self._gui_ctrl_key(event, "r"):
            self.search_backward_command()
            return "break"
        if self._gui_ctrl_key(event, "k"):
            self.gui_kill_line()
            return "break"
        if self._gui_ctrl_key(event, "w"):
            self.gui_kill_region()
            return "break"
        if self._gui_ctrl_key(event, "y"):
            self.gui_yank()
            return "break"
        if self._gui_ctrl_key(event, "d"):
            self.gui_delete_char()
            return "break"
        if self._gui_ctrl_key(event, "o"):
            self.gui_open_line()
            return "break"
        if self._gui_ctrl_key(event, "l"):
            self.gui_recenter()
            return "break"
        if char == "\x00" or (self._gui_control_pressed(event) and keysym.lower() in {"space", "at"}):
            self.set_mark_command()
            return "break"
        if char == "\x1f" or (self._gui_control_pressed(event) and keysym.lower() in {"slash", "underscore"}):
            self.run_command("undo")
            return "break"
        return None

    def _handle_gui_c_x_key(self, event: tk.Event) -> str:
        self.gui_prefix = None
        keysym = (event.keysym or "").lower()
        char = event.char or ""
        if self._gui_ctrl_key(event, "s"):
            self.run_command("save-buffer")
        elif self._gui_ctrl_key(event, "w"):
            self.run_command("write-file")
        elif self._gui_ctrl_key(event, "f"):
            self.run_command("find-file")
        elif self._gui_ctrl_key(event, "c"):
            self.run_command("kill-emacs")
        elif self._gui_ctrl_key(event, "x"):
            self.run_command("exchange-point-and-mark")
        elif self._gui_ctrl_key(event, "g"):
            self.keyboard_quit()
        elif char.lower() == "h" or keysym == "h":
            self.run_command("mark-whole-buffer")
        elif char in {"+", "="} or keysym in {"plus", "equal"}:
            self.run_command("view.font_larger")
        elif char == "-" or keysym == "minus":
            self.run_command("view.font_smaller")
        elif char.lower() == "u" or keysym == "u" or char == "\x1f":
            self.run_command("undo")
        elif char == "?" or keysym == "question":
            self.run_command("help")
        else:
            self.set_status_message(f"Undefined C-x binding: {self._gui_key_label(event)}")
        return "break"

    def _handle_gui_meta_key(self, event: tk.Event) -> str:
        self.gui_prefix = None
        keysym = (event.keysym or "").lower()
        char = event.char or ""
        key = char.lower() if char else keysym
        if key == "x":
            self.run_command("execute-extended-command")
        elif key == "v":
            self.gui_page_up()
        elif key == "f":
            self.gui_forward_word()
        elif key == "b":
            self.gui_backward_word()
        elif key == "w":
            self.gui_copy_region()
        elif key == "g":
            self.gui_prefix = "M-g"
            self.set_status_message("M-g")
        elif char == "<" or keysym == "less":
            self.gui_beginning_of_buffer()
        elif char == ">" or keysym == "greater":
            self.gui_end_of_buffer()
        else:
            self.set_status_message(f"Undefined Meta binding: {self._gui_key_label(event)}")
        return "break"

    def _handle_gui_m_g_key(self, event: tk.Event) -> str:
        self.gui_prefix = None
        key = (event.char or event.keysym or "").lower()
        if key == "g":
            self.run_command("goto-line")
        elif self._gui_ctrl_key(event, "g"):
            self.keyboard_quit()
        else:
            self.set_status_message("Use M-g g for goto-line")
        return "break"

    def keyboard_quit(self) -> None:
        self.gui_prefix = None
        self.gui_mark_index = None
        self.text.tag_remove("sel", "1.0", "end")
        self.set_status_message("Quit")

    def _gui_after_point_motion(self) -> None:
        if self.gui_mark_index is not None:
            self.text.tag_remove("sel", "1.0", "end")
            point = self.text.index("insert")
            if self.text.compare(self.gui_mark_index, "<", point):
                self.text.tag_add("sel", self.gui_mark_index, point)
            elif self.text.compare(point, "<", self.gui_mark_index):
                self.text.tag_add("sel", point, self.gui_mark_index)
        self.text.see("insert")
        self.update_status()

    def gui_beginning_of_line(self) -> None:
        self.text.mark_set("insert", "insert linestart")
        self._gui_after_point_motion()

    def gui_end_of_line(self) -> None:
        self.text.mark_set("insert", "insert lineend")
        self._gui_after_point_motion()

    def gui_forward_char(self) -> None:
        self.text.mark_set("insert", "insert +1c")
        self._gui_after_point_motion()

    def gui_backward_char(self) -> None:
        self.text.mark_set("insert", "insert -1c")
        self._gui_after_point_motion()

    def gui_next_line(self) -> None:
        self.text.mark_set("insert", "insert +1 line")
        self._gui_after_point_motion()

    def gui_previous_line(self) -> None:
        self.text.mark_set("insert", "insert -1 line")
        self._gui_after_point_motion()

    def gui_page_down(self) -> None:
        lines = self._gui_visible_line_count()
        self.text.yview_scroll(1, "pages")
        self.text.mark_set("insert", f"insert +{lines} lines")
        self._gui_after_point_motion()

    def gui_page_up(self) -> None:
        lines = self._gui_visible_line_count()
        self.text.yview_scroll(-1, "pages")
        self.text.mark_set("insert", f"insert -{lines} lines")
        self._gui_after_point_motion()

    def _gui_visible_line_count(self) -> int:
        try:
            line_height = max(1, int(self.text_font.metrics("linespace")))
            return max(1, self.text.winfo_height() // line_height - 2)
        except Exception:
            return 20

    def gui_beginning_of_buffer(self) -> None:
        self.text.mark_set("insert", "1.0")
        self._gui_after_point_motion()

    def gui_end_of_buffer(self) -> None:
        self.text.mark_set("insert", "end-1c")
        self._gui_after_point_motion()

    def gui_forward_word(self) -> None:
        self.text.mark_set("insert", "insert +1c wordend")
        self._gui_after_point_motion()

    def gui_backward_word(self) -> None:
        self.text.mark_set("insert", "insert -1c wordstart")
        self._gui_after_point_motion()

    def gui_recenter(self) -> None:
        self.text.see("insert")
        self.update_status()

    def set_mark_command(self) -> None:
        self.gui_mark_index = self.text.index("insert")
        self.set_status_message("Mark set")

    def mark_whole_buffer(self) -> str:
        self.gui_mark_index = "1.0"
        self.text.tag_remove("sel", "1.0", "end")
        self.text.tag_add("sel", "1.0", "end-1c")
        self.text.mark_set("insert", "end-1c")
        self.text.see("insert")
        self.update_status()
        return "break"

    def exchange_point_and_mark(self) -> None:
        if self.gui_mark_index is None:
            self.set_status_message("No mark set")
            return
        point = self.text.index("insert")
        self.text.mark_set("insert", self.gui_mark_index)
        self.gui_mark_index = point
        self._gui_after_point_motion()
        self.set_status_message("Point and mark exchanged")

    def _gui_region_indices(self) -> Optional[tuple[str, str]]:
        try:
            return self.text.index("sel.first"), self.text.index("sel.last")
        except tk.TclError:
            pass
        if self.gui_mark_index is None:
            return None
        point = self.text.index("insert")
        if self.text.compare(self.gui_mark_index, "==", point):
            return None
        if self.text.compare(self.gui_mark_index, "<", point):
            return self.gui_mark_index, point
        return point, self.gui_mark_index

    def gui_copy_region(self) -> None:
        region = self._gui_region_indices()
        if region is None:
            self.set_status_message("No active region")
            return
        start, end = region
        self.gui_kill_ring = self.text.get(start, end)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.gui_kill_ring)
        except tk.TclError:
            pass
        self.set_status_message("Copied region")

    def gui_kill_region(self, *, delete_only: bool = False) -> None:
        region = self._gui_region_indices()
        if region is None:
            self.set_status_message("No active region")
            return
        start, end = region
        killed = self.text.get(start, end)
        if not delete_only:
            self.gui_kill_ring = killed
        self.text.delete(start, end)
        self.gui_mark_index = None
        self.text.tag_remove("sel", "1.0", "end")
        self.schedule_highlight()
        self.update_status()
        self.set_status_message("Deleted region" if delete_only else "Killed region")

    def gui_delete_char(self) -> None:
        if self._gui_region_indices() is not None:
            self.gui_kill_region(delete_only=True)
            return
        self.text.delete("insert", "insert +1c")
        self.schedule_highlight()
        self.update_status()

    def gui_kill_line(self) -> None:
        if self._gui_region_indices() is not None:
            self.gui_kill_region()
            return
        start = self.text.index("insert")
        line_end = self.text.index("insert lineend")
        if self.text.compare(start, "==", line_end):
            end = self.text.index("insert +1c")
        else:
            end = line_end
        killed = self.text.get(start, end)
        if not killed:
            self.set_status_message("Nothing killed")
            return
        self.gui_kill_ring = killed
        self.text.delete(start, end)
        self.gui_mark_index = None
        self.schedule_highlight()
        self.update_status()
        self.set_status_message("Killed line")

    def gui_yank(self) -> None:
        if self.gui_kill_ring:
            self.text.insert("insert", self.gui_kill_ring)
            self.schedule_highlight()
            self.update_status()
            self.set_status_message("Yanked")
            return
        self._event_generate("<<Paste>>")

    def gui_open_line(self) -> None:
        point = self.text.index("insert")
        self.text.insert(point, "\n")
        self.text.mark_set("insert", point)
        self.schedule_highlight()
        self.update_status()

    def gui_display_line_numbers_mode(self) -> None:
        self.set_status_message("display-line-numbers-mode is only implemented in --nox")

    # ----- File handling ---------------------------------------------------

    def new_file(self) -> None:
        if not self.confirm_discard_changes():
            return
        self.current_path = None
        self.current_encoding = None
        self.force_lexer_alias = None
        self.set_text_content("", dirty=False)
        self.text.edit_reset()
        self.update_title()
        self.schedule_highlight(immediate=True)
        self.emit("after_new")

    def open_file_dialog(self) -> None:
        if not self.confirm_discard_changes():
            return
        filename = filedialog.askopenfilename(parent=self.root)
        if filename:
            self.open_path(Path(filename))

    def open_path(self, path: Path) -> None:
        if not path.exists():
            messagebox.showerror(APP_NAME, f"File not found:\n{path}", parent=self.root)
            return
        try:
            text, encoding = read_text_with_fallback(path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open file:\n{path}\n\n{exc}", parent=self.root)
            return
        self._loading_file = True
        try:
            self.current_path = path
            self.current_encoding = encoding
            self.force_lexer_alias = None
            self.set_text_content(text, dirty=False)
            self.text.edit_reset()
            self.update_title()
            self.schedule_highlight(immediate=True)
        finally:
            self._loading_file = False
        self.emit("after_open", path)

    def save_file(self) -> bool:
        if self.current_path is None:
            return self.save_as_dialog()
        return self.save_path(self.current_path)

    def save_as_dialog(self) -> bool:
        filename = filedialog.asksaveasfilename(parent=self.root)
        if not filename:
            return False
        return self.save_path(Path(filename))

    def save_path(self, path: Path) -> bool:
        self.emit("before_save", path)
        text = self.text.get("1.0", "end-1c")
        encoding = self.current_encoding or self.options.default_encoding
        try:
            write_text_exact(path, text, encoding=encoding)
        except UnicodeEncodeError:
            encoding = "utf-8"
            try:
                write_text_exact(path, text, encoding=encoding)
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Could not save file:\n{path}\n\n{exc}", parent=self.root)
                return False
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not save file:\n{path}\n\n{exc}", parent=self.root)
            return False
        self.current_path = path
        self.current_encoding = encoding
        self.dirty = False
        self.text.edit_modified(False)
        self.update_title()
        self.schedule_highlight(immediate=True)
        self.emit("after_save", path)
        return True

    def reload_from_disk(self) -> None:
        if self.current_path is None:
            return
        if self.dirty:
            ok = messagebox.askyesno(APP_NAME, "Discard unsaved changes and reload from disk?", parent=self.root)
            if not ok:
                return
        self.open_path(self.current_path)

    def confirm_discard_changes(self) -> bool:
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel(APP_NAME, "Save changes?", parent=self.root)
        if answer is None:
            return False
        if answer is True:
            return self.save_file()
        return True

    def exit_app(self) -> None:
        if self.confirm_discard_changes():
            self.root.destroy()

    def set_text_content(self, value: str, *, dirty: bool) -> None:
        self.text.delete("1.0", "end")
        self.text.insert("1.0", value)
        self.dirty = dirty
        self.text.edit_modified(False)
        self.update_title()
        self.update_status()
        self.schedule_highlight()

    # ----- Edit and search -------------------------------------------------

    def delete_selection(self) -> None:
        try:
            self.text.delete("sel.first", "sel.last")
        except tk.TclError:
            pass

    def select_all(self) -> str:
        self.text.tag_add("sel", "1.0", "end-1c")
        self.text.mark_set("insert", "1.0")
        self.text.see("insert")
        return "break"

    def find_dialog(self) -> None:
        initial = self.get_selected_or_last_find()
        value = simpledialog.askstring("Find", "Text to find:", initialvalue=initial, parent=self.root)
        if value:
            self.last_find = value
            self.find_next()

    def find_next(self) -> None:
        pattern = self.last_find
        if not pattern:
            self.find_dialog()
            return
        nocase = 0 if self.options.find_case_sensitive else 1
        start = self.text.index("insert +1c")
        pos = self.text.search(pattern, start, stopindex="end", nocase=nocase)
        if not pos:
            pos = self.text.search(pattern, "1.0", stopindex=start, nocase=nocase)
        if not pos:
            self.set_status_message(f"Not found: {pattern}")
            return
        end = f"{pos}+{len(pattern)}c"
        self.text.tag_remove("sel", "1.0", "end")
        self.text.tag_add("sel", pos, end)
        self.text.mark_set("insert", pos)
        self.text.see(pos)
        self.update_status()

    def search_forward_command(self) -> None:
        self.find_dialog()

    def search_backward_command(self) -> None:
        initial = self.get_selected_or_last_find()
        value = simpledialog.askstring("Search Backward", "Text to find:", initialvalue=initial, parent=self.root)
        if value:
            self.last_find = value
            self.find_previous()

    def find_previous(self) -> None:
        pattern = self.last_find
        if not pattern:
            self.search_backward_command()
            return
        nocase = 0 if self.options.find_case_sensitive else 1
        start = self.text.index("insert -1c")
        pos = self.text.search(pattern, start, stopindex="1.0", backwards=True, nocase=nocase)
        if not pos:
            pos = self.text.search(pattern, "end-1c", stopindex=start, backwards=True, nocase=nocase)
        if not pos:
            self.set_status_message(f"Not found: {pattern}")
            return
        end = f"{pos}+{len(pattern)}c"
        self.text.tag_remove("sel", "1.0", "end")
        self.text.tag_add("sel", pos, end)
        self.text.mark_set("insert", pos)
        self.text.see(pos)
        self.update_status()

    def replace_all_dialog(self) -> None:
        find_value = simpledialog.askstring("Replace All", "Find:", initialvalue=self.get_selected_or_last_find(), parent=self.root)
        if not find_value:
            return
        replace_value = simpledialog.askstring("Replace All", "Replace with:", initialvalue="", parent=self.root)
        if replace_value is None:
            return
        count = self.replace_all(find_value, replace_value)
        self.set_status_message(f"Replaced {count} occurrence(s)")

    def replace_all(self, find_value: str, replace_value: str) -> int:
        if not find_value:
            return 0
        nocase = 0 if self.options.find_case_sensitive else 1
        count = 0
        pos = "1.0"
        self.text.mark_set("insert", "1.0")
        while True:
            pos = self.text.search(find_value, pos, stopindex="end", nocase=nocase)
            if not pos:
                break
            end = f"{pos}+{len(find_value)}c"
            self.text.delete(pos, end)
            self.text.insert(pos, replace_value)
            pos = f"{pos}+{len(replace_value)}c"
            count += 1
        if count:
            self.schedule_highlight()
        return count

    def goto_line_dialog(self) -> None:
        value = simpledialog.askinteger("Go To Line", "Line number:", minvalue=1, parent=self.root)
        if value is None:
            return
        self.text.mark_set("insert", f"{value}.0")
        self.text.see("insert")
        self.update_status()

    def get_selected_or_last_find(self) -> str:
        try:
            selected = self.text.get("sel.first", "sel.last")
            if "\n" not in selected:
                return selected
        except tk.TclError:
            pass
        return self.last_find

    # ----- Input helpers ---------------------------------------------------

    def _on_tab(self, event: tk.Event) -> str:
        if self.options.indent_with_spaces:
            self.text.insert("insert", " " * self.options.tab_width)
        else:
            self.text.insert("insert", "\t")
        return "break"

    def _on_return(self, event: tk.Event) -> Optional[str]:
        if not self.options.auto_indent:
            return None
        line_start = self.text.index("insert linestart")
        current = self.text.get(line_start, "insert")
        indent = current[: len(current) - len(current.lstrip(" \t"))]
        self.text.insert("insert", "\n" + indent)
        return "break"

    # ----- Highlighting ----------------------------------------------------

    def schedule_highlight(self, event: Optional[tk.Event] = None, *, immediate: bool = False) -> None:
        if self.highlight_after_id is not None:
            try:
                self.root.after_cancel(self.highlight_after_id)
            except tk.TclError:
                pass
            self.highlight_after_id = None
        if immediate:
            self.highlight_now()
        else:
            self.highlight_after_id = self.root.after(160, self.highlight_now)

    def highlight_now(self) -> None:
        self.highlight_after_id = None
        for tag in tuple(self.token_tags):
            self.text.tag_remove(tag, "1.0", "end")

        content = self.text.get("1.0", "end-1c")
        try:
            result = compute_highlight_spans(
                content,
                path=self.current_path,
                options=self.options,
                force_lexer_alias=self.force_lexer_alias,
            )
            self.force_lexer_alias = result.force_lexer_alias
            self.current_lexer_name = result.lexer_name
            for span in result.spans:
                tag = self.tag_for_token(span.token_type)
                self.configure_token_tag(tag, span.token_type)
                self.text.tag_add(tag, f"1.0+{span.start}c", f"1.0+{span.end}c")
            self.text.tag_raise("sel")
            self.emit("after_highlight", self.current_lexer_name)
        except Exception:
            self.current_lexer_name = "Highlight error"
        finally:
            self.update_status()

    def get_lexer(self) -> Any:
        lexer, alias = get_shared_lexer(self.current_path, self.force_lexer_alias)
        self.force_lexer_alias = alias
        return lexer

    def _lexer_name_for_status(self) -> str:
        name, alias = lexer_name_for_status(self.current_path, self.force_lexer_alias)
        self.force_lexer_alias = alias
        return name

    def tag_for_token(self, token_type: Any) -> str:
        tag = "tok_" + str(token_type).replace("Token", "").replace(".", "_").strip("_")
        if tag == "tok_":
            tag = "tok_Text"
        self.token_tags.add(tag)
        return tag

    def configure_token_tag(self, tag: str, token_type: Any) -> None:
        if tag in self.configured_token_tags:
            return
        self.configured_token_tags.add(tag)
        style = pygments_style_for_token(self.options.pygments_style, token_type)
        config: Dict[str, Any] = {}
        if style.get("color"):
            config["foreground"] = "#" + style["color"]
        if style.get("bgcolor"):
            config["background"] = "#" + style["bgcolor"]
        if style.get("bold") or style.get("italic"):
            config["font"] = self.get_token_font(bool(style.get("bold")), bool(style.get("italic")))
        if style.get("underline"):
            config["underline"] = 1
        if config:
            self.text.tag_configure(tag, **config)

    def get_token_font(self, bold: bool, italic: bool) -> tkfont.Font:
        key = (bold, italic)
        font = self.token_font_cache.get(key)
        if font is None:
            font = tkfont.Font(
                family=self.text_font.actual("family"),
                size=self.text_font.actual("size"),
                weight="bold" if bold else "normal",
                slant="italic" if italic else "roman",
            )
            self.token_font_cache[key] = font
        return font

    def clear_highlight_style_cache(self) -> None:
        self.configured_token_tags.clear()
        self.token_font_cache.clear()
        for tag in tuple(self.token_tags):
            try:
                self.text.tag_configure(tag, foreground="", background="", font="", underline=0)
            except tk.TclError:
                pass

    # ----- Customization ---------------------------------------------------

    def load_customizations(self, *, silent: bool) -> None:
        # Keep built-ins, but replace previously registered custom commands.
        for name in list(self.custom_commands):
            self.commands.pop(name, None)
        self.custom_commands.clear()

        loaded: List[Path] = []
        errors: List[str] = []
        for path in unique_paths(self.config_paths):
            if not path.exists():
                continue
            try:
                self.execute_customization_file(path)
                loaded.append(path)
            except Exception:
                errors.append(f"{path}\n{traceback.format_exc()}")
        if errors:
            messagebox.showerror(APP_NAME, "Customization error:\n\n" + "\n".join(errors), parent=self.root)
        elif not silent:
            if loaded:
                self.set_status_message("Reloaded customization: " + ", ".join(str(p) for p in loaded))
            else:
                self.set_status_message("No customization file found")

    def execute_customization_file(self, path: Path) -> None:
        code = path.read_text(encoding="utf-8")
        namespace = {
            "api": self.api,
            "editor": self.api,
            "Path": Path,
            "datetime": _dt,
            "tk": tk,
            "__file__": str(path),
            "__name__": "simplepypad_user_config",
        }
        exec(compile(code, str(path), "exec"), namespace)

    def run_buffer_as_customization(self) -> None:
        code = self.text.get("1.0", "end-1c")
        filename = str(self.current_path) if self.current_path else "<current buffer>"
        namespace = {
            "api": self.api,
            "editor": self.api,
            "Path": Path,
            "datetime": _dt,
            "tk": tk,
            "__file__": filename,
            "__name__": "simplepypad_buffer_customization",
        }
        try:
            exec(compile(code, filename, "exec"), namespace)
            self.set_status_message("Customization buffer executed")
        except Exception:
            self.report_exception("Customization buffer failed")

    def open_user_config(self) -> None:
        path = default_user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
        if self.confirm_discard_changes():
            self.open_path(path)

    # ----- View, options and dialogs --------------------------------------

    def toggle_wrap(self) -> None:
        self.set_option("wrap", not self.options.wrap)

    def toggle_highlight(self) -> None:
        self.set_option("highlight", not self.options.highlight)

    def change_font_size(self, delta: int) -> None:
        self.set_option("font_size", max(6, min(72, self.options.font_size + delta)))

    def set_style_dialog(self) -> None:
        current = self.options.pygments_style
        style = simpledialog.askstring("Pygments Style", "Style name, e.g. default, monokai, friendly:", initialvalue=current, parent=self.root)
        if style:
            self.set_option("pygments_style", style.strip())

    def auto_detect_syntax(self) -> None:
        self.force_lexer_alias = None
        self.schedule_highlight(immediate=True)

    def set_syntax_alias_dialog(self) -> None:
        alias = simpledialog.askstring("Syntax", "Pygments alias, e.g. python, javascript, html, rust:", parent=self.root)
        if alias is not None:
            self.force_lexer_alias = alias.strip() or None
            self.schedule_highlight(immediate=True)

    def set_option(self, name: str, value: Any) -> None:
        if not hasattr(self.options, name):
            raise AttributeError(f"unknown option: {name}")
        setattr(self.options, name, value)
        self.apply_options()

    def apply_options(self) -> None:
        self.text_font.configure(family=self.options.font_family, size=self.options.font_size)
        tab_px = max(1, self.text_font.measure(" " * int(self.options.tab_width)))
        self.text.configure(font=self.text_font, tabs=(tab_px,), wrap="word" if self.options.wrap else "none")
        if self.options.wrap:
            self.xscroll.grid_remove()
        else:
            self.xscroll.grid(row=1, column=0, sticky="ew")
        self.clear_highlight_style_cache()
        self.schedule_highlight()
        self.update_status()

    def command_palette(self) -> None:
        top = tk.Toplevel(self.root)
        top.title("Command Palette")
        top.transient(self.root)
        top.geometry("420x360")

        filter_var = tk.StringVar()
        entry = tk.Entry(top, textvariable=filter_var)
        entry.pack(fill="x", padx=6, pady=(6, 3))
        listbox = tk.Listbox(top, activestyle="dotbox")
        listbox.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        names = sorted(self.commands)

        def refresh(*_: Any) -> None:
            needle = filter_var.get().lower()
            listbox.delete(0, "end")
            for name in names:
                if needle in name.lower():
                    listbox.insert("end", name)
            if listbox.size():
                listbox.selection_set(0)

        def run_selected(event: Optional[tk.Event] = None) -> str:
            selection = listbox.curselection()
            if not selection:
                return "break"
            name = listbox.get(selection[0])
            top.destroy()
            self.run_command(name)
            return "break"

        filter_var.trace_add("write", refresh)
        entry.bind("<Return>", run_selected)
        listbox.bind("<Double-Button-1>", run_selected)
        listbox.bind("<Return>", run_selected)
        top.bind("<Escape>", lambda event: top.destroy())
        refresh()
        entry.focus_set()

    def about_dialog(self) -> None:
        pygments = "available" if PYGMENTS_AVAILABLE else "not installed"
        messagebox.showinfo(
            "About SimplePyPad",
            f"{APP_NAME}\n\n"
            "A tiny Tkinter editor with Pygments syntax highlighting.\n"
            "Customization language: Python.\n\n"
            f"Pygments: {pygments}\n"
            f"User config: {default_user_config_path()}",
            parent=self.root,
        )

    # ----- Status, events and errors --------------------------------------

    def _on_modified(self, event: tk.Event) -> None:
        if self.text.edit_modified():
            if not self._loading_file:
                self.dirty = True
                self.update_title()
                self.schedule_highlight()
                self.emit("text_changed")
            self.text.edit_modified(False)
        self.update_status()

    def update_title(self) -> None:
        name = str(self.current_path) if self.current_path else "Untitled"
        mark = "*" if self.dirty else ""
        self.root.title(f"{mark}{name} - {APP_NAME}")

    def update_status(self) -> None:
        try:
            line, col = self.text.index("insert").split(".")
            col_num = int(col) + 1
        except Exception:
            line, col_num = "1", 1
        enc = self.current_encoding or self.options.default_encoding
        if self._status_message:
            left = self._status_message
        else:
            left = f"Ln {line}, Col {col_num}"
        parts = [left, self.current_lexer_name, enc]
        if self.force_lexer_alias:
            parts.append(f"syntax={self.force_lexer_alias}")
        self.status_var.set("  |  ".join(parts))

    def set_status_message(self, message: str) -> None:
        self._status_message = message
        self.update_status()
        self.root.after(4000, self.clear_status_message)

    def clear_status_message(self) -> None:
        self._status_message = ""
        self.update_status()

    def emit(self, event_name: str, *args: Any) -> None:
        for callback in list(self.hooks.get(event_name, [])):
            try:
                _call_flexibly(callback, self.api, *args)
            except Exception:
                self.report_exception(f"Hook failed: {event_name}")

    def report_exception(self, title: str) -> None:
        messagebox.showerror(APP_NAME, title + "\n\n" + traceback.format_exc(), parent=self.root)


DEFAULT_CONFIG_TEMPLATE = '''# SimplePyPad user config. This file is Python, not a sandbox.
# Only run code you trust. This file is executed as Python code.

# Examples:
# api.set_font("Consolas", 12)
# api.set_theme("friendly")
# api.set_option("wrap", False)


def insert_timestamp(api):
    api.insert(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


api.add_command("custom.insert_timestamp", insert_timestamp)
api.add_menu_item("Tools", "Insert Timestamp", "custom.insert_timestamp")
api.bind_key("<F5>", lambda api, event: (api.run_command("custom.insert_timestamp"), "break")[-1])
'''


def _call_flexibly(callback: Callable[..., Any], *args: Any) -> Any:
    """Call callback with as many leading args as its signature accepts."""
    try:
        sig = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback(*args)

    params = list(sig.parameters.values())
    if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params):
        return callback(*args)

    positional = [
        p
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    required = [p for p in positional if p.default is inspect.Parameter.empty]
    if len(required) > len(args):
        return callback(*args)
    return callback(*args[: len(positional)])



def write_text_exact(path: Path, text: str, *, encoding: str) -> None:
    with path.open("w", encoding=encoding, newline="") as f:
        f.write(text)

def read_text_with_fallback(path: Path) -> tuple[str, str]:
    preferred = locale.getpreferredencoding(False) or "utf-8"
    data = path.read_bytes()
    candidates = ["utf-8-sig"] if data.startswith(b"\xef\xbb\xbf") else []
    candidates.extend(["utf-8", preferred, "cp932", "shift_jis", "latin-1"])
    candidates = unique_strings(candidates)
    last_error: Optional[Exception] = None
    for encoding in candidates:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError as exc:
            last_error = exc
    raise last_error or UnicodeDecodeError("utf-8", data, 0, 1, "could not decode")


def unique_strings(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        key = value.lower()
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def unique_paths(values: Iterable[Path]) -> List[Path]:
    seen: set[Path] = set()
    result: List[Path] = []
    for value in values:
        try:
            key = value.expanduser().resolve()
        except Exception:
            key = value.expanduser()
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def default_config_dir() -> Path:
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
        return Path.home() / "AppData" / "Roaming" / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / APP_SLUG
    return Path.home() / ".config" / APP_SLUG


def default_user_config_path() -> Path:
    return default_config_dir() / "init.py"


NOX_HELP = """SimplePyPad --nox: Emacs-style terminal mode

Core keys:
  C-x C-s        save-buffer
  C-x C-w        write-file
  C-x C-f        find-file
  C-x C-c        quit, asking about unsaved changes
  C-g            cancel prefix / minibuffer
  F1             help
  M-x            run command by name

Movement:
  C-a / C-e      beginning / end of line
  C-f / C-b      forward / backward character
  M-f / M-b      forward / backward word
  C-n / C-p      next / previous line
  M-< / M->      beginning / end of buffer
  C-v / M-v      page down / page up
  M-g g          go to line

Display/customization:
  M-x display-line-numbers-mode
                 toggle the line-number gutter
  M-x view.toggle_highlight
                 toggle shared Pygments highlighting
  M-x view.set_style
                 set the shared Pygments style
  M-x syntax.set_alias / syntax.auto
                 force or auto-detect syntax
  M-x custom.open_config / custom.reload
                 edit or reload the same init.py used by Tk mode

Editing:
  C-d            delete character
  DEL            delete previous character
  C-k            kill line
  C-y            yank
  C-o            open line
  C-/ or C-_     undo
  C-SPC          set mark, if the terminal sends it
  C-x h          mark whole buffer
  C-x C-x        exchange point and mark
  C-w            kill region
  M-w            copy region

Search:
  C-s            search forward
  C-r            search backward

This terminal mode uses Emacs-style key bindings and does not implement vi commands.
"""


def _nox_text_to_lines(text: str) -> List[str]:
    """Represent text as editable lines while preserving exact final newlines."""
    return text.split("\n")


def _nox_lines_to_text(lines: List[str]) -> str:
    return "\n".join(lines) if lines else ""


@dataclass
class NoxKeyEvent:
    kind: str
    key: Any
    sequence: str = ""


class NoxEditorAPI:
    """Customization API for --nox.

    It intentionally mirrors the high-level Tk API where terminal mode can do so.
    Tk-only hooks are accepted as harmless no-ops instead of making a shared
    init.py fail just because a terminal has no menu bar.
    """

    def __init__(self, app: "NoxEmacsEditor") -> None:
        self._app = app

    @property
    def text_widget(self) -> None:
        return None

    @property
    def root(self) -> None:
        return None

    @property
    def path(self) -> Optional[Path]:
        return self._app.path

    def get_text(self) -> str:
        return self._app._text()

    def set_text(self, value: str, *, dirty: bool = True) -> None:
        self._app.set_text_content(value, dirty=dirty)

    def insert(self, value: str, index: str = "insert") -> None:
        if index not in {"insert", "point"}:
            self._app.goto_index(index)
        self._app.insert_string(value)

    def replace_selection(self, value: str) -> bool:
        if not self._app._has_region():
            return False
        self._app.insert_string(value)
        return True

    def get_selection(self) -> str:
        if not self._app._has_region():
            return ""
        start, end = self._app._region_offsets()
        return self._app._text()[start:end]

    def set_option(self, name: str, value: Any) -> None:
        self._app.set_option(name, value)

    def get_option(self, name: str) -> Any:
        if not hasattr(self._app.options, name):
            raise AttributeError(f"unknown option: {name}")
        return getattr(self._app.options, name)

    def set_font(self, family: Optional[str] = None, size: Optional[int] = None) -> None:
        if family is not None:
            self.set_option("font_family", family)
        if size is not None:
            self.set_option("font_size", int(size))

    def set_theme(self, pygments_style: str) -> None:
        self.set_option("pygments_style", pygments_style)

    def set_language(self, alias: Optional[str]) -> None:
        self._app.force_lexer_alias = alias or None
        self._app.invalidate_highlight(clear_style=True)

    def bind_key(self, sequence: str, callback: Callable[..., Any], *, widget: str = "text") -> None:
        self._app.add_custom_key_binding(sequence, callback)

    def add_command(self, name: str, callback: Callable[..., Any]) -> None:
        self._app.add_command(name, callback, custom=True)

    def run_command(self, name: str) -> Any:
        return self._app.run_command(name)

    def commands(self) -> List[str]:
        return sorted(self._app.commands)

    def add_menu_item(
        self,
        menu: str,
        label: str,
        command: Union[str, Callable[..., Any]],
        *,
        accelerator: Optional[str] = None,
    ) -> None:
        # Terminal mode has no menus. Accepting this call lets one init.py
        # serve both modes. Callable menu items are exposed through M-x.
        if callable(command):
            command_name = "menu." + label.strip().lower().replace(" ", "_")
            self._app.add_command(command_name, command, custom=True)
        return None

    def on(self, event_name: str, callback: Callable[..., Any]) -> None:
        self._app.hooks.setdefault(event_name, []).append(callback)

    def open_file(self, path: Union[str, os.PathLike[str]]) -> None:
        self._app._read_file_into_buffer(Path(path).expanduser())

    def save_file(self, path: Optional[Union[str, os.PathLike[str]]] = None) -> bool:
        if path is not None:
            return self._app._save_to(Path(path).expanduser())
        return self._app.save_buffer()

    def show_message(self, title: str, message: str) -> None:
        self._app.set_status_message(f"{title}: {message}")

    def ask_string(self, title: str, prompt: str, initial: str = "") -> Optional[str]:
        return self._app.prompt(f"{title}: {prompt} ", initial=initial)

    def status(self, message: str) -> None:
        self._app.set_status_message(message)


class NoxEmacsEditor:
    """A small curses editor with Emacs-like keys for ``--nox`` mode."""

    CTRL_A = "\x01"
    CTRL_B = "\x02"
    CTRL_C = "\x03"
    CTRL_D = "\x04"
    CTRL_E = "\x05"
    CTRL_F = "\x06"
    CTRL_G = "\x07"
    CTRL_K = "\x0b"
    CTRL_L = "\x0c"
    CTRL_N = "\x0e"
    CTRL_O = "\x0f"
    CTRL_P = "\x10"
    CTRL_R = "\x12"
    CTRL_S = "\x13"
    CTRL_V = "\x16"
    CTRL_W = "\x17"
    CTRL_X = "\x18"
    CTRL_Y = "\x19"
    CTRL_SPACE = "\x00"
    CTRL_UNDO = "\x1f"
    ESC = "\x1b"
    DEL = "\x7f"

    def __init__(self, stdscr: Any, args: argparse.Namespace, curses_module: Any) -> None:
        self.stdscr = stdscr
        self.curses = curses_module
        self.options = Options()
        initial_path: Optional[Path] = Path(args.file).expanduser() if args.file else None
        self.path: Optional[Path] = None
        self.encoding = self.options.default_encoding
        self.lines: List[str] = [""]
        self.clean_text = ""
        self.row = 0
        self.col = 0
        self.top_line = 0
        self.left_col = 0
        self.preferred_col: Optional[int] = None
        self.prefix: Optional[str] = None
        self.message = "C-x C-s save  C-x C-c quit  F1 help  M-x command"
        self.kill_ring = ""
        self.mark: Optional[tuple[int, int]] = None
        self.undo_stack: List[tuple[str, int, int, Optional[tuple[int, int]]]] = []
        self.last_search = ""
        self.running = True
        self.show_line_numbers = True
        self.force_lexer_alias: Optional[str] = None
        self.current_lexer_name = "Text"
        self.commands: Dict[str, Callable[[], Any]] = {}
        self.custom_commands: set[str] = set()
        self.custom_key_bindings: Dict[tuple[str, Any], Callable[..., Any]] = {}
        self.hooks: Dict[str, List[Callable[..., Any]]] = {}
        self.api = NoxEditorAPI(self)
        self.config_paths: List[Path] = []
        if not args.no_user_config:
            self.config_paths.append(default_user_config_path())
        self.config_paths.extend(Path(p).expanduser() for p in args.config)

        self._highlight_cache_key: Optional[tuple[Any, ...]] = None
        self._highlight_segments_by_line: Dict[int, List[tuple[int, int, int]]] = {}
        self._colors_ready = False
        self._default_color = -1
        self._next_color_pair = 1
        self._pair_cache: Dict[tuple[int, int], int] = {}
        self._attr_cache: Dict[tuple[str, Any], int] = {}

        self._register_builtin_commands()
        self.load_customizations(silent=True)

        if initial_path is not None:
            self._read_file_into_buffer(initial_path)
        else:
            self.invalidate_highlight(clear_style=True)

    @property
    def dirty(self) -> bool:
        return self._text() != self.clean_text

    def run(self) -> int:
        self._setup_terminal()
        while self.running:
            self._refresh()
            kind, key = self._read_key()
            self._handle_key(kind, key)
        return 0

    def _setup_terminal(self) -> None:
        self.curses.raw()
        self.curses.noecho()
        self.stdscr.keypad(True)
        try:
            self.curses.curs_set(1)
        except self.curses.error:
            pass
        try:
            self.stdscr.timeout(-1)
        except self.curses.error:
            pass
        self._setup_colors()

    def _setup_colors(self) -> None:
        if self._colors_ready:
            return
        self._colors_ready = True
        try:
            self.curses.start_color()
        except self.curses.error:
            return
        try:
            self.curses.use_default_colors()
            self._default_color = -1
        except self.curses.error:
            self._default_color = self.curses.COLOR_BLACK

    def _hex_to_curses_color(self, value: Optional[str]) -> int:
        if not value:
            return self._default_color
        value = value.strip().lstrip("#")
        if len(value) != 6:
            return self._default_color
        try:
            red = int(value[0:2], 16)
            green = int(value[2:4], 16)
            blue = int(value[4:6], 16)
        except ValueError:
            return self._default_color

        colors = getattr(self.curses, "COLORS", 0) or 0
        if colors >= 256:
            return self._rgb_to_xterm256(red, green, blue)
        return self._nearest_ansi8(red, green, blue)

    def _rgb_to_xterm256(self, red: int, green: int, blue: int) -> int:
        if max(red, green, blue) - min(red, green, blue) < 12:
            if red < 8:
                return 16
            if red > 248:
                return 231
            return 232 + int(round((red - 8) / 247 * 23))
        r = int(round(red / 255 * 5))
        g = int(round(green / 255 * 5))
        b = int(round(blue / 255 * 5))
        return 16 + 36 * r + 6 * g + b

    def _nearest_ansi8(self, red: int, green: int, blue: int) -> int:
        palette = [
            (self.curses.COLOR_BLACK, (0, 0, 0)),
            (self.curses.COLOR_RED, (205, 49, 49)),
            (self.curses.COLOR_GREEN, (13, 188, 121)),
            (self.curses.COLOR_YELLOW, (229, 229, 16)),
            (self.curses.COLOR_BLUE, (36, 114, 200)),
            (self.curses.COLOR_MAGENTA, (188, 63, 188)),
            (self.curses.COLOR_CYAN, (17, 168, 205)),
            (self.curses.COLOR_WHITE, (229, 229, 229)),
        ]
        best = min(palette, key=lambda item: (red - item[1][0]) ** 2 + (green - item[1][1]) ** 2 + (blue - item[1][2]) ** 2)
        return int(best[0])

    def _color_pair_attr(self, fg: int, bg: int) -> int:
        if not self._colors_ready:
            self._setup_colors()
        if not (getattr(self.curses, "COLORS", 0) or 0):
            return 0
        key = (fg, bg)
        pair = self._pair_cache.get(key)
        if pair is None:
            max_pairs = max(1, getattr(self.curses, "COLOR_PAIRS", 64) or 64)
            if self._next_color_pair >= max_pairs:
                return 0
            pair = self._next_color_pair
            self._next_color_pair += 1
            try:
                self.curses.init_pair(pair, fg, bg)
            except self.curses.error:
                return 0
            self._pair_cache[key] = pair
        try:
            return int(self.curses.color_pair(pair))
        except self.curses.error:
            return 0

    def _attr_for_token(self, token_type: Any) -> int:
        cache_key = (self.options.pygments_style, token_type)
        cached = self._attr_cache.get(cache_key)
        if cached is not None:
            return cached
        style = pygments_style_for_token(self.options.pygments_style, token_type)
        attr = 0
        fg = self._hex_to_curses_color(style.get("color")) if style.get("color") else self._default_color
        bg = self._hex_to_curses_color(style.get("bgcolor")) if style.get("bgcolor") else self._default_color
        if fg != self._default_color or bg != self._default_color:
            attr |= self._color_pair_attr(fg, bg)
        if style.get("bold"):
            attr |= getattr(self.curses, "A_BOLD", 0)
        if style.get("underline"):
            attr |= getattr(self.curses, "A_UNDERLINE", 0)
        # Most curses builds have no italic attribute, so italic is ignored.
        self._attr_cache[cache_key] = attr
        return attr

    def invalidate_highlight(self, *, clear_style: bool = False) -> None:
        self._highlight_cache_key = None
        self._highlight_segments_by_line = {}
        if clear_style:
            self._attr_cache.clear()

    def _ensure_highlight_cache(self) -> None:
        text = self._text()
        key = (
            text,
            str(self.path) if self.path is not None else None,
            self.force_lexer_alias,
            self.options.highlight,
            self.options.pygments_style,
            self.options.max_highlight_chars,
        )
        if key == self._highlight_cache_key:
            return
        self._highlight_cache_key = key
        self._highlight_segments_by_line = {}
        try:
            result = compute_highlight_spans(
                text,
                path=self.path,
                options=self.options,
                force_lexer_alias=self.force_lexer_alias,
            )
            self.force_lexer_alias = result.force_lexer_alias
            self.current_lexer_name = result.lexer_name
            self._highlight_segments_by_line = self._build_highlight_segments(result.spans)
            self.emit("after_highlight", self.current_lexer_name)
        except Exception:
            self.current_lexer_name = "Highlight error"
            self._highlight_segments_by_line = {}

    def _build_highlight_segments(self, spans: List[HighlightSpan]) -> Dict[int, List[tuple[int, int, int]]]:
        if not spans:
            return {}
        line_starts: List[int] = []
        offset = 0
        for line in self.lines:
            line_starts.append(offset)
            offset += len(line) + 1
        segments: Dict[int, List[tuple[int, int, int]]] = {}
        for span in spans:
            start_line = max(0, bisect.bisect_right(line_starts, span.start) - 1)
            end_line = max(0, bisect.bisect_right(line_starts, max(span.start, span.end - 1)) - 1)
            attr = self._attr_for_token(span.token_type)
            if attr == 0:
                continue
            for line_index in range(start_line, min(end_line, len(self.lines) - 1) + 1):
                line_start = line_starts[line_index]
                line_len = len(self.lines[line_index])
                seg_start = max(0, span.start - line_start)
                seg_end = min(line_len, span.end - line_start)
                if seg_end > seg_start:
                    segments.setdefault(line_index, []).append((seg_start, seg_end, attr))
        return segments

    # ----- Commands, customization and options ------------------------------

    def _register_builtin_commands(self) -> None:
        builtin: Dict[str, Callable[[], Any]] = {
            "app.exit": self.quit_editor,
            "backward-char": self.backward_char,
            "backward-word": self.backward_word,
            "beginning-of-buffer": self.beginning_of_buffer,
            "beginning-of-line": self.beginning_of_line,
            "copy-region-as-kill": self.copy_region,
            "custom.open_config": self.open_user_config,
            "custom.reload": lambda: self.load_customizations(silent=False),
            "custom.run_buffer": self.run_buffer_as_customization,
            "delete-char": self.delete_char,
            "display-line-numbers-mode": self.toggle_line_numbers,
            "edit.copy": self.copy_region,
            "edit.cut": self.kill_region,
            "edit.delete": self.delete_char,
            "edit.paste": self.yank,
            "edit.select_all": self.mark_whole_buffer,
            "edit.undo": self.undo,
            "end-of-buffer": self.end_of_buffer,
            "end-of-line": self.end_of_line,
            "exchange-point-and-mark": self.exchange_point_and_mark,
            "execute-extended-command": self.command_prompt,
            "file.open": self.find_file,
            "file.save": self.save_buffer,
            "file.save_as": self.write_file,
            "find-file": self.find_file,
            "forward-char": self.forward_char,
            "forward-word": self.forward_word,
            "goto-line": self.goto_line_prompt,
            "help": self.show_help,
            "keyboard-quit": lambda: self.set_status_message("Quit"),
            "kill-emacs": self.quit_editor,
            "kill-line": self.kill_line,
            "kill-region": self.kill_region,
            "mark-whole-buffer": self.mark_whole_buffer,
            "next-line": self.next_line,
            "open-line": self.open_line,
            "previous-line": self.previous_line,
            "recenter-top-bottom": self.recenter,
            "save-buffer": self.save_buffer,
            "scroll-down-command": self.page_up,
            "scroll-up-command": self.page_down,
            "search-backward": lambda: self.search_prompt(backward=True),
            "search.find": lambda: self.search_prompt(backward=False),
            "search.goto_line": self.goto_line_prompt,
            "search-forward": lambda: self.search_prompt(backward=False),
            "set-mark-command": self.set_mark,
            "syntax.auto": self.auto_detect_syntax,
            "syntax.set_alias": self.set_syntax_alias_prompt,
            "undo": self.undo,
            "view.set_style": self.set_style_prompt,
            "view.toggle_highlight": self.toggle_highlight,
            "write-file": self.write_file,
            "yank": self.yank,
        }
        for name, fn in builtin.items():
            self.add_command(name, fn)

    def add_command(self, name: str, callback: Callable[..., Any], *, custom: bool = False) -> None:
        if custom:
            self.custom_commands.add(name)

        def wrapper(cb: Callable[..., Any] = callback, is_custom: bool = custom) -> Any:
            return self._call_custom(cb) if is_custom else cb()

        self.commands[name] = wrapper

    def run_command(self, name: str) -> Any:
        callback = self.commands.get(name)
        if callback is None:
            self.set_status_message(f"Unknown command: {name}")
            return None
        try:
            return callback()
        except Exception:
            self.report_exception(f"Command failed: {name}")
            return None

    def _call_custom(self, callback: Callable[..., Any], *extra_args: Any) -> Any:
        return _call_flexibly(callback, self.api, *extra_args)

    def add_custom_key_binding(self, sequence: str, callback: Callable[..., Any]) -> None:
        key = self._parse_key_sequence(sequence)
        if key is None:
            self.set_status_message(f"Unsupported --nox key binding: {sequence}")
            return
        self.custom_key_bindings[key] = callback

    def _parse_key_sequence(self, sequence: str) -> Optional[tuple[str, Any]]:
        raw = sequence.strip()
        inner = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
        normalized = inner.replace("_", "-").strip()
        lower = normalized.lower()
        if lower.startswith(("control-", "ctrl-", "c-")):
            name = normalized.split("-", 1)[1]
            name_lower = name.lower()
            if name_lower in {"space", "spc", "@"}:
                return "char", self.CTRL_SPACE
            if name_lower in {"delete", "del", "?"}:
                return "char", self.DEL
            if len(name) == 1:
                return "char", chr(ord(name.upper()) & 0x1F)
        if lower.startswith(("meta-", "alt-", "m-")):
            name = normalized.split("-", 1)[1]
            if len(name) == 1:
                return "meta", name
        if lower.startswith("f") and lower[1:].isdigit():
            number = int(lower[1:])
            try:
                return "key", self.curses.KEY_F(number)
            except Exception:
                f0 = getattr(self.curses, "KEY_F0", None)
                if f0 is not None:
                    return "key", f0 + number
        special = {
            "escape": ("esc", None),
            "esc": ("esc", None),
            "return": ("char", "\n"),
            "enter": ("char", "\n"),
            "tab": ("char", "\t"),
            "space": ("char", " "),
            "backspace": ("char", self.DEL),
            "delete": ("key", self.curses.KEY_DC),
            "left": ("key", self.curses.KEY_LEFT),
            "right": ("key", self.curses.KEY_RIGHT),
            "up": ("key", self.curses.KEY_UP),
            "down": ("key", self.curses.KEY_DOWN),
            "home": ("key", self.curses.KEY_HOME),
            "end": ("key", self.curses.KEY_END),
            "prior": ("key", self.curses.KEY_PPAGE),
            "next": ("key", self.curses.KEY_NPAGE),
        }
        if lower in special:
            return special[lower]
        if len(raw) == 1:
            return "char", raw
        return None

    def _run_custom_key_binding(self, kind: str, key: Any) -> bool:
        callback = self.custom_key_bindings.get((kind, key))
        if callback is None:
            return False
        try:
            event = NoxKeyEvent(kind=kind, key=key, sequence=self._key_name(kind, key))
            result = _call_flexibly(callback, self.api, event)
            if result is not None:
                self.message = str(result) if result != "break" else ""
        except Exception:
            self.report_exception(f"Custom key binding failed: {self._key_name(kind, key)}")
        return True

    def load_customizations(self, *, silent: bool) -> None:
        for name in list(self.custom_commands):
            self.commands.pop(name, None)
        self.custom_commands.clear()
        self.custom_key_bindings.clear()

        loaded: List[Path] = []
        errors: List[str] = []
        for path in unique_paths(self.config_paths):
            if not path.exists():
                continue
            try:
                self.execute_customization_file(path)
                loaded.append(path)
            except Exception:
                errors.append(f"{path}\n{traceback.format_exc()}")
        if errors:
            self.set_status_message("Customization error: " + errors[-1].splitlines()[-1])
            if os.environ.get("SIMPLEPYPAD_NOX_DEBUG"):
                Path("simplepypad-nox-customization-error.log").write_text("\n".join(errors), encoding="utf-8")
        elif not silent:
            if loaded:
                self.set_status_message("Reloaded customization: " + ", ".join(str(p) for p in loaded))
            else:
                self.set_status_message("No customization file found")

    def execute_customization_file(self, path: Path) -> None:
        code = path.read_text(encoding="utf-8")
        namespace = {
            "api": self.api,
            "editor": self.api,
            "Path": Path,
            "datetime": _dt,
            "tk": tk,
            "__file__": str(path),
            "__name__": "simplepypad_user_config",
        }
        exec(compile(code, str(path), "exec"), namespace)

    def run_buffer_as_customization(self) -> None:
        code = self._text()
        filename = str(self.path) if self.path else "<current buffer>"
        namespace = {
            "api": self.api,
            "editor": self.api,
            "Path": Path,
            "datetime": _dt,
            "tk": tk,
            "__file__": filename,
            "__name__": "simplepypad_buffer_customization",
        }
        try:
            exec(compile(code, filename, "exec"), namespace)
            self.set_status_message("Customization buffer executed")
        except Exception:
            self.report_exception("Customization buffer failed")

    def open_user_config(self) -> None:
        if not self._confirm_discard_or_save():
            return
        path = default_user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
        self._read_file_into_buffer(path)

    def set_option(self, name: str, value: Any) -> None:
        if not hasattr(self.options, name):
            raise AttributeError(f"unknown option: {name}")
        setattr(self.options, name, value)
        if name in {"highlight", "pygments_style", "max_highlight_chars"}:
            self.invalidate_highlight(clear_style=name == "pygments_style")
        self.set_status_message(f"{name} = {value}")

    def toggle_highlight(self) -> None:
        self.set_option("highlight", not self.options.highlight)

    def set_style_prompt(self) -> None:
        value = self.prompt("Pygments style: ", initial=self.options.pygments_style)
        if value is None or not value.strip():
            self.set_status_message("Style change canceled")
            return
        self.set_option("pygments_style", value.strip())

    def auto_detect_syntax(self) -> None:
        self.force_lexer_alias = None
        self.invalidate_highlight(clear_style=True)
        self.set_status_message("Syntax auto-detect")

    def set_syntax_alias_prompt(self) -> None:
        value = self.prompt("Pygments alias, empty for auto: ", initial=self.force_lexer_alias or "")
        if value is None:
            self.set_status_message("Syntax change canceled")
            return
        self.force_lexer_alias = value.strip() or None
        self.invalidate_highlight(clear_style=True)
        self.set_status_message("Syntax auto" if self.force_lexer_alias is None else f"Syntax: {self.force_lexer_alias}")

    def set_status_message(self, message: str) -> None:
        self.message = message

    def emit(self, event_name: str, *args: Any) -> None:
        for callback in list(self.hooks.get(event_name, [])):
            try:
                _call_flexibly(callback, self.api, *args)
            except Exception:
                self.report_exception(f"Hook failed: {event_name}")

    def report_exception(self, title: str) -> None:
        detail = traceback.format_exc().strip().splitlines()[-1]
        self.set_status_message(f"{title}: {detail}")
        if os.environ.get("SIMPLEPYPAD_NOX_DEBUG"):
            Path("simplepypad-nox-error.log").write_text(title + "\n\n" + traceback.format_exc(), encoding="utf-8")

    # ----- Text model ------------------------------------------------------

    def _text(self) -> str:
        return _nox_lines_to_text(self.lines)

    def _set_text_and_point(self, text: str, offset: int) -> None:
        self.lines = _nox_text_to_lines(text)
        if not self.lines:
            self.lines = [""]
        self.row, self.col = self._offset_to_point(offset)
        self._clamp_point()
        self.preferred_col = None
        self.invalidate_highlight()

    def set_text_content(self, value: str, *, dirty: bool = True) -> None:
        self._push_undo()
        offset = min(self._point_to_offset(), len(value))
        self._set_text_and_point(value, offset)
        if not dirty:
            self.clean_text = value
        self.mark = None
        self.emit("text_changed")

    def goto_index(self, index: str) -> None:
        value = str(index).strip()
        if value in {"insert", "point", "cursor"}:
            return
        if value in {"end", "end-1c"}:
            self.end_of_buffer()
            return
        if value.startswith("line:"):
            try:
                line_number = int(value.split(":", 1)[1])
            except ValueError:
                return
            self.row = max(0, min(line_number - 1, len(self.lines) - 1))
            self.col = min(self.col, len(self.lines[self.row]))
            self.preferred_col = None
            return
        if "." in value:
            line_text, col_text = value.split(".", 1)
            try:
                line_index = max(0, int(line_text) - 1)
            except ValueError:
                return
            line_index = min(line_index, len(self.lines) - 1)
            if col_text == "end":
                col_index = len(self.lines[line_index])
            else:
                try:
                    col_index = int(col_text)
                except ValueError:
                    return
            self.row = line_index
            self.col = max(0, min(col_index, len(self.lines[self.row])))
            self.preferred_col = None
            return
        try:
            offset = int(value)
        except ValueError:
            return
        self.row, self.col = self._offset_to_point(offset)
        self.preferred_col = None

    def _point_to_offset(self, point: Optional[tuple[int, int]] = None) -> int:
        if point is None:
            row, col = self.row, self.col
        else:
            row, col = point
        row = max(0, min(row, len(self.lines) - 1))
        col = max(0, min(col, len(self.lines[row])))
        offset = 0
        for index in range(row):
            offset += len(self.lines[index]) + 1
        return offset + col

    def _offset_to_point(self, offset: int) -> tuple[int, int]:
        text_length = len(self._text())
        offset = max(0, min(offset, text_length))
        running = 0
        for index, line in enumerate(self.lines):
            end = running + len(line)
            if offset <= end:
                return index, offset - running
            running = end + 1
        return len(self.lines) - 1, len(self.lines[-1])

    def _clamp_point(self) -> None:
        if not self.lines:
            self.lines = [""]
        self.row = max(0, min(self.row, len(self.lines) - 1))
        self.col = max(0, min(self.col, len(self.lines[self.row])))

    def _push_undo(self) -> None:
        snapshot = (self._text(), self.row, self.col, self.mark)
        if not self.undo_stack or self.undo_stack[-1] != snapshot:
            self.undo_stack.append(snapshot)
            if len(self.undo_stack) > 200:
                del self.undo_stack[0]

    def undo(self) -> None:
        if not self.undo_stack:
            self.message = "No further undo information"
            return
        text, row, col, mark = self.undo_stack.pop()
        self.lines = _nox_text_to_lines(text)
        if not self.lines:
            self.lines = [""]
        self.row = row
        self.col = col
        self.mark = mark
        self._clamp_point()
        self.invalidate_highlight()
        self.message = "Undo"
        self.emit("text_changed")

    # ----- Rendering -------------------------------------------------------

    def _text_height(self) -> int:
        height, _ = self.stdscr.getmaxyx()
        return max(1, height - 2)

    def _gutter_width(self, width: Optional[int] = None) -> int:
        if not self.show_line_numbers:
            return 0
        if width is not None and width < 12:
            return 0
        return max(4, len(str(max(1, len(self.lines)))) + 1)

    def _text_width(self, width: int) -> int:
        return max(1, width - self._gutter_width(width))

    def _ensure_cursor_visible(self) -> None:
        height, width = self.stdscr.getmaxyx()
        text_height = max(1, height - 2)
        usable_width = self._text_width(width)
        if self.row < self.top_line:
            self.top_line = self.row
        elif self.row >= self.top_line + text_height:
            self.top_line = self.row - text_height + 1
        if self.col < self.left_col:
            self.left_col = self.col
        elif self.col >= self.left_col + usable_width:
            self.left_col = self.col - usable_width + 1
        self.top_line = max(0, self.top_line)
        self.left_col = max(0, self.left_col)

    def _addnstr(self, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
        if width <= 0:
            return
        try:
            self.stdscr.addnstr(y, x, text, width, attr)
        except self.curses.error:
            pass

    def _render_text_line(self, screen_row: int, x0: int, file_row: int, line: str, width: int) -> None:
        visible_start = self.left_col
        visible_end = self.left_col + width
        segments = self._highlight_segments_by_line.get(file_row, [])
        if not segments:
            self._addnstr(screen_row, x0, line[visible_start:visible_end], width)
            return

        x = x0
        pos = visible_start
        for seg_start, seg_end, attr in sorted(segments):
            if seg_end <= visible_start:
                continue
            if seg_start >= visible_end:
                break
            plain_end = min(seg_start, visible_end)
            if pos < plain_end:
                plain = line[pos:plain_end]
                self._addnstr(screen_row, x, plain, width - (x - x0))
                x += len(plain)
                pos = plain_end
            color_start = max(seg_start, visible_start, pos)
            color_end = min(seg_end, visible_end)
            if color_start < color_end:
                chunk = line[color_start:color_end]
                self._addnstr(screen_row, x, chunk, width - (x - x0), attr)
                x += len(chunk)
                pos = color_end
        if pos < visible_end:
            self._addnstr(screen_row, x, line[pos:visible_end], width - (x - x0))

    def _refresh(self, prompt: Optional[str] = None, prompt_cursor: Optional[int] = None) -> None:
        self._clamp_point()
        self._ensure_cursor_visible()
        height, width = self.stdscr.getmaxyx()
        text_height = max(1, height - 2)
        self.stdscr.erase()
        self._ensure_highlight_cache()

        gutter_width = self._gutter_width(width)
        text_width = self._text_width(width)
        gutter_attr = getattr(self.curses, "A_DIM", 0)
        for screen_row in range(text_height):
            file_row = self.top_line + screen_row
            if file_row >= len(self.lines):
                break
            line = self.lines[file_row]
            if gutter_width:
                gutter = f"{file_row + 1:>{gutter_width - 1}} "
                self._addnstr(screen_row, 0, gutter, gutter_width, gutter_attr)
            self._render_text_line(screen_row, gutter_width, file_row, line, text_width)

        mode = self._mode_line()
        self._addnstr(text_height, 0, mode.ljust(width), width, self.curses.A_REVERSE)

        if prompt is not None:
            cursor_abs = max(0, prompt_cursor or 0)
            start = max(0, cursor_abs - width + 1)
            shown = prompt[start:start + width]
            self._addnstr(text_height + 1, 0, shown.ljust(width), width)
            cursor_y = text_height + 1
            cursor_x = max(0, min(width - 1, cursor_abs - start))
        else:
            message = self.message or ""
            self._addnstr(text_height + 1, 0, message.ljust(width), width)
            cursor_y = self.row - self.top_line
            cursor_x = gutter_width + self.col - self.left_col
            cursor_y = max(0, min(text_height - 1, cursor_y))
            cursor_x = max(0, min(width - 1, cursor_x))

        try:
            self.stdscr.move(cursor_y, cursor_x)
        except self.curses.error:
            pass
        self.stdscr.refresh()

    def _mode_line(self) -> str:
        path = str(self.path) if self.path is not None else "<new buffer>"
        dirty = "**" if self.dirty else "--"
        mark = " Mark" if self._has_region() else ""
        syntax = self.current_lexer_name or "Text"
        return f"-{dirty}- {APP_NAME} --nox  {path}  ({syntax})  Ln {self.row + 1}, Col {self.col + 1}{mark}  {self.encoding}"

    def show_help(self) -> None:
        help_lines = NOX_HELP.strip("\n").splitlines()
        top = 0
        while True:
            height, width = self.stdscr.getmaxyx()
            body_height = max(1, height - 1)
            self.stdscr.erase()
            for index in range(body_height):
                line_index = top + index
                if line_index >= len(help_lines):
                    break
                self._addnstr(index, 0, help_lines[line_index], width)
            footer = "Help: Space/C-v next, M-v previous, q or C-g close"
            self._addnstr(height - 1, 0, footer.ljust(width), width, self.curses.A_REVERSE)
            self.stdscr.refresh()
            kind, key = self._read_key()
            if self._is_cancel_key(kind, key) or (kind == "char" and key in {"q", "Q", "\r", "\n"}):
                self.message = "Help closed"
                return
            if (kind == "char" and key in {" ", self.CTRL_V}) or (kind == "key" and key == self.curses.KEY_NPAGE):
                top = min(max(0, len(help_lines) - body_height), top + body_height)
            elif (kind == "meta" and key == "v") or (kind == "key" and key == self.curses.KEY_PPAGE):
                top = max(0, top - body_height)
            elif kind == "key" and key == self.curses.KEY_DOWN:
                top = min(max(0, len(help_lines) - body_height), top + 1)
            elif kind == "key" and key == self.curses.KEY_UP:
                top = max(0, top - 1)

    # ----- Key reading -----------------------------------------------------

    def _read_key(self) -> tuple[str, Any]:
        while True:
            try:
                key = self.stdscr.get_wch()
            except KeyboardInterrupt:
                # Some pseudo-terminals still surface C-c as SIGINT even after
                # curses.raw().  Keep C-x C-c working instead of treating it as
                # C-g, because this mode is supposed to be Emacs-like, not
                # a plain interrupt.
                return "char", self.CTRL_C
            except self.curses.error:
                continue
            if isinstance(key, str) and key == self.ESC:
                try:
                    self.stdscr.timeout(90)
                    nxt = self.stdscr.get_wch()
                except self.curses.error:
                    return "esc", None
                finally:
                    try:
                        self.stdscr.timeout(-1)
                    except self.curses.error:
                        pass
                if isinstance(nxt, str):
                    return "meta", nxt
                return "meta_key", nxt
            if isinstance(key, str):
                return "char", key
            return "key", key

    def _is_cancel_key(self, kind: str, key: Any) -> bool:
        return (kind == "char" and key == self.CTRL_G) or kind == "esc"

    def _is_backspace_key(self, kind: str, key: Any) -> bool:
        if kind == "key" and key == self.curses.KEY_BACKSPACE:
            return True
        return kind == "char" and key in {self.DEL, "\b"}

    def _is_enter_key(self, kind: str, key: Any) -> bool:
        return (kind == "char" and key in {"\n", "\r"}) or (kind == "key" and key == self.curses.KEY_ENTER)

    def _is_f1_key(self, kind: str, key: Any) -> bool:
        if kind != "key":
            return False
        candidates = []
        for name in ("KEY_F1", "KEY_HELP"):
            value = getattr(self.curses, name, None)
            if value is not None:
                candidates.append(value)
        f0 = getattr(self.curses, "KEY_F0", None)
        if f0 is not None:
            candidates.append(f0 + 1)
        return key in candidates

    def _is_printable_char(self, kind: str, key: Any) -> bool:
        return kind == "char" and isinstance(key, str) and len(key) == 1 and key.isprintable()

    # ----- Main key dispatch ----------------------------------------------

    def _handle_key(self, kind: str, key: Any) -> None:
        if self.prefix == "C-x":
            self.prefix = None
            self._handle_c_x_key(kind, key)
            return

        if self._is_cancel_key(kind, key):
            self.prefix = None
            self.mark = None
            self.message = "Quit"
            return

        if self._run_custom_key_binding(kind, key):
            return

        if kind == "char" and key == self.CTRL_X:
            self.prefix = "C-x"
            self.message = "C-x"
            return

        if kind == "meta":
            self._handle_meta_key(key)
            return

        if kind == "meta_key":
            if key == self.curses.KEY_PPAGE:
                self.page_up()
            else:
                self.message = "Unknown Meta key"
            return

        if kind == "key":
            self._handle_special_key(key)
            return

        if kind != "char":
            return

        if key == self.CTRL_A:
            self.beginning_of_line()
        elif key == self.CTRL_B:
            self.backward_char()
        elif key == self.CTRL_D:
            self.delete_char()
        elif key == self.CTRL_E:
            self.end_of_line()
        elif key == self.CTRL_F:
            self.forward_char()
        elif key == self.CTRL_K:
            self.kill_line()
        elif key == self.CTRL_L:
            self.recenter()
        elif key == self.CTRL_N:
            self.next_line()
        elif key == self.CTRL_O:
            self.open_line()
        elif key == self.CTRL_P:
            self.previous_line()
        elif key == self.CTRL_R:
            self.search_prompt(backward=True)
        elif key == self.CTRL_S:
            self.search_prompt(backward=False)
        elif key == self.CTRL_V:
            self.page_down()
        elif key == self.CTRL_W:
            self.kill_region()
        elif key == self.CTRL_Y:
            self.yank()
        elif key in {self.CTRL_UNDO}:
            self.undo()
        elif key == self.CTRL_SPACE:
            self.set_mark()
        elif self._is_enter_key(kind, key):
            self.insert_newline()
        elif self._is_backspace_key(kind, key):
            self.backspace()
        elif key == "\t":
            self.insert_string((" " * self.options.tab_width) if self.options.indent_with_spaces else "\t")
        elif self._is_printable_char(kind, key):
            self.insert_string(key)
        else:
            self.message = self._describe_unhandled_key(kind, key)

    def _handle_special_key(self, key: Any) -> None:
        if key == self.curses.KEY_LEFT:
            self.backward_char()
        elif key == self.curses.KEY_RIGHT:
            self.forward_char()
        elif key == self.curses.KEY_UP:
            self.previous_line()
        elif key == self.curses.KEY_DOWN:
            self.next_line()
        elif key == self.curses.KEY_HOME:
            self.beginning_of_line()
        elif key == self.curses.KEY_END:
            self.end_of_line()
        elif key == self.curses.KEY_NPAGE:
            self.page_down()
        elif key == self.curses.KEY_PPAGE:
            self.page_up()
        elif key == self.curses.KEY_DC:
            self.delete_char()
        elif self._is_f1_key("key", key):
            self.show_help()
        elif self._is_backspace_key("key", key):
            self.backspace()
        elif self._is_enter_key("key", key):
            self.insert_newline()
        else:
            self.message = self._describe_unhandled_key("key", key)

    def _handle_c_x_key(self, kind: str, key: Any) -> None:
        if kind == "char" and key == self.CTRL_S:
            self.save_buffer()
        elif kind == "char" and key == self.CTRL_W:
            self.write_file()
        elif kind == "char" and key == self.CTRL_F:
            self.find_file()
        elif kind == "char" and key == self.CTRL_C:
            self.quit_editor()
        elif kind == "char" and key == self.CTRL_X:
            self.exchange_point_and_mark()
        elif kind == "char" and key in {"h", "H"}:
            self.mark_whole_buffer()
        elif kind == "char" and key in {"?"}:
            self.show_help()
        elif kind == "char" and key in {"u", "U", self.CTRL_UNDO}:
            self.undo()
        elif self._is_cancel_key(kind, key):
            self.message = "Quit"
        else:
            self.message = f"Unknown C-x binding: {self._key_name(kind, key)}"

    def _handle_meta_key(self, key: Any) -> None:
        if key in {"x", "X"}:
            self.command_prompt()
        elif key in {"v", "V"}:
            self.page_up()
        elif key == "<":
            self.beginning_of_buffer()
        elif key == ">":
            self.end_of_buffer()
        elif key in {"f", "F"}:
            self.forward_word()
        elif key in {"b", "B"}:
            self.backward_word()
        elif key in {"w", "W"}:
            self.copy_region()
        elif key in {"g", "G"}:
            kind, second = self._read_key()
            if kind == "char" and second in {"g", "G"}:
                self.goto_line_prompt()
            elif self._is_cancel_key(kind, second):
                self.message = "Quit"
            else:
                self.message = "Use M-g g for goto-line"
        else:
            self.message = f"Unknown Meta binding: M-{key}"

    def _describe_unhandled_key(self, kind: str, key: Any) -> str:
        return f"Unhandled key: {self._key_name(kind, key)}"

    def _key_name(self, kind: str, key: Any) -> str:
        if kind == "char" and isinstance(key, str):
            code = ord(key)
            if code < 32:
                return "C-" + chr(code + 64)
            if code == 127:
                return "DEL"
            return key
        if kind == "meta" and isinstance(key, str):
            return "M-" + key
        return str(key)

    # ----- Movement --------------------------------------------------------

    def _moved(self) -> None:
        self._clamp_point()

    def beginning_of_line(self) -> None:
        self.col = 0
        self.preferred_col = None
        self._moved()

    def end_of_line(self) -> None:
        self.col = len(self.lines[self.row])
        self.preferred_col = None
        self._moved()

    def forward_char(self) -> None:
        if self.col < len(self.lines[self.row]):
            self.col += 1
        elif self.row < len(self.lines) - 1:
            self.row += 1
            self.col = 0
        self.preferred_col = None
        self._moved()

    def backward_char(self) -> None:
        if self.col > 0:
            self.col -= 1
        elif self.row > 0:
            self.row -= 1
            self.col = len(self.lines[self.row])
        self.preferred_col = None
        self._moved()

    def next_line(self) -> None:
        if self.preferred_col is None:
            self.preferred_col = self.col
        if self.row < len(self.lines) - 1:
            self.row += 1
        self.col = min(self.preferred_col, len(self.lines[self.row]))
        self._moved()

    def previous_line(self) -> None:
        if self.preferred_col is None:
            self.preferred_col = self.col
        if self.row > 0:
            self.row -= 1
        self.col = min(self.preferred_col, len(self.lines[self.row]))
        self._moved()

    def beginning_of_buffer(self) -> None:
        self.row = 0
        self.col = 0
        self.preferred_col = None
        self._moved()

    def end_of_buffer(self) -> None:
        self.row = len(self.lines) - 1
        self.col = len(self.lines[self.row])
        self.preferred_col = None
        self._moved()

    def page_down(self) -> None:
        amount = self._text_height()
        self.row = min(len(self.lines) - 1, self.row + amount)
        self.top_line = min(max(0, len(self.lines) - 1), self.top_line + amount)
        self.col = min(self.col, len(self.lines[self.row]))
        self.preferred_col = None
        self._moved()

    def page_up(self) -> None:
        amount = self._text_height()
        self.row = max(0, self.row - amount)
        self.top_line = max(0, self.top_line - amount)
        self.col = min(self.col, len(self.lines[self.row]))
        self.preferred_col = None
        self._moved()

    def recenter(self) -> None:
        self.top_line = max(0, self.row - self._text_height() // 2)
        self.message = "Recentered"

    def toggle_line_numbers(self) -> None:
        self.show_line_numbers = not self.show_line_numbers
        self.left_col = max(0, self.left_col)
        self.message = "Line numbers on" if self.show_line_numbers else "Line numbers off"

    def _is_word_char(self, ch: str) -> bool:
        return ch.isalnum() or ch == "_"

    def forward_word(self) -> None:
        text = self._text()
        offset = self._point_to_offset()
        while offset < len(text) and not self._is_word_char(text[offset]):
            offset += 1
        while offset < len(text) and self._is_word_char(text[offset]):
            offset += 1
        self.row, self.col = self._offset_to_point(offset)
        self.preferred_col = None

    def backward_word(self) -> None:
        text = self._text()
        offset = self._point_to_offset()
        while offset > 0 and not self._is_word_char(text[offset - 1]):
            offset -= 1
        while offset > 0 and self._is_word_char(text[offset - 1]):
            offset -= 1
        self.row, self.col = self._offset_to_point(offset)
        self.preferred_col = None

    # ----- Editing ---------------------------------------------------------

    def _has_region(self) -> bool:
        return self.mark is not None and self.mark != (self.row, self.col)

    def _region_offsets(self) -> tuple[int, int]:
        if self.mark is None:
            current = self._point_to_offset()
            return current, current
        start = self._point_to_offset(self.mark)
        end = self._point_to_offset()
        if end < start:
            start, end = end, start
        return start, end

    def insert_string(self, value: str) -> None:
        if not value:
            return
        self._push_undo()
        text = self._text()
        if self._has_region():
            start, end = self._region_offsets()
        else:
            start = end = self._point_to_offset()
        new_text = text[:start] + value + text[end:]
        self.mark = None
        self._set_text_and_point(new_text, start + len(value))
        self.message = ""
        self.emit("text_changed")

    def insert_newline(self) -> None:
        indent = ""
        if self.options.auto_indent:
            current = self.lines[self.row]
            indent = current[: len(current) - len(current.lstrip(" \t"))]
        self.insert_string("\n" + indent)

    def open_line(self) -> None:
        self.insert_string("\n")
        self.backward_char()
        self.message = "Opened line"

    def delete_char(self) -> None:
        if self._has_region():
            self.kill_region(delete_only=True)
            return
        text = self._text()
        start = self._point_to_offset()
        if start >= len(text):
            self.message = "End of buffer"
            return
        self._push_undo()
        self._set_text_and_point(text[:start] + text[start + 1:], start)
        self.mark = None
        self.message = ""
        self.emit("text_changed")

    def backspace(self) -> None:
        if self._has_region():
            self.kill_region(delete_only=True)
            return
        text = self._text()
        end = self._point_to_offset()
        if end <= 0:
            self.message = "Beginning of buffer"
            return
        self._push_undo()
        self._set_text_and_point(text[:end - 1] + text[end:], end - 1)
        self.mark = None
        self.message = ""
        self.emit("text_changed")

    def kill_line(self) -> None:
        if self._has_region():
            self.kill_region()
            return
        text = self._text()
        start = self._point_to_offset()
        if start >= len(text):
            self.message = "End of buffer"
            return
        newline = text.find("\n", start)
        if newline == -1:
            end = len(text)
        elif newline == start:
            end = newline + 1
        else:
            end = newline
        killed = text[start:end]
        if not killed:
            self.message = "Nothing killed"
            return
        self._push_undo()
        self.kill_ring = killed
        self._set_text_and_point(text[:start] + text[end:], start)
        self.mark = None
        self.message = "Killed line"
        self.emit("text_changed")

    def yank(self) -> None:
        if not self.kill_ring:
            self.message = "Kill ring is empty"
            return
        self.insert_string(self.kill_ring)
        self.message = "Yanked"

    def set_mark(self) -> None:
        self.mark = (self.row, self.col)
        self.message = "Mark set"

    def mark_whole_buffer(self) -> None:
        self.mark = (0, 0)
        self.end_of_buffer()
        self.message = "Buffer marked"

    def exchange_point_and_mark(self) -> None:
        if self.mark is None:
            self.message = "No mark set"
            return
        old = (self.row, self.col)
        self.row, self.col = self.mark
        self.mark = old
        self._clamp_point()
        self.message = "Point and mark exchanged"

    def copy_region(self) -> None:
        if not self._has_region():
            self.message = "No active region"
            return
        start, end = self._region_offsets()
        self.kill_ring = self._text()[start:end]
        self.message = "Copied region"

    def kill_region(self, *, delete_only: bool = False) -> None:
        if not self._has_region():
            self.message = "No active region"
            return
        text = self._text()
        start, end = self._region_offsets()
        killed = text[start:end]
        self._push_undo()
        if not delete_only:
            self.kill_ring = killed
        self._set_text_and_point(text[:start] + text[end:], start)
        self.mark = None
        self.message = "Deleted region" if delete_only else "Killed region"
        self.emit("text_changed")

    # ----- Files -----------------------------------------------------------

    def _read_file_into_buffer(self, target: Path) -> None:
        if target.exists():
            text, encoding = read_text_with_fallback(target)
            self.encoding = encoding
            self.message = f"Opened {target}"
        else:
            text = ""
            self.encoding = self.options.default_encoding
            self.message = f"New file: {target}"
        self.path = target
        self.lines = _nox_text_to_lines(text)
        self.clean_text = text
        self.row = 0
        self.col = 0
        self.top_line = 0
        self.left_col = 0
        self.mark = None
        self.undo_stack.clear()
        self._clamp_point()
        self.invalidate_highlight(clear_style=True)
        self.emit("after_open", target)

    def save_buffer(self) -> bool:
        if self.path is None:
            return self.write_file()
        return self._save_to(self.path)

    def write_file(self) -> bool:
        initial = str(self.path) if self.path is not None else ""
        value = self.prompt("Write file: ", initial=initial)
        if value is None or not value.strip():
            self.message = "Write canceled"
            return False
        return self._save_to(Path(value).expanduser())

    def _save_to(self, target: Path) -> bool:
        text = self._text()
        encoding = self.encoding or self.options.default_encoding
        self.emit("before_save", target)
        try:
            write_text_exact(target, text, encoding=encoding)
        except UnicodeEncodeError:
            encoding = "utf-8"
            try:
                write_text_exact(target, text, encoding=encoding)
            except Exception as exc:
                self.message = f"Could not write {target}: {exc}"
                return False
        except Exception as exc:
            self.message = f"Could not write {target}: {exc}"
            return False
        self.path = target
        self.encoding = encoding
        self.clean_text = text
        self.message = f"Wrote {target}"
        self.invalidate_highlight(clear_style=True)
        self.emit("after_save", target)
        return True

    def find_file(self) -> None:
        if not self._confirm_discard_or_save():
            return
        initial = str(self.path) if self.path is not None else ""
        value = self.prompt("Find file: ", initial=initial)
        if value is None or not value.strip():
            self.message = "Find file canceled"
            return
        target = Path(value).expanduser()
        try:
            self._read_file_into_buffer(target)
        except Exception as exc:
            self.message = f"Could not open {target}: {exc}"

    def quit_editor(self) -> None:
        if self.dirty:
            answer = self.prompt_char("Buffer modified; save it? (y, n, C-g) ")
            if answer in {"y", "Y"}:
                if not self.save_buffer():
                    return
            elif answer in {"n", "N"}:
                pass
            else:
                self.message = "Quit canceled"
                return
        self.running = False

    def _confirm_discard_or_save(self) -> bool:
        if not self.dirty:
            return True
        answer = self.prompt_char("Buffer modified; save before opening another file? (y, n, C-g) ")
        if answer in {"y", "Y"}:
            return self.save_buffer()
        if answer in {"n", "N"}:
            return True
        self.message = "Canceled"
        return False

    # ----- Prompt, commands and search ------------------------------------

    def prompt(self, label: str, *, initial: str = "") -> Optional[str]:
        buffer = list(initial)
        pos = len(buffer)
        while True:
            full = label + "".join(buffer)
            self._refresh(full, len(label) + pos)
            kind, key = self._read_key()
            if self._is_cancel_key(kind, key):
                return None
            if self._is_enter_key(kind, key):
                return "".join(buffer)
            if self._is_backspace_key(kind, key):
                if pos > 0:
                    del buffer[pos - 1]
                    pos -= 1
                continue
            if kind == "char" and key == self.CTRL_A:
                pos = 0
            elif kind == "char" and key == self.CTRL_E:
                pos = len(buffer)
            elif kind == "char" and key == self.CTRL_B:
                pos = max(0, pos - 1)
            elif kind == "char" and key == self.CTRL_F:
                pos = min(len(buffer), pos + 1)
            elif kind == "char" and key == self.CTRL_K:
                del buffer[pos:]
            elif kind == "char" and key == self.CTRL_Y:
                for ch in self.kill_ring:
                    buffer.insert(pos, ch)
                    pos += 1
            elif kind == "key" and key == self.curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif kind == "key" and key == self.curses.KEY_RIGHT:
                pos = min(len(buffer), pos + 1)
            elif self._is_printable_char(kind, key):
                buffer.insert(pos, key)
                pos += 1

    def prompt_char(self, label: str) -> Optional[str]:
        while True:
            self._refresh(label, len(label))
            kind, key = self._read_key()
            if self._is_cancel_key(kind, key):
                return None
            if kind == "char" and isinstance(key, str):
                return key

    def command_prompt(self) -> None:
        value = self.prompt("M-x ")
        if value is None or not value.strip():
            self.message = "M-x canceled"
            return
        command = value.strip()
        if command in self.commands:
            self.run_command(command)
            return
        matches = [name for name in self.commands if name.startswith(command)]
        if len(matches) == 1:
            self.run_command(matches[0])
        elif matches:
            self.message = "Ambiguous: " + ", ".join(matches[:5])
        else:
            self.message = f"No such command: {command}"

    def search_prompt(self, *, backward: bool) -> None:
        label = "Search backward: " if backward else "Search: "
        value = self.prompt(label, initial=self.last_search)
        if value is None:
            self.message = "Search canceled"
            return
        if not value:
            self.message = "Empty search string"
            return
        self.last_search = value
        text = self._text()
        current = self._point_to_offset()
        if backward:
            pos = text.rfind(value, 0, current)
            wrapped = False
            if pos < 0:
                pos = text.rfind(value)
                wrapped = pos >= 0
        else:
            pos = text.find(value, current + 1)
            wrapped = False
            if pos < 0:
                pos = text.find(value)
                wrapped = pos >= 0
        if pos < 0:
            self.message = f"Search failed: {value}"
            return
        self.row, self.col = self._offset_to_point(pos)
        self.message = f"Found{', wrapped' if wrapped else ''}: {value}"

    def goto_line_prompt(self) -> None:
        value = self.prompt("Goto line: ", initial=str(self.row + 1))
        if value is None:
            self.message = "Goto canceled"
            return
        try:
            number = int(value.strip())
        except ValueError:
            self.message = f"Invalid line number: {value}"
            return
        number = max(1, min(number, len(self.lines)))
        self.row = number - 1
        self.col = min(self.col, len(self.lines[self.row]))
        self.preferred_col = None
        self.message = f"Line {number}"


def run_nox_mode(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(f"{APP_NAME}: --nox needs an interactive terminal", file=sys.stderr)
        return 1

    try:
        import curses
    except Exception as exc:  # pragma: no cover - platform dependent
        print(f"{APP_NAME}: curses is not available, so --nox cannot start.\n{exc}", file=sys.stderr)
        return 1

    def _main(stdscr: Any) -> int:
        try:
            editor = NoxEmacsEditor(stdscr, args, curses)
        except Exception as exc:
            print(f"{APP_NAME}: could not initialize --nox: {exc}", file=sys.stderr)
            return 1
        return editor.run()

    try:
        return int(curses.wrapper(_main) or 0)
    except Exception as exc:
        if os.environ.get("SIMPLEPYPAD_NOX_DEBUG"):
            print(traceback.format_exc(), file=sys.stderr)
        print(f"{APP_NAME}: --nox failed: {exc}", file=sys.stderr)
        return 1

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SimplePyPad: tiny GUI/terminal editor with Emacs-style keys and Pygments highlighting")
    parser.add_argument("file", nargs="?", help="file to open")
    parser.add_argument("--config", action="append", default=[], help="additional Python customization file")
    parser.add_argument("--no-user-config", action="store_true", help="do not load the default user config")
    parser.add_argument("--nox", action="store_true", help="run an Emacs-style terminal editor instead of the Tk GUI")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.nox:
        return run_nox_mode(args)

    if not TK_AVAILABLE:
        print(f"{APP_NAME}: Tkinter is not available. Use --nox for terminal mode.\n{TK_IMPORT_ERROR}", file=sys.stderr)
        return 1

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"{APP_NAME}: could not start Tk GUI. Use --nox for terminal mode.\n{exc}", file=sys.stderr)
        return 1

    try:
        root.tk.call("tk", "scaling", 1.0)
    except tk.TclError:
        pass
    app = SimplePyPad(
        root,
        config_paths=[Path(p).expanduser() for p in args.config],
        load_user_config=not args.no_user_config,
    )
    if args.file:
        path = Path(args.file).expanduser()
        if path.exists():
            app.open_path(path)
        else:
            app.current_path = path
            app.current_encoding = app.options.default_encoding
            app.update_title()
            app.schedule_highlight(immediate=True)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
