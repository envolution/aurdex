#!/usr/bin/env python3
"""
AUR Package Browser TUI
A Textual-based terminal interface for browsing AUR package metadata
"""

import json
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
import appdirs
import os
import urllib.request
import gzip
import traceback

import fnmatch
import re

from textual import on
from textual.coordinate import Coordinate

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Header,
    Footer,
    Input,
    DataTable,
    Static,
    Button,
    Checkbox,
    RadioSet,
    RadioButton,
    Label,
    DirectoryTree,
)
from textual.binding import Binding
from textual.screen import ModalScreen
from textual import work
from textual.timer import Timer

from rich.syntax import Syntax  # code highlighting

try:
    import pygit2

    PYGIT2_AVAILABLE = True
except ImportError:
    PYGIT2_AVAILABLE = False
    pygit2 = None  # type: ignore

try:
    import pyalpm
except ImportError:
    pyalpm = None


class ProvideDB:
    def __init__(
        self,
        repos: Tuple[str, ...] = ("core", "extra", "multilib", "testing"),
        aur_data: Optional[List[Dict]] = None,
        pacman_db_path: str = "/var/lib/pacman",
    ):
        self.provide_db = {}
        self.pacman_db_path = pacman_db_path
        self.repos = repos
        self.aur_data = aur_data or []
        self.available = False
        self.handle = None
        self._has_sync_dbs = False

        if pyalpm:
            try:
                self.handle = pyalpm.Handle("/", self.pacman_db_path)
                self.localdb = self.handle.get_localdb()
                for repo in repos:
                    self.handle.register_syncdb(repo, pyalpm.SIG_DATABASE_OPTIONAL)
                if self.handle.get_syncdbs():
                    self._has_sync_dbs = True
            except Exception as e:
                print(
                    f"Warning: pyalpm failed to initialize with {self.pacman_db_path} : {e}"
                )

        if self._has_sync_dbs or self.aur_data:
            self.available = True
            self.refresh()
        else:
            print("Warning: No usable package sources (sync DBs or AUR JSON).")

    def refresh(self, aur_data=None):
        """
        Rebuild the internal provide database from sync repositories and optional AUR metadata.

        This clears and repopulates the provide_db dictionary. If sync repositories
        are available (via pyalpm), their packages are loaded first. If AUR metadata
        is provided (either at init or as an override here), it is also included.

        Parameters:
            aur_data (list[dict] | None): Optional override list of AUR packages
                in the same format as the `packages-meta-ext-v1.json` AUR metadata.

        Each package entry inserted includes:
            - 'name': package name
            - 'version': package version
            - 'repo': repository name ('core', 'aur', etc.)
            - 'provides': list of virtual names it provides (including its own name)
        """
        self.provide_db.clear()
        self.aur_data = aur_data or self.aur_data

        # sync db (only if pyalpm is available and initialized)
        if self.handle:
            try:
                for db in self.handle.get_syncdbs():
                    for pkg in db.pkgcache:
                        raw_provides = set(pkg.provides)
                        provides = {p.split("=")[0] for p in raw_provides}
                        provides.add(pkg.name)
                        installed = (
                            self.localdb.get_pkg(pkg.name) is not None
                            if self.handle
                            else False
                        )
                        pkg_entry = {
                            "name": pkg.name,
                            "version": pkg.version,
                            "repo": db.name,
                            "provides": sorted(provides),
                            "installed": installed,
                        }
                        for provided in provides:
                            self.provide_db.setdefault(provided, []).append(pkg_entry)
            except Exception as e:
                print(f"Warning: failed to load sync DBs: {e}")

        for entry in self.aur_data:
            name = entry.get("Name")
            version = entry.get("Version")
            raw_provides = entry.get("Provides", [])
            provides = {p.split("=")[0] for p in raw_provides}
            provides.add(name)
            installed = self.localdb.get_pkg(name) is not None if self.handle else False
            pkg_entry = {
                "name": name,
                "version": version,
                "repo": "aur",
                "provides": sorted(provides),
                "installed": installed,
            }
            for provided in provides:
                self.provide_db.setdefault(provided, []).append(pkg_entry)

    def find_providers(self, virtual_name):
        """
        Return a list of package entries that provide the given virtual name.

        A package is included if it either directly matches the given name
        or lists it in its 'provides' field. Each result is a dict containing:
        - 'name': package name
        - 'version': package version
        - 'repo': repository name (e.g. 'core', 'aur')
        - 'provides': list of all virtual names this package provides
        """
        if not self.available:
            return []
        return self.provide_db.get(virtual_name, [])

    def find_all_provides_from(self, virtual_name):
        """
        Return a set of all virtual names provided by any package that provides the given name.

        This performs a transitive lookup: first, it finds all packages that provide
        'virtual_name'; then it collects all 'provides' values from those packages.

        Useful for understanding the full set of alternate or related virtuals.
        """
        if not self.available:
            return set()
        providers = self.find_providers(virtual_name)
        return set(p for pkg in providers for p in pkg["provides"])

    def is_installed(self, package_name: str) -> bool:
        """
        Return True if the package is currently installed in the local database.
        """
        if not self.handle:
            return False
        return self.localdb.get_pkg(package_name) is not None


class FilterModal(ModalScreen[bool | None]):
    def __init__(
        self,
        initial_abandoned: bool = False,
        initial_out_of_date: bool = False,
        initial_maintainer: str = "",
        initial_provides: str = "",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.initial_abandoned = initial_abandoned
        self.initial_out_of_date = initial_out_of_date
        self.initial_maintainer = initial_maintainer
        self.initial_provides = initial_provides

    def compose(self) -> ComposeResult:
        with Container(id="modal-dialog-scrim"):
            with Container(id="filter-modal-dialog"):
                yield Label("Filter Packages", id="filter-title")
                yield Checkbox(
                    "Abandoned (no maintainer)",
                    value=self.initial_abandoned,
                    id="filter-abandoned",
                )
                yield Checkbox(
                    "Out of Date",
                    value=self.initial_out_of_date,
                    id="filter-out-of-date",
                )
                yield Input(
                    placeholder="Maintainer contains...",
                    value=self.initial_maintainer,
                    id="filter-maintainer",
                )
                yield Input(
                    placeholder="Provides contains...",
                    value=self.initial_provides,
                    id="filter-provides",
                )
                with Horizontal(id="filter-buttons"):
                    yield Button("Apply", variant="primary", id="filter-apply")
                    yield Button("Cancel", variant="default", id="filter-cancel")

    @on(Button.Pressed, "#filter-apply")
    def apply_filters(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#filter-cancel")
    def cancel_filters(self) -> None:
        self.dismiss(False)


class SortModal(ModalScreen[Optional[Dict[str, Any]]]):
    """Modal for selecting sort options."""

    def __init__(
        self, current_sort_key: str, current_sort_reverse: bool, **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.current_sort_key = current_sort_key
        self.current_sort_reverse = current_sort_reverse

    def compose(self) -> ComposeResult:
        with Container(id="modal-dialog-scrim"):
            with Container(id="sort-modal-dialog"):
                yield Label("Sort packages by:", id="sort-title")
                with RadioSet(id="sort-options"):
                    yield RadioButton("Name (A-Z)", id="sort-name")
                    yield RadioButton("First Submitted", id="sort-first")
                    yield RadioButton("Last Modified", id="sort-last")
                    yield RadioButton("Number of Votes", id="sort-votes")
                    yield RadioButton("Popularity", id="sort-popularity")
                yield Checkbox(
                    "Reverse Sort Order",
                    value=self.current_sort_reverse,
                    id="sort-reverse-checkbox",
                )
                with Horizontal(id="sort-buttons"):
                    yield Button("Apply", variant="primary", id="sort-apply")
                    yield Button("Cancel", variant="default", id="sort-cancel")

    def on_mount(self) -> None:
        """Pre-select the current sort option."""
        self.app.log(
            f"SortModal.on_mount: Initializing with current_sort_key='{self.current_sort_key}', current_sort_reverse={self.current_sort_reverse}"
        )
        try:
            radio_set = self.query_one(
                RadioSet
            )  # self here refers to SortModal instance
            found_button = False
            for button in radio_set.query(RadioButton):
                if button.id == self.current_sort_key:  # Compare with the ID
                    button.value = True
                    found_button = True
                    break
            if not found_button:
                self.app.log(
                    f"SortModal.on_mount: Sort key '{self.current_sort_key}' did not match any RadioButton ID."
                )
        except Exception as e:
            self.app.log(f"SortModal.on_mount: Error pre-selecting sort option: {e}")

    @on(Button.Pressed, "#sort-apply")
    def apply_sort(self) -> None:
        radio_set = self.query_one("#sort-options", RadioSet)
        reverse_checkbox = self.query_one("#sort-reverse-checkbox", Checkbox)

        if radio_set.pressed_button:
            # The id of the pressed RadioButton is our string sort key
            sort_key_from_button = radio_set.pressed_button.id

            self.app.log(
                f"SortModal apply_sort: sort_key_from_button='{sort_key_from_button}' (type: {type(sort_key_from_button)})"
            )

            self.dismiss(
                {
                    "sort_key": str(
                        sort_key_from_button
                    ),  # Ensure it's a string, though id should be
                    "reverse": reverse_checkbox.value,
                }
            )
        else:
            self.app.log("SortModal apply_sort: No radio button pressed.")
            self.dismiss(None)  # No option selected

    @on(Button.Pressed, "#sort-cancel")
    def cancel_sort(self) -> None:
        self.dismiss(None)


class PackageDetails(VerticalScroll):
    """Widget to display detailed package information"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._static_content = Static()
        self.package_data: Optional[Dict[str, Any]] = None
        self.enriched_dependencies: Optional[Dict[str, List[Dict]]] = None

    def compose(self):
        yield self._static_content

    def update(self, content):
        """Keep your existing update interface"""
        self._static_content.update(content)

    def update_package(
        self,
        package: Dict[str, Any],
        enriched_dependencies: Optional[Dict[str, List[Dict]]] = None,
    ) -> None:
        """Update the displayed package information using Textual inline styling."""
        self.package_data = package
        self.enriched_dependencies = enriched_dependencies

        if not package:
            self.update("[dim italic]Select a package to see details.[/dim]")
            return

        first_submitted_dt = datetime.fromtimestamp(package.get("FirstSubmitted", 0))
        last_modified_dt = datetime.fromtimestamp(package.get("LastModified", 0))

        first_submitted = first_submitted_dt.strftime("%Y-%m-%d %H:%M:%S")
        last_modified = last_modified_dt.strftime("%Y-%m-%d %H:%M:%S")

        maintainer = package.get("Maintainer")
        comaintainers = package.get("CoMaintainers", [])
        all_maintainers_list = [
            m for m in ([maintainer] + comaintainers) if m is not None
        ]
        all_maintainers_str = (
            ", ".join(all_maintainers_list)
            if all_maintainers_list
            else "[dim]None[/dim]"
        )

        submitter = package.get("Submitter", "[dim]_Not specified_[/dim]")

        # --- Build content string with Textual styling ---
        content_parts = []

        # Section: Votes, Popularity, Out of Date
        ood_status_text = "Yes" if package.get("OutOfDate") else "No"
        ood_style_tag = "[b $warning]" if package.get("OutOfDate") else "[b $success]"

        content_parts.append(
            f"[b $text]Votes:[/] [b $primary]{package.get('NumVotes', 0)}[/]  "
            f"[b $text]Popularity:[/] [b $primary]{package.get('Popularity', 0):.2f}[/]  "
            f"[b $text]Out of Date:[/] {ood_style_tag}{ood_status_text}[/]\n\n"
        )
        #
        # Section: Name, Version, Description
        content_parts.append(
            f"[b $primary]{package.get('Name', 'Unknown')}[/] - [dim $secondary]{package.get('Version', 'Unknown')}[/]\n"
            f"[italic $text-subtle]{package.get('Description', 'No description available.')}[/]\n\n"
        )  # Ensure $text-subtle is defined in CSS or use a default like $foreground-darken-2

        # Section: Core Details
        content_parts.append(
            f"[b $accent]ID:[/] [$text]{package.get('ID', '[dim]Unknown[/dim]')}[/$text]\n"
        )
        content_parts.append(
            f"[b $accent]PackageBase:[/] [$text]{package.get('PackageBase', '[dim]Unknown[/dim]')}[/$text]\n"
        )
        content_parts.append(f"[b $accent]Submitter:[/] [$text]{submitter}[/$text]\n")

        url_val = package.get("URL")
        url_display = (
            f"[$link]{url_val}[/$link]" if url_val else "[dim]_Not specified_[/dim]"
        )
        content_parts.append(f"[b $accent]URL:[/] {url_display}\n")

        aur_path = package.get("URLPath")
        aur_link_full = f"https://aur.archlinux.org{aur_path}"
        aur_display = (
            f"[$link]{aur_link_full}[/$link]"
            if aur_path
            else "[dim]_Not specified_[/dim]"
        )
        content_parts.append(f"[b $accent]AUR Link:[/] {aur_display}\n")

        keywords_list_data = package.get("Keywords", [])
        keywords_str_val = (
            f"{', '.join(keywords_list_data)}"
            if keywords_list_data
            else "[dim]None[/dim]"
        )
        content_parts.append(
            f"[b $accent]Keywords:[/] [$text-muted]{keywords_str_val}[/$text-muted]\n\n"
        )

        # Section: Dates and Maintainers
        content_parts.append(
            f"[b $accent]Last Modified:[/] [b $text]{last_modified}[/]\n"
            f"[b $accent]First Submitted:[/] [b $text]{first_submitted}[/]\n"
            f"[b $accent]Maintainer(s):[/] [i $text]{all_maintainers_str}[/]\n\n"
        )

        # Section: Dependencies, Provides, Conflicts, Replaces, Groups
        list_sections_config_data = [
            ("Conflicts", "Conflicts", False),
            ("Replaces", "Replaces", False),
            ("Groups", "Groups", False),
            ("Dependencies", "Depends", True),
            ("Optional Dependencies", "OptDepends", True),
            ("Make Dependencies", "MakeDepends", True),
            ("Check Dependencies", "CheckDepends", True),
            ("Provides", "Provides", False),
        ]

        has_any_list_content_flag = False
        for (
            section_title_str,
            package_key_str,
            use_enriched_logic_flag,
        ) in list_sections_config_data:
            items_for_section_list = []
            is_enriched_data_flag = False

            if (
                use_enriched_logic_flag
                and self.enriched_dependencies
                and package_key_str in self.enriched_dependencies
            ):
                items_for_section_list = self.enriched_dependencies[package_key_str]
                is_enriched_data_flag = True
            elif package.get(package_key_str):
                raw_list = package.get(package_key_str, [])
                if use_enriched_logic_flag:
                    for item_spec in raw_list:
                        cleaned_name = (
                            item_spec.split(":")[0]
                            .split("=")[0]
                            .split("<")[0]
                            .split(">")[0]
                            .strip()
                        )
                        description = (
                            item_spec.split(":", 1)[1].strip()
                            if ":" in item_spec
                            else None
                        )
                        items_for_section_list.append(
                            {
                                "name": cleaned_name,
                                "original_spec": item_spec,
                                "description": description,
                                "providers": None,
                            }
                        )
                else:
                    items_for_section_list = raw_list

            if items_for_section_list:
                if not has_any_list_content_flag:
                    content_parts.append("\n[dim]---[/dim]\n\n")  # separator
                    has_any_list_content_flag = True

                content_parts.append(f"[b $text on $panel]{section_title_str}[/]\n")

                if is_enriched_data_flag or (
                    use_enriched_logic_flag
                    and items_for_section_list
                    and isinstance(items_for_section_list[0], dict)
                ):
                    for dep_item in items_for_section_list:
                        content_parts.append(
                            f"  [dim]-[/dim] [$secondary]{dep_item['original_spec']}[/$secondary]\n"
                        )
                        if dep_item.get("providers") is not None:
                            providers = dep_item["providers"]
                            if providers:
                                for provider_pkg_item in providers:
                                    installed_tag = (
                                        " [b $success](installed)[/]"
                                        if provider_pkg_item.get("installed")
                                        else ""
                                    )
                                    content_parts.append(
                                        f"    [dim]-[/dim] [$text-subtle]{provider_pkg_item.get('repo', 'N/A')}/{provider_pkg_item.get('name', 'N/A')}[/$text-subtle]"
                                        f" [dim $text-subtle]({provider_pkg_item.get('version', 'N/A')})[/]{installed_tag}\n"
                                    )
                            else:
                                content_parts.append(
                                    "    [dim]-[/dim] [italic $warning][Not Available][/]\n"
                                )
                else:  # Simple list items
                    for item_val_str in items_for_section_list:
                        content_parts.append(
                            f"  [dim]-[/dim] [$secondary]{item_val_str}[/$secondary]\n"
                        )
                content_parts.append("\n")

        if not has_any_list_content_flag:
            content_parts.append(
                "[dim italic align=center]_No explicit dependencies, provisions, conflicts, or groups listed._[/]\n\n"
            )

        # Section: License
        license_data = package.get("License", [])
        license_text = (
            f"{', '.join(license_data)}" if license_data else "[dim]Unknown[/dim]"
        )
        content_parts.append("\n[dim]---[/dim]\n\n")
        content_parts.append(f"[b $accent]License(s):[/] [b $text]{license_text}[/]\n")

        self.update("".join(content_parts))


class GitViewModal(ModalScreen[None]):
    """Modal to display Git repository details for a package."""

    DEFAULT_CSS = """
    GitViewModal {
        layout: vertical;
    #    padding: 1 2;
        background: $surface;
        border: round $primary;
    #    width: 85%;
    #    height: 85%;
        width: 1fr;
        height: 1fr;
    }

    #git-modal-title {
        dock: top;
        width: 100%;
        text-align: center;
        border: round $primary;
    }

    #git-status-label {
        dock: top;
        height: auto;
        padding: 0 1;
    #    margin: 0 0 1 1;
        content-align: center middle;
        border: round $primary;
        text-align: center;
        min-height: 1;
    }

    #git-main-container {
        layout: horizontal;
        height: 1fr;
    #    padding-top: 1;
    }

    #git-left-pane {
        layout: vertical;
        width: 35%;
    #    padding-right: 1;
        border-right: solid $primary-lighten-2;
    }

    #git-file-tree-container {
        border: round $accent;
        height: 50%;
    #    padding: 1;
    #    margin-bottom: 1;
    }
    #git-file-tree-container Label { width: 100%; text-align: center; padding-bottom: 1;}

    #git-file-tree {
        height: 1fr;
        background: $panel;
    }

    #git-commit-history-container {
        border: round $accent;
        height: 1fr; /* Takes remaining space in left pane */
    #    padding: 1;
    }
    #git-commit-history-container Label { \
        width: 100%; 
        text-align: center; 
    #    padding-bottom: 1;
    }


    #git-commit-history {
        height: 1fr;
        background: $panel;
    }

    #git-content-view-container {
        width: 1fr;
        height: 100%; /* Make it take full height of its allocated space in the horizontal layout */
        layout: vertical; /* So that height: 1fr on its child works */
    #    padding: 0 1 0 2;
    }
    #git-content-view-container Label { 
        width: 100%; 
        text-align: center; 
    #    padding-bottom: 1;
    }

    #git-content-scroll-wrapper {
        height: 1fr; /* Takes remaining space after the Label in git-content-view-container */
        overflow-y: scroll; /* This container handles the scrolling */
        background: $panel; /* Optional: move background here if desired */
        border: round $accent; /* Optional: move border here if desired */
    }    

    #git-content-view { /* This should already be fine but for completeness */
    #    padding: 1;
        width: 100%;
    }

    #git-close-button {
        width: auto; /* Shrink to content */
        min-width: 0;
    #    padding: 0 1; /* Less padding */
        height: 1; /* Make it compact */
        border-top: none; /* If it's in the footer, might not need top border */
        dock: right; /* Example: tuck it into the corner of the footer */
    }
    """
    BINDINGS = [
        Binding("escape", "close_modal", "Close", show=True),
        Binding("ctrl+r", "force_update_repo", "Force Update Repo", show=True),
    ]

    def __init__(self, package_data: Dict[str, Any], cache_base_path: str, **kwargs):
        super().__init__(**kwargs)
        self.package_data = package_data
        self.package_base = self.package_data.get("PackageBase")
        self.repo_url = f"https://aur.archlinux.org/{self.package_base}.git"
        self.cache_base_path = cache_base_path
        self.repo_path = os.path.join(
            self.cache_base_path, self.package_base or "_unknown_package_"
        )

        # Ensure repo_path directory exists for DirectoryTree initialization
        os.makedirs(self.repo_path, exist_ok=True)

        self.repo: Optional[pygit2.Repository] = None

    def compose(self) -> ComposeResult:
        with Container(id="git-modal-container"):  # Main container for the modal
            yield Label(
                f"Git Repository: {self.package_base or 'Unknown'}",
                id="git-modal-title",
            )
            yield Label("Initializing...", id="git-status-label")
            with Horizontal(id="git-main-container"):
                with Vertical(id="git-left-pane"):
                    with Container(id="git-file-tree-container"):
                        yield Label("Files")
                        yield DirectoryTree(self.repo_path, id="git-file-tree")
                    with Container(id="git-commit-history-container"):
                        yield Label("Commit History")
                        yield DataTable(id="git-commit-history", cursor_type="row")
                with Container(id="git-content-view-container"):
                    with Container(id="git-content-scroll-wrapper"):
                        yield Static("", id="git-content-view")
        yield Footer()

    async def on_mount(self) -> None:
        history_table = self.query_one("#git-commit-history", DataTable)
        history_table.add_column("SHA", width=8)
        history_table.add_column("Author", width=15)
        history_table.add_column("Date", width=17)
        history_table.add_column("Message")  # Flexible width

        if not PYGIT2_AVAILABLE:
            self.query_one("#git-status-label", Label).update(
                "[b red]Error: pygit2 library not found. Please install it (pip install pygit2).[/]"
            )
            return
        if not self.package_base:
            self.query_one("#git-status-label", Label).update(
                "[b red]Error: PackageBase not found. Cannot fetch Git repository.[/]"
            )
            return

        self.perform_git_operation()  # Will run in a worker thread

    @work(exclusive=True, thread=True)
    async def perform_git_operation(self) -> None:
        status_label = self.query_one("#git-status-label", Label)
        file_tree = self.query_one("#git-file-tree", DirectoryTree)
        commit_table = self.query_one("#git-commit-history", DataTable)

        # Type check for pygit2 to satisfy linters when PYGIT2_AVAILABLE is false
        if not pygit2:
            self.app.call_from_thread(
                status_label.update, "[b red]Pygit2 not loaded (internal error).[/]"
            )
            return

        try:
            self.app.call_from_thread(status_label.update, "Accessing local cache...")

            # discover_repository checks self.repo_path and its parents.
            # We want to check specifically if self.repo_path is a valid repo.
            is_repo = False
            try:
                if (
                    pygit2.Repository(self.repo_path).is_bare == False
                ):  # Or check for .git dir
                    is_repo = os.path.exists(os.path.join(self.repo_path, ".git"))
            except pygit2.GitError:  # Not a repository or path doesn't exist as repo
                is_repo = False

            if is_repo:
                self.app.call_from_thread(
                    status_label.update,
                    f"Pulling latest changes for [b]{self.package_base}[/]...",
                )
                self.repo = pygit2.Repository(self.repo_path)

                remote = self.repo.remotes["origin"]
                self.app.log(f"Fetching from remote {remote.name} ({remote.url})")
                remote.fetch()
                self.app.log("Fetch complete.")

                # Determine the remote head reference (e.g., refs/remotes/origin/master)
                remote_head_ref_name = None
                current_local_branch_name = self.repo.head.shorthand
                possible_remote_refs = [
                    f"refs/remotes/origin/{current_local_branch_name}",
                    "refs/remotes/origin/master",
                    "refs/remotes/origin/main",
                ]

                for ref_name_option in possible_remote_refs:
                    try:
                        if self.repo.lookup_reference(ref_name_option):
                            remote_head_ref_name = ref_name_option
                            self.app.log(
                                f"Found remote head ref: {remote_head_ref_name}"
                            )
                            break
                    except pygit2.KeyError:  # Ref not found
                        continue

                if not remote_head_ref_name:
                    self.app.call_from_thread(
                        status_label.update,
                        f"[b red]Error: Could not determine remote default branch for {self.package_base}.[/]",
                    )
                    return

                remote_head_commit_id = self.repo.lookup_reference(
                    remote_head_ref_name
                ).target

                local_branch_ref_name = self.repo.head.name
                self.repo.references[local_branch_ref_name].set_target(
                    remote_head_commit_id
                )
                self.repo.checkout_head(strategy=pygit2.GIT_CHECKOUT_FORCE)
                self.app.call_from_thread(status_label.update, "Pull complete.")
            else:
                self.app.call_from_thread(
                    status_label.update,
                    f"Cloning [b]{self.package_base}[/] from AUR...",
                )
                # Ensure parent directory of self.repo_path exists (already done by os.makedirs in __init__)
                self.repo = pygit2.clone_repository(self.repo_url, self.repo_path)
                self.app.call_from_thread(status_label.update, "Clone complete.")

            self.app.call_from_thread(file_tree.reload)  # Reload DirectoryTree

            self.app.call_from_thread(status_label.update, "Loading commit history...")
            commits_data = []
            if self.repo:
                for commit in self.repo.walk(
                    self.repo.head.target,
                    pygit2.GIT_SORT_TIME,
                ):
                    commits_data.append(
                        {
                            "id": commit.id,
                            "sha_short": str(commit.id)[:7],
                            "author": commit.author.name,
                            "date": datetime.fromtimestamp(commit.commit_time).strftime(
                                "%Y-%m-%d %H:%M"
                            ),
                            "message": commit.message.splitlines()[0].strip(),
                        }
                    )

            self.app.call_from_thread(commit_table.clear)
            for c_data in commits_data:
                self.app.call_from_thread(
                    commit_table.add_row,
                    c_data["sha_short"],
                    c_data["author"],
                    c_data["date"],
                    c_data["message"],
                    key=str(c_data["id"]),
                )

            if not commits_data:
                self.app.call_from_thread(
                    status_label.update, "No commits found or repository is empty."
                )
            else:
                self.app.call_from_thread(
                    status_label.update,
                    "Ready.  Select a file or a commit for viewing.",
                )

        except pygit2.GitError as e:  # Catch specific pygit2 errors
            err_msg = f"[b red]Git operation error: {e}[/]\nURL: {self.repo_url}\nPath: {self.repo_path}"
            self.app.call_from_thread(status_label.update, err_msg)
            self.app.log(f"Pygit2 Error: {e} - {traceback.format_exc()}")
        except Exception as e:  # Catch other general errors
            err_msg = f"[b red]Unexpected error: {e}[/]"
            self.app.call_from_thread(status_label.update, err_msg)
            self.app.log(f"General Error: {e} - {traceback.format_exc()}")

    @on(DirectoryTree.FileSelected, "#git-file-tree")
    def show_file_content(self, event: DirectoryTree.FileSelected) -> None:
        content_view = self.query_one("#git-content-view", Static)
        status_label = self.query_one("#git-status-label", Label)
        file_path = event.path

        if not file_path.is_file():
            content_view.update(
                f"[dim]Selected item is a directory: {file_path.name}[/dim]"
            )
            status_label.update(f"Selected directory: {file_path.name}")
            return

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            lexer = "text"
            filename_lower = file_path.name.lower()
            file_suffix_lower = file_path.suffix.lower()

            if filename_lower == "pkgbuild" or filename_lower == ".SRCINFO":
                lexer = "bash"
            elif file_suffix_lower == ".install":
                lexer = "bash"
            elif file_suffix_lower == ".toml" or file_suffix_lower == ".desktop":
                lexer = "toml"
            elif file_suffix_lower == ".py":
                lexer = "python"
            elif file_suffix_lower in [".md", ".markdown"]:
                lexer = "markdown"
            elif file_suffix_lower == ".json":
                lexer = "json"
            elif file_suffix_lower in [".yaml", ".yml"]:
                lexer = "yaml"
            elif file_suffix_lower == ".diff" or file_suffix_lower == ".patch":
                lexer = "diff"

            # Use Rich's Syntax object for highlighting
            syntax_obj = Syntax(
                content,
                lexer,
                line_numbers=True,
                word_wrap=True,
            )
            content_view.update(syntax_obj)
            status_label.update(f"Viewing: {file_path.name}")

        except Exception as e:
            content_view.update(f"[b red]Error reading file {file_path.name}: {e}[/]")
            self.app.log(f"File Read Error: {e} - {traceback.format_exc()}")

    @on(DataTable.RowSelected, "#git-commit-history")
    def show_commit_diff(self, event: DataTable.RowSelected) -> None:
        content_view = self.query_one("#git-content-view", Static)
        status_label = self.query_one("#git-status-label", Label)

        if not self.repo or not pygit2:  # Check pygit2 again for linter
            status_label.update("[b red]Repository not loaded.[/]")
            return

        commit_id_str = str(event.row_key.value)
        try:
            commit_id = pygit2.Oid(hex=commit_id_str)
            commit = self.repo.get(commit_id)
            if not commit or not isinstance(
                commit, pygit2.Commit
            ):  # pygit2.Commit type
                raise ValueError("Selected item is not a valid commit.")

            parent_tree = commit.parents[0].tree if commit.parents else None

            diff = self.repo.diff(
                parent_tree, commit.tree, context_lines=3, interhunk_lines=1
            )

            diff_text = diff.patch
            if (
                not diff_text and diff.stats.files_changed == 0
            ):  # Check if diff is empty and no files changed
                diff_text = "No textual changes in this commit (e.g., mode change only or empty commit)."
            elif (
                not diff_text
            ):  # Diff might be non-empty but patch is empty (e.g. binary files)
                diff_text = "No textual patch generated (may involve binary files or other non-text changes)."

            syntax_obj = Syntax(
                diff_text,
                "diff",
                line_numbers=True,
                word_wrap=False,
            )
            content_view.update(syntax_obj)
            status_label.update(
                f"Viewing diff for commit: {str(commit.id)[:7]} - {str(commit.message.splitlines()[0].strip())}"
            )

        except Exception as e:
            content_view.update(f"[b red]Error generating diff: {e}[/]")
            self.app.log(f"Diff Generation Error: {e} - {traceback.format_exc()}")

    @on(Button.Pressed, "#git-close-button")
    def action_close_modal(self) -> None:
        self.dismiss()

    def action_force_update_repo(self) -> None:
        """Called when 'ctrl+r' is pressed. Force re-clone/pull."""
        status_label = self.query_one("#git-status-label", Label)
        if not PYGIT2_AVAILABLE or not self.package_base:
            self.app.notify(
                "Cannot update: Pygit2 not available or PackageBase missing.",
                severity="error",
            )
            return

        self.app.notify("Force updating repository...")
        self.query_one(
            "#git-file-tree", DirectoryTree
        ).clear()  # May need .reload() or similar
        self.query_one("#git-commit-history", DataTable).clear()
        self.query_one("#git-content-view", Static).update("")
        status_label.update("Force updating...")

        self.perform_git_operation()  # Re-run the git operation


class auricle(App):
    """Main AUR Browser application"""

    CSS = """
    Screen {
        layout: vertical;
        background: $background;
        height: 100vh;
    }
    
    #main-container {
        background: $surface;
        layout: horizontal;
        color: $foreground;
    }
    
    #package-table {
        height: 1fr;
        border: round $accent;
        background: $panel;
        align: center middle;
        scrollbar-gutter: stable;
        content-align: left middle;
    }
    
    #package-details {
        width: 60%;
        height: 1fr;
        border: round $accent;
        background: $surface;
        padding: 1 1; /* top/bottom left/right */
    }
    
    #search-container {
        height: auto; /* Adjusted for Label and Input */
        padding-bottom: 1;
    }
    #filter-status {
        padding: 1 1 1 1; /* top right bottom left */
        height: auto;
        min-height: 1;
    }
   
    #modal-dialog-scrim {
        width: 80%;
        height: 80%;
        align: center middle; /* Textual's way to center a single child */
        /* Optional: background for dimming effect */
    }


    /* Styling for the actual visible dialog box for filters */
    #filter-modal-dialog {
        width: auto;
        max-width: 50%;
        height: auto;
        max-height: 80%;
        border: round $primary;
        background: $panel;
        layout: vertical; /* To stack label, inputs, buttons */
        overflow-y: auto;
    }

    /* Styling for the actual visible dialog box for sorting */
    #sort-modal-dialog {
        width: auto;
        max-width: 50%;
        height: auto;
        max-height: 70%;
        border: round $primary;
        background: $panel;
        layout: vertical;
        overflow-y: auto;
    }

    /* Titles within modals (no change needed here, just ensure IDs match) */
    #filter-title, #sort-title {
        padding-bottom: 1;
        content-align: center middle;
        width: 1fr;
        height: auto;
    }

    /* Inputs, Checkboxes, RadioSet in modals */
    #filter-modal-dialog Input, #filter-modal-dialog Checkbox {
        margin-bottom: 1; /* Space between items */
        width: 1fr;
        height: auto;
    }
    #sort-modal-dialog RadioSet {
        width: 1fr;
        height: auto;
        background: $panel;
    }
    #sort-modal-dialog RadioButton {
        width: 1fr; /* Make radio buttons take full width for better touch/click */
        height: auto;
    }

    /* Buttons container in modals */
    #filter-buttons, #sort-buttons {
        padding-top: 1;
        align-horizontal: center;
    }
    #filter-buttons Button, #sort-buttons Button {
        width: auto;
        height: auto;
        align-horizontal: center;
    }
    
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("/", "search", "Search", show=True),
        Binding("s", "sort", "Sort", show=True),
        Binding("f", "filter", "Filter", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("R", "reset_filters", "Reset Filters", show=True),
        Binding("U", "download_from_aur", "Update from AUR", show=True),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "cursor_top", "Top", show=False),
        Binding(
            "G", "cursor_bottom", "Bottom", show=False
        ),  # Changed from shift+g to G
        Binding("ctrl+d", "page_down", "Page Down", show=False),
        Binding("ctrl+u", "page_up", "Page Up", show=False),
        Binding(
            "enter", "select_package", "View Git Repo", show=True
        ),  # Changed description
        Binding("escape", "clear_search", "Clear Search", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.packages: List[Dict[str, Any]] = []
        self.filtered_packages: List[Dict[str, Any]] = []
        self.displayed_packages: List[Dict[str, Any]] = []
        self.current_sort = "sort-name"
        self.current_sort_reverse = False
        self.search_term = ""
        self.chunk_size = 1000
        self.loaded_count = 0

        self.config_path_dir = appdirs.user_config_dir(appname="auricle")
        self.config_file = os.path.join(self.config_path_dir, "filters.json")
        self.git_cache_path = appdirs.user_cache_dir(appname="auricle")

        self.config_data_keys = [
            "filters",
            "search_term",
            "current_sort",
            "current_sort_reverse",
        ]  # Helper for saving/loading
        self.default_filters_structure = {  # Define the canonical structure and defaults
            "abandoned": False,
            "out_of_date": False,
            "maintainer": "",  # Empty string means "not active"
            "provides": "",  # Empty string means "not active"
        }
        self.filters = self.default_filters_structure.copy()  # Initialize self.filters
        self._filter_modal: Optional[FilterModal] = None  # For type hinting

        self.provide_db: Optional[ProvideDB] = None
        self._dep_resolve_timer: Optional[Timer] = None
        self.DEP_RESOLVE_DELAY: float = 0.5  # seconds for delay before resolving deps

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
            urllib.request.urlretrieve(url, self.datafile)
            self.notify(f"Download complete: {self.datafile}")
            self._load_package_data()
            self.action_refresh()
        except Exception as e:
            self.notify(f"Download failed: {e}", severity="error", timeout=10)
            if not os.path.exists(self.datafile):
                self.packages = []
                self.filtered_packages = []
                self.update_package_list()

    def _load_package_data(self):
        try:
            self.notify(f"Loading package data from {self.datafile}...")
            with gzip.open(self.datafile, "rt", encoding="utf-8") as f:
                self.packages = json.load(f)

            if self.provide_db is None:
                # ProvideDB will handle pyalpm initialization internally
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

    def load_aur_packages(self, force_download=False):
        os.makedirs(self.config_path_dir, exist_ok=True)
        self.datafile = os.path.join(
            self.config_path_dir, "packages-meta-ext-v1.json.gz"
        )

        if not os.path.exists(self.datafile) or force_download:
            self.notify("Downloading AUR metadata (packages-meta-ext-v1.json.gz)...")
            self.download_metadata()
            return

        self._load_package_data()

    @work(exclusive=True, thread=True)
    async def resolve_package_dependencies(self, package_data: Dict[str, Any]) -> None:
        """
        Worker thread to resolve dependencies for a given package and update details pane.
        """
        if not self.provide_db or not package_data:
            self.app.log("resolve_package_dependencies: No provide_db or package_data.")
            return

        self.app.log(f"Resolving dependencies for: {package_data.get('Name')}")
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
        self.app.log(f"Finished resolving dependencies for: {package_data.get('Name')}")

    def on_mount(self) -> None:
        self.title = "AUR Package Browser"
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
        self.query_one("#package-table", DataTable).focus()  # Focus table last

    def load_app_config(self):  # Renamed from load_filter_config
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file) as f:
                    loaded_config = json.load(f)

                # Load filters
                loaded_config_filters = loaded_config.get("filters", {})
                self.app.log(
                    f"load_app_config: loaded_config_filters = {loaded_config_filters}"
                )  # DEBUG

                current_app_filters = {}
                for key, default_value in self.default_filters_structure.items():
                    # Get the value from loaded config if key exists, else use app's default for that key
                    current_app_filters[key] = loaded_config_filters.get(
                        key, default_value
                    )
                self.theme = loaded_config.get("theme", "nord")
                self.filters = current_app_filters  # Assign the newly constructed, complete filters dict

                # Load search term
                self.search_term = loaded_config.get("search_term", "")
                # We'll update the input widget in on_mount after it exists

                # Load sort options
                self.current_sort = loaded_config.get("current_sort", "sort-name")
                self.current_sort_reverse = loaded_config.get(
                    "current_sort_reverse", False
                )

                self.notify("Loaded saved application configuration.")
            except Exception as e:
                self.notify(
                    f"Failed to load app configuration: {e}", severity="warning"
                )
                # Reset to defaults if loading fails catastrophically
                self.search_term = ""
                self.current_sort = "sort-name"
                self.current_sort_reverse = False
                self.filters = (
                    self.default_filters_structure.copy()
                )  # Use the canonical default structure

        else:
            # If no config file, ensure defaults are set (already done in __init__)
            self.notify("No configuration file found, using defaults.")

        self.app.log(f"load_app_config: self.filters set to = {self.filters}")  # DEBUG

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

    def reset_display(self) -> None:
        self.displayed_packages = []
        self.loaded_count = 0
        self.load_more_packages()  # Load the first chunk

    def update_package_list(self) -> None:
        table = self.query_one("#package-table", DataTable)
        current_cursor_key = None
        if table.row_count > 0 and table.cursor_coordinate.row < table.row_count:
            try:
                current_cursor_key = table.get_row_at(table.cursor_coordinate.row)[
                    -1
                ]  # Assuming key is last
            except IndexError:  # Cursor might be invalid if table was cleared
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

        if found_old_cursor and new_cursor_row_index < table.row_count:
            table.cursor_coordinate = Coordinate(
                new_cursor_row_index, table.cursor_coordinate.column
            )
        elif (
            table.row_count > 0
        ):  # Default to first row if old cursor not found or invalid
            table.cursor_coordinate = Coordinate(0, 0)

        self.update_subtitle_only()
        # Update details for the (potentially new) selected package if table has rows
        if table.row_count > 0:
            self.update_package_details()

    def update_filter_status(self) -> None:
        status_label = self.query_one("#filter-status", Label)
        active_filters_desc = []
        if self.filters.get("abandoned"):
            active_filters_desc.append("Abandoned")
        if self.filters.get("out_of_date"):
            active_filters_desc.append("Out of Date")

        maintainer_val = self.filters.get(
            "maintainer", ""
        )  # Get value, default to empty string
        if maintainer_val:  # Only add to description if the string is not empty
            active_filters_desc.append(f"Maintainer: '{maintainer_val}'")

        provides_val = self.filters.get(
            "provides", ""
        )  # Get value, default to empty string
        if provides_val:  # Only add to description if the string is not empty
            active_filters_desc.append(f"Provides: '{provides_val}'")

        if active_filters_desc:
            status_label.update(
                f"[b]Active Filters:[/] {', '.join(active_filters_desc)}"
            )
        else:
            status_label.update("No active filters.")

    def action_filter(self) -> None:
        modal = FilterModal(
            initial_abandoned=self.filters.get("abandoned", False),
            initial_out_of_date=self.filters.get("out_of_date", False),
            initial_maintainer=self.filters.get("maintainer", ""),
            initial_provides=self.filters.get("provides", ""),
        )
        self._filter_modal = modal
        self.push_screen(modal, self.handle_filter_result)

    def handle_filter_result(self, result: Optional[bool]) -> None:
        if not result or not self._filter_modal:
            return

        modal = self._filter_modal
        self.filters["abandoned"] = modal.query_one("#filter-abandoned", Checkbox).value
        self.filters["out_of_date"] = modal.query_one(
            "#filter-out-of-date", Checkbox
        ).value
        self.filters["maintainer"] = modal.query_one("#filter-maintainer", Input).value
        self.filters["provides"] = modal.query_one("#filter-provides", Input).value

        self.action_refresh()  # This will re-filter and update list & status

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
                self.update_subtitle_only()

    def update_subtitle_only(self) -> None:
        total = len(self.packages)
        filtered_count = len(self.filtered_packages)
        displayed_count = len(
            self.displayed_packages
        )  # This is what's in the table's backing list

        count_str = f"Showing {displayed_count}"
        if displayed_count < filtered_count:
            count_str += f" of {filtered_count} matches"
        elif filtered_count < total:
            count_str += " matches"  # All filtered results are shown
        else:  # No filters, all packages shown up to displayed_count
            count_str += " packages"

        if self.search_term or any(
            self.filters.values()
        ):  # If any search or filter active
            self.sub_title = f"{count_str} (Search/Filters Active, {total} total)"
        else:
            self.sub_title = f"{count_str} ({total} total)"

    def get_selected_package(self) -> Optional[Dict[str, Any]]:
        table = self.query_one("#package-table", DataTable)
        if table.row_count > 0 and 0 <= table.cursor_row < len(self.displayed_packages):
            return self.displayed_packages[table.cursor_row]
        return None

    def get_package_by_id(self, package_id_str: str) -> Optional[Dict[str, Any]]:
        if not package_id_str:
            return None
        try:
            package_id_to_find = int(package_id_str)
        except ValueError:
            return None

        # Check displayed first for speed, then filtered, then all
        for pkg_list in [
            self.displayed_packages,
            self.filtered_packages,
            self.packages,
        ]:
            for pkg in pkg_list:
                if pkg.get("ID") == package_id_to_find:
                    return pkg
        return None

    def sort_packages(self) -> None:  # No longer takes sort_by argument
        """Sort packages based on self.current_sort and self.current_sort_reverse"""
        sort_key = self.current_sort
        user_wants_reverse = self.current_sort_reverse

        # Determine the 'natural' reverse state for Python's list.sort() for each key
        # True if the natural order (e.g. most votes) requires reverse=True in sort()
        natural_py_sort_reverse = {
            "sort-name": False,  # A-Z is natural (reverse=False)
            "sort-first": True,  # Newest first is natural (reverse=True)
            "sort-last": True,  # Newest first is natural (reverse=True)
            "sort-votes": True,  # Most votes first is natural (reverse=True)
            "sort-popularity": True,  # Highest popularity first is natural (reverse=True)
        }.get(sort_key, False)  # Default to False if key not found

        # If user wants reverse, flip the natural_py_sort_reverse state
        actual_py_sort_reverse = natural_py_sort_reverse ^ user_wants_reverse  # XOR

        if sort_key == "sort-name":
            self.filtered_packages.sort(
                key=lambda x: str(x.get("Name", "")).lower(),
                reverse=actual_py_sort_reverse,
            )
        elif sort_key == "sort-first":
            self.filtered_packages.sort(
                key=lambda x: x.get("FirstSubmitted", 0), reverse=actual_py_sort_reverse
            )
        elif sort_key == "sort-last":
            self.filtered_packages.sort(
                key=lambda x: x.get("LastModified", 0), reverse=actual_py_sort_reverse
            )
        elif sort_key == "sort-votes":
            self.filtered_packages.sort(
                key=lambda x: x.get("NumVotes", 0), reverse=actual_py_sort_reverse
            )
        elif sort_key == "sort-popularity":
            self.filtered_packages.sort(
                key=lambda x: x.get("Popularity", 0.0), reverse=actual_py_sort_reverse
            )
        else:  # Should not happen if sort_key is always valid
            self.filtered_packages.sort(key=lambda x: str(x.get("Name", "")).lower())

    def update_package_details(self) -> None:
        """Updates the package details pane with basic information for the selected package."""
        package = self.get_selected_package()
        details_pane = self.query_one("#package-details", PackageDetails)
        if package:
            # This call will render basic info.
            # Richer dependency info is triggered by the timer in on_row_highlighted_main_table.
            details_pane.update_package(package=package.copy())  # Pass a copy
        else:
            details_pane.update("Select a package to see details.")

    @on(
        DataTable.RowHighlighted, "#package-table"
    )  # Renamed from RowSelected to RowHighlighted for continuous update
    def on_row_highlighted_main_table(self, event: DataTable.RowHighlighted) -> None:  # pyright: ignore
        self.check_load_more()
        package = (
            self.get_selected_package()
        )  # Uses self.displayed_packages[table.cursor_row]
        details_pane = self.query_one("#package-details", PackageDetails)

        if package:
            # 1. Update basic info immediately (without enriched_dependencies)
            details_pane.update_package(package=package.copy())  # Send a copy

            # 2. Cancel any pending dependency resolution timer
            if self._dep_resolve_timer is not None:
                try:
                    self._dep_resolve_timer.stop()  # Use stop() for timers started from main thread
                except Exception as e:
                    self.app.log(f"Error stopping timer: {e}")
                self._dep_resolve_timer = None

            # 3. Set a new timer to call the worker with a copy of package data
            #    This ensures the worker gets the data for *this* selection
            #    even if the user scrolls quickly.
            current_package_copy = package.copy()
            self._dep_resolve_timer = self.set_timer(
                self.DEP_RESOLVE_DELAY,
                # Use a lambda to capture the current_package_copy
                callback=lambda pkg_data=current_package_copy: self.resolve_package_dependencies(
                    pkg_data
                ),
            )
        else:
            # If no package is selected (e.g., table is empty), clear details
            details_pane.update("Select a package to see details.")

    @on(DataTable.RowSelected, "#package-table")  # This is for 'Enter' press
    def on_package_selected_for_git_view(self, event: DataTable.RowSelected) -> None:
        if not event.row_key.value:
            self.notify("No package selected (invalid key).", severity="warning")
            return

        package_id_str = str(event.row_key.value)
        package = self.get_package_by_id(package_id_str)

        if package:
            if not PYGIT2_AVAILABLE:
                self.notify(
                    "[b red]Pygit2 library not installed.[/] Cannot show Git details. (pip install pygit2)",
                    timeout=10,
                )
                return
            if not package.get("PackageBase"):
                self.notify(
                    "PackageBase missing, cannot fetch Git repository.",
                    severity="error",
                )
                return

            # Ensure git_cache_path exists before passing to modal
            os.makedirs(self.git_cache_path, exist_ok=True)
            self.push_screen(
                GitViewModal(package_data=package, cache_base_path=self.git_cache_path)
            )
        else:
            self.notify(
                f"Could not retrieve package details for ID {package_id_str}.",
                severity="error",
            )

    @on(Input.Submitted, "#search-input")
    def on_search_submitted(self, event: Input.Submitted) -> None:
        self.filter_packages(event.value)
        self.update_package_list()
        self.query_one("#package-table", DataTable).focus()

    @on(Input.Changed, "#search-input")
    def on_search_changed(self, event: Input.Changed) -> None:
        if (
            len(event.value) == 0 or len(event.value) >= 2
        ):  # Min 2 chars for live search or empty
            self.filter_packages(event.value)
            self.update_package_list()

    def action_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_clear_search(self) -> None:
        search_input = self.query_one("#search-input", Input)
        if search_input.value:  # Only clear and re-filter if there was a value
            search_input.value = (
                ""  # This will trigger on_search_changed if value was not empty
            )
            self.filter_packages(
                ""
            )  # Ensure filtering happens if value was cleared manually
            self.update_package_list()
        self.query_one("#package-table", DataTable).focus()

    def action_sort(self) -> None:
        self.app.log(
            f"auricle.action_sort: Opening SortModal. self.current_sort='{self.current_sort}', self.current_sort_reverse={self.current_sort_reverse}"
        )  # DEBUG
        sort_modal = SortModal(
            current_sort_key=self.current_sort,
            current_sort_reverse=self.current_sort_reverse,
        )
        self.push_screen(sort_modal, self.handle_sort_result)

    def handle_sort_result(self, sort_info: Optional[Dict[str, Any]]) -> None:
        """Handle sort modal result"""
        if sort_info and isinstance(sort_info, dict):
            new_sort_key = sort_info.get("sort_key")
            new_reverse_state = sort_info.get("reverse", False)

            if new_sort_key:
                self.current_sort = new_sort_key
                self.current_sort_reverse = new_reverse_state

                self.sort_packages()  # Call without args, it will use self.current_sort & self.current_sort_reverse
                self.reset_display()
                self.update_package_list()

                friendly_sort_name = (
                    self.current_sort.replace("sort-", "").replace("-", " ").title()
                )
                reverse_text = " (Reversed)" if self.current_sort_reverse else ""
                self.notify(f"Sorted by {friendly_sort_name}{reverse_text}")

    def action_refresh(self) -> None:
        self.filter_packages(self.search_term)  # Re-apply current search and filters
        self.update_package_list()
        self.update_filter_status()
        self.notify("Package list refreshed.")

    def action_reset_filters(self) -> None:
        # Reset filter values to defaults
        self.filters = {
            "abandoned": False,
            "out_of_date": False,
            "maintainer": "",
            "provides": "",
        }
        # Optionally clear search term too, or keep it separate
        # self.query_one("#search-input", Input).value = ""
        # self.search_term = ""

        self.action_refresh()  # Re-filter (which will now be with no filters) and update
        self.notify("All filters reset.")

    def action_download_from_aur(self) -> None:
        self.load_aur_packages(force_download=True)

    def action_cursor_down(self) -> None:
        self.query_one("#package-table", DataTable).action_cursor_down()
        # self.check_load_more() # Already handled by RowHighlighted

    def action_cursor_up(self) -> None:
        self.query_one("#package-table", DataTable).action_cursor_up()

    def action_cursor_top(self) -> None:
        table = self.query_one("#package-table", DataTable)
        if table.row_count > 0:
            table.move_cursor(row=0)
            # table.move_cursor(row=0, column=table.cursor_column)

    def action_cursor_bottom(self) -> None:  # Bound to 'G'
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

    def action_page_down(self) -> None:
        self.query_one("#package-table", DataTable).action_page_down()
        # self.check_load_more() # Handled by RowHighlighted

    def action_page_up(self) -> None:
        self.query_one("#package-table", DataTable).action_page_up()

    def save_app_config(
        self,
    ) -> None:  # Renamed from save_on_exit for clarity if called elsewhere
        os.makedirs(self.config_path_dir, exist_ok=True)
        config_to_save = {
            "theme": self.theme,
            "filters": self.filters,
            "search_term": self.search_term,  # Or self.query_one("#search-input", Input).value
            "current_sort": self.current_sort,
            "current_sort_reverse": self.current_sort_reverse,
        }
        try:
            with open(self.config_file, "w") as f:
                json.dump(config_to_save, f, indent=4)
            self.log.info(f"Saved app configuration to {self.config_file}")
        except Exception as e:
            self.log.error(f"Failed to save app configuration: {e}")


def main():
    app = auricle()
    app.run()
    app.save_app_config()  # Save filters when app exits normally


if __name__ == "__main__":
    main()
