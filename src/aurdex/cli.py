import argparse
import importlib.metadata
import json
import os
import httpx
from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.tree import Tree
import appdirs

from .db import PackageDB, DependencyResolver, AUR_JSON, AUR_DB_URL


def main():
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
        help="(WIP) Search package name for info.",
    )
    parser.add_argument("--test-search", action="store_true", help="(WIP)")
    parser.add_argument(
        "--deptree",
        nargs="+",
        metavar="PACKAGE",
        help="Resolve and display the dependency installation tree for one or more packages.",
    )
    parser.add_argument(
        "--rebuild-db",
        action="store_true",
        help="Force a full download and rebuild of the package database.",
    )
    parser.add_argument(
        "--update-db",
        action="store_true",
        help="Download AUR metadata and update the package database.",
    )
    args = parser.parse_args()

    console = Console()

    if args.rebuild_db:
        db = PackageDB(console=console)
        num_updates = 0
        try:
            num_updates = db.rebuild(full=True, download=True)
            console.print(f"[bold green]{num_updates} packages rebuilt.[/bold green]")
        except Exception as e:
            console.print(f"[bold red]Database rebuild failed: {e}[/bold red]")
        return
    elif args.update_db:
        db = PackageDB(console=console)
        try:
            num_updates = db.rebuild(full=False, download=True)
            console.print(f"[bold green]{num_updates} packages updated.[/bold green]")
        except Exception as e:
            console.print(f"[bold red]Database update failed: {e}[/bold red]")
        return

    # --- Database Preparation ---
    db = PackageDB(console=console)
    # db._ensure_database()

    if args.deptree:
        resolver = DependencyResolver(db, console=console)
        package_names = args.deptree
        console.print(
            f"[bold]Resolving dependency tree for: {', '.join(package_names)}...[/bold]"
        )
        result = resolver.resolve_dependency_tree(package_names)

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

    if args.search:
        console.print("[bold cyan]Running search...[/bold cyan]")

        for term in args.search:
            results = db.search(search_term=term)
            console.print(f"[b]{term}[/b]: Found {len(results)} packages.")

            if results:
                console.print("  First 20 results:")
                for pkg in results[:20]:
                    console.print(f"    - {pkg['name']} ({pkg['source']})")

        return

    if args.test_search:
        console.print("[bold cyan]Running search test...[/bold cyan]")
        results1 = db.search()
        console.print(f"Test 1 (no filters): Found {len(results1)} packages.")

        search_term = "yay"
        results2 = db.search(search_term=search_term)
        console.print(
            f"Test 2 (search_term='{search_term}'): Found {len(results2)} packages."
        )
        if results2:
            console.print("First 5 results:")
            for pkg in results2[:5]:
                console.print(f"  - {pkg['name']} ({pkg['source']})")

        maintainer = "envolution"
        results3 = db.search(filters={"maintainer": maintainer})
        console.print(
            f"Test 3 (maintainer='{maintainer}'): Found {len(results3)} packages."
        )
        if results3:
            console.print("First 5 results:")
            for pkg in results3[:5]:
                console.print(f"  - {pkg['name']} ({pkg['source']})")
        return

    if args.package_name:
        package = db.package_info(args.package_name)
        if not package:
            print(f"Package '{args.package_name}' not found.")
            return

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="bold magenta")
        table.add_column()

        for key, value in package.items():
            if isinstance(value, list) and value:
                table.add_row(f"{key}:", "")
                for item in value:
                    if isinstance(item, dict):
                        table.add_row("", Text(str(item), "dim"))
                    else:
                        table.add_row("", str(item))
            elif value:
                table.add_row(f"{key}:", str(value))

        console.print(table)
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
