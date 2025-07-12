#!/usr/bin/env python3
"""
aurdex.db – unified AUR + repo package cache
"""

import gzip
import json
import sqlite3
import logging
import functools
import contextlib
import os
import threading
import httpx
import re
from pathlib import Path
from typing import Optional, Tuple, Iterator, List, Dict, Any
from appdirs import user_cache_dir
from rich.console import Console

try:
    import pyalpm

    _HAVE_PYALPM = True
except ModuleNotFoundError:
    _HAVE_PYALPM = False

LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 3
APP_NAME = "aurdex"
AUR_JSON = Path(user_cache_dir(APP_NAME)) / "packages-meta-ext-v1.json.gz"
AUR_DB_URL = "https://aur.manjaro.org/packages-meta-ext-v1.json.gz"
DB_PATH = Path(user_cache_dir(APP_NAME)) / "packages.db"


# --------------------------------------------------------------------------- #
# SQLite schema (normalised, single DB)                                       #
# --------------------------------------------------------------------------- #

DDL = f"""
BEGIN;

CREATE TABLE db_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE packages (
    pkg_id            INTEGER,
    name              TEXT NOT NULL COLLATE NOCASE,
    version           TEXT,
    description       TEXT,
    url               TEXT,
    url_path          TEXT,
    filename          TEXT,
    maintainer        TEXT,
    submitter         TEXT,
    packager          TEXT,
    arch              TEXT,
    build_date        INTEGER,
    install_date      INTEGER,
    first_submitted   INTEGER,
    last_modified     INTEGER,
    popularity        REAL,
    out_of_date       INTEGER,
    package_base      TEXT,
    package_base_id   INTEGER,
    num_votes         INTEGER,
    isize             INTEGER,
    size              INTEGER,
    md5sum            TEXT,
    sha256sum         TEXT,
    base64_sig        TEXT,
    has_scriptlet     BOOLEAN,
    source            TEXT NOT NULL,
    metadata          TEXT, -- JSON blob for denormalized data
    PRIMARY KEY (name, source)
);

CREATE TABLE links (
    name      TEXT NOT NULL,
    source    TEXT NOT NULL,
    link_type TEXT NOT NULL,
    target    TEXT NOT NULL,
    FOREIGN KEY (name, source) REFERENCES packages(name, source) ON DELETE CASCADE
);

CREATE TABLE package_groups (
    name      TEXT NOT NULL,
    source    TEXT NOT NULL,
    groupname TEXT NOT NULL,
    FOREIGN KEY (name, source) REFERENCES packages(name, source) ON DELETE CASCADE
);

-- helpful indices
CREATE INDEX idx_pkg_name          ON packages(name);
CREATE INDEX idx_links_type_target ON links(link_type, target);
CREATE INDEX idx_links_name        ON links(name);
CREATE INDEX idx_groups_group      ON package_groups(groupname);

PRAGMA user_version = {SCHEMA_VERSION};
COMMIT;
"""

LINK_FIELDS = {
    "Depends",
    "OptDepends",
    "MakeDepends",
    "CheckDepends",
    "Provides",
    "Replaces",
    "Conflicts",
}


def regexp(expr, item):
    """Case-insensitive regex search function for SQLite."""
    if item is None:
        return False
    reg = re.compile(expr, re.IGNORECASE)
    return reg.search(item) is not None


class PackageDB:
    """Unified AUR / repo package cache."""

    def __init__(
        self,
        db_path: Path = DB_PATH,
        aur_json: Path = AUR_JSON,
        console: Optional[Console] = None,
    ) -> None:
        self.db_path = db_path
        self.aur_json = aur_json
        self.console = console or Console()
        self.db_age = None
        self._db_lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.db_path.exists():
            self.db_age = self.db_path.stat().st_mtime
        self.installed_packages = self._get_installed_packages()
        self.installed_provides = self._get_installed_provides()
        self._pyalpm_handle = None

    def _get_installed_packages(self) -> Dict[str, str]:
        if not _HAVE_PYALPM:
            return {}
        try:
            handle = pyalpm.Handle("/", "/var/lib/pacman")  # type: ignore[attr-defined]
            localdb = handle.get_localdb()
            return {pkg.name: pkg.version for pkg in localdb.pkgcache}
        except pyalpm.error as e:  # type: ignore[attr-defined]
            LOGGER.error(f"Could not read local package database: {e}")
            return {}

    def _get_installed_provides(self) -> Dict[str, str]:
        """Create a map of provided names to the packages that provide them."""
        if not _HAVE_PYALPM:
            return {}
        provides_map = {}
        try:
            handle = pyalpm.Handle("/", "/var/lib/pacman")  # type: ignore[attr-defined]
            localdb = handle.get_localdb()
            for pkg in localdb.pkgcache:
                for p in pkg.provides:
                    provided_name = p.split("=")[0].strip()
                    provides_map[provided_name] = pkg.name
        except pyalpm.error as e:  # type: ignore[attr-defined]
            LOGGER.error(f"Could not read local package database for provides: {e}")
        return provides_map

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = None
        try:
            with self._db_lock:
                conn = sqlite3.connect(self.db_path)
                conn.row_factory = sqlite3.Row
                conn.create_function("REGEXP", 2, regexp)
                yield conn
        except sqlite3.Error as e:
            LOGGER.error(f"Database connection error: {e}")
            raise
        finally:
            if conn:
                conn.close()

    def get_package_dependencies(self, pkg_name: str) -> List[str]:
        """Get dependencies for a single package, optimized."""
        query = """
            SELECT l.target
            FROM links l
            WHERE l.name = ?
              AND l.link_type = 'Depends'
              AND (l.source = 'aur' OR l.source IN (SELECT source FROM packages WHERE name = ? AND source != 'aur'))
            ORDER BY l.source != 'aur' -- Prioritize AUR
        """
        with self.connection() as conn:
            results = conn.execute(query, (pkg_name, pkg_name)).fetchall()
        return [
            re.split(r"[<>=]", row[0])[0].strip().split(":", 1)[0] for row in results
        ]

    def get_packages_dependencies(self, pkg_names: List[str]) -> Dict[str, List[str]]:
        """Get dependencies for a list of packages in a batch."""
        if not pkg_names:
            return {}

        placeholders = ",".join("?" for _ in pkg_names)
        query = f"""
            SELECT p.name, l.target
            FROM packages p
            JOIN links l ON p.name = l.name AND p.source = l.source
            WHERE p.name IN ({placeholders}) AND l.link_type = 'Depends'
            ORDER BY p.name
        """
        with self.connection() as conn:
            results = conn.execute(query, pkg_names).fetchall()

        deps_map: Dict[str, List[str]] = {name: [] for name in pkg_names}
        for row in results:
            pkg_name, dep_target = row
            dep_name = re.split(r"[<>=]", dep_target)[0].strip().split(":", 1)[0]
            if pkg_name in deps_map:
                deps_map[pkg_name].append(dep_name)

        return deps_map

    def search_by_provides(self, token: str) -> List[Tuple[str, str]]:
        """Return (name, source) where token ∈ Provides."""
        # Normalize the token by removing version constraints
        base_token = re.split(r"[<>=]", token)[0].strip().split(":", 1)[0]
        q = "SELECT name, source FROM links WHERE link_type = 'Provides' AND (target = ? OR target LIKE ?)"
        with self.connection() as c:
            return c.execute(q, (base_token, f"{base_token}=%")).fetchall()

    def search_by_depends(self, token: str) -> List[Tuple[str, str, str]]:
        """Return (name, source, type) where token ∈ any dependency type (Depends, MakeDepends, etc.)."""
        base_token = re.split(r"[<>=]", token)[0].strip().split(":", 1)[0]
        q = """
        SELECT name, source, link_type
        FROM links
        WHERE link_type IN ('Depends', 'CheckDepends', 'MakeDepends', 'OptDepends')
        AND (
            target = ?
            OR target LIKE ? || '=%'
            OR target LIKE ? || ':%'
        );
        """
        with self.connection() as c:
            return c.execute(q, (base_token, base_token, base_token)).fetchall()

    @functools.lru_cache(maxsize=8192)
    def package_info(
        self, name: str, source: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        with self.connection() as conn:
            all_rows = conn.execute(
                "SELECT * FROM packages WHERE name=?", (name,)
            ).fetchall()
            if not all_rows:
                return None
            source_to_row = {row["source"]: row for row in all_rows}
            if source == "aur" or (source is None and "aur" in source_to_row):
                base_row = source_to_row.get("aur")
            elif source and source in source_to_row:
                base_row = source_to_row[source]
            else:
                base_row = all_rows[0]
            if not base_row:
                return None

            pkg = dict(base_row)

            if metadata_str := pkg.get("metadata"):
                try:
                    metadata_json = json.loads(metadata_str)
                    pkg.update(metadata_json)
                except json.JSONDecodeError:
                    LOGGER.warning(f"Could not parse metadata for {name}")

            if pkg.get("source") == "aur":
                pkg["PackageBase"] = base_row["package_base"]
            elif "aur" in source_to_row:
                pkg["PackageBase"] = source_to_row["aur"]["package_base"]
                pkg["URLPath"] = source_to_row["aur"]["url_path"]

            pkg_name, pkg_source = pkg["name"], pkg["source"]

            for link_type in LINK_FIELDS:
                q = "SELECT target FROM links WHERE name=? AND source=? AND link_type=?"
                pkg[link_type] = [
                    row[0] for row in conn.execute(q, (pkg_name, pkg_source, link_type))
                ]

            q = "SELECT groupname FROM package_groups WHERE name=? AND source=?"
            pkg["Groups"] = [row[0] for row in conn.execute(q, (pkg_name, pkg_source))]

            return pkg

    def _is_regex(self, s: str) -> bool:
        try:
            re.compile(s)
            return True
        except re.error:
            return False

    def search(
        self,
        search_term: str = "",
        filters: Optional[Dict[str, Any]] = None,
        sort_by: str = "popularity",
        sort_reverse: bool = True,
        limit: int = -1,  # No limit
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        filters = filters or {}
        link_type_filters = {
            "provides": "Provides",
            "depends": "Depends",
            "makedepends": "MakeDepends",
            "checkdepends": "CheckDepends",
            "optdepends": "OptDepends",
        }
        query = "SELECT DISTINCT p.source, p.name, p.version, COALESCE(p.popularity, 0.0) AS popularity, COALESCE(p.num_votes, 0) AS num_votes, p.pkg_id FROM packages p"
        params: List[Any] = []
        where_clauses, joins = [], []
        if search_term:
            operator = "REGEXP" if self._is_regex(search_term) else "LIKE"
            search_val = search_term if operator == "REGEXP" else f"%{search_term}%"
            where_clauses.append(f"(p.name {operator} ?)")
            params.append(search_val)
        for key, value in filters.items():
            link_type = link_type_filters.get(key)
            if key in ["abandoned", "out_of_date"]:
                if value:
                    where_clauses.append(
                        f"p.{key} IS NOT NULL"
                        if key == "out_of_date"
                        else "(p.maintainer IS NULL OR p.maintainer = '')"
                    )
            elif link_type and value:
                alias = f"l_{key}"
                joins.append(
                    f"JOIN links {alias} ON p.name = {alias}.name AND p.source = {alias}.source"
                )
                where_clauses.append(f"{alias}.link_type = ? AND {alias}.target LIKE ?")
                params.extend([link_type, f"{value}%"])
            elif isinstance(value, str) and value:
                operator = "REGEXP" if self._is_regex(value) else "="
                where_clauses.append(f"p.{key} {operator} ?")
                params.append(value)
            elif key == "repos" and isinstance(value, list) and value:
                placeholders = ",".join("?" for _ in value)
                where_clauses.append(f"p.source IN ({placeholders})")
                params.extend(value)

        if joins:
            query += " " + " ".join(list(dict.fromkeys(joins)))
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        valid_sort_columns = [
            "name",
            "popularity",
            "num_votes",
            "last_modified",
            "first_submitted",
        ]
        sort_by = sort_by if sort_by in valid_sort_columns else "popularity"
        order = "DESC" if sort_reverse else "ASC"
        query += f" ORDER BY {sort_by} {order} LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def _ensure_database(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        rebuild_required = False
        if not self.db_path.exists():
            rebuild_required = True
            LOGGER.info("Database file not found, full rebuild required.")
            self.console.print(
                "[bold yellow]Database not found. Performing initial build...[/bold yellow]"
            )
        else:
            with self.connection() as conn:
                try:
                    # Check schema version
                    ver_row = conn.execute("PRAGMA user_version").fetchone()
                    ver = ver_row[0] if ver_row else 0
                    if ver != SCHEMA_VERSION:
                        rebuild_required = True
                        LOGGER.info(
                            f"Schema version mismatch (db: {ver}, required: {SCHEMA_VERSION}), full rebuild required."
                        )
                        self.console.print(
                            "[bold yellow]Database schema is outdated. Rebuilding...[/bold yellow]"
                        )
                    else:
                        # Check build status
                        status_row = conn.execute(
                            "SELECT value FROM db_metadata WHERE key = 'build_status'"
                        ).fetchone()
                        status = status_row[0] if status_row else "pending"
                        if status != "complete":
                            rebuild_required = True
                            LOGGER.info(
                                f"Database build status is '{status}', full rebuild required."
                            )
                            self.console.print(
                                "[bold yellow]Database is not fully populated. Rebuilding...[/bold yellow]"
                            )
                except sqlite3.DatabaseError:
                    rebuild_required = True
                    LOGGER.warning(
                        "Database error while checking schema version, full rebuild required."
                    )
                    self.console.print(
                        "[bold red]Database is corrupted. Rebuilding...[/bold red]"
                    )

        if rebuild_required:
            try:
                os.unlink(self.db_path)
            except FileNotFoundError:
                self.console.print(
                    f"[dim]No existing database found at {self.db_path}[/dim]"
                )
            except Exception as e:
                self.console.print(f"[bold red]Failed to remove DB:[/bold red] {e}")

            try:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                self.console.print(
                    f"[bold red]Failed to create directory:[/bold red] {e}"
                )
                raise
            self.rebuild(full=True, download=True)

    def _download_aur_json(self) -> None:
        """Downloads the AUR JSON metadata file."""
        if not self.console:
            # If no console is available, we can't show progress.
            print("Downloading AUR metadata...")
        try:
            with httpx.stream(
                "GET", AUR_DB_URL, follow_redirects=True, timeout=60
            ) as response:
                response.raise_for_status()
                with open(self.aur_json, "wb") as f:
                    if self.console:
                        with self.console.status(
                            "[bold green]Downloading metadata...", spinner="dots"
                        ):
                            for chunk in response.iter_bytes():
                                f.write(chunk)
                    else:
                        for chunk in response.iter_bytes():
                            f.write(chunk)
            if self.console:
                self.console.print("[bold green]Download complete.[/bold green]")
            else:
                print("Download complete.")
        except httpx.RequestError as e:
            if self.console:
                self.console.print(
                    f"[bold red]Error downloading metadata: {e}[/bold red]"
                )
            else:
                print(f"Error downloading metadata: {e}")
            raise  # Re-raise the exception to be handled by the caller
        except Exception as e:
            if self.console:
                self.console.print(f"[bold red]An error occurred: {e}[/bold red]")
            else:
                print(f"An error occurred: {e}")
            raise

    def rebuild(self, full: bool = False, download: bool = False) -> int:
        if download:
            self._download_aur_json()

        if self.console:
            self.console.print(
                f"[bold green]{'Full database rebuild' if full else 'Updating database'}...[/bold green]"
            )
        count = 0
        with self.connection() as conn:
            if full:
                count = self._full_rebuild(conn)
            else:
                count = self._update_database(conn)
        if self.console:
            self.console.print("[bold green]Database ready.[/bold green]")

        if self.db_path.exists():
            self.db_age = self.db_path.stat().st_mtime

        return count

    def _full_rebuild(self, conn: sqlite3.Connection) -> int:
        LOGGER.info("Performing full database rebuild...")
        return self._rebuild(conn)

    def _get_pyalpm_handle(self):
        if not _HAVE_PYALPM:
            return None
        if self._pyalpm_handle is None:
            handle = pyalpm.Handle("/", "/var/lib/pacman")  # type: ignore[attr-defined]
            repo_regex = re.compile(r"^\[(.+)\]$")
            try:
                with open("/etc/pacman.conf", "r") as f:
                    for line in f:
                        if match := repo_regex.match(line.strip()):
                            repo_name = match.group(1)
                            if repo_name.lower() != "options":
                                handle.register_syncdb(
                                    repo_name,
                                    pyalpm.SIG_DATABASE_OPTIONAL,  # type: ignore[attr-defined]
                                )
            except FileNotFoundError:
                LOGGER.error("/etc/pacman.conf not found. Cannot register sync repos.")
            except pyalpm.error as e:  # type: ignore[attr-defined]
                LOGGER.error(f"Error registering sync repos: {e}")
            self._pyalpm_handle = handle
        return self._pyalpm_handle

    def _update_repo_incrementally(self, conn: sqlite3.Connection) -> None:
        if not _HAVE_PYALPM:
            return

        LOGGER.info("Performing incremental repo update...")

        # --- Step 1: Get CURRENT state from pyalpm (Sync Repos + Local DB) ---
        current_system_packages, pkg_lookup = self._get_current_system_packages()
        if not current_system_packages:
            return

        # --- Step 2: Get STORED state from our database ---
        stored_system_packages = {
            tuple(row)
            for row in conn.execute(
                "SELECT name, version, source FROM packages WHERE source != 'aur'"
            )
        }

        # --- Step 3: Find the deltas ---
        packages_to_add_or_update = current_system_packages - stored_system_packages
        packages_to_delete = stored_system_packages - current_system_packages

        # --- Step 4: Act on deltas ---
        cur = conn.cursor()

        if packages_to_delete:
            LOGGER.info(
                f"Deleting {len(packages_to_delete)} obsolete repo/local packages."
            )
            cur.executemany(
                "DELETE FROM packages WHERE name = ? AND version = ? AND source = ?",
                list(packages_to_delete),
            )

        if packages_to_add_or_update:
            LOGGER.info(
                f"Adding/updating {len(packages_to_add_or_update)} repo/local packages."
            )
            repo_pkg_data = []
            for name, version, source in packages_to_add_or_update:
                pkg_obj, source_name = pkg_lookup.get((name, version, source))
                if pkg_obj:
                    repo_pkg_data.append((pkg_obj, source_name))

            if repo_pkg_data:
                self._insert_repo_pkg(cur, repo_pkg_data)

        LOGGER.info("Incremental repo update finished.")

    def _update_database(self, conn: sqlite3.Connection) -> int:
        LOGGER.info("Performing incremental database update...")
        aur_updated_count = self._ingest_aur_full(conn)
        self._update_repo_incrementally(conn)
        conn.commit()
        LOGGER.info("Incremental update finished.")
        return aur_updated_count

    def _rebuild(self, conn: sqlite3.Connection) -> int:
        LOGGER.info("Creating/refreshing package cache…")
        conn.executescript(
            "PRAGMA writable_schema = 1; "
            "DELETE FROM sqlite_master WHERE type IN ('table', 'index', 'trigger'); "
            "PRAGMA writable_schema = 0; "
            "VACUUM; "
            "PRAGMA integrity_check;"
        )
        conn.executescript(DDL)
        aur_count = self._ingest_aur_full(conn)
        self._ingest_repo(conn)
        conn.commit()
        return aur_count

    def _ingest_aur_full(self, conn: sqlite3.Connection) -> int:
        if not self.aur_json.is_file():
            LOGGER.warning(f"AUR JSON file not found: {self.aur_json}, downloading...")
            self._download_aur_json()

        with gzip.open(self.aur_json, "rt", encoding="utf-8") as fp:
            records = json.load(fp)

        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO db_metadata (key, value) VALUES ('build_status', 'pending')"
        )

        db_packages = {
            row["name"]: dict(row)
            for row in conn.execute("SELECT * FROM packages WHERE source = 'aur'")
        }
        aur_package_names = {rec.get("Name") for rec in records if rec.get("Name")}

        packages_to_delete = [
            (name,) for name in db_packages if name not in aur_package_names
        ]
        if packages_to_delete:
            cur.executemany(
                "DELETE FROM packages WHERE name = ? AND source = 'aur'",
                packages_to_delete,
            )
            LOGGER.info(f"Deleted {len(packages_to_delete)} obsolete AUR packages.")

        packages_to_update = []
        for rec in records:
            pkg_name = rec.get("Name")
            if not pkg_name:
                continue

            db_pkg = db_packages.get(pkg_name)
            needs_update = False

            if not db_pkg:
                needs_update = True
            else:
                if rec.get("LastModified", 0) != (db_pkg.get("last_modified") or 0):
                    needs_update = True
                elif rec.get("Maintainer") != db_pkg.get("maintainer"):
                    needs_update = True
                elif rec.get("OutOfDate") != db_pkg.get("out_of_date"):
                    needs_update = True
                elif rec.get("NumVotes", 0) != (db_pkg.get("num_votes") or 0):
                    needs_update = True
                else:
                    db_metadata = json.loads(db_pkg.get("metadata", "{}"))
                    db_comaintainers = db_metadata.get("CoMaintainers", [])
                    aur_comaintainers = rec.get("CoMaintainers", [])
                    if sorted(db_comaintainers) != sorted(aur_comaintainers):
                        needs_update = True

            if needs_update:
                packages_to_update.append(rec)

        if packages_to_update:
            package_data = [
                self._prepare_package_row_data(rec, "aur") for rec in packages_to_update
            ]
            link_data = [
                link
                for rec in packages_to_update
                for link in self._prepare_link_data(rec, "aur")
            ]
            group_data = [
                group
                for rec in packages_to_update
                for group in self._prepare_group_data(rec, "aur")
            ]

            self._insert_package_row(cur, package_data)
            self._insert_links(cur, link_data)
            self._insert_groups(cur, group_data)

        cur.execute(
            "UPDATE db_metadata SET value = 'complete' WHERE key = 'build_status'"
        )
        conn.commit()

        updated_count = len(packages_to_update)
        LOGGER.info(
            f"AUR packages ingested (full scan): {updated_count} new/updated packages processed."
        )
        return updated_count

    def _get_current_system_packages(self) -> Tuple[set, dict]:
        if not _HAVE_PYALPM:
            return set(), {}
        handle = self._get_pyalpm_handle()
        if not handle:
            return set(), {}

        current_system_packages = set()
        pkg_lookup = {}

        # Get all packages from sync repos first, as they take precedence
        repo_pkgs = {}
        for db in handle.get_syncdbs():
            for pkg in db.pkgcache:
                repo_pkgs[pkg.name] = (pkg, db.name)
                key = (pkg.name, str(pkg.version), db.name)
                current_system_packages.add(key)
                pkg_lookup[key] = (pkg, db.name)

        # Add any packages from the local db that were not in a sync repo
        localdb = handle.get_localdb()
        for pkg in localdb.pkgcache:
            if pkg.name not in repo_pkgs:
                key = (pkg.name, str(pkg.version), "local")
                current_system_packages.add(key)
                pkg_lookup[key] = (pkg, "local")

        return current_system_packages, pkg_lookup

    def _ingest_repo(self, conn: sqlite3.Connection) -> None:
        if not _HAVE_PYALPM:
            LOGGER.info("pyalpm not available; skipping repo ingest.")
            return

        cur = conn.cursor()
        current_system_packages, pkg_lookup = self._get_current_system_packages()

        repo_pkg_data = []
        for name, version, source in current_system_packages:
            pkg_obj, source_name = pkg_lookup.get((name, version, source))
            if pkg_obj:
                repo_pkg_data.append((pkg_obj, source_name))

        if repo_pkg_data:
            self._insert_repo_pkg(cur, repo_pkg_data)

        LOGGER.info("Repo packages ingested.")

    def _prepare_package_row_data(self, rec: Dict[str, Any], source: str) -> Tuple:
        metadata = {
            "License": rec.get("License", []),
            "Keywords": rec.get("Keywords", []),
            "CoMaintainers": rec.get("CoMaintainers", []),
        }
        return (
            rec.get("ID"),
            rec.get("Name"),
            rec.get("Version"),
            rec.get("Description"),
            rec.get("URL"),
            rec.get("URLPath"),
            rec.get("Maintainer"),
            rec.get("Submitter"),
            rec.get("FirstSubmitted"),
            rec.get("LastModified"),
            rec.get("Popularity"),
            rec.get("OutOfDate"),
            rec.get("PackageBase"),
            rec.get("PackageBaseID"),
            rec.get("NumVotes"),
            source,
            json.dumps(metadata),
        )

    def _insert_package_row(self, cur: sqlite3.Cursor, data: List[Tuple]) -> None:
        cur.executemany(
            """INSERT INTO packages (
            pkg_id, name, version, description, url, url_path,
            maintainer, submitter, first_submitted, last_modified,
            popularity, out_of_date, package_base, package_base_id,
            num_votes, source, metadata)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(name, source) DO UPDATE SET
                version=excluded.version,
                description=excluded.description,
                url=excluded.url,
                url_path=excluded.url_path,
                maintainer=excluded.maintainer,
                submitter=excluded.submitter,
                first_submitted=excluded.first_submitted,
                last_modified=excluded.last_modified,
                popularity=excluded.popularity,
                out_of_date=excluded.out_of_date,
                package_base=excluded.package_base,
                package_base_id=excluded.package_base_id,
                num_votes=excluded.num_votes,
                metadata=excluded.metadata""",
            data,
        )

    def _prepare_repo_pkg_data(self, pkg: Any, source: str) -> Tuple:
        files = getattr(pkg, "files", None)
        backup = getattr(pkg, "backup", None)
        metadata = {
            "License": pkg.licenses,
            "files": [f[0] for f in files] if files is not None else [],
            "backup": [{"filename": b[0], "md5sum": b[1]} for b in backup]
            if backup is not None
            else [],
        }
        return (
            pkg.name,
            str(pkg.version),
            pkg.desc,
            pkg.url,
            getattr(pkg, "filename", None),
            getattr(pkg, "packager", None),
            getattr(pkg, "arch", None),
            getattr(pkg, "builddate", None),
            getattr(pkg, "installdate", None),
            getattr(pkg, "isize", None),
            getattr(pkg, "size", None),
            getattr(pkg, "md5sum", None),
            getattr(pkg, "sha256sum", None),
            getattr(pkg, "base64_sig", None),
            getattr(pkg, "has_scriptlet", False),
            source,
            json.dumps(metadata),
            getattr(pkg, "builddate", None),
            getattr(pkg, "packager", None),
        )

    def _insert_repo_pkg(
        self, cur: sqlite3.Cursor, data: List[Tuple[Any, str]]
    ) -> None:
        package_data = [
            self._prepare_repo_pkg_data(pkg, source) for pkg, source in data
        ]
        link_data = [
            link
            for pkg, source in data
            for link in self._prepare_link_data(
                {
                    "Name": pkg.name,
                    "Version": str(pkg.version),
                    "Depends": pkg.depends,
                    "OptDepends": pkg.optdepends,
                    "CheckDepends": getattr(pkg, "checkdepends", []),
                    "MakeDepends": getattr(pkg, "makedepends", []),
                    "Provides": pkg.provides,
                    "Replaces": pkg.replaces,
                    "Conflicts": pkg.conflicts,
                },
                source,
            )
        ]
        group_data = [
            group
            for pkg, source in data
            for group in self._prepare_group_data(
                {"Name": pkg.name, "Groups": pkg.groups}, source
            )
        ]

        cur.executemany(
            "DELETE FROM links WHERE name = ? AND source = ?",
            [(pkg.name, source) for pkg, source in data],
        )
        cur.executemany(
            "DELETE FROM package_groups WHERE name = ? AND source = ?",
            [(pkg.name, source) for pkg, source in data],
        )

        cur.executemany(
            """INSERT INTO packages (
            name, version, description, url, filename, packager, arch, build_date,
            install_date, isize, size, md5sum, sha256sum, base64_sig,
            has_scriptlet, source, metadata, last_modified, maintainer)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(name, source) DO UPDATE SET
                version=excluded.version,
                description=excluded.description,
                url=excluded.url,
                filename=excluded.filename,
                packager=excluded.packager,
                arch=excluded.arch,
                build_date=excluded.build_date,
                install_date=excluded.install_date,
                isize=excluded.isize,
                size=excluded.size,
                md5sum=excluded.md5sum,
                sha256sum=excluded.sha256sum,
                base64_sig=excluded.base64_sig,
                has_scriptlet=excluded.has_scriptlet,
                metadata=excluded.metadata,
                last_modified=excluded.last_modified,
                maintainer=excluded.maintainer""",
            package_data,
        )
        self._insert_links(cur, link_data)
        self._insert_groups(cur, group_data)

    def _prepare_link_data(
        self, rec: Dict[str, Any], source: str
    ) -> List[Tuple[str, str, str, str]]:
        name = rec["Name"]
        version = str(rec.get("Version"))
        links = []
        for field in LINK_FIELDS:
            items_val = rec.get(field)
            if items_val is None:
                continue
            items = set(items_val)
            if field == "Provides" and source != "aur" and version:
                cleaned_version = version.split("-")[0].split(":")[-1]
                items.add(f"{name}={cleaned_version}")
            if items:
                links.extend([(name, source, field, item) for item in items])
        return links

    def _insert_links(self, cur: sqlite3.Cursor, data: List[Tuple]) -> None:
        cur.executemany(
            "INSERT INTO links (name, source, link_type, target) VALUES (?,?,?,?)",
            data,
        )

    def _prepare_group_data(
        self, rec: Dict[str, Any], source: str
    ) -> List[Tuple[str, str, str]]:
        name = rec["Name"]
        groups = []
        grp_list = rec.get("Groups")
        if grp_list:
            groups.extend([(name, source, grp) for grp in grp_list])
        return groups

    def _insert_groups(self, cur: sqlite3.Cursor, data: List[Tuple]) -> None:
        cur.executemany(
            "INSERT INTO package_groups VALUES (?,?,?)",
            data,
        )

    def get_enriched_dependencies(
        self, package: Dict[str, Any]
    ) -> Dict[str, List[Dict]]:
        enriched_deps: Dict[str, List[Dict]] = {}
        dep_types = ["Depends", "MakeDepends", "CheckDepends", "OptDepends"]

        all_dep_specs = []
        for dep_type in dep_types:
            all_dep_specs.extend(package.get(dep_type, []))

        if not all_dep_specs:
            return {}

        base_dep_names = {
            re.split(r"[<>=]", spec)[0].strip().split(":", 1)[0]
            for spec in all_dep_specs
        }

        all_candidates: Dict[str, List[Dict]] = {name: [] for name in base_dep_names}
        already_added: set[tuple[str, str, str]] = set()

        with self.connection() as conn:
            for dep_name in base_dep_names:
                # 1. Find replacers
                q_replaces = "SELECT p.* FROM links l JOIN packages p ON p.name = l.name AND p.source = l.source WHERE l.link_type = 'Replaces' AND (l.target = ? OR l.target LIKE ?)"
                for row in conn.execute(
                    q_replaces, (dep_name, f"{dep_name}=%")
                ).fetchall():
                    pkg_data = dict(row)
                    if (
                        dep_name,
                        pkg_data["name"],
                        pkg_data["source"],
                    ) not in already_added:
                        pkg_data["resolution_type"] = "replaces"
                        all_candidates[dep_name].append(pkg_data)
                        already_added.add(
                            (dep_name, pkg_data["name"], pkg_data["source"])
                        )

                # 2. Find providers
                q_provides = "SELECT p.* FROM links l JOIN packages p ON p.name = l.name AND p.source = l.source WHERE l.link_type = 'Provides' AND (l.target = ? OR l.target LIKE ?)"
                for row in conn.execute(
                    q_provides, (dep_name, f"{dep_name}=%")
                ).fetchall():
                    pkg_data = dict(row)
                    if (
                        dep_name,
                        pkg_data["name"],
                        pkg_data["source"],
                    ) not in already_added:
                        pkg_data["resolution_type"] = "provides"
                        all_candidates[dep_name].append(pkg_data)
                        already_added.add(
                            (dep_name, pkg_data["name"], pkg_data["source"])
                        )

                # 3. Find direct matches
                q_direct = "SELECT * FROM packages WHERE name = ?"
                for row in conn.execute(q_direct, (dep_name,)).fetchall():
                    pkg_data = dict(row)
                    if (
                        dep_name,
                        pkg_data["name"],
                        pkg_data["source"],
                    ) not in already_added:
                        pkg_data["resolution_type"] = "direct"
                        all_candidates[dep_name].append(pkg_data)
                        already_added.add(
                            (dep_name, pkg_data["name"], pkg_data["source"])
                        )

        for dep_type in dep_types:
            enriched_deps[dep_type] = []
            for spec in package.get(dep_type, []):
                base_name = re.split(r"[<>=]", spec)[0].strip().split(":", 1)[0]
                description = spec.split(":", 1)[1].strip() if ":" in spec else None

                candidates = all_candidates.get(base_name, [])

                has_repo_provider = any(
                    p["resolution_type"] == "provides" and p["source"] != "aur"
                    for p in candidates
                )

                valid_providers = []
                if has_repo_provider:
                    for p in candidates:
                        if p["resolution_type"] == "replaces" and p["source"] == "aur":
                            continue
                        valid_providers.append(p)
                else:
                    valid_providers = candidates

                def sort_key(p):
                    type_order = {"replaces": 0, "provides": 1, "direct": 2}
                    return (
                        p["source"] == "aur",
                        type_order.get(p["resolution_type"], 99),
                        p["name"],
                    )

                valid_providers.sort(key=sort_key)

                enriched_deps[dep_type].append(
                    {
                        "name": base_name,
                        "original_spec": spec,
                        "description": description,
                        "providers": valid_providers,
                    }
                )

        return enriched_deps

    def get_dependants(
        self, package_name: str, provides: List[str]
    ) -> Dict[str, List[Dict]]:
        all_provides = [package_name] + provides
        placeholders = ",".join("?" for _ in all_provides)

        query = f"""
            SELECT l.target, p.name, p.source, l.link_type
            FROM links l
            JOIN packages p ON p.name = l.name AND p.source = l.source
            WHERE l.link_type IN ('Depends', 'CheckDepends', 'MakeDepends', 'OptDepends')
            AND l.target IN ({placeholders})
        """
        with self.connection() as conn:
            results = conn.execute(query, all_provides).fetchall()

        dependants: Dict[str, List[Dict]] = {p: [] for p in all_provides}
        for target, name, source, link_type in results:
            base_target = re.split(r"[<>=]", target)[0].strip().split(":", 1)[0]
            if base_target in dependants:
                dependants[base_target].append(
                    {"name": name, "source": source, "link_type": link_type}
                )

        # Return only those with dependants
        return {k: v for k, v in dependants.items() if v}

    def get_repo_names(self) -> List[str]:
        """Returns a list of unique repository names."""
        with self.connection() as conn:
            return [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT source FROM packages"
                ).fetchall()
            ]


class DependencyResolver:
    """Resolves package dependency trees and detects cycles."""

    def __init__(self, db: PackageDB, console: Console):
        self.db = db
        self.console = console
        self.installed: Dict[str, str] = db.installed_packages
        self.installed_provides: Dict[str, str] = db.installed_provides

    def resolve_dependency_tree_deep(self, package_names: List[str]) -> Dict[str, Any]:
        visiting: set[str] = set()
        visited: set[str] = set()
        satisfied: set[str] = set()
        order: List[Dict[str, Any]] = []
        cycles: List[List[str]] = []

        packages_to_resolve = []
        for name in package_names:
            info = self.db.package_info(name.split(":", 1)[0])
            if info:
                packages_to_resolve.append(info["name"])
            else:
                self.console.print(
                    f"[red]Error: Could not find any package matching '{name}'.[/red]"
                )
                return {"order": [], "cycles": [], "installed": {}, "satisfied": []}

        for name in packages_to_resolve:
            if name in self.installed:
                satisfied.add(name)
            elif name in self.installed_provides:
                provider = self.installed_provides[name]
                satisfied.add(provider)

            if name not in visited:
                self._dfs_deep(name, visiting, visited, order, cycles, [], satisfied)

        final_order_info = [self.db.package_info(p["name"], p["source"]) for p in order]
        final_order = [
            p for p in final_order_info if p and p["name"] not in self.installed
        ]

        return {
            "order": final_order,
            "cycles": cycles,
            "installed": self.installed.keys(),
            "satisfied": list(satisfied),
        }

    def resolve_dependency_tree_shallow(
        self, package_names: List[str]
    ) -> Dict[str, Any]:
        visiting: set[str] = set()
        visited: set[str] = set()
        satisfied: set[str] = set()
        order: List[Dict[str, Any]] = []
        cycles: List[List[str]] = []

        packages_to_resolve = []
        for name in package_names:
            info = self.db.package_info(name.split(":", 1)[0])
            if info:
                packages_to_resolve.append(info["name"])
            else:
                self.console.print(
                    f"[red]Error: Could not find any package matching '{name}'.[/red]"
                )
                return {"order": [], "cycles": [], "installed": {}, "satisfied": []}

        for name in packages_to_resolve:
            if name in self.installed:
                satisfied.add(name)
            elif name in self.installed_provides:
                provider = self.installed_provides[name]
                satisfied.add(provider)

            if name not in visited:
                self._dfs_shallow(name, visiting, visited, order, cycles, [], satisfied)

        final_order_info = [self.db.package_info(p["name"], p["source"]) for p in order]
        final_order = [
            p for p in final_order_info if p and p["name"] not in self.installed
        ]

        return {
            "order": final_order,
            "cycles": cycles,
            "installed": self.installed.keys(),
            "satisfied": list(satisfied),
        }

    def _dfs_deep(
        self,
        pkg_name: str,
        visiting: set[str],
        visited: set[str],
        order: List[Dict[str, Any]],
        cycles: List[List[str]],
        path: List[str],
        satisfied: set[str],
    ):
        visiting.add(pkg_name)
        path.append(pkg_name)

        deps = self.db.get_package_dependencies(pkg_name)
        for dep_name_full in deps:
            dep_name = dep_name_full.split(":", 1)[0]

            if dep_name in self.installed:
                if dep_name not in satisfied:
                    satisfied.add(dep_name)
                    if dep_name not in visited:
                        self._dfs_deep(
                            dep_name,
                            visiting,
                            visited,
                            order,
                            cycles,
                            path[:],
                            satisfied,
                        )
                continue

            if dep_name in self.installed_provides:
                provider = self.installed_provides[dep_name]
                if provider not in satisfied:
                    satisfied.add(provider)
                    if provider not in visited:
                        self._dfs_deep(
                            provider,
                            visiting,
                            visited,
                            order,
                            cycles,
                            path[:],
                            satisfied,
                        )
                continue

            if dep_name in visiting:
                try:
                    cycle_start_index = path.index(dep_name)
                    cycles.append(path[cycle_start_index:])
                except ValueError:
                    cycles.append(path + [dep_name])
                continue

            if dep_name not in visited:
                self._dfs_deep(
                    dep_name, visiting, visited, order, cycles, path[:], satisfied
                )

        path.pop()
        visiting.remove(pkg_name)
        if pkg_name not in visited:
            visited.add(pkg_name)
            pkg_info = self.db.package_info(pkg_name)
            if pkg_info:
                order.append(
                    {
                        "name": pkg_name,
                        "source": pkg_info["source"],
                        "version": pkg_info["version"],
                    }
                )

    def _dfs_shallow(
        self,
        pkg_name: str,
        visiting: set[str],
        visited: set[str],
        order: List[Dict[str, Any]],
        cycles: List[List[str]],
        path: List[str],
        satisfied: set[str],
    ):
        visiting.add(pkg_name)
        path.append(pkg_name)

        deps = self.db.get_package_dependencies(pkg_name)
        for dep_name_full in deps:
            dep_name = dep_name_full.split(":", 1)[0]

            if dep_name in self.installed:
                satisfied.add(dep_name)
                continue

            if dep_name in self.installed_provides:
                provider = self.installed_provides[dep_name]
                satisfied.add(provider)
                continue

            if dep_name in visiting:
                try:
                    cycle_start_index = path.index(dep_name)
                    cycles.append(path[cycle_start_index:])
                except ValueError:
                    cycles.append(path + [dep_name])
                continue

            if dep_name not in visited:
                self._dfs_shallow(
                    dep_name, visiting, visited, order, cycles, path[:], satisfied
                )

        path.pop()
        visiting.remove(pkg_name)
        if pkg_name not in visited:
            visited.add(pkg_name)
            pkg_info = self.db.package_info(pkg_name)
            if pkg_info:
                order.append(
                    {
                        "name": pkg_name,
                        "source": pkg_info["source"],
                        "version": pkg_info["version"],
                    }
                )

    def get_repo_names(self) -> List[str]:
        """Returns a list of unique repository names."""
        with self.db.connection() as conn:
            return [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT source FROM packages WHERE source != 'aur'"
                ).fetchall()
            ]
