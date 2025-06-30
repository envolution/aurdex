import os
import appdirs
import json
import gzip
import re
import fnmatch
import httpx
import logging as log

from typing import Optional, List, Dict, Any

from textual import on, work
from textual.events import Key, MouseDown
from textual.binding import Binding
from textual.timer import Timer
from textual.logging import TextualHandler
from textual.app import App, ComposeResult
from textual.containers import Container, Vertical
from textual.widgets import (
    Header,
    Footer,
    Input,
    DataTable,
    Label,
    Checkbox,
)

from textual.coordinate import Coordinate


from .db import ProvideDB
from .widgets import (
    FilterModal,
    SortModal,
    PackageDetails,
    GitViewModal,
    CommentsModal,
    ProfileModal,
)

log.root.handlers.clear()
log.basicConfig(level=log.DEBUG, handlers=[TextualHandler()], force=True)


class aurdex(App):
    """Main AUR Browser application"""

    CSS_PATH = "tcss/main.tcss"
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("/", "search", "Search", show=True),
        Binding("s", "sort", "Sort", show=True),
        Binding("f", "filter", "Filter", show=True),
        Binding("R", "reset_filters", "Reset Filters", show=True),
        Binding("c", "view_comments", "View Comments", show=True),
        Binding("U", "download_from_aur", "Update from AUR", show=True),
        Binding("p", "profiles", "Profiles", show=True),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "cursor_top", "Top", show=False),
        Binding("G", "cursor_bottom", "Bottom", show=False),
        Binding("ctrl+d", "page_down", "Page Down", show=False),
        Binding("ctrl+u", "page_up", "Page Up", show=False),
        Binding("escape", "clear_search", "Clear Search", show=False),
    ]

    def __init__(
        self, profile_name: Optional[str] = None, *args: Any, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.startup_profile = profile_name
        self.packages: List[Dict[str, Any]] = []
        self.filtered_packages: List[Dict[str, Any]] = []
        self.displayed_packages: List[Dict[str, Any]] = []
        self.current_sort = "sort-name"
        self.current_sort_reverse = False
        self.search_term = ""
        self.chunk_size = 1000
        self.loaded_count = 0

        self.config_path_dir = appdirs.user_config_dir(appname="aurdex")
        self.config_file = os.path.join(self.config_path_dir, "settings.json")
        self.git_cache_path = appdirs.user_cache_dir(appname="aurdex")

        self.default_profile_name = "default"
        self.current_profile_name = "default"
        self.profiles = {}
        self.default_filters_structure = {
            "abandoned": False,
            "out_of_date": False,
            "maintainer": "",
            "provides": "",
        }
        self.filters = self.default_filters_structure.copy()  # Initialize self.filters
        self._filter_modal: Optional[FilterModal] = None  # For type hinting

        self.provide_db: Optional[ProvideDB] = None
        self._dep_resolve_timer: Optional[Timer] = None
        self.DEP_RESOLVE_DELAY: float = 1.1  # seconds for delay before resolving deps
        self._last_input = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="main-container"):
            with Vertical(id="left-pane", classes="column"):
                with Container(id="search-container"):
                    yield Label("No active filters.", id="filter-status")
                    yield Input(
                        placeholder="Search packages... (Press / to focus)",
                        id="search-input",
                    )
                yield DataTable(id="package-table", cursor_type="row")
            yield PackageDetails(id="package-details")
        yield Footer()

    @work(exclusive=True, thread=True)
    async def download_metadata(self):
        url = "https://aur.manjaro.org/packages-meta-ext-v1.json.gz"
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                with open(self.datafile, "wb") as fp:
                    fp.write(resp.content)

            self.notify("Sync complete")
            log.info(f"Download complete: {self.datafile}")
            self._load_package_data()
            self.action_refresh()
        except Exception as e:
            self.notify(f"Download failed: {e}", severity="error", timeout=10)
            if not os.path.exists(self.datafile):
                self.packages = []
                self.filtered_packages = []
                self.update_package_list()

    def action_refresh(self) -> None:
        self.filter_packages(self.search_term)  # Re-apply current search and filters
        self.update_package_list()
        self.update_filter_status()
        self.notify("Package list refreshed.")

    def action_reset_filters(self) -> None:
        """Action to reset all filters to their default state."""
        self.filters = self.default_filters_structure.copy()
        self.search_term = ""
        self.query_one("#search-input", Input).value = ""
        self.filter_packages("")
        self.update_package_list()
        self.update_filter_status()
        self.notify("All filters have been reset.")

    @on(Key)
    def track_key(self, event: Key) -> None:
        if event.key == "enter":
            self._last_input = "keyboard"

    @on(MouseDown)
    def track_click(self, event: MouseDown) -> None:
        self._last_input = "mouse"

    def _load_package_data(self):
        try:
            with gzip.open(self.datafile, "rt", encoding="utf-8") as f:
                self.packages = json.load(f)

            if self.provide_db is None:
                self.provide_db = ProvideDB(aur_data=self.packages)
            else:
                self.provide_db.refresh(aur_data=self.packages)

            self.filtered_packages = self.packages.copy()
            self.notify(
                f"Loaded {len(self.packages)} packages.", severity="information"
            )
            self.sort_packages()
        except Exception as e:
            self.packages = []
            self.filtered_packages = []
            self.notify(
                f"Error loading package data: {e}", severity="error", timeout=10
            )

    def load_aur_packages(self, force_download: bool = False) -> None:
        os.makedirs(self.config_path_dir, exist_ok=True)
        self.datafile = os.path.join(
            self.config_path_dir, "packages-meta-ext-v1.json.gz"
        )

        if not os.path.exists(self.datafile) or force_download:
            self.notify("Syncing AUR metadata")
            log.info(f"Syncing AUR metadata to {self.datafile}")
            self.download_metadata()
            return

        self._load_package_data()

    @work(exclusive=True, thread=True)
    async def resolve_package_dependencies(self, package_data: Dict[str, Any]) -> None:
        """
        Worker thread to resolve dependencies for a given package and update details pane.
        """
        if not self.provide_db or not package_data:
            log.warning("resolve_package_dependencies: No provide_db or package_data.")
            return

        log.debug(f"Resolving dependencies for: {package_data.get('Name')}")
        details_pane = self.query_one("#package-details", PackageDetails)

        enriched_deps: Dict[str, List[Dict]] = {}
        dep_types_to_process = {
            "Depends": package_data.get("Depends", []),
            "MakeDepends": package_data.get("MakeDepends", []),
            "CheckDepends": package_data.get("CheckDepends", []),
            "OptDepends": package_data.get("OptDepends", []),
        }

        for dep_type_key, dep_list_from_pkg in dep_types_to_process.items():
            enriched_deps[dep_type_key] = []
            for dep_item_full_spec in dep_list_from_pkg:
                # Parse the dependency string (e.g., "libfoo>=1.0" or "bash: for building")
                # Cleaned name for ProvideDB lookup:
                cleaned_dep_name = (
                    dep_item_full_spec.split(":")[0]
                    .split("=")[0]
                    .split("<")[0]
                    .split(">")[0]
                    .strip()
                )
                # Optional description (primarily for OptDepends):
                dep_description = (
                    dep_item_full_spec.split(":", 1)[1].strip()
                    if ":" in dep_item_full_spec
                    else None
                )

                providers = self.provide_db.find_providers(cleaned_dep_name)
                enriched_deps[dep_type_key].append(
                    {
                        "name": cleaned_dep_name,  # The name used for lookup
                        "original_spec": dep_item_full_spec,  # The full string from JSON
                        "description": dep_description,  # Description if any
                        "providers": providers,  # List of provider dicts from ProvideDB
                    }
                )

        # Update the PackageDetails widget from the main thread
        self.call_from_thread(
            details_pane.update_package,
            package=package_data,  # Pass the original package data
            enriched_dependencies=enriched_deps,  # Pass the newly resolved data
        )
        log.debug(f"Finished resolving dependencies for: {package_data.get('Name')}")

    def on_mount(self) -> None:
        self.update_title()
        self.sub_title = "Browse Arch User Repository packages"

        table = self.query_one("#package-table", DataTable)
        table.add_column(
            "Name", key="name"
        )  # Give keys for sorting if needed by DataTable itself
        table.add_column("Version", width=12, key="version")
        table.add_column("Votes", width=6, key="votes")
        table.add_column("Pop.", width=6, key="popularity")
        table.focus()

        os.makedirs(self.config_path_dir, exist_ok=True)
        os.makedirs(self.git_cache_path, exist_ok=True)

        self.load_app_config()
        self.query_one("#search-input", Input).value = self.search_term
        self.load_aur_packages()

        # Apply filters and search term (if any from previous session, though not saved currently)
        self.filter_packages(self.search_term)
        self.update_package_list()  # Populate table
        self.update_filter_status()
        self.query_one(
            "#package-table", DataTable
        ).focus()  # Focus package table on startup

    def action_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_clear_search(self) -> None:
        search_input = self.query_one("#search-input", Input)
        search_input.value = ""
        self.filter_packages("")
        self.update_package_list()
        self.update_filter_status()
        self.query_one("#package-table", DataTable).focus()

    def load_app_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file) as f:
                    config = json.load(f)
                    self.profiles = config.get("profiles", {})
                    self.default_profile_name = config.get("default_profile", "default")

                profile_to_load_name = self.startup_profile or self.default_profile_name
                profile_to_load = self.profiles.get(profile_to_load_name, {})
                self.load_profile(profile_to_load_name, profile_to_load)

            except Exception as e:
                self.notify(f"Failed to load profiles: {e}", severity="warning")
                self.profiles = {"default": self.get_current_settings()}
                self.load_profile("default", self.profiles["default"])
        else:
            self.profiles = {"default": self.get_current_settings()}
            self.load_profile("default", self.profiles["default"])
            self.notify("No configuration file found, created default profile.")

    def load_profile(self, profile_name: str, profile_data: Dict[str, Any]):
        self.current_profile_name = profile_name
        self.filters = profile_data.get(
            "filters", self.default_filters_structure.copy()
        )
        self.search_term = profile_data.get("search_term", "")
        self.current_sort = profile_data.get("current_sort", "sort-name")
        self.current_sort_reverse = profile_data.get("current_sort_reverse", False)
        self.theme = profile_data.get("theme", "nord")
        self.query_one("#search-input", Input).value = self.search_term
        self.update_title()
        self.filter_packages(self.search_term)
        self.update_package_list()
        self.update_filter_status()

    def get_current_settings(self) -> Dict[str, Any]:
        return {
            "filters": self.filters,
            "search_term": self.search_term,
            "current_sort": self.current_sort,
            "current_sort_reverse": self.current_sort_reverse,
            "theme": self.theme,
        }

    def update_title(self):
        if self.current_profile_name == "default":
            self.title = "aurdex"
        else:
            self.title = f"Profile: {self.current_profile_name} - aurdex"

        self.sub_title = "Browsing the AUR"
        if self.loaded_count:
            self.sub_title = (
                self.sub_title
                + f" - showing ({self.loaded_count}/{len(self.packages)}) packages"
            )

    def filter_packages(self, search_term: str) -> None:
        self.search_term = search_term.lower()
        current_packages = self.packages  # Start with all packages

        # Apply text search first if present
        if self.search_term:
            temp_filtered = []

            # Check if it looks like a regex pattern (contains regex special chars)
            is_regex = any(char in self.search_term for char in r"[]{}()+^$|\\")

            for pkg in current_packages:
                # Build searchable text from Name and Keywords only
                name = str(pkg.get("Name", "")).lower()
                keywords = " ".join(pkg.get("Keywords", [])).lower()
                searchable_text = f"{name} {keywords}".strip()

                if is_regex:
                    try:
                        # Try regex search
                        if re.search(self.search_term, searchable_text, re.IGNORECASE):
                            temp_filtered.append(pkg)
                    except re.error:
                        # Fall back to wildcard if regex is invalid
                        if fnmatch.fnmatch(searchable_text, f"*{self.search_term}*"):
                            temp_filtered.append(pkg)
                else:
                    # Use wildcard matching (supports * and ?)
                    if fnmatch.fnmatch(searchable_text, f"*{self.search_term}*"):
                        temp_filtered.append(pkg)

            current_packages = temp_filtered

        # Apply checkbox/input filters
        active_filter_criteria = []
        if self.filters.get("abandoned"):
            active_filter_criteria.append(lambda pkg: pkg.get("Maintainer") is None)
        if self.filters.get("out_of_date"):
            active_filter_criteria.append(
                lambda pkg: pkg.get("OutOfDate") is not None
            )  # OutOfDate stores timestamp if OOD

        maintainer_filter = self.filters.get("maintainer", "").lower()
        if maintainer_filter:
            active_filter_criteria.append(
                lambda pkg: maintainer_filter in str(pkg.get("Maintainer", "")).lower()
                or any(
                    maintainer_filter in str(cm).lower()
                    for cm in pkg.get("CoMaintainers", [])
                    if cm
                )
            )

        provides_filter = self.filters.get("provides", "").lower()
        if provides_filter:
            active_filter_criteria.append(
                lambda pkg: provides_filter in " ".join(pkg.get("Provides", [])).lower()
            )

        if active_filter_criteria:
            self.filtered_packages = [
                pkg
                for pkg in current_packages
                if all(criterion(pkg) for criterion in active_filter_criteria)
            ]
        else:  # If only search term was applied or no filters at all
            self.filtered_packages = current_packages

        self.sort_packages()  # Sort the newly filtered list
        self.reset_display()  # Reset display to show from the top of the new list

    def load_more_packages(self) -> bool:
        if self.loaded_count >= len(self.filtered_packages):
            return False
        remaining = len(self.filtered_packages) - self.loaded_count
        to_load = min(self.chunk_size, remaining)
        next_chunk = self.filtered_packages[
            self.loaded_count : self.loaded_count + to_load
        ]
        self.displayed_packages.extend(next_chunk)
        self.loaded_count += to_load
        return to_load > 0

    def check_load_more(self) -> None:
        table = self.query_one("#package-table", DataTable)
        if table.cursor_row >= len(
            self.displayed_packages
        ) - 20 and self.loaded_count < len(
            self.filtered_packages
        ):  # Load if within 20 rows of end
            if self.load_more_packages():
                current_cursor_row = table.cursor_row  # Preserve cursor

                # Add new rows to table efficiently
                table_row_count = (
                    table.row_count
                )  # This is the index where new rows will start
                new_packages_chunk = self.displayed_packages[
                    table_row_count:
                ]  # Get only the newly loaded packages

                for package in new_packages_chunk:  # Iterate through the new packages
                    table.add_row(
                        package.get("Name", "Unknown"),
                        package.get("Version", "Unknown"),
                        str(package.get("NumVotes", 0)),
                        f"{package.get('Popularity', 0):.2f}",
                        key=str(package.get("ID", 0)),  # Add key here for each row
                    )

                if (
                    current_cursor_row < table.row_count
                ):  # Restore cursor if still valid
                    table.cursor_coordinate = Coordinate(
                        current_cursor_row, table.cursor_column
                    )
                self.update_title()

    def reset_display(self) -> None:
        self.displayed_packages = []
        self.loaded_count = 0
        self.load_more_packages()  # Load the first chunk
        self.update_title()

    def update_package_list(self) -> None:
        table = self.query_one("#package-table", DataTable)
        current_cursor_key = None
        if table.row_count > 0 and table.is_valid_coordinate(table.cursor_coordinate):
            try:
                row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
                current_cursor_key = row_key.value
            except Exception:
                pass

        table.clear()

        new_cursor_row_index = 0
        found_old_cursor = False

        for i, package in enumerate(self.displayed_packages):
            package_id_str = str(package.get("ID", 0))
            table.add_row(
                package.get("Name", "Unknown"),
                package.get("Version", "Unknown"),
                str(package.get("NumVotes", 0)),
                f"{package.get('Popularity', 0):.2f}",  # Adjusted formatting
                key=package_id_str,
            )
            if package_id_str == current_cursor_key:
                new_cursor_row_index = i
                found_old_cursor = True

        if found_old_cursor:
            table.cursor_coordinate = Coordinate(
                row=new_cursor_row_index,
                column=table.cursor_coordinate.column,
            )
        elif table.row_count > 0:
            table.cursor_coordinate = Coordinate(
                row=0, column=table.cursor_coordinate.column
            )

    def update_filter_status(self) -> None:
        active_filters = []
        if self.search_term:
            active_filters.append(f"Search: '{self.search_term}'")
        if self.filters.get("abandoned"):
            active_filters.append("Abandoned")
        if self.filters.get("out_of_date"):
            active_filters.append("Out of Date")
        if self.filters.get("maintainer"):
            active_filters.append(f"Maintainer: '{self.filters['maintainer']}'")
        if self.filters.get("provides"):
            active_filters.append(f"Provides: '{self.filters['provides']}'")

        status_label = self.query_one("#filter-status", Label)
        if active_filters:
            status_label.update(f"Active filters: {', '.join(active_filters)}")
        else:
            status_label.update("No active filters.")

    def sort_packages(self) -> None:
        key_map = {
            "sort-name": "Name",
            "sort-first": "FirstSubmitted",
            "sort-last": "LastModified",
            "sort-votes": "NumVotes",
            "sort-popularity": "Popularity",
        }
        sort_key = key_map.get(self.current_sort, "Name")

        # Handle numerical and string sorting appropriately
        if sort_key in ["NumVotes", "Popularity", "FirstSubmitted", "LastModified"]:
            self.filtered_packages.sort(
                key=lambda p: p.get(sort_key, 0), reverse=self.current_sort_reverse
            )
        else:  # Default to string sorting for Name, etc.
            self.filtered_packages.sort(
                key=lambda p: str(p.get(sort_key, "")).lower(),
                reverse=self.current_sort_reverse,
            )

    def action_sort(self) -> None:
        def on_sort_modal_closed(result: Optional[Dict[str, Any]]) -> None:
            if result:
                self.current_sort = result["sort_key"]
                self.current_sort_reverse = result["reverse"]
                self.sort_packages()
                self.reset_display()
                self.update_package_list()

        self.push_screen(
            SortModal(
                current_sort_key=self.current_sort,
                current_sort_reverse=self.current_sort_reverse,
            ),
            on_sort_modal_closed,
        )

    def action_filter(self) -> None:
        def on_filter_modal_closed(result: bool | None) -> None:
            if result:
                modal = self._filter_modal
                if modal:
                    self.filters["abandoned"] = modal.query_one(
                        "#filter-abandoned", Checkbox
                    ).value
                    self.filters["out_of_date"] = modal.query_one(
                        "#filter-out-of-date", Checkbox
                    ).value
                    self.filters["maintainer"] = modal.query_one(
                        "#filter-maintainer", Input
                    ).value
                    self.filters["provides"] = modal.query_one(
                        "#filter-provides", Input
                    ).value

                    self.filter_packages(self.query_one("#search-input", Input).value)
                    self.update_package_list()
                    self.update_filter_status()

        self._filter_modal = FilterModal(
            initial_abandoned=self.filters.get("abandoned", False),
            initial_out_of_date=self.filters.get("out_of_date", False),
            initial_maintainer=self.filters.get("maintainer", ""),
            initial_provides=self.filters.get("provides", ""),
        )
        self.push_screen(self._filter_modal, on_filter_modal_closed)

    def action_view_comments(self) -> None:
        table = self.query_one("#package-table", DataTable)
        if table.row_count > 0:
            try:
                row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
                package_id = row_key.value
                package = next(
                    (p for p in self.packages if str(p.get("ID")) == package_id), None
                )
                if package:
                    self.push_screen(CommentsModal(package_data=package))
            except Exception:
                pass

    def action_download_from_aur(self) -> None:
        self.load_aur_packages(force_download=True)

    def action_profiles(self) -> None:
        def on_profile_modal_closed(result: Optional[Dict[str, Any]]) -> None:
            if result:
                action = result.get("action")

                if action == "load":
                    profile_name = result.get("profile_name")
                    if profile_name:
                        self.profiles = result.get("profiles", self.profiles)
                        self.default_profile_name = result.get(
                            "default_profile", self.default_profile_name
                        )
                        self.load_profile(
                            profile_name, self.profiles.get(profile_name, {})
                        )
                        self.save_app_config()
                elif action == "cancel":
                    self.profiles = result.get("profiles", self.profiles)
                    self.default_profile_name = result.get(
                        "default_profile", self.default_profile_name
                    )

                    # If the current profile was deleted, load the default
                    if self.current_profile_name not in self.profiles:
                        self.load_profile(
                            self.default_profile_name,
                            self.profiles.get(self.default_profile_name, {}),
                        )

                    self.save_app_config()

        self.push_screen(
            ProfileModal(
                profiles=self.profiles,
                default_profile=self.default_profile_name,
                current_profile=self.current_profile_name,
                current_settings=self.get_current_settings(),
            ),
            on_profile_modal_closed,
        )

    @on(DataTable.RowHighlighted, "#package-table")
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.check_load_more()
        if self._dep_resolve_timer:
            self._dep_resolve_timer.stop()

        package_id = event.row_key.value
        package = next(
            (p for p in self.packages if str(p.get("ID")) == package_id), None
        )

        if package:
            details_pane = self.query_one("#package-details", PackageDetails)
            details_pane.update_package(package=package)

            self._dep_resolve_timer = self.set_timer(
                self.DEP_RESOLVE_DELAY,
                lambda: self.resolve_package_dependencies(package),
            )

    @on(DataTable.RowSelected, "#package-table")
    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        package_id = event.row_key.value
        package = next(
            (p for p in self.packages if str(p.get("ID")) == package_id), None
        )
        if package:
            # Automatically open GitViewModal on row selection
            self.push_screen(
                GitViewModal(package_data=package, cache_base_path=self.git_cache_path)
            )

    @on(Input.Changed, "#search-input")
    def on_input_changed(self, event: Input.Changed) -> None:
        self.filter_packages(event.value)
        self.update_package_list()

    def save_app_config(self):
        """Saves the current application configuration to a file."""
        config_to_save = {
            "default_profile": self.default_profile_name,
            "profiles": self.profiles,
        }
        try:
            os.makedirs(self.config_path_dir, exist_ok=True)
            with open(self.config_file, "w") as f:
                json.dump(config_to_save, f, indent=4)
            log.info(f"App configuration saved to {self.config_file}")
        except Exception as e:
            self.notify(f"Failed to save configuration: {e}", severity="error")

    def save_current_profile(self):
        """Saves the current profile settings."""
        self.profiles[self.current_profile_name] = self.get_current_settings()
        self.save_app_config()

    @on(Input.Submitted, "#search-input")
    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.filter_packages(event.value)
        self.update_package_list()
        self.query_one("#package-table", DataTable).focus()

        # --------------- Hotkeys/VIM-like navigation ------

    def action_cursor_down(self) -> None:
        if isinstance(self.focused, DataTable):
            self.focused.action_cursor_down()
        if isinstance(self.focused, PackageDetails):
            self.focused.action_scroll_down()

    def action_cursor_up(self) -> None:
        if isinstance(self.focused, DataTable):
            self.focused.action_cursor_up()
        if isinstance(self.focused, PackageDetails):
            self.focused.action_scroll_up()

    def action_cursor_top(self) -> None:
        if isinstance(self.focused, DataTable):
            table = self.query_one("#package-table", DataTable)
            if table.row_count > 0:
                table.move_cursor(row=0)
                # table.move_cursor(row=0, column=table.cursor_column)
        if isinstance(self.focused, PackageDetails):
            self.focused.action_scroll_home()

    def action_cursor_bottom(self) -> None:  # Bound to 'G'
        if isinstance(self.focused, DataTable):
            table = self.query_one("#package-table", DataTable)
            # Load all packages before going to bottom
            while self.load_more_packages():
                pass  # Keep loading until all are in self.displayed_packages

            # After all are loaded, update table fully if new ones were added
            # This check_load_more might have added some, but update_package_list ensures all displayed are in table
            self.update_package_list()  # This will rebuild the table with all items

            if table.row_count > 0:
                table.move_cursor(row=table.row_count - 1)
                # table.move_cursor(row=table.row_count - 1, column=table.cursor_column)
            self.update_title()
        if isinstance(self.focused, PackageDetails):
            self.focused.action_scroll_end()

    def action_page_down(self) -> None:
        self.query_one("#package-table", DataTable).action_page_down()
        # self.check_load_more() # Handled by RowHighlighted

    def action_page_up(self) -> None:
        self.query_one("#package-table", DataTable).action_page_up()
