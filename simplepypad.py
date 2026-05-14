#!/usr/bin/env python3
"""
SimplePyPad: a tiny Tkinter editor with Pygments highlighting and Python customization.

Run:
    python simplepypad.py [file]

Customize:
    Edit the user config file from Tools -> Open User Config.
"""

from __future__ import annotations

import argparse
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

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, simpledialog

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
        self._menu_add(file_menu, "New", "file.new", "Ctrl+N")
        self._menu_add(file_menu, "Open...", "file.open", "Ctrl+O")
        self._menu_add(file_menu, "Save", "file.save", "Ctrl+S")
        self._menu_add(file_menu, "Save As...", "file.save_as", "Ctrl+Shift+S")
        file_menu.add_separator()
        self._menu_add(file_menu, "Reload From Disk", "file.reload")
        file_menu.add_separator()
        self._menu_add(file_menu, "Exit", "app.exit")

        edit_menu = self.get_or_create_menu("Edit")
        self._menu_add(edit_menu, "Undo", "edit.undo", "Ctrl+Z")
        self._menu_add(edit_menu, "Redo", "edit.redo", "Ctrl+Y")
        edit_menu.add_separator()
        self._menu_add(edit_menu, "Cut", "edit.cut", "Ctrl+X")
        self._menu_add(edit_menu, "Copy", "edit.copy", "Ctrl+C")
        self._menu_add(edit_menu, "Paste", "edit.paste", "Ctrl+V")
        self._menu_add(edit_menu, "Delete", "edit.delete")
        edit_menu.add_separator()
        self._menu_add(edit_menu, "Select All", "edit.select_all", "Ctrl+A")
        edit_menu.add_separator()
        self._menu_add(edit_menu, "Find...", "search.find", "Ctrl+F")
        self._menu_add(edit_menu, "Find Next", "search.find_next", "F3")
        self._menu_add(edit_menu, "Replace All...", "search.replace_all", "Ctrl+H")
        self._menu_add(edit_menu, "Go To Line...", "search.goto_line", "Ctrl+G")

        view_menu = self.get_or_create_menu("View")
        self._menu_add(view_menu, "Toggle Word Wrap", "view.toggle_wrap")
        view_menu.add_separator()
        self._menu_add(view_menu, "Larger Font", "view.font_larger", "Ctrl++")
        self._menu_add(view_menu, "Smaller Font", "view.font_smaller", "Ctrl+-")
        self._menu_add(view_menu, "Reset Font Size", "view.font_reset")
        view_menu.add_separator()
        self._menu_add(view_menu, "Toggle Highlight", "view.toggle_highlight")
        self._menu_add(view_menu, "Pygments Style...", "view.set_style")

        tools_menu = self.get_or_create_menu("Tools")
        self._menu_add(tools_menu, "Command Palette...", "tools.command_palette", "Ctrl+Shift+P")
        tools_menu.add_separator()
        self._menu_add(tools_menu, "Auto Detect Syntax", "syntax.auto")
        self._menu_add(tools_menu, "Set Syntax by Pygments Alias...", "syntax.set_alias")
        tools_menu.add_separator()
        self._menu_add(tools_menu, "Open User Config", "custom.open_config")
        self._menu_add(tools_menu, "Reload Customization", "custom.reload")
        self._menu_add(tools_menu, "Run Current Buffer as Customization", "custom.run_buffer")

        help_menu = self.get_or_create_menu("Help")
        self._menu_add(help_menu, "About", "help.about")

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
        builtin: Dict[str, Callable[[], Any]] = {
            "file.new": self.new_file,
            "file.open": self.open_file_dialog,
            "file.save": self.save_file,
            "file.save_as": self.save_as_dialog,
            "file.reload": self.reload_from_disk,
            "app.exit": self.exit_app,
            "edit.undo": lambda: self._event_generate("<<Undo>>"),
            "edit.redo": lambda: self._event_generate("<<Redo>>"),
            "edit.cut": lambda: self._event_generate("<<Cut>>"),
            "edit.copy": lambda: self._event_generate("<<Copy>>"),
            "edit.paste": lambda: self._event_generate("<<Paste>>"),
            "edit.delete": self.delete_selection,
            "edit.select_all": self.select_all,
            "search.find": self.find_dialog,
            "search.find_next": self.find_next,
            "search.replace_all": self.replace_all_dialog,
            "search.goto_line": self.goto_line_dialog,
            "view.toggle_wrap": self.toggle_wrap,
            "view.font_larger": lambda: self.change_font_size(+1),
            "view.font_smaller": lambda: self.change_font_size(-1),
            "view.font_reset": lambda: self.set_option("font_size", 11),
            "view.toggle_highlight": self.toggle_highlight,
            "view.set_style": self.set_style_dialog,
            "tools.command_palette": self.command_palette,
            "syntax.auto": self.auto_detect_syntax,
            "syntax.set_alias": self.set_syntax_alias_dialog,
            "custom.open_config": self.open_user_config,
            "custom.reload": lambda: self.load_customizations(silent=False),
            "custom.run_buffer": self.run_buffer_as_customization,
            "help.about": self.about_dialog,
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
        bindings = {
            "<Control-n>": "file.new",
            "<Control-o>": "file.open",
            "<Control-s>": "file.save",
            "<Control-Shift-S>": "file.save_as",
            "<Control-f>": "search.find",
            "<F3>": "search.find_next",
            "<Control-h>": "search.replace_all",
            "<Control-g>": "search.goto_line",
            "<Control-a>": "edit.select_all",
            "<Control-plus>": "view.font_larger",
            "<Control-equal>": "view.font_larger",
            "<Control-minus>": "view.font_smaller",
            "<Control-Shift-P>": "tools.command_palette",
        }
        for seq, command in bindings.items():
            self.root.bind(seq, lambda event, name=command: self._run_command_from_event(name))

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

        if not self.options.highlight or not PYGMENTS_AVAILABLE:
            self.current_lexer_name = "Text" if not PYGMENTS_AVAILABLE else "Highlight Off"
            self.update_status()
            return

        content = self.text.get("1.0", "end-1c")
        if not content:
            self.current_lexer_name = self._lexer_name_for_status()
            self.update_status()
            return
        if len(content) > self.options.max_highlight_chars:
            self.current_lexer_name = f"Highlight skipped > {self.options.max_highlight_chars:,} chars"
            self.update_status()
            return

        try:
            lexer = self.get_lexer()
            self.current_lexer_name = getattr(lexer, "name", "Text") or "Text"
            offset = 0
            for token_type, value in lex(content, lexer):  # type: ignore[misc]
                if not value:
                    continue
                start_offset = offset
                offset += len(value)
                if value.isspace():
                    continue
                tag = self.tag_for_token(token_type)
                self.configure_token_tag(tag, token_type)
                self.text.tag_add(tag, f"1.0+{start_offset}c", f"1.0+{offset}c")
            self.text.tag_raise("sel")
            self.emit("after_highlight", self.current_lexer_name)
        except Exception:
            self.current_lexer_name = "Highlight error"
        finally:
            self.update_status()

    def get_lexer(self) -> Any:
        if not PYGMENTS_AVAILABLE:
            return None
        if self.force_lexer_alias:
            try:
                return get_lexer_by_name(self.force_lexer_alias, stripnl=False, stripall=False)  # type: ignore[misc]
            except ClassNotFound:
                self.force_lexer_alias = None
        filename = str(self.current_path) if self.current_path else "untitled.txt"
        try:
            return get_lexer_for_filename(filename, stripnl=False, stripall=False)  # type: ignore[misc]
        except ClassNotFound:
            return TextLexer(stripnl=False, stripall=False)  # type: ignore[operator]

    def _lexer_name_for_status(self) -> str:
        try:
            return getattr(self.get_lexer(), "name", "Text") or "Text"
        except Exception:
            return "Text"

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
        try:
            style_cls = get_style_by_name(self.options.pygments_style)  # type: ignore[misc]
        except Exception:
            style_cls = get_style_by_name("default")  # type: ignore[misc]
        style = style_cls.style_for_token(token_type)
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
# Only run code you trust. Computers, tragically, do exactly what you tell them.

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
    candidates = unique_strings(["utf-8-sig", "utf-8", preferred, "cp932", "shift_jis", "latin-1"])
    last_error: Optional[Exception] = None
    data = path.read_bytes()
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


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SimplePyPad: tiny GUI editor with Pygments highlighting")
    parser.add_argument("file", nargs="?", help="file to open")
    parser.add_argument("--config", action="append", default=[], help="additional Python customization file")
    parser.add_argument("--no-user-config", action="store_true", help="do not load the default user config")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    root = tk.Tk()
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
