import os
import httpx
import logging as log
import time
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple, TYPE_CHECKING

from textual import on, work
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.reactive import reactive
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.events import Key
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
from textual.widgets._header import HeaderIcon
from textual.reactive import Reactive


from rich.syntax import Syntax
from rich.text import Text
from rich.errors import StyleSyntaxError, MissingStyle

from .formatters import format_package_details

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

if TYPE_CHECKING:
    from .main import aurdex


# optionals
try:
    import pygit2  # type: ignore

    PYGIT2_AVAILABLE = True
except ImportError:
    PYGIT2_AVAILABLE = False
    pygit2 = None  # type: ignore


class CustomHeader(Container):
    """A custom header that displays the database age."""

    CSS_PATH = "tcss/main.tcss"

    db_age: reactive[Optional[float]] = reactive(None)
    icon = Reactive("⭘")

    def compose(self) -> ComposeResult:
        yield HeaderIcon().data_bind(CustomHeader.icon)
        yield Static(id="header-title-subtitle")
        yield Static(id="header-age")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._update_timer = self.set_interval(1, self.refresh_header_text)

    def format_db_age(self) -> str:
        if self.db_age is None:
            return "DB age: [dim]unknown[/dim]"

        now = time.time()
        age_seconds = now - self.db_age

        if age_seconds < 60:
            return f"database update: [b]{int(age_seconds)}s ago[/b]"
        elif age_seconds < 3600:
            return f"database update: [b]{int(age_seconds / 60)}m ago[/b]"
        elif age_seconds < 86400:
            return f"database update: [b]{int(age_seconds / 3600)}h ago[/b]"
        else:
            return f"database update: [b]{int(age_seconds / 86400)}d ago[/b]"

    def refresh_header_text(self) -> None:
        """Builds and sets the header's renderable text."""
        title = self.app.title
        sub_title = self.app.sub_title

        title_text = Text(title, style="bold", no_wrap=True)
        sub_title_text = Text(sub_title, no_wrap=True, overflow="ellipsis")

        left_part = Text.assemble(title_text, "", sub_title_text)
        right_part = Text.from_markup(self.format_db_age())

        self.query_one("#header-title-subtitle", Static).update(left_part)
        self.query_one("#header-age", Static).update(right_part)


class FilterModal(ModalScreen[bool | None]):
    TITLE = "title"
    SUB_TITLE = "subtitle"

    def __init__(
        self,
        initial_abandoned: bool = False,
        initial_out_of_date: bool = False,
        initial_maintainer: str = "",
        initial_provides: str = "",
        repo_filters: Dict[str, bool] = None,
        all_repos: List[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.initial_abandoned = initial_abandoned
        self.initial_out_of_date = initial_out_of_date
        self.initial_maintainer = initial_maintainer
        self.initial_provides = initial_provides
        self.repo_filters = repo_filters or {}
        self.all_repos = all_repos or []

    def compose(self) -> ComposeResult:
        with Container(id="modal-dialog-scrim"):
            with Container(id="filter-modal-dialog"):
                if self.all_repos:
                    yield Label("Repositories to include", classes="filter-separator")
                    with Container(id="filter-repos"):
                        for repo in sorted(self.all_repos):
                            yield Checkbox(
                                repo,
                                value=self.repo_filters.get(repo, True),
                                id=f"filter-repo-{repo}",
                                classes="filter-repo-checkbox",
                            )

                yield Label("Filters", classes="filter-separator")
                with Container(id="filter-options"):
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

    @on(Key)
    def handle_escape(self, event: Key) -> None:
        if event.key == "escape":
            self.dismiss()


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
        try:
            radio_set = self.query_one(RadioSet)
            for button in radio_set.query(RadioButton):
                if button.id == self.current_sort_key:
                    button.value = True
                    break
        except Exception as e:
            log.error(f"SortModal.on_mount: Error pre-selecting sort option: {e}")

    @on(Button.Pressed, "#sort-apply")
    def apply_sort(self) -> None:
        radio_set = self.query_one("#sort-options", RadioSet)
        reverse_checkbox = self.query_one("#sort-reverse-checkbox", Checkbox)

        if radio_set.pressed_button:
            sort_key_from_button = radio_set.pressed_button.id
            self.dismiss(
                {
                    "sort_key": str(sort_key_from_button),
                    "reverse": reverse_checkbox.value,
                }
            )
        else:
            self.dismiss(None)

    @on(Key)
    def handle_escape(self, event: Key) -> None:
        if event.key == "escape":
            self.dismiss()

    @on(Button.Pressed, "#sort-cancel")
    def cancel_sort(self) -> None:
        self.dismiss(None)


class PackageDetails(VerticalScroll):
    """Widget to display detailed package information"""

    if TYPE_CHECKING:
        app: aurdex

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._static_content = Static(id="package-details-content")
        self.package_data: Optional[Dict[str, Any]] = None
        self.enriched_dependencies: Optional[Dict[str, List[Dict]]] = None
        self.enriched_dependants: Optional[List[Dict]] = None

    def compose(self):
        yield self._static_content

    def update(self, content: str | Text):
        self._static_content.update(content)

    def display_loading(self) -> None:
        self.update(
            Text.from_markup(
                "[dim italic]Loading package details...[/dim italic]", justify="center"
            )
        )

    def update_package(
        self,
        package: Dict[str, Any],
        enriched_dependencies: Optional[Dict[str, List[Dict]]] = None,
        enriched_dependants: Optional[Dict[str, List[Dict]]] = None,
    ) -> None:
        self.package_data = package
        self.enriched_dependencies = enriched_dependencies
        self.enriched_dependants = enriched_dependants

        if not package:
            self.update("[dim italic]Select a package to see details.[/dim]")
            return

        formatted_text = format_package_details(
            package=package,
            enriched_dependencies=enriched_dependencies,
            enriched_dependants=enriched_dependants,
            installed_packages=self.app.provide_db.installed_packages,
        )
        self.update(formatted_text)


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

        os.makedirs(self.repo_path, exist_ok=True)

        self.repo: Optional[pygit2.Repository] = None

    def compose(self) -> ComposeResult:
        with Container(id="git-modal-container"):
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
        history_table.add_column("Message")

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

        self.perform_git_operation()

    @work(exclusive=True, thread=True)
    async def perform_git_operation(self) -> None:
        status_label = self.query_one("#git-status-label", Label)
        file_tree = self.query_one("#git-file-tree", DirectoryTree)
        commit_table = self.query_one("#git-commit-history", DataTable)

        if not pygit2:
            self.app.call_from_thread(
                status_label.update, "[b red]Pygit2 not loaded (internal error).[/]"
            )
            return

        try:
            self.app.call_from_thread(status_label.update, "Accessing local cache...")

            is_repo = False
            try:
                if pygit2.Repository(self.repo_path).is_bare == False:
                    is_repo = os.path.exists(os.path.join(self.repo_path, ".git"))
            except pygit2.GitError:
                is_repo = False

            if is_repo:
                self.app.call_from_thread(
                    status_label.update,
                    f"Pulling latest changes for [b]{self.package_base}[/]...",
                )
                self.repo = pygit2.Repository(self.repo_path)

                if self.repo is not None:
                    remote = self.repo.remotes["origin"]
                    remote.fetch()

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
                                break
                        except pygit2.KeyError:
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
                self.repo = pygit2.clone_repository(self.repo_url, self.repo_path)
                self.app.call_from_thread(status_label.update, "Clone complete.")

            self.app.call_from_thread(file_tree.reload)

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

        except pygit2.GitError as e:
            err_msg = f"[b red]Git operation error: {e}[/]\nURL: {self.repo_url}\nPath: {self.repo_path}"
            self.app.call_from_thread(status_label.update, err_msg)
        except Exception as e:
            err_msg = f"[b red]Unexpected error: {e}[/]"
            self.app.call_from_thread(status_label.update, err_msg)

    @on(DirectoryTree.FileSelected, "#git-file-tree")
    def show_file_content(self, event: DirectoryTree.FileSelected) -> None:
        content_view = self.query_one("#git-content-view", Static)
        status_label = self.query_one("#git-status-label", Label)
        file_path = event.path

        if not file_path.is_file():
            return

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            lexer = "text"
            filename_lower = file_path.name.lower()
            file_suffix_lower = file_path.suffix.lower()

            if filename_lower == "pkgbuild" or filename_lower == ".srcinfo":
                lexer = "bash"
            elif file_suffix_lower == ".install":
                lexer = "bash"
            elif file_suffix_lower in [".toml", ".desktop"]:
                lexer = "toml"
            elif file_suffix_lower == ".py":
                lexer = "python"
            elif file_suffix_lower in [".md", ".markdown"]:
                lexer = "markdown"
            elif file_suffix_lower == ".json":
                lexer = "json"
            elif file_suffix_lower in [".yaml", ".yml"]:
                lexer = "yaml"
            elif file_suffix_lower in [".diff", ".patch"]:
                lexer = "diff"

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

    @on(DataTable.RowSelected, "#git-commit-history")
    def show_commit_diff(self, event: DataTable.RowSelected) -> None:
        content_view = self.query_one("#git-content-view", Static)
        status_label = self.query_one("#git-status-label", Label)

        if not self.repo or not pygit2:
            status_label.update("[b red]Repository not loaded.[/]")
            return

        commit_id_str = str(event.row_key.value)
        try:
            commit_id = pygit2.Oid(hex=commit_id_str)
            commit = self.repo.get(commit_id)
            if not commit or not isinstance(commit, pygit2.Commit):
                raise ValueError("Selected item is not a valid commit.")

            parent_tree = commit.parents[0].tree if commit.parents else None

            diff = self.repo.diff(
                parent_tree, commit.tree, context_lines=3, interhunk_lines=1
            )

            diff_text = diff.patch
            if not diff_text:
                diff_text = "No textual changes in this commit."

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

    def action_close_modal(self) -> None:
        self.dismiss()

    def action_force_update_repo(self) -> None:
        if not PYGIT2_AVAILABLE or not self.package_base:
            self.app.notify(
                "Cannot update: Pygit2 not available or PackageBase missing.",
                severity="error",
            )
            return

        self.app.notify("Force updating repository...")
        self.query_one("#git-file-tree", DirectoryTree).clear()
        self.query_one("#git-commit-history", DataTable).clear()
        self.query_one("#git-content-view", Static).update("")
        self.query_one("#git-status-label", Label).update("Force updating...")

        self.perform_git_operation()


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
        yield Label(f"Comments for {self.package_base}", id="modal-title")
        yield Container(LoadingIndicator(), id="loading")
        yield VerticalScroll(id="comments-scroller")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#comments-scroller", VerticalScroll).display = False
        await self._load_and_render_comments()

    async def _fetch_aur_page_html(
        self, package_url: str
    ) -> Tuple[Optional[str], bool]:
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
        return None, False

    def _convert_html_node_to_textual_widget(
        self, node: Tag | NavigableString
    ) -> Optional[Widget | Text]:
        if isinstance(node, NavigableString):
            text = str(node)
            processed_text = text.replace("[", "\\[")
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

        elif tag_name == "code":
            inline_code_text = _process_inline_children(node)
            if inline_code_text.plain.strip():
                inline_code_text.stylize("reverse dim")
                return inline_code_text
            return None

        elif tag_name == "br":
            return Text("\n")

        else:
            fallback_text = _process_inline_children(node)
            if fallback_text.plain.strip():
                return fallback_text
            return None

    def _parse_aur_comment_html(self, html_content: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html_content, "html.parser")
        extracted_comments = []

        comment_sections = soup.find_all("div", class_="comments package-comments")

        for section_div in comment_sections:
            section_is_pinned = False
            section_header_div = section_div.find("div", class_="comments-header")
            if section_header_div:
                section_title_h3 = section_header_div.find("h3")
                if section_title_h3:
                    section_title_span = section_title_h3.find("span", class_="text")
                    if (
                        section_title_span
                        and "Pinned Comments" in section_title_span.get_text()
                    ):
                        section_is_pinned = True

            comment_header_tags = section_div.find_all("h4", class_="comment-header")

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
                        href=lambda href: href and href.startswith("/account/"),
                    )
                    if user_link_attempt:
                        user_name_str = user_link_attempt.get_text(strip=True)

                comment_data["user"] = user_name_str

                date_link_specific = header_tag.find("a", class_="date")
                if date_link_specific:
                    comment_data["date"] = date_link_specific.get_text(strip=True)
                else:
                    comment_data["date"] = date_str

                edited_span = header_tag.find("span", class_="edited")
                if edited_span:
                    comment_data["edited"] = edited_span.get_text(strip=False).strip()
                else:
                    comment_data["edited"] = None

                comment_data["pinned"] = section_is_pinned

                content_div = header_tag.find_next_sibling(
                    "div", class_="article-content"
                )
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
        return extracted_comments

    async def _load_and_render_comments(self, load_more: bool = False) -> None:
        if self._all_comments_loaded or self._is_loading_more:
            return

        self._is_loading_more = True
        loading_indicator_container = self.query_one("#loading", Container)
        comments_scroller = self.query_one("#comments-scroller", VerticalScroll)

        if not load_more:
            self.comment_counter = 0
            self._current_offset = 0
            self.parsed_comments.clear()
            comments_scroller.remove_children()
            comments_scroller.display = False
            loading_indicator_container.display = True

        aur_package_url = f"https://aur.archlinux.org/packages/{self.package_base}?O={self._current_offset}"
        html_content, _ = await self._fetch_aur_page_html(aur_package_url)
        has_next_page = False
        if html_content:
            soup_nav = BeautifulSoup(html_content, "html.parser")
            has_next_page = bool(
                soup_nav.find("a", class_="page", string=lambda s: s and "Next" in s)
            )

        if html_content:
            newly_parsed_comments = self._parse_aur_comment_html(html_content)
            if self._pinned_rendered:
                newly_parsed_comments = [
                    c for c in newly_parsed_comments if not c["pinned"]
                ]

            if not newly_parsed_comments and not load_more:
                comments_scroller.mount(
                    Static("No comments found.", classes="info-message")
                )
            else:
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
                comments_scroller.mount(
                    Static("Failed to load comments.", classes="error-message")
                )
            else:
                self._all_comments_loaded = True
                if comments_scroller.children:
                    self.notify("--- No more comments (due to load error) ---")
                    comments_scroller.mount(
                        Static("--- No more comments ---", classes="centered-text")
                    )

        loading_indicator_container.display = False
        comments_scroller.display = True
        self._is_loading_more = False

        if (
            not load_more
            and comments_scroller.virtual_size.height <= comments_scroller.size.height
            and not self._all_comments_loaded
            and not self._is_loading_more
        ):
            self.call_later(self._load_and_render_comments, load_more=True)

    def render_comment(self, idx: int, comment: dict[str, Any]) -> Container:
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
        if not self._all_comments_loaded and not self._is_loading_more:
            self.call_later(self._load_and_render_comments, load_more=True)
        elif self._all_comments_loaded:
            self.notify("No more comments")

    def action_close_modal(self) -> None:
        self.dismiss()


class ProfileModal(ModalScreen[Optional[Dict[str, Any]]]):
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
