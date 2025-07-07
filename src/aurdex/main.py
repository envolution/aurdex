import os
import appdirs
import json
import re
import logging as log
import time
import threading

from typing import Optional, List, Dict, Any

from textual import on, work
from textual.events import Key, MouseDown
from textual.binding import Binding
from textual.timer import Timer
from textual.logging import TextualHandler
from textual.app import App, ComposeResult
from textual.containers import Container, Vertical
from textual.widgets import (
    Footer,
    Input,
    DataTable,
    Label,
    Checkbox,
    LoadingIndicator,
)

from textual.coordinate import Coordinate


from .db import PackageDB
from .widgets import (
    FilterModal,
    SortModal,
    PackageDetails,
    GitViewModal,
    CommentsModal,
    ProfileModal,
    CustomHeader,
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
        self,
        profile_name: Optional[str] = None,
        db: Optional[PackageDB] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.startup_profile = profile_name
        self.provide_db = db or PackageDB()
        self.filtered_packages: List[Dict[str, Any]] = []
        self.displayed_packages: List[Dict[str, Any]] = []
        self.current_sort = "sort-popularity"
        self.current_sort_reverse = True
        self.search_term = ""
        self.chunk_size = 1000
        self.loaded_count = 0

        self.config_path_dir = appdirs.user_config_dir(appname="aurdex")
        self.config_file = os.path.join(self.config_path_dir, "settings.json")
        self.cache_path_dir = appdirs.user_cache_dir(appname="aurdex")

        self.default_profile_name = "default"
        self.current_profile_name = "default"
        self.profiles = {}
        self.default_filters_structure = {
            "abandoned": False,
            "out_of_date": False,
            "maintainer": "",
            "provides": "",
            "repos": [],
        }
        self.filters = self.default_filters_structure.copy()
        self._filter_modal: Optional[FilterModal] = None

        self._dep_resolve_timer: Optional[Timer] = None
        self.DEP_RESOLVE_DELAY: float = 0.05
        self._search_timer: Optional[Timer] = None
        self.SEARCH_DEBOUNCE_DELAY: float = 0.3
        self._last_input = None
        self._dep_resolve_cancel_event: Optional[threading.Event] = None

    def compose(self) -> ComposeResult:
        yield CustomHeader()
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
        yield LoadingIndicator(id="loading-indicator")

    def on_mount(self) -> None:
        self.update_title()
        self.query_one(CustomHeader).db_age = self.provide_db.db_age
        table = self.query_one("#package-table", DataTable)
        table.add_column("Name", key="name", width=None)
        table.add_column("Version", width=12, key="version")
        table.add_column("Votes", width=6, key="votes")
        table.add_column("Pop.", width=6, key="popularity")
        table.focus()

        self.query_one("#loading-indicator").display = False

        os.makedirs(self.config_path_dir, exist_ok=True)
        os.makedirs(self.cache_path_dir, exist_ok=True)

        self.load_app_config()
        self.query_one("#search-input", Input).value = self.search_term
        self.action_refresh()

    def action_refresh(self) -> None:
        self.filter_packages()
        self.update_package_list()
        self.update_filter_status()

    def action_reset_filters(self) -> None:
        self.filters = self.default_filters_structure.copy()
        self.search_term = ""
        self.query_one("#search-input", Input).value = ""
        self.filter_packages()
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

    @work(exclusive=True, thread=True)
    async def update_package_details_worker(
        self, package_name: str, package_source: str, cancel_event: threading.Event
    ) -> None:
        """Worker to fetch, process, and display package details."""
        if cancel_event.is_set():
            return

        # --- Initial data fetch ---
        package_data = self.provide_db.package_info(name=package_name, source=package_source)
        if not package_data:
            return

        if cancel_event.is_set():
            return

        # --- Update UI with basic info ---
        details_pane = self.query_one("#package-details", PackageDetails)
        self.call_from_thread(details_pane.update_package, package=package_data)

        if cancel_event.is_set():
            return

        # --- Dependency and Dependant Resolution (heavy part) ---
        enriched_deps = self.provide_db.get_enriched_dependencies(package_data)
        if cancel_event.is_set():
            return

        dependants_by_provide = self.provide_db.get_dependants(
            package_name, package_data.get("Provides", [])
        )
        if cancel_event.is_set():
            return

        # --- Final UI update with enriched data ---
        self.call_from_thread(
            details_pane.update_package,
            package=package_data,
            enriched_dependencies=enriched_deps,
            enriched_dependants=dependants_by_provide,
        )

    def action_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_clear_search(self) -> None:
        search_input = self.query_one("#search-input", Input)
        search_input.value = ""
        self.search_term = ""
        self.filter_packages()
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
        self.filter_packages()
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
        self.title = "aurdex"
        if self.current_profile_name != "default":
            self.title = f"Profile: {self.current_profile_name} - aurdex"

        if self.loaded_count:
            self.sub_title = f" - showing ({self.loaded_count}/{len(self.filtered_packages)}) packages"
        self.query_one(CustomHeader).refresh_header_text()

    @work(exclusive=True, thread=True)
    def search_packages_worker(self) -> None:
        """Perform package search in a background thread."""
        sort_key_map = {
            "sort-name": "name",
            "sort-first": "first_submitted",
            "sort-last": "last_modified",
            "sort-votes": "num_votes",
            "sort-popularity": "popularity",
        }
        sort_by = sort_key_map.get(self.current_sort, "popularity")

        results = self.provide_db.search(
            search_term=self.search_term,
            filters=self.filters,
            sort_by=sort_by,
            sort_reverse=self.current_sort_reverse,
            limit=200000,
        )
        self.call_from_thread(self.update_search_results, results)

    def update_search_results(self, packages: List[Dict[str, Any]]) -> None:
        """Update the UI with the new search results."""
        self.filtered_packages = packages
        self.reset_display()
        self.update_package_list()
        self.update_filter_status()

    def filter_packages(self) -> None:
        """Trigger the background search worker."""
        self.search_packages_worker()

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
        ) - 20 and self.loaded_count < len(self.filtered_packages):
            if self.load_more_packages():
                current_cursor_row = table.cursor_row

                table_row_count = table.row_count
                new_packages_chunk = self.displayed_packages[table_row_count:]

                for package in new_packages_chunk:
                    key = f"{package['name']}:{package['source']}"
                    table.add_row(
                        "/".join(
                            [
                                f"[dim]{package.get('source', '?')}",
                                f"[/dim][b]{package.get('name', 'Unknown')}[/]",
                            ]
                        ),
                        package.get("version", "Unknown"),
                        str(package.get("num_votes", 0)),
                        f"{package.get('popularity', 0):.2f}",
                        key=key,
                    )

                if current_cursor_row < table.row_count:
                    table.cursor_coordinate = Coordinate(
                        current_cursor_row, table.cursor_column
                    )
                self.update_title()

    def reset_display(self) -> None:
        self.displayed_packages = []
        self.loaded_count = 0
        self.load_more_packages()
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
            key = f"{package['name']}:{package['source']}"
            table.add_row(
                "/".join(
                    [
                        f"[dim]{package.get('source', '?')}",
                        f"[/dim][b]{package.get('name', 'Unknown')}[/]",
                    ]
                ),
                package.get("version", "Unknown"),
                str(package.get("num_votes", 0)),
                f"{package.get('popularity', 0):.2f}",
                key=key,
            )
            if key == current_cursor_key:
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
        self.update_title()

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

        if "repos" in self.filters and self.filters["repos"]:
            active_filters.append(f"Repos: {', '.join(self.filters['repos'])}")

        status_label = self.query_one("#filter-status", Label)
        if active_filters:
            status_label.update(f"Active filters: {', '.join(active_filters)}")
        else:
            status_label.update("No active filters.")

    def action_sort(self) -> None:
        def on_sort_modal_closed(result: Optional[Dict[str, Any]]) -> None:
            if result:
                self.current_sort = result["sort_key"]
                self.current_sort_reverse = result["reverse"]
                self.filter_packages()

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

                    # Handle repo filters
                    selected_repos = []
                    for repo in modal.all_repos:
                        if modal.query_one(f"#filter-repo-{repo}", Checkbox).value:
                            selected_repos.append(repo)
                    self.filters["repos"] = selected_repos

                    self.filter_packages()
                    self.update_package_list()
                    self.update_filter_status()

        all_repos = self.provide_db.get_repo_names()
        self._filter_modal = FilterModal(
            initial_abandoned=self.filters.get("abandoned", False),
            initial_out_of_date=self.filters.get("out_of_date", False),
            initial_maintainer=self.filters.get("maintainer", ""),
            initial_provides=self.filters.get("provides", ""),
            repo_filters={
                repo: repo in self.filters.get("repos", all_repos) for repo in all_repos
            },
            all_repos=all_repos,
        )
        self.push_screen(self._filter_modal, on_filter_modal_closed)

    def action_view_comments(self) -> None:
        table = self.query_one("#package-table", DataTable)
        if table.row_count > 0:
            try:
                row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
                name, source = row_key.value.split(":")
                package = self.provide_db.package_info(name=name, source=source)
                if package:
                    self.push_screen(CommentsModal(package_data=package))
            except Exception:
                pass

    @work(exclusive=True, thread=True)
    def action_download_from_aur(self) -> None:
        """Worker thread to rebuild the database."""
        self.call_from_thread(
            setattr, self.query_one("#loading-indicator"), "display", True
        )
        start_time = time.time()
        try:
            # We trigger a non-full rebuild, but with a fresh download.
            updated_count = self.provide_db.rebuild(full=False, download=True)
            end_time = time.time()
            elapsed = end_time - start_time
            self.call_from_thread(
                self.notify,
                f"Database update completed in {elapsed:.2f}s. {updated_count} packages updated.",
            )
            self.call_from_thread(
                setattr,
                self.query_one(CustomHeader),
                "db_age",
                self.provide_db.db_age,
            )
            self.call_from_thread(self.action_refresh)
        except Exception as e:
            log.error(f"Error rebuilding database: {e}")
            self.call_from_thread(
                self.notify, f"Error updating database: {e}", severity="error"
            )
        finally:
            self.call_from_thread(
                setattr, self.query_one("#loading-indicator"), "display", False
            )

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
        if self._dep_resolve_cancel_event:
            self._dep_resolve_cancel_event.set()

        if not event.row_key.value:
            return

        details_pane = self.query_one("#package-details", PackageDetails)
        details_pane.display_loading()

        name, source = event.row_key.value.split(":")
        
        self._dep_resolve_cancel_event = threading.Event()
        self._dep_resolve_timer = self.set_timer(
            self.DEP_RESOLVE_DELAY,
            lambda: self.update_package_details_worker(
                name, source, self._dep_resolve_cancel_event
            ),
        )

    @on(DataTable.RowSelected, "#package-table")
    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if not event.row_key.value:
            return
        name, source = event.row_key.value.split(":")
        package = self.provide_db.package_info(name=name, source=source)
        self.log.debug("DATATABLE=> ", "{package}")
        if self._last_input == "keyboard":
            if package:
                if package.get("source") == "aur":
                    if package.get("PackageBase"):
                        self.push_screen(
                            GitViewModal(
                                package_data=package,
                                cache_base_path=self.cache_path_dir,
                            )
                        )
                    else:
                        self.notify(
                            "Error: PackageBase missing for this AUR package.",
                            severity="error",
                        )
                else:
                    self.notify(
                        "Git view is only available for AUR packages.",
                        severity="warning",
                    )

    @on(Input.Changed, "#search-input")
    def on_input_changed(self, event: Input.Changed) -> None:
        """Debounce the search input."""
        self.search_term = event.value
        if self._search_timer:
            self._search_timer.stop()
        self._search_timer = self.set_timer(
            self.SEARCH_DEBOUNCE_DELAY, self.filter_packages
        )

    def save_app_config(self):
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
        self.profiles[self.current_profile_name] = self.get_current_settings()
        self.save_app_config()

    @on(Input.Submitted, "#search-input")
    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.search_term = event.value
        self.filter_packages()
        self.query_one("#package-table", DataTable).focus()

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
        if isinstance(self.focused, PackageDetails):
            self.focused.action_scroll_home()

    def action_cursor_bottom(self) -> None:
        if isinstance(self.focused, DataTable):
            table = self.query_one("#package-table", DataTable)
            while self.load_more_packages():
                pass
            self.update_package_list()
            if table.row_count > 0:
                table.move_cursor(row=table.row_count - 1)
            self.update_title()
        if isinstance(self.focused, PackageDetails):
            self.focused.action_scroll_end()

    def action_page_down(self) -> None:
        self.query_one("#package-table", DataTable).action_page_down()

    def action_page_up(self) -> None:
        self.query_one("#package-table", DataTable).action_page_up()
