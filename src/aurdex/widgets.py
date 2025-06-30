import os
import httpx
import logging as log
import traceback
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

from textual import on, work
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.reactive import reactive
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import (
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
    LoadingIndicator,
    Tree,
)

from rich.syntax import Syntax
from rich.text import Text
from rich.errors import StyleSyntaxError, MissingStyle

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag


# optionals
try:
    import pygit2  # type: ignore

    PYGIT2_AVAILABLE = True
except ImportError:
    PYGIT2_AVAILABLE = False
    pygit2 = None  # type: ignore


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
        log.info(
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
                log.warning(
                    f"SortModal.on_mount: Sort key '{self.current_sort_key}' did not match any RadioButton ID."
                )
        except Exception as e:
            log.error(f"SortModal.on_mount: Error pre-selecting sort option: {e}")

    @on(Button.Pressed, "#sort-apply")
    def apply_sort(self) -> None:
        radio_set = self.query_one("#sort-options", RadioSet)
        reverse_checkbox = self.query_one("#sort-reverse-checkbox", Checkbox)

        if radio_set.pressed_button:
            # The id of the pressed RadioButton is our string sort key
            sort_key_from_button = radio_set.pressed_button.id

            log.info(
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
            log.warning("SortModal apply_sort: No radio button pressed.")
            self.dismiss(None)  # No option selected

    @on(Button.Pressed, "#sort-cancel")
    def cancel_sort(self) -> None:
        self.dismiss(None)


from textual.content import Content


class PackageDetails(VerticalScroll):
    """Widget to display detailed package information"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._static_content = Static()
        self.package_data: Optional[Dict[str, Any]] = None
        self.enriched_dependencies: Optional[Dict[str, List[Dict]]] = None

    def compose(self):
        yield self._static_content

    def update(self, content: str | Text):
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

    CSS_PATH = "tcss/gitview.tcss"
    BINDINGS = [
        Binding("escape", "close_modal", "Close", show=True),
        Binding("ctrl+r", "force_update_repo", "Force Update Repo", show=True),
        Binding("ctrl+q", "close_modal", "Close", show=False),
    ]

    def __init__(
        self, package_data: Dict[str, Any], cache_base_path: str, **kwargs: Any
    ):
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
                    pygit2.Repository(self.repo_path).is_bare == False  # type: ignore[attr-defined]
                ):  # Or check for .git dir
                    is_repo = os.path.exists(os.path.join(self.repo_path, ".git"))
            except pygit2.GitError:  # Not a repository or path doesn't exist as repo
                is_repo = False

            if is_repo:
                self.app.call_from_thread(
                    status_label.update,
                    f"Pulling latest changes for [b]{self.package_base}[/]...",
                )
                self.repo = pygit2.Repository(self.repo_path)  # type: ignore[attr-defined]

                if self.repo is not None:
                    remote = self.repo.remotes["origin"]  # type: ignore
                    log.info(f"Fetching from remote {remote.name} ({remote.url})")
                    remote.fetch()
                    log.info("Fetch complete.")

                    # Determine the remote head reference (e.g., refs/remotes/origin/master)
                    remote_head_ref_name = None
                    current_local_branch_name = self.repo.head.shorthand  # type: ignore
                    possible_remote_refs = [
                        f"refs/remotes/origin/{current_local_branch_name}",
                        "refs/remotes/origin/master",
                        "refs/remotes/origin/main",
                    ]

                    for ref_name_option in possible_remote_refs:
                        try:
                            if self.repo.lookup_reference(ref_name_option):  # type: ignore
                                remote_head_ref_name = ref_name_option
                                log.info(
                                    f"Found remote head ref: {remote_head_ref_name}"
                                )
                                break
                        except pygit2.KeyError:  # Ref not found # type: ignore
                            continue

                    if not remote_head_ref_name:
                        self.app.call_from_thread(
                            status_label.update,
                            f"[b red]Error: Could not determine remote default branch for {self.package_base}.[/]",
                        )
                        return

                    remote_head_commit_id = self.repo.lookup_reference(  # type: ignore
                        remote_head_ref_name
                    ).target

                    local_branch_ref_name = self.repo.head.name  # type: ignore
                    self.repo.references[local_branch_ref_name].set_target(  # type: ignore
                        remote_head_commit_id
                    )
                    self.repo.checkout_head(strategy=pygit2.GIT_CHECKOUT_FORCE)  # type: ignore
                    self.app.call_from_thread(status_label.update, "Pull complete.")
            else:
                self.app.call_from_thread(
                    status_label.update,
                    f"Cloning [b]{self.package_base}[/] from AUR...",
                )
                # Ensure parent directory of self.repo_path exists (already done by os.makedirs in __init__)
                self.repo = pygit2.clone_repository(self.repo_url, self.repo_path)  # type: ignore
                self.app.call_from_thread(status_label.update, "Clone complete.")

            self.app.call_from_thread(file_tree.reload)  # Reload DirectoryTree

            self.app.call_from_thread(status_label.update, "Loading commit history...")
            commits_data = []
            if self.repo:
                for commit in self.repo.walk(
                    self.repo.head.target,  # type: ignore
                    pygit2.GIT_SORT_TIME,  # type: ignore
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
            log.error(f"Pygit2 Error: {e} - {traceback.format_exc()}")
        except Exception as e:  # Catch other general errors
            err_msg = f"[b red]Unexpected error: {e}[/]"
            self.app.call_from_thread(status_label.update, err_msg)
            log.error(f"General Error: {e} - {traceback.format_exc()}")

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
            log.error(f"File Read Error: {e} - {traceback.format_exc()}")

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
            log.error(f"Diff Generation Error: {e} - {traceback.format_exc()}")

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


class CommentsModal(ModalScreen[None]):
    CSS_PATH = "tcss/comments.tcss"
    BINDINGS = [
        Binding("escape", "close_modal", "Close", show=True),
        Binding("ctrl+q", "close_modal", "Close", show=False),
        Binding("n", "next_comments", "Next Comments", show=True),
    ]

    COMMENT_BATCH_SIZE = 10
    _current_offset = reactive(0)
    _all_comments_loaded = reactive(False)
    _is_loading_more = reactive(False)

    def __init__(self, package_data: Dict[str, Any]):
        super().__init__()
        self.package_base = package_data.get("PackageBase", "unknown_package")
        self.parsed_comments = []
        self.comment_counter = 0
        self._pinned_rendered = False

    def compose(self) -> ComposeResult:
        # Ensure Label is imported, fixed in previous step
        yield Label(f"Comments for {self.package_base}", id="modal-title")
        yield Container(LoadingIndicator(), id="loading")
        yield VerticalScroll(id="comments-scroller")
        yield Footer()

    async def on_mount(self) -> None:
        # Using notify for debugging initial load
        self.query_one("#comments-scroller", VerticalScroll).display = False
        await self._load_and_render_comments()
        log.debug("Comments loading process finished (check UI for results).")

    async def _fetch_aur_page_html(
        self, package_url: str
    ) -> Tuple[Optional[str], bool]:
        self.notify(f"Fetching comments from: {package_url}")
        headers = {
            "User-Agent": "TextualCommentsModalClient/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    package_url, headers=headers, follow_redirects=True
                )
                response.raise_for_status()
                return response.text, True
        except httpx.HTTPStatusError as e:
            log.error(f"HTTP error: {e} for {package_url}")
        except httpx.RequestError as e:
            log.error(f"Request error: {e} for {package_url}")
        except Exception as e:
            log.error(f"Unexpected error during fetch: {e} for {package_url}")
        return None, False

    def _convert_html_node_to_textual_widget(
        self, node: Tag | NavigableString
    ) -> Optional[Widget | Text]:
        """Converts a BeautifulSoup node into a Textual widget or Rich Text object."""
        if isinstance(node, NavigableString):
            text = str(node)
            processed_text = text.replace("[", "\\[")  # Escape literal brackets
            return Text(processed_text) if text else None

        if not isinstance(node, Tag):
            return None

        tag_name = node.name.lower()

        def _process_inline_children(current_node: Tag) -> Text:
            content_text = Text()
            for child in current_node.contents:
                if isinstance(child, (Tag, NavigableString)):
                    child_renderable = self._convert_html_node_to_textual_widget(child)
                    if child_renderable:
                        if isinstance(child_renderable, Text):
                            content_text.append(child_renderable)
                        elif isinstance(child_renderable, Widget):
                            renderable = getattr(child_renderable, "renderable", None)
                            if renderable and hasattr(renderable, "plain"):
                                content_text.append(renderable.plain)
                            else:
                                content_text.append(str(child_renderable))
            return content_text

        if tag_name == "p":
            paragraph_text = _process_inline_children(node)
            if paragraph_text.plain.strip():
                return Static(
                    paragraph_text,
                    classes="comment-paragraph",
                    expand=True,
                    shrink=False,
                )
            return None

        elif tag_name == "a":
            raw_href = str(node.get("href", "#")) or "#"
            safe_href = raw_href.split()[0].split(">")[0].strip()
            inner_text = _process_inline_children(node).plain.strip() or safe_href
            link_text = Text(inner_text, style="underline")
            try:
                link_text.stylize(f"link {safe_href}")
            except (MissingStyle, StyleSyntaxError):
                log.warning(f"Skipped malformed link: {raw_href}")
            return link_text

        elif tag_name in ("strong", "b"):
            strong_text = _process_inline_children(node)
            if strong_text.plain.strip():
                strong_text.stylize("bold")
                return strong_text
            return None

        elif tag_name in ("em", "i"):
            italic_text = _process_inline_children(node)
            if italic_text.plain.strip():
                italic_text.stylize("italic")
                return italic_text
            return None

        elif tag_name == "pre":
            code_node = node.find("code")
            target_node_for_text = code_node if code_node else node
            code_text = target_node_for_text.get_text(separator="")
            if code_text:
                return Static(
                    Text(code_text),
                    classes="comment-code-block",
                    expand=True,
                    shrink=False,
                )
            return None

        elif tag_name == "code":  # Inline code
            inline_code_text = _process_inline_children(node)
            if inline_code_text.plain.strip():
                inline_code_text.stylize("reverse dim")
                return inline_code_text
            return None

        elif tag_name == "br":
            return Text("\n")

        else:  # Handle other tags by rendering their inline content
            fallback_text = _process_inline_children(node)
            if fallback_text.plain.strip():
                return fallback_text
            return None

    def _parse_aur_comment_html(self, html_content: str) -> List[Dict[str, Any]]:
        """Parses AUR HTML string to extract comment data."""
        log.debug("Parsing HTML content...")
        soup = BeautifulSoup(html_content, "html.parser")
        extracted_comments = []

        comment_sections = soup.find_all("div", class_="comments package-comments")  # type: ignore

        for section_div in comment_sections:
            section_is_pinned = False
            section_header_div = section_div.find("div", class_="comments-header")  # type: ignore[attr-defined]
            if section_header_div:
                section_title_h3 = section_header_div.find("h3")  # type: ignore
                if section_title_h3:
                    section_title_span = section_title_h3.find("span", class_="text")  # type: ignore
                    if (
                        section_title_span
                        and "Pinned Comments" in section_title_span.get_text()
                    ):
                        section_is_pinned = True

            comment_header_tags = section_div.find_all("h4", class_="comment-header")  # type: ignore

            for header_tag in comment_header_tags:
                comment_data: Dict[str, Any] = {"body_widgets": []}

                header_text_content = header_tag.get_text(separator=" ", strip=True)
                user_name_str = "Unknown User"
                date_str = "Unknown Date"

                separator = " commented on "
                if separator in header_text_content:
                    parts = header_text_content.split(separator, 1)
                    user_name_str = parts[0].strip()
                    if len(parts) > 1:
                        date_str = parts[1].strip()
                else:
                    user_link_attempt = header_tag.find(
                        "a",
                        href=lambda href: href and href.startswith("/account/"),  # type: ignore[attr-defined]
                    )  # type: ignore[attr-defined]
                    if user_link_attempt:
                        user_name_str = user_link_attempt.get_text(strip=True)

                comment_data["user"] = user_name_str

                date_link_specific = header_tag.find("a", class_="date")  # type: ignore[attr-defined]
                if date_link_specific:
                    comment_data["date"] = date_link_specific.get_text(strip=True)
                else:
                    comment_data["date"] = date_str

                edited_span = header_tag.find("span", class_="edited")  # type: ignore[attr-defined]
                if edited_span:
                    comment_data["edited"] = edited_span.get_text(strip=False).strip()
                else:
                    comment_data["edited"] = None

                comment_data["pinned"] = section_is_pinned

                content_div = header_tag.find_next_sibling(
                    "div", class_="article-content"
                )  # type: ignore[attr-defined]
                if content_div:
                    main_content_wrapper = content_div.find("div")
                    if main_content_wrapper:
                        for child_node in main_content_wrapper.children:
                            if isinstance(child_node, (Tag, NavigableString)):
                                widget_or_text = (
                                    self._convert_html_node_to_textual_widget(
                                        child_node
                                    )
                                )
                                if widget_or_text:
                                    if isinstance(widget_or_text, Text):
                                        if widget_or_text.plain.strip():
                                            comment_data["body_widgets"].append(
                                                Static(
                                                    widget_or_text,
                                                    classes="comment-paragraph",
                                                    expand=True,
                                                    shrink=False,
                                                )
                                            )
                                    elif isinstance(widget_or_text, Widget):
                                        comment_data["body_widgets"].append(
                                            widget_or_text
                                        )

                if comment_data["body_widgets"] or (
                    comment_data["user"] != "Unknown User"
                ):
                    extracted_comments.append(comment_data)
                else:
                    log.warning(
                        f"Skipping comment block, no content or user found. Header: {header_tag.get_text(strip=True).strip()}"
                    )
        log.debug(
            f"HTML parsing finished, found {len(extracted_comments)} comments."
        )  # Notify on parsing completion
        assert isinstance(extracted_comments, list)
        return extracted_comments

    async def _load_and_render_comments(self, load_more: bool = False) -> None:
        """Fetches, parses, and renders comments."""
        log.debug(
            f"Loading comments... (More: {load_more})"
        )  # Notify about loading start
        if self._all_comments_loaded or self._is_loading_more:
            log.debug("Already loaded or currently loading comments.")
            return

        self._is_loading_more = True
        loading_indicator_container = self.query_one("#loading", Container)
        comments_scroller = self.query_one("#comments-scroller", VerticalScroll)

        if not load_more:
            log.debug("Performing initial load.")
            self.comment_counter = 0
            self._current_offset = 0
            self.parsed_comments.clear()
            comments_scroller.remove_children()
            comments_scroller.display = False
            loading_indicator_container.display = True
        else:
            log.debug("Loading next batch of comments.")
            pass

        aur_package_url = f"https://aur.archlinux.org/packages/{self.package_base}?O={self._current_offset}"
        html_content, _ = await self._fetch_aur_page_html(aur_package_url)
        has_next_page = False
        if html_content:
            soup_nav = BeautifulSoup(html_content, "html.parser")
            has_next_page = bool(
                soup_nav.find("a", class_="page", string=lambda s: s and "Next" in s)  # type: ignore
            )

        if html_content:
            newly_parsed_comments = self._parse_aur_comment_html(html_content)
            if self._pinned_rendered:
                newly_parsed_comments = [
                    c for c in newly_parsed_comments if not c["pinned"]
                ]

            if not newly_parsed_comments and not load_more:
                log.debug("No comments found for this package.")
                comments_scroller.mount(
                    Static("No comments found.", classes="info-message")
                )
            else:
                log.debug(f"Rendering {len(newly_parsed_comments)} comments.")
                for comment_data in newly_parsed_comments:
                    if comment_data.get("pinned"):
                        idx = 0
                    else:
                        self.comment_counter += 1
                        idx = self.comment_counter
                    comments_scroller.mount(self.render_comment(idx, comment_data))
                if not self._pinned_rendered and any(
                    c.get("pinned") for c in newly_parsed_comments
                ):
                    self._pinned_rendered = True

                self.parsed_comments.extend(newly_parsed_comments)
                self._current_offset += self.COMMENT_BATCH_SIZE

                if not has_next_page:
                    self._all_comments_loaded = True
                    if comments_scroller.children:
                        comments_scroller.mount(
                            Static("--- No more comments ---", classes="centered-text")
                        )
        else:
            if not load_more:
                log.error("Failed to load comments.")
                comments_scroller.mount(
                    Static("Failed to load comments.", classes="error-message")
                )
            else:
                log.error("Failed to load more comments")
                self._all_comments_loaded = True
                if comments_scroller.children:
                    self.notify("--- No more comments (due to load error) ---")
                    comments_scroller.mount(
                        Static("--- No more comments ---", classes="centered-text")
                    )

        loading_indicator_container.display = False
        comments_scroller.display = True
        self._is_loading_more = False
        log.debug("Finished loading comments.")

        if (
            not load_more
            and comments_scroller.virtual_size.height <= comments_scroller.size.height
            and not self._all_comments_loaded
            and not self._is_loading_more
        ):
            log.debug(
                "Initial content loaded, but screen not full. Attempting to load more."
            )
            self.call_later(self._load_and_render_comments, load_more=True)

    def render_comment(self, idx: int, comment: dict[str, Any]) -> Container:
        """Renders a single comment with its header and body."""
        header_widgets = []
        if idx:
            header_widgets.append(Static(f"#{idx}", classes="comment-number"))

        header_widgets.append(Static(comment["user"], classes="comment-user"))
        header_widgets.append(Static(comment["date"], classes="comment-date"))

        if comment.get("edited"):
            header_widgets.append(
                Static(f"✎ {comment['edited']}", classes="comment-edited")
            )
        if comment.get("pinned") or idx == 0:
            header_widgets.append(Static("📌", classes="comment-pinned-label"))

        header_container = Horizontal(*header_widgets, id="comment-header")

        body_widgets_list = []
        for widget in comment.get("body_widgets", []):
            body_widgets_list.append(widget)

        if comment.get("pinned"):
            pinned_content_holder_children = [header_container] + body_widgets_list
            pinned_content_holder = Container(
                *pinned_content_holder_children, classes="pinned-content-wrapper"
            )
            return Container(
                pinned_content_holder, classes="comment-block is-pinned-outer"
            )
        else:
            regular_comment_children = [header_container] + body_widgets_list
            return Container(*regular_comment_children, classes="comment-block")

    def action_next_comments(self) -> None:
        """Loads the next batch of comments."""
        if not self._all_comments_loaded and not self._is_loading_more:
            log.debug("Fetching next comments...")
            self.call_later(self._load_and_render_comments, load_more=True)
        elif self._all_comments_loaded:
            self.notify("No more comments")

    def action_close_modal(self) -> None:
        """Closes the modal screen."""
        self.dismiss()


class ProfileModal(ModalScreen[Optional[Dict[str, Any]]]):
    """Modal for managing settings profiles."""

    CSS_PATH = "tcss/profile.tcss"
    BINDINGS = [
        Binding("n", "new_profile", "New"),
        Binding("l", "load_profile", "Load"),
        Binding("d", "delete_profile", "Delete"),
        Binding("s", "set_default", "Set Default"),
        Binding("escape", "cancel", "Close"),
    ]

    def __init__(
        self,
        profiles: Dict[str, Any],
        default_profile: str,
        current_profile: str,
        current_settings: Dict[str, Any],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.profiles = profiles
        self.default_profile = default_profile
        self.current_profile = current_profile
        self.current_settings = current_settings

    def compose(self) -> ComposeResult:
        with Container(id="profile-modal-dialog"):
            yield Label("Profiles", id="profile-title")
            with Horizontal():
                with Vertical(id="profile-tree-container"):
                    yield Tree("Profiles", id="profile-tree")
                with Vertical(id="profile-preview-container"):
                    yield Static(id="profile-preview")
        yield Footer()

    def on_mount(self) -> None:
        self.update_tree()
        self.update_preview()

    def update_tree(self) -> None:
        tree = self.query_one("#profile-tree", Tree)
        tree.clear()
        for profile_name in self.profiles.keys():
            label = f"{profile_name}"
            if (
                profile_name == self.default_profile
                and profile_name == self.current_profile
            ):
                label += " (default, current)"
            elif profile_name == self.default_profile:
                label += " (default)"
            elif profile_name == self.current_profile:
                label += " (current)"
            tree.root.add(label, data=profile_name, allow_expand=False)
        tree.root.expand()

    @on(Tree.NodeHighlighted)
    def update_preview(self) -> None:
        tree = self.query_one("#profile-tree", Tree)
        preview = self.query_one("#profile-preview", Static)
        if tree.cursor_node and tree.cursor_node.data is not None:
            profile_name = tree.cursor_node.data
            profile_data = self.profiles.get(profile_name, {})
            formatted = self.format_profile_data(profile_data)
            preview.update(f"Profile: {profile_name}\n{formatted}")
        else:
            preview.update("")

    def format_profile_data(self, profile_data: Dict[str, Any]) -> str:
        lines = []
        for key, value in profile_data.items():
            if isinstance(value, dict):
                lines.append(f"[b]{key}:[/b]")
                for sub_key, sub_value in value.items():
                    lines.append(f"  {sub_key}: {sub_value}")
            else:
                lines.append(f"[b]{key}:[/b] {value}")
        return "\n".join(lines)

    @on(Tree.NodeSelected)
    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        self.update_preview()

    def action_new_profile(self) -> None:
        def on_input_submitted(value: Optional[str]):
            if value:
                self.profiles[value] = self.current_settings
                self.update_tree()

        self.app.push_screen(
            InputModal(prompt="Enter new profile name:"), on_input_submitted
        )

    def action_load_profile(self) -> None:
        tree = self.query_one("#profile-tree", Tree)
        if tree.cursor_node and tree.cursor_node.data is not None:
            self.dismiss(
                {
                    "action": "load",
                    "profile_name": tree.cursor_node.data,
                    "profiles": self.profiles,
                }
            )

    def action_set_default(self) -> None:
        tree = self.query_one("#profile-tree", Tree)
        if tree.cursor_node and tree.cursor_node.data is not None:
            self.default_profile = tree.cursor_node.data
            self.update_tree()

    def action_delete_profile(self) -> None:
        tree = self.query_one("#profile-tree", Tree)
        if tree.cursor_node and tree.cursor_node.data is not None:
            profile_to_delete = tree.cursor_node.data
            if profile_to_delete != "default":
                del self.profiles[profile_to_delete]
                if self.default_profile == profile_to_delete:
                    self.default_profile = "default"
                self.update_tree()
                self.update_preview()

    def action_cancel(self) -> None:
        self.dismiss(
            {
                "action": "cancel",
                "profiles": self.profiles,
                "default_profile": self.default_profile,
            }
        )


class InputModal(ModalScreen[str]):
    """A modal screen to get a single line of input from the user."""

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Container(id="modal-dialog-scrim"):
            with Container(id="input-modal-dialog"):
                yield Label(self.prompt)
                yield Input(id="input-modal-input")

    def on_mount(self) -> None:
        self.query_one("#input-modal-input", Input).focus()

    @on(Input.Submitted)
    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)
