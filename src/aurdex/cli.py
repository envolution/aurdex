import argparse
import importlib.metadata
from contextlib import nullcontext
import json
import os
from rich.console import Console
from rich.table import Table
from rich.tree import Tree
import appdirs

from .db import PackageDB, DependencyResolver
from .formatters import format_package_details


def translate_textual_to_rich_markup(markup_string: str) -> str:
    """Translates Textual's style variables to Rich-compatible color names."""
    style_map = {
        "$text": "default",
        "$link": "blue",
        "$primary": "cyan",
        "$secondary": "sky_blue1",
        "$accent": "medium_purple",
        "$warning": "yellow",
        "$error": "red",
        "$success": "green",
        "$panel": "grey70",
        "$text-muted": "grey50",
        "$text-subtle": "grey50",
    }
    for textual_style, rich_style in style_map.items():
        markup_string = markup_string.replace(textual_style, rich_style)
    return markup_string


def main():
    valid_filter_keys = [
        "maintainer",
        "source",
        "depends",
        "makedepends",
        "checkdepends",
        "optdepends",
        "provides",
        "out_of_date",
        "abandoned",
        "comaintainers",
        "license",
    ]
    PAGER_ENABLE = 40  # how many outputs before using PAGER
    appname = "aurdex"
    parser = argparse.ArgumentParser(
        description="Aurdex - A terminal UI for the Arch User Repository."
    )
    parser.add_argument(
        "package_name",
        nargs="?",
        help="Display information for a specific package and exit.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{appname} {importlib.metadata.version(appname)}",
    )
    parser.add_argument(
        "--list-profiles", action="store_true", help="List available profiles and exit."
    )
    parser.add_argument("--profile", help="Load a specific profile on startup.")
    parser.add_argument(
        "-s",
        "--search",
        nargs="+",
        metavar="TERM",
        help="One or more search terms. Regular expressions are automatically detected (e.g. '^lib.*').",
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=20,
        help="Limit results to integer limit.  Defaults to 20, '-1' sets to infinite.",
    )
    parser.add_argument(
        "-f",
        "--filter",
        metavar="key=value",
        action="append",
        help=(
            'Apply one or more filters (can be repeated and combined with --search). Example: "-f maintainer=alice -f out_of_date"\n'
            f"\nSupported keys: {', '.join(valid_filter_keys)}"
        ),
    )
    parser.add_argument(
        "--deptree",
        nargs="+",
        metavar="PACKAGE",
        help="Resolve and display the shallow dependency installation tree for one or more packages.",
    )
    parser.add_argument(
        "--deptree-deep",
        nargs="+",
        metavar="PACKAGE",
        help="Resolve and display the deep dependency installation tree for one or more packages.",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Force a full download and rebuild of the package database.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Download AUR metadata and update the package database.",
    )
    args = parser.parse_args()

    console = Console()
    arg_dict = vars(args)
    active_flags = {k: v for k, v in arg_dict.items() if v not in (None, False)}

    if args.rebuild:
        db = PackageDB(console=console)
        num_updates = 0
        try:
            num_updates = db.rebuild(full=True, download=True)
            console.print(f"[bold green]{num_updates} packages rebuilt.[/bold green]")
        except Exception as e:
            console.print(f"[bold red]Database rebuild failed: {e}[/bold red]")

        if len(active_flags) == 2:  # avoid launching gui if we're only rebuilding
            return

    elif args.update:
        db = PackageDB(console=console)
        try:
            num_updates = db.rebuild(full=False, download=True)
            console.print(f"[bold green]{num_updates} packages updated.[/bold green]")
        except Exception as e:
            console.print(f"[bold red]Database update failed: {e}[/bold red]")

        if len(active_flags) == 2:  # avoid launching gui if we're only updating
            return

    # --- Database Preparation ---
    db = PackageDB(console=console)
    db._ensure_database()

    if args.deptree or args.deptree_deep:
        resolver = DependencyResolver(db, console=console)
        package_names = args.deptree or args.deptree_deep
        deep_search = args.deptree_deep is not None

        console.print(
            f"[bold]Resolving {'deep' if deep_search else 'shallow'} dependency tree for: {', '.join(package_names)}...[/bold]"
        )

        if deep_search:
            result = resolver.resolve_dependency_tree_deep(package_names)
        else:
            result = resolver.resolve_dependency_tree_shallow(package_names)

        if not result["order"] and not result["cycles"] and not result.get("satisfied"):
            console.print(
                "[yellow]Could not resolve any packages. Are the names correct?[/yellow]"
            )
            return

        tree = Tree(
            "📦 [bold cyan]Installation Plan[/bold cyan]", guide_style="bright_black"
        )

        if result["cycles"]:
            cycle_tree = tree.add(
                "🚨 [bold red]Step 1: Resolve Circular Dependencies[/bold red]"
            )
            cycle_tree.add(
                "[yellow]The following packages form a dependency cycle and require manual intervention.[/yellow]"
            )
            for i, cycle in enumerate(result["cycles"]):
                cycle_node = cycle_tree.add(f"Cycle {i + 1}:")
                build_first = cycle[-1]
                cycle_node.add(
                    f"[magenta]Build [b]{build_first}[/b] with --nodeps, then build the others normally.[/magenta]"
                )
                cycle_node.add(
                    " -> ".join(f"[b]{p}[/b]" for p in cycle) + f" -> [b]{cycle[0]}[/b]"
                )

        if result["order"] or result.get("satisfied"):
            step_title = (
                "Step 2: Install Dependencies"
                if result["cycles"]
                else "Step 1: Install Dependencies"
            )
            dep_tree = tree.add(f"✅ [bold green]{step_title}[/bold green]")

            if result.get("satisfied"):
                installed_branch = dep_tree.add("[dim]Already satisfied[/dim]")
                for pkg_name in sorted(result["satisfied"]):
                    installed_branch.add(f"✔️ {pkg_name}")

            repo_packages = [p for p in result["order"] if p.get("source") != "aur"]
            aur_packages = [p for p in result["order"] if p.get("source") == "aur"]

            if repo_packages:
                repo_branch = dep_tree.add("[blue]From Repositories[/blue]")
                for pkg in repo_packages:
                    display_name = f"[b]{pkg.get('name', 'N/A')}[/b] [dim]({pkg.get('version', 'N/A')})[/dim]"
                    repo_branch.add(
                        f"📦 {display_name} [dim]({pkg.get('source')})[/dim]"
                    )

            if aur_packages:
                aur_branch = dep_tree.add("[yellow]From AUR[/yellow]")
                for pkg in aur_packages:
                    display_name = f"[b]{pkg.get('name', 'N/A')}[/b] [dim]({pkg.get('version', 'N/A')})[/dim]"
                    aur_branch.add(f"🔨 {display_name}")

        console.print(tree)
        return

    filters = {}
    if args.filter:
        for f in args.filter:
            # Handle key[=value] format
            if "=" in f:
                key, value = f.split("=", 1)
                key = key.strip().lower()
                value = value.strip().lower()
            else:
                key = f.strip().lower()
                value = "true"  # default for boolean-style flags

            if key not in valid_filter_keys:
                parser.error(
                    f"Invalid filter key: '{key}'.\n"
                    f"Valid filter keys are: {', '.join(valid_filter_keys)}"
                )
            filters[key] = value

    if args.search or args.filter:
        terms = args.search or [""]  # empty search if no terms given
        for term in terms:
            if filters:
                console.print(
                    f"[bold cyan]Running search for '{term}' with {filters}...[/bold cyan]"
                )
            else:
                console.print(f"[bold cyan]Running search for {term}...[/bold cyan]")

            results = db.search(search_term=term, filters=filters, limit=args.limit)

            if results:
                table = Table(show_header=True, pad_edge=True)
                table.add_column("Source", style="cyan", no_wrap=True, justify="right")
                table.add_column("Name", style="bold")
                table.add_column("Version", style="dim")
                for pkg in results:
                    table.add_row(pkg["source"], pkg["name"], pkg["version"])

                output_header = f"[b]'{term}'[/b]: Found {len(results)} packages (limit: {args.limit})."
                output_hint = (
                    "[bold cyan]Using PAGER...[/bold cyan]"
                    if len(results) > PAGER_ENABLE
                    else "[bold cyan]Printing directly...[/bold cyan]"
                )

                console.print(output_hint)
                output_func = (
                    console.pager if len(results) > PAGER_ENABLE else nullcontext
                )

                with output_func():
                    console.print(output_header)
                    console.print(table)

        return

    if args.package_name:
        package = db.package_info(args.package_name)
        if not package:
            print(f"Package '{args.package_name}' not found.")
            return

        enriched_deps = db.get_enriched_dependencies(package)
        dependants_by_provide = db.get_dependants(
            package["name"], package.get("Provides", [])
        )

        formatted_output = format_package_details(
            package=package,
            enriched_dependencies=enriched_deps,
            enriched_dependants=dependants_by_provide,
            installed_packages=db.installed_packages,
        )
        rich_output = translate_textual_to_rich_markup(formatted_output)
        console.print(rich_output)
        return

    config_path_dir = appdirs.user_config_dir(appname="aurdex")
    config_file = os.path.join(config_path_dir, "settings.json")

    if args.list_profiles:
        if os.path.exists(config_file):
            with open(config_file) as f:
                config = json.load(f)
                profiles = config.get("profiles", {})
                default_profile = config.get("default_profile", "default")
                for profile_name in profiles:
                    if profile_name == default_profile:
                        print(f"{profile_name} (default)")
                    else:
                        print(profile_name)
        else:
            print("No profiles found.")
        return

    from .main import aurdex

    app = aurdex(profile_name=args.profile, db=db)
    app.run()
    app.save_current_profile()


if __name__ == "__main__":
    main()
