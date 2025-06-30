import argparse
import importlib.metadata
import json
import os
import appdirs


def main():
    appname = "aurdex"
    parser = argparse.ArgumentParser(
        description="Aurdex - A terminal UI for the Arch User Repository."
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
    args = parser.parse_args()

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

    app = aurdex(profile_name=args.profile)
    app.run()
    app.save_current_profile()


if __name__ == "__main__":
    main()
