from datetime import datetime
from typing import Optional, List, Dict, Any
from rich.text import Text


def format_package_details(
    package: Dict[str, Any],
    enriched_dependencies: Optional[Dict[str, List[Dict]]] = None,
    enriched_dependants: Optional[Dict[str, List[Dict]]] = None,
    installed_packages: Optional[Dict[str, Any]] = None,
) -> Any:
    if not package:
        return Text.from_markup("[dim italic]Select a package to see details.[/dim]")

    installed_packages = installed_packages or {}
    content_parts = []

    first_submitted_val = package.get("first_submitted")
    last_modified_val = package.get("last_modified")

    first_submitted = (
        datetime.fromtimestamp(first_submitted_val).strftime("%Y-%m-%d %H:%M:%S")
        if first_submitted_val is not None
        else "[dim]N/A[/dim]"
    )
    last_modified = (
        datetime.fromtimestamp(last_modified_val).strftime("%Y-%m-%d %H:%M:%S")
        if last_modified_val is not None
        else "[dim]N/A[/dim]"
    )

    maintainer = package.get("maintainer")
    comaintainers = package.get("CoMaintainers", [])
    all_maintainers_list = [m for m in ([maintainer] + comaintainers) if m is not None]
    all_maintainers_str = (
        ", ".join(all_maintainers_list) if all_maintainers_list else "[dim]None[/dim]"
    )

    submitter = package.get("submitter", "[dim]_Not specified_[/dim]")

    ood_val = package.get("out_of_date")
    ood_status_text = "Yes" if ood_val else "No"
    ood_style_tag = "[b $warning]" if ood_val else "[b $success]"

    content_parts.append(
        f"[b $text]Votes:[/] [b $primary]{package.get('num_votes', 0) or 0}[/]  "
        f"[b $text]Popularity:[/] [b $primary]{package.get('popularity', 0) or 0:.2f}[/]  "
        f"[b $text]Out of Date:[/] {ood_style_tag}{ood_status_text}[/]\n\n"
    )

    content_parts.append(
        f"[b $primary]{package.get('name', 'Unknown')}[/] - [dim $secondary]{package.get('version', 'Unknown')}[/]\n"
        f"[italic $text-subtle]{package.get('description', 'No description available.')}[/]\n\n"
    )

    content_parts.append(
        f"[b $accent]ID:[/] [$text]{package.get('pkg_id', '[dim]Unknown[/dim]') or '[dim]Unknown[/dim]'}[/$text]\n"
    )
    content_parts.append(
        f"[b $accent]PackageBase:[/] [$text]{package.get('package_base', '[dim]Unknown[/dim]') or '[dim]Unknown[/dim]'}[/]\n"
    )
    url_val = package.get("url")
    url_display = (
        f"[$link]{url_val}[/$link]" if url_val else "[dim]_Not specified_[/dim]"
    )
    content_parts.append(f"[b $accent]Homepage :[/] {url_display}\n")
    content_parts.append(
        f"[b $accent]Submitter:[/] [$text]{submitter or '[dim]Not specified[/dim]'}[/]\n"
    )
    license_data = package.get("License", [])
    license_text = (
        f"{', '.join(license_data)}" if license_data else "[dim]Unknown[/dim]"
    )
    content_parts.append(f"[b $accent]License(s):[/] [b $text]{license_text}[/]\n")

    aur_path = package.get("url_path")
    aur_link_full = f"https://aur.archlinux.org{aur_path}"
    aur_display = (
        f"[$link]{aur_link_full}[/$link]" if aur_path else "[dim]_Not specified_[/dim]"
    )
    content_parts.append(
        f"[b $accent]AUR Link:[/] [link]https://aur.archlinux.org/packages/{package.get('package_base', '[dim]Unknown[/dim]')}[/link]\n"
    )
    content_parts.append(f"[b $accent]AUR Snapshot:[/] {aur_display}\n")
    content_parts.append(
        f"[b $accent]AUR Clone Repo:[/] [link]https://aur.archlinux.org/{package.get('package_base', '[dim]Unknown[/dim]')}.git[/link]\n"
    )

    keywords_list_data = package.get("Keywords", [])
    keywords_str_val = (
        f"{', '.join(keywords_list_data)}" if keywords_list_data else "[dim]None[/dim]"
    )
    content_parts.append(
        f"[b $accent]Keywords:[/] [$text-muted]{keywords_str_val}[/]\n\n"
    )

    content_parts.append(
        f"[b $accent]Last Modified:[/] [b $text]{last_modified}[/]\n"
        f"[b $accent]First Submitted:[/] [b $text]{first_submitted}[/]\n"
        f"[b $accent]Maintainer(s):[/] [i $text]{all_maintainers_str}[/]\n\n"
    )

    list_sections_config_data = [
        ("Replaces", "Replaces", False),
        ("Groups", "Groups", False),
        ("Provides", "Provides", False),
        ("Conflicts", "Conflicts", False),
        ("Dependencies", "Depends", True),
        ("Optional Dependencies", "OptDepends", True),
        ("Make Dependencies", "MakeDepends", True),
        ("Check Dependencies", "CheckDepends", True),
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
            and enriched_dependencies
            and package_key_str in enriched_dependencies
        ):
            items_for_section_list = enriched_dependencies[package_key_str]
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
                        item_spec.split(":", 1)[1].strip() if ":" in item_spec else None
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
                has_any_list_content_flag = True

            content_parts.append(f"[b $text]{section_title_str}[/]\n")

            if is_enriched_data_flag or (
                use_enriched_logic_flag
                and items_for_section_list
                and isinstance(items_for_section_list[0], dict)
            ):
                for dep_item in items_for_section_list:
                    content_parts.append(
                        f"  [dim]-[/dim] [$accent]{dep_item['original_spec']}[/$accent]\n"
                    )
                    if dep_item.get("providers") is not None:
                        providers = dep_item["providers"]
                        if providers:
                            last_index = len(providers) - 1
                            for i, provider_pkg_item in enumerate(providers):
                                source = provider_pkg_item.get("source")
                                if source == "local":
                                    continue
                                name = provider_pkg_item.get("name")
                                is_installed = installed_packages.get(name) is not None
                                status_icon = (
                                    "[b $success]✔[/]" if is_installed else " "
                                )
                                tree_char = "└─" if i == last_index else "├─"

                                resolution_text = ""
                                if (
                                    provider_pkg_item.get("resolution_type")
                                    == "replaces"
                                ):
                                    resolution_text = f" [b $warning][dim]-⚠️ Replaces {dep_item['name']}-[/dim][/]"

                                line = (
                                    f"    {tree_char}{status_icon}"
                                    f"[$text-subtle]{provider_pkg_item.get('source', 'N/A')}/"
                                    f"{provider_pkg_item.get('name', 'N/A')}[/]{resolution_text} "
                                    f"[dim $text-subtle]({provider_pkg_item.get('version', 'N/A')})[/]"
                                )
                                if is_installed:
                                    line = f"[b $success]{line}[/b $success]"
                                content_parts.append(line + "\n")
                        else:
                            content_parts.append(
                                "    [dim]└─[/dim][b $error]✗Not Available[/b $error]\n"
                            )
                    elif use_enriched_logic_flag:
                        content_parts.append("    [dim]  └─ Resolving...[/dim]\n")
            else:
                for item_val_str in items_for_section_list:
                    content_parts.append(
                        f"  [dim]-[/dim] [$secondary]{item_val_str}[/$secondary]\n"
                    )
            content_parts.append("\n")

    if not has_any_list_content_flag:
        content_parts.append(
            "[dim italic align=center]_No explicit dependencies, provisions, conflicts, or groups listed._[/]\n\n"
        )

    if enriched_dependants:
        content_parts.append("[b $text on $panel]Dependants[/]\n")
        for provide, dependants in enriched_dependants.items():
            if dependants:
                content_parts.append(f"  [b $accent]{provide}[/b $accent]\n")
                last_index = len(dependants) - 1
                for i, dependant in enumerate(dependants):
                    tree_char = "└─" if i == last_index else "├─"
                    content_parts.append(
                        f"    {tree_char} [$secondary]{dependant['source']}/{dependant['name']} ({dependant['link_type']})[/$secondary]\n"
                    )
        content_parts.append("\n")
    elif enriched_dependants == {}:
        content_parts.append("[b $text on $panel]Dependants[/]\n")
        content_parts.append("  [dim italic]None[/dim italic]\n\n")
    else:  # Loading state
        content_parts.append("[b $text on $panel]Dependants[/]\n")
        content_parts.append("  [dim]Loading...[/dim]\n\n")
    # return Text.from_markup("".join(content_parts))
    return "".join(content_parts)
