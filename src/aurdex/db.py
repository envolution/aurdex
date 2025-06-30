from typing import Optional, List, Dict, Tuple

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

    def refresh(self, aur_data: Optional[List[Dict]] = None):
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
