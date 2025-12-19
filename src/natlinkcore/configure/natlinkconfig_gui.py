#pylint:disable=W0621, W0703, W0603
import sys
import platform
import os
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import queue
import logging
from functools import partial
from platformdirs import user_log_dir
from pathlib import Path
from argparse import ArgumentParser
from typing import Optional, Callable, Dict
from dataclasses import dataclass, field

from natlinkcore.configure.natlinkconfigfunctions import NatlinkConfig
from natlinkcore import natlinkstatus
from natlinkcore.tkinter_dialogs import GetDirFromDialog

# Setup logging
appname = "natlink"
logdir = Path(user_log_dir(appname=appname, ensure_exists=True))
logfilename = logdir / "config_gui_log.txt"
file_handler = logging.FileHandler(logfilename)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logfile_logger = logging.getLogger()
logfile_logger.setLevel(logging.DEBUG)
file_handler.setLevel(logging.DEBUG)
logfile_logger.addHandler(file_handler)

SYMBOL_COLLAPSED = '▶'
SYMBOL_EXPANDED = '▼'

# UI Configuration Constants
STDOUT_FLUSH_DELAY_MS = 50
OUTPUT_TEXT_HEIGHT = 6
OUTPUT_TEXT_WIDTH = 60
THREAD_QUEUE_CHECK_INTERVAL_MS = 100
CLOSE_DELAY_MS = 500


@dataclass
class ProjectWidgets:
    """Widgets for a single project configuration"""
    checkbox_var: tk.BooleanVar
    entry_var: tk.StringVar
    frame: ttk.Frame
    entry: ttk.Entry
    browse_btn: ttk.Button
    clear_btn: ttk.Button


@dataclass
class AppState:
    """Centralized application state"""
    thread_running: bool = False
    close_after_operation: bool = False
    extras_visible: bool = False
    project_states: Dict[str, bool] = field(default_factory=dict)


def write_to_text_widget(text_widget, message):
    """Helper to write text to a disabled text widget"""
    text_widget.configure(state='normal')
    text_widget.insert('end', message)
    text_widget.see('end')
    text_widget.configure(state='disabled')


class StdoutRedirector:
    """Redirect stdout/stderr to a Text widget with buffering"""
    def __init__(self, text_widget, root):
        self.text_widget = text_widget
        self.root = root
        self.buffer = []
        self.flush_scheduled = False

    def write(self, message):
        if message:  # Allow empty lines, preserve formatting
            self.buffer.append(message)
            if not self.flush_scheduled:
                self.flush_scheduled = True
                self.root.after(STDOUT_FLUSH_DELAY_MS, self.flush_buffer)

    def flush_buffer(self):
        if self.buffer:
            combined_message = ''.join(self.buffer)
            write_to_text_widget(self.text_widget, combined_message)
            self.buffer.clear()
        self.flush_scheduled = False

    def flush(self):
        if self.buffer:
            self.flush_buffer()


class TextWidgetHandler(logging.Handler):
    """Logging handler that writes to a tkinter Text widget"""
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record) + '\n'
        write_to_text_widget(self.text_widget, msg)


class DialogManager:
    """Manages custom dialogs for the application"""

    @staticmethod
    def show_operation_in_progress(parent) -> str:
        """Show dialog when closing during operation

        Returns:
            'wait' - wait for operation to complete then close
            'force' - force close immediately
            'cancel' - cancel the close request
        """
        dialog = tk.Toplevel(parent)
        dialog.title("Operation in Progress")
        dialog.geometry("400x150")
        dialog.transient(parent)
        dialog.grab_set()

        # Center dialog
        dialog.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() // 2) - (dialog.winfo_width() // 2)
        y = parent.winfo_y() + (parent.winfo_height() // 2) - (dialog.winfo_height() // 2)
        dialog.geometry(f"+{x}+{y}")

        ttk.Label(dialog, text="Pip install is in progress.",
                 font=('TkDefaultFont', 10, 'bold')).pack(pady=(20, 5))
        ttk.Label(dialog, text="What would you like to do?").pack(pady=5)

        result = {'action': None}

        def set_result(action):
            result['action'] = action
            dialog.destroy()

        button_frame = ttk.Frame(dialog)
        button_frame.pack(pady=20)

        ttk.Button(button_frame, text="Wait & Close After",
                  command=lambda: set_result('wait'), width=18).pack(side='left', padx=5)
        ttk.Button(button_frame, text="Force Close Now",
                  command=lambda: set_result('force'), width=18).pack(side='left', padx=5)
        ttk.Button(button_frame, text="Cancel",
                  command=lambda: set_result('cancel'), width=12).pack(side='left', padx=5)

        parent.wait_window(dialog)
        return result['action']


class NatlinkConfigGUI:
    """Main GUI class for Natlink Configuration"""

    # Project configuration - add new projects here!
    # Each project needs:
    # - name: lowercase identifier
    # - label: display name
    # - prefix: 2-char abbreviation for widget names
    # - config_key: key in natlink.ini [directories]
    # - state_attr: attribute name for storing enabled state
    # - enable_method: NatlinkConfig method to enable
    # - disable_method: NatlinkConfig method to disable
    # - is_enabled_method: NatlinkStatus method to check if enabled
    # - get_method: NatlinkStatus method to get directory
    #
    PROJECTS = [
        {
            'name': 'dragonfly',
            'label': 'Dragonfly',
            'prefix': 'df',
            'config_key': 'dragonflyuserdirectory',
            'state_attr': 'dragonfly2_state',
            'enable_method': 'enable_dragonfly',
            'disable_method': 'disable_dragonfly',
            'is_enabled_method': 'dragonflyIsEnabled',
            'get_method': 'getDragonflyUserDirectory'
        },
        {
            'name': 'unimacro',
            'label': 'Unimacro',
            'prefix': 'um',
            'config_key': 'unimacrodirectory',
            'state_attr': 'unimacro_state',
            'enable_method': 'enable_unimacro',
            'disable_method': 'disable_unimacro',
            'is_enabled_method': 'unimacroIsEnabled',
            'get_method': 'getUnimacroUserDirectory'
        }
    ]

    # Autohotkey configuration
    AHK_CONFIGS = [
        {
            'label': 'Autohotkey exe dir:',
            'config_key': 'ahkexedir',
            'config_section': 'autohotkey',
            'var_suffix': 'ahk_exe',
            'get_method': 'getAhkExeDir'
        },
        {
            'label': 'Autohotkey scripts dir:',
            'config_key': 'ahkuserdir',
            'config_section': 'autohotkey',
            'var_suffix': 'ahk_scripts',
            'get_method': 'getAhkUserDir'
        }
    ]

    def __init__(self, config, status, prerelease_enabled):
        self.config = config
        self.status = status
        self.prerelease_enabled = prerelease_enabled

        # Centralized state management
        self.state = AppState()
        self.thread_queue = queue.Queue()
        self.worker_thread = None  # Store reference to background thread

        # Initialize project states dynamically
        for project in self.PROJECTS:
            is_enabled = getattr(status, project['is_enabled_method'])()
            self.state.project_states[project['name']] = is_enabled

        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr

        # Structured widget storage
        self.project_widgets: Dict[str, ProjectWidgets] = {}
        self.project_checkbox_vars: Dict[str, tk.BooleanVar] = {}  # Temporary storage for checkbox vars
        self.extras_widgets = {}
        self.interactive_widgets = []

        self.root = tk.Tk()
        self.setup_window()
        self.create_widgets()
        self.setup_logging()
        self.expand_config_paths()

        # Size window to fit content after widgets are created
        self.root.update_idletasks()
        self.root.geometry('')  # Clear geometry to auto-size

    def setup_window(self) -> None:
        self.root.title('Natlink Configuration')
        self.root.resizable(True, True)

    def create_widgets(self) -> None:
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill='both', expand=False)

        current_row = 0
        self.create_environment_frame(main_frame).grid(row=current_row, column=0, sticky='w', pady=5)
        current_row += 1

        self.create_projects_frame(main_frame).grid(row=current_row, column=0, sticky='w', pady=5)
        current_row += 1

        # Create project frames dynamically with calculated rows
        for project in self.PROJECTS:
            frame = self.create_project_frame(main_frame, project)
            # Frame already stored in ProjectWidgets by create_project_frame
            frame.grid(row=current_row, column=0, sticky='ew', pady=2)
            self._toggle_frame_visibility(frame, self.state.project_states[project['name']])
            current_row += 1

        self.create_extras_toggle(main_frame).grid(row=current_row, column=0, sticky='w', pady=5)
        current_row += 1

        self.extras_widgets['frame'] = self.create_extras_frame(main_frame)
        self.extras_widgets['frame'].grid(row=current_row, column=0, sticky='ew', pady=2)
        self._toggle_frame_visibility(self.extras_widgets['frame'], self.state.extras_visible)
        current_row += 1

        self.create_button_frame(main_frame).grid(row=current_row, column=0, sticky='ew', pady=10)
        main_frame.columnconfigure(0, weight=1)

    def create_environment_frame(self, parent):
        frame = ttk.Frame(parent)
        pyVersion = platform.python_version()

        # Platform-specific version info
        if platform.system() == 'Windows':
            osVersion = sys.getwindowsversion()
            os_info = f"Windows OS: {osVersion.major}, Build: {osVersion.build}"
        else:
            os_info = f"{platform.system()} {platform.release()}"

        try:
            dragon_version = self.status.getDNSVersion()
        except FileNotFoundError:
            dragon_version = "Not Available"

        # Create label with better spacing and separators
        ttk.Label(frame, text="Environment:", font=('TkDefaultFont', 9, 'bold')).pack(side='left', padx=(0, 10))

        info_text = f"{os_info}  •  Python: {pyVersion}  •  Dragon: {dragon_version}"
        ttk.Label(frame, text=info_text, font=('TkDefaultFont', 9)).pack(side='left')
        return frame

    def create_projects_frame(self, parent):
        frame = ttk.Frame(parent)
        ttk.Label(frame, text='Configure Projects:', font=('TkDefaultFont', 9, 'bold')).pack(side='left', padx=(0, 10))

        for project in self.PROJECTS:
            has_path = bool(self._get_expanded_path(project['get_method']))
            is_enabled = self.state.project_states[project['name']] or has_path
            self.state.project_states[project['name']] = is_enabled

            # Create checkbox var and store temporarily
            checkbox_var = tk.BooleanVar(value=is_enabled)
            self.project_checkbox_vars[project['name']] = checkbox_var

            checkbox = ttk.Checkbutton(frame, text=project['label'],
                                      variable=checkbox_var,
                                      command=self._make_checkbox_handler(project['name']))
            checkbox.pack(side='left', padx=5)
            self.interactive_widgets.append(checkbox)

        return frame

    def _make_checkbox_handler(self, project_name: str) -> Callable:
        """Create a checkbox toggle handler for a specific project"""
        def handler():
            self.handle_checkbox_toggle(project_name)
        return handler

    def create_project_frame(self, parent, project):
        """Create project configuration frame from project config dict"""
        frame = ttk.Frame(parent)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text=project['label'], foreground='black').grid(row=0, column=0, columnspan=4, sticky='w', pady=2)

        directory = self._get_expanded_path(project['get_method'])
        entry_var = tk.StringVar(value=directory)

        # Create widgets
        ttk.Label(frame, text=f"{project['label']} user directory:").grid(
            row=1, column=0, sticky='w', padx=(10, 5), pady=2)

        entry = ttk.Entry(frame, textvariable=entry_var, width=40)
        entry.grid(row=1, column=1, sticky='ew', padx=5, pady=2)
        self.interactive_widgets.append(entry)

        browse_btn = ttk.Button(frame, text='Browse...',
                               command=self._make_browse_handler(project['name'], project['label']))
        browse_btn.grid(row=1, column=2, padx=2, pady=2)
        self.interactive_widgets.append(browse_btn)

        clear_btn = ttk.Button(frame, text='Clear',
                              command=self._make_clear_handler(project['name']))
        clear_btn.grid(row=1, column=3, padx=2, pady=2)
        self.interactive_widgets.append(clear_btn)

        # Get checkbox_var from temporary storage
        checkbox_var = self.project_checkbox_vars.get(
            project['name'],
            tk.BooleanVar(value=self.state.project_states[project['name']])
        )

        # Store in structured ProjectWidgets dataclass
        self.project_widgets[project['name']] = ProjectWidgets(
            checkbox_var=checkbox_var,
            entry_var=entry_var,
            frame=frame,
            entry=entry,
            browse_btn=browse_btn,
            clear_btn=clear_btn
        )

        return frame

    def _make_browse_handler(self, project_name: str, label: str) -> Callable:
        """Create a browse button handler for a specific project"""
        def handler():
            self.handle_browse(project_name, label)
        return handler

    def _make_clear_handler(self, project_name: str) -> Callable:
        """Create a clear button handler for a specific project"""
        def handler():
            self.handle_clear(project_name)
        return handler

    def create_extras_toggle(self, parent):
        frame = ttk.Frame(parent)
        self.extras_widgets['symbol_var'] = tk.StringVar(value=SYMBOL_COLLAPSED)

        symbol = self._create_clickable_label(frame, textvariable=self.extras_widgets['symbol_var'],
                                              callback=self.handle_extras_toggle)
        symbol.pack(side='left', padx=2)

        title = self._create_clickable_label(frame, text='Natlink Extras',
                                             callback=self.handle_extras_toggle)
        title.pack(side='left', padx=2)

        return frame

    def create_extras_frame(self, parent):
        """Create the extras configuration frame"""
        frame = ttk.Frame(parent)
        frame.columnconfigure(1, weight=1)
        row = 0

        # Loglevel section
        row = self._create_loglevel_section(frame, row)

        # Autohotkey directories section
        row = self._create_autohotkey_section(frame, row)

        # Output window section
        self._create_output_section(frame, row)

        return frame

    def _create_loglevel_section(self, frame, row: int) -> int:
        """Create the loglevel configuration section

        Returns:
            Next available row number
        """
        ttk.Label(frame, text='Natlink Loglevel:').grid(row=row, column=0, sticky='w', padx=(10, 5), pady=2)
        log_level = self._get_config_value(self.status.getLogging, "Info")
        self.extras_widgets['logging_var'] = tk.StringVar(value=log_level)
        logging_combo = ttk.Combobox(frame, textvariable=self.extras_widgets['logging_var'],
                                     values=("Critical", "Fatal", "Error", "Warning", "Info", "Debug"),
                                     state='readonly', width=15)
        logging_combo.grid(row=row, column=1, sticky='w', padx=5, pady=2)
        logging_combo.bind('<<ComboboxSelected>>', lambda e: self.handle_logging_change())
        self.interactive_widgets.append(logging_combo)
        return row + 1

    def _create_autohotkey_section(self, frame, row: int) -> int:
        """Create the autohotkey directories configuration section

        Returns:
            Next available row number
        """
        for ahk_config in self.AHK_CONFIGS:
            directory = self._get_expanded_path(ahk_config['get_method'])
            entry_var = tk.StringVar(value=directory)

            # Store in extras_widgets with descriptive key
            widget_key = f"ahk_{ahk_config['var_suffix']}"
            self.extras_widgets[widget_key] = entry_var

            # Create directory row with proper handlers
            browse_handler = self._make_ahk_browse_handler(
                entry_var, ahk_config['label'], ahk_config['var_suffix'],
                ahk_config['config_key'], ahk_config['config_section']
            )
            clear_handler = self._make_ahk_clear_handler(
                entry_var, ahk_config['var_suffix'],
                ahk_config['config_key'], ahk_config['config_section']
            )

            ttk.Label(frame, text=ahk_config['label']).grid(
                row=row, column=0, sticky='w', padx=(10, 5), pady=2)

            entry = ttk.Entry(frame, textvariable=entry_var, width=40)
            entry.grid(row=row, column=1, sticky='ew', padx=5, pady=2)
            self.interactive_widgets.append(entry)

            browse_btn = ttk.Button(frame, text='Browse...', command=browse_handler)
            browse_btn.grid(row=row, column=2, padx=2, pady=2)
            self.interactive_widgets.append(browse_btn)

            clear_btn = ttk.Button(frame, text='Clear', command=clear_handler)
            clear_btn.grid(row=row, column=3, padx=2, pady=2)
            self.interactive_widgets.append(clear_btn)

            row += 1

        return row

    def _create_output_section(self, frame, row: int):
        """Create the output window section"""
        ttk.Label(frame, text='Natlink GUI Output').grid(
            row=row, column=0, columnspan=4, sticky='w', padx=(10, 5), pady=(10, 2))
        row += 1
        output_text = scrolledtext.ScrolledText(frame, height=OUTPUT_TEXT_HEIGHT, width=OUTPUT_TEXT_WIDTH,
                                                wrap='word', state='disabled')
        output_text.grid(row=row, column=0, columnspan=4, sticky='ew', padx=(10, 5), pady=2)
        self.extras_widgets['output_text'] = output_text

    def _make_ahk_browse_handler(self, entry_var: tk.StringVar, title: str,
                                  var_suffix: str, config_key: str, config_section: str) -> Callable:
        """Create a browse handler for autohotkey directory"""
        def handler():
            def on_complete():
                self._handle_ahk_dir_set(var_suffix, config_key, config_section)
            self.browse_directory(entry_var, title, on_complete)
        return handler

    def _make_ahk_clear_handler(self, entry_var: tk.StringVar, var_suffix: str,
                                config_key: str, config_section: str) -> Callable:
        """Create a clear handler for autohotkey directory"""
        def handler():
            self._handle_ahk_dir_clear(var_suffix, config_key, config_section)
            entry_var.set("")
        return handler

    def create_button_frame(self, parent):
        frame = ttk.Frame(parent)
        button_configs = [
            ('Exit', self.on_closing),
            ('Open Natlink Config File', self.handle_open_config)
        ]
        buttons = self._create_button_group(frame, button_configs, pack=True)
        self.interactive_widgets.extend(buttons)
        return frame

    def setup_logging(self) -> None:
        output_text = self.extras_widgets['output_text']
        sys.stdout = StdoutRedirector(output_text, self.root)
        sys.stderr = StdoutRedirector(output_text, self.root)

        text_handler = TextWidgetHandler(output_text)
        text_handler.setFormatter(formatter)
        text_handler.setLevel(logging.DEBUG)

        try:
            if self.config.config_get("settings", "log_level") != "Debug":
                text_handler.setLevel(logging.INFO)
        except Exception as exc:
            logging.debug(f"Failed to get log_level: {exc}")

        logfile_logger.addHandler(text_handler)

    def expand_config_paths(self) -> None:
        """Expand any ~ paths in config"""
        paths_updated = False

        # Expand project paths
        for project in self.PROJECTS:
            if self._expand_path_for_project(project):
                paths_updated = True

        # Expand AHK paths
        for ahk_config in self.AHK_CONFIGS:
            if self._expand_path_for_ahk(ahk_config):
                paths_updated = True

        if paths_updated:
            try:
                self._save_config_and_refresh()
                logging.info("Config file updated with expanded paths")

                # Update widgets from refreshed status
                for project in self.PROJECTS:
                    self._update_project_widget_from_status(project)

                for ahk_config in self.AHK_CONFIGS:
                    self._update_ahk_widget_from_status(ahk_config)
            except Exception as exc:
                logging.error(f"Failed to write expanded paths: {exc}")

    def _expand_path_for_project(self, project: dict) -> bool:
        """Expand path for a project, return True if updated"""
        try:
            path = self.config.config_get('directories', project['config_key'])
            if path and '~' in path:
                expanded = os.path.expanduser(path)
                self.config.config_set('directories', project['config_key'], expanded)
                if project['name'] in self.project_widgets:
                    self.project_widgets[project['name']].entry_var.set(expanded)
                logging.debug(f"Expanded {project['config_key']}: {path} -> {expanded}")
                return True
        except (KeyError, AttributeError, OSError) as exc:
            logging.debug(f"Failed to expand path for {project.get('config_key', 'unknown')}: {exc}")
        return False

    def _expand_path_for_ahk(self, ahk_config: dict) -> bool:
        """Expand path for AHK config, return True if updated"""
        try:
            path = self.config.config_get(ahk_config['config_section'], ahk_config['config_key'])
            if path and '~' in path:
                expanded = os.path.expanduser(path)
                self.config.config_set(ahk_config['config_section'], ahk_config['config_key'], expanded)
                widget_key = f"ahk_{ahk_config['var_suffix']}"
                if widget_key in self.extras_widgets:
                    self.extras_widgets[widget_key].set(expanded)
                logging.debug(f"Expanded {ahk_config['config_key']}: {path} -> {expanded}")
                return True
        except (KeyError, AttributeError, OSError) as exc:
            logging.debug(f"Failed to expand AHK path for {ahk_config.get('config_key', 'unknown')}: {exc}")
        return False

    def _update_project_widget_from_status(self, project: dict) -> None:
        """Update project widget value from status"""
        try:
            path = getattr(self.status, project['get_method'])()
            if path and project['name'] in self.project_widgets:
                self.project_widgets[project['name']].entry_var.set(path)
        except (AttributeError, KeyError) as exc:
            logging.debug(f"Failed to update widget for {project.get('name', 'unknown')}: {exc}")

    def _update_ahk_widget_from_status(self, ahk_config: dict) -> None:
        """Update AHK widget value from status"""
        try:
            path = getattr(self.status, ahk_config['get_method'])()
            widget_key = f"ahk_{ahk_config['var_suffix']}"
            if path and widget_key in self.extras_widgets:
                self.extras_widgets[widget_key].set(path)
        except (AttributeError, KeyError) as exc:
            logging.debug(f"Failed to update AHK widget for {ahk_config.get('var_suffix', 'unknown')}: {exc}")

    def _save_config_and_refresh(self) -> None:
        """Write config to disk and refresh status"""
        self.config.config_write()
        self.status.refresh()

    @staticmethod
    def _toggle_frame_visibility(frame, visible):
        """Toggle frame visibility using grid/grid_remove"""
        frame.grid() if visible else frame.grid_remove()

    def _create_clickable_label(self, parent, text=None, textvariable=None, callback=None):
        """Create a clickable label with standard styling

        Args:
            parent: Parent widget
            text: Label text (optional if textvariable provided)
            textvariable: StringVar for label text (optional)
            callback: Function to call on click

        Returns:
            The created label widget
        """
        kwargs = {'foreground': 'black', 'cursor': 'hand2'}
        if text is not None:
            kwargs['text'] = text
        if textvariable is not None:
            kwargs['textvariable'] = textvariable

        label = ttk.Label(parent, **kwargs)
        if callback:
            label.bind('<Button-1>', lambda e: callback())
        return label

    def _create_button_group(self, parent, button_configs, pack=True):
        """Create a group of buttons

        Args:
            parent: Parent frame
            button_configs: List of tuples (text, command, optional_width)
            pack: If True, pack buttons with side='left', otherwise return list

        Returns:
            List of created buttons
        """
        buttons = []
        for config in button_configs:
            text = config[0]
            command = config[1]
            width = config[2] if len(config) > 2 else None

            btn_kwargs = {'text': text, 'command': command}
            if width:
                btn_kwargs['width'] = width

            btn = ttk.Button(parent, **btn_kwargs)
            if pack:
                btn.pack(side='left', padx=5)
            buttons.append(btn)
        return buttons


    # Event Handlers
    def handle_checkbox_toggle(self, project_name: str) -> None:
        """Toggle project section visibility"""
        if project_name not in self.project_widgets:
            return

        widgets = self.project_widgets[project_name]
        is_enabled = widgets.checkbox_var.get()
        self.state.project_states[project_name] = is_enabled
        self._toggle_frame_visibility(widgets.frame, is_enabled)

    def handle_browse(self, project_name: str, label: str) -> None:
        """Browse for project directory"""
        if project_name not in self.project_widgets:
            return

        widgets = self.project_widgets[project_name]

        def on_browse_complete():
            widgets.checkbox_var.set(True)
            self.handle_checkbox_toggle(project_name)
            self._handle_project_userdir(project_name)

        self.browse_directory(widgets.entry_var, f'{label} user directory', on_browse_complete)

    def handle_clear(self, project_name: str) -> None:
        """Clear project configuration"""
        project_config = self._get_project_config(project_name)
        if not project_config or project_name not in self.project_widgets:
            return

        widgets = self.project_widgets[project_name]
        disable_method = getattr(self.config, project_config['disable_method'])
        disable_method()
        widgets.entry_var.set("")
        widgets.checkbox_var.set(False)
        self.handle_checkbox_toggle(project_name)

    def _configure_project(self, project_label: str, userdir: str, config_key: str,
                          enable_method: Callable, disable_method: Callable) -> None:
        """Generic project configuration handler

        Args:
            project_label: Display name for the project
            userdir: User directory path
            config_key: Configuration key in natlink.ini
            enable_method: Method to enable the project
            disable_method: Method to disable the project
        """
        if self.state.thread_running or not userdir:
            return

        try:
            current_config = self.config.config_get("directories", config_key)

            # Clear old directory if changing
            if current_config and current_config != userdir:
                print(f"Clearing old directory: {current_config}")
                disable_method()

            # Set directory in config
            result = self._set_directory_config(config_key, userdir)
            if not result:
                return

            if current_config == result:
                print(f"{project_label} directory already set to: {result}")
                return

            print(f"{project_label} directory saved to config: {result}")

            # Run pip install in background
            print(f"Starting {project_label} pip install...")
            self.start_long_operation(lambda: enable_method(result), f'{project_label.lower()}_done')

        except (KeyError, AttributeError, RuntimeError) as exc:
            error_msg = f"Error configuring {project_label}: {exc}"
            print(error_msg)
            logging.error(error_msg)

    def _get_project_config(self, project_name: str) -> Optional[dict]:
        """Get project configuration by name"""
        return next((p for p in self.PROJECTS if p['name'] == project_name), None)

    def _set_directory_config(self, config_key: str, directory: str, section: str = 'directories') -> Optional[str]:
        """Generic method to set a directory path in config

        Args:
            config_key: The config key to set
            directory: The directory path (will be expanded and validated). Empty string clears the config.
            section: Config section (default: 'directories')

        Returns:
            The expanded directory path (or empty string if cleared), or None if operation failed
        """
        if directory:
            directory = os.path.expanduser(directory)
            # Validate and normalize the path
            directory = os.path.normpath(os.path.abspath(directory))

            # Validate it's actually a directory
            if not os.path.isdir(directory):
                error_msg = f"Path is not a valid directory: {directory}"
                logging.warning(error_msg)
                print(error_msg)
                return None

        try:
            current = self.config.config_get(section, config_key)
            if current == directory:
                return directory  # Already set, no change needed

            # Save previous value before changing
            if current:
                self.config.config_set('previous settings', config_key, current)

            self.config.config_set(section, config_key, directory)
            self._save_config_and_refresh()
            return directory
        except (KeyError, OSError, IOError) as exc:
            error_msg = f"Failed to set {config_key}: {exc}"
            logging.error(error_msg)
            print(error_msg)
            return None

    def _handle_project_userdir(self, project_name: str) -> None:
        """Generic handler for project userdir configuration"""
        project = self._get_project_config(project_name)
        if not project or project_name not in self.project_widgets:
            return

        userdir = self.project_widgets[project_name].entry_var.get()
        enable_method = getattr(self.config, project['enable_method'])
        disable_method = getattr(self.config, project['disable_method'])

        self._configure_project(project['label'], userdir, project['config_key'],
                               enable_method, disable_method)

    def _handle_ahk_dir_set(self, var_suffix: str, config_key: str, section: str = 'autohotkey') -> None:
        """Generic handler for setting AHK directory"""
        widget_key = f"ahk_{var_suffix}"
        if widget_key not in self.extras_widgets:
            return

        value = self.extras_widgets[widget_key].get()
        if value:
            result = self._set_directory_config(config_key, value, section)
            if result:
                print(f"AHK directory set to: {result}")
            else:
                print(f"Failed to set AHK directory")

    def _handle_ahk_dir_clear(self, var_suffix: str, config_key: str, section: str = 'autohotkey') -> None:
        """Generic handler for clearing AHK directory"""
        result = self._set_directory_config(config_key, '', section)
        widget_key = f"ahk_{var_suffix}"

        if result is not None:
            if widget_key in self.extras_widgets:
                self.extras_widgets[widget_key].set("")
            print(f"Cleared AHK directory configuration")
        else:
            print(f"Failed to clear AHK directory configuration")

    def handle_logging_change(self) -> None:
        self.config.setLogging(self.extras_widgets['logging_var'].get())

    def handle_open_config(self) -> None:
        self.config.openConfigFile()

    def handle_extras_toggle(self) -> None:
        self.state.extras_visible = not self.state.extras_visible
        self._toggle_frame_visibility(self.extras_widgets['frame'], self.state.extras_visible)
        self.extras_widgets['symbol_var'].set(
            SYMBOL_EXPANDED if self.state.extras_visible else SYMBOL_COLLAPSED
        )

    def _get_expanded_path(self, method_name):
        """Get path from status and expand ~"""
        try:
            path = getattr(self.status, method_name)()
            return os.path.expanduser(path) if path else ""
        except Exception:
            return ""

    def _get_config_value(self, method, default):
        """Safely get config value with default"""
        try:
            return method()
        except Exception:
            return default

    def browse_directory(self, var, title, callback=None) -> None:
        current_dir = var.get()
        result = GetDirFromDialog(title=title, initialdir=current_dir or None)
        if result:
            var.set(os.path.expanduser(result))
            if callback:
                callback()

    # Threading Support for pip installs
    def start_long_operation(self, func: Callable, callback_event: str) -> None:
        """Start a long-running operation in a background thread

        Args:
            func: Function to execute in background
            callback_event: Event name for completion callback
        """
        self.state.thread_running = True
        self.disable_inputs()

        def worker():
            try:
                func()
                self.thread_queue.put(('success', callback_event))
            except Exception as exc:
                self.thread_queue.put(('error', str(exc)))

        self.worker_thread = threading.Thread(target=worker, daemon=False)
        self.worker_thread.start()
        self.check_thread_queue()

    def check_thread_queue(self) -> None:
        """Check for thread completion and handle results"""
        try:
            status, data = self.thread_queue.get_nowait()
            self.state.thread_running = False
            self.enable_inputs()

            if status == 'error':
                messagebox.showerror("Error", f"Operation failed: {data}")
            else:
                print(f"Operation completed: {data}")
                try:
                    self.status.refresh()
                except Exception:
                    pass

            if self.state.close_after_operation:
                self.state.close_after_operation = False
                print("Operation complete. Closing GUI as requested...")
                self.root.after(CLOSE_DELAY_MS, self.on_closing)
        except queue.Empty:
            self.root.after(THREAD_QUEUE_CHECK_INTERVAL_MS, self.check_thread_queue)

    def _set_widgets_state(self, state):
        """Set state for all interactive widgets

        Args:
            state: 'normal', 'disabled', or 'auto' (auto determines readonly for Combobox)
        """
        for widget in self.interactive_widgets:
            try:
                if state == 'auto':
                    widget_state = 'readonly' if isinstance(widget, ttk.Combobox) else 'normal'
                else:
                    widget_state = state
                widget.config(state=widget_state)
            except tk.TclError:
                pass

    def disable_inputs(self) -> None:
        self._set_widgets_state('disabled')

    def enable_inputs(self) -> None:
        self._set_widgets_state('auto')

    def on_closing(self) -> None:
        """Handle window close event"""
        if self.state.thread_running:
            result = DialogManager.show_operation_in_progress(self.root)

            if result == 'wait':
                self.state.close_after_operation = True
                print("Will close GUI when operation completes...")
                return
            elif result == 'force':
                print("Force closing GUI...")
                # Give thread time to cleanup before terminating
                if self.worker_thread and self.worker_thread.is_alive():
                    print("Waiting for background operation to cleanup...")
                    self.worker_thread.join(timeout=2.0)
                    if self.worker_thread.is_alive():
                        print("Warning: Background operation did not complete cleanly")
                self.state.thread_running = False
            else:  # cancel
                return

        # Restore stdout/stderr
        sys.stdout = self.original_stdout
        sys.stderr = self.original_stderr

        # Refresh status
        try:
            self.config.status.refresh()
        except Exception:
            pass

        # Destroy window and force exit
        self.root.destroy()
        self.root.quit()

    def run(self) -> None:
        """Start the GUI main loop"""
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.mainloop()


def main_gui():
    parser = ArgumentParser(description="check for --pre")
    parser.add_argument('--pre', action='store_true', help='Enable pre-release mode')
    args = parser.parse_args()

    extra_pip_options = ['--pre'] if args.pre else []
    logging.debug(f"prerelease_enabled: {args.pre}")

    config = NatlinkConfig(extra_pip_options=extra_pip_options)
    status = natlinkstatus.NatlinkStatus()

    try:
        app = NatlinkConfigGUI(config, status, args.pre)
        app.run()
    except Exception as exc:
        messagebox.showerror("Error", f"Exception in GUI: {exc}")
        raise
    finally:
        # Ensure logging handlers are closed
        for handler in logging.root.handlers[:]:
            handler.close()
            logging.root.removeHandler(handler)


if __name__ == '__main__':
    main_gui()
