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
SCHEMA_VERSION = 1
APP_NAME = "aurdex"
AUR_JSON = Path(user_cache_dir(APP_NAME)) / "packages-meta-ext-v1.json.gz"
AUR_DB_URL = "https://aur.manjaro.org/packages-meta-ext-v1.json.gz"
DB_PATH = Path(user_cache_dir(APP_NAME)) / "packages.db"
LAST_MODIFIED_FILE = (
    Path(user_cache_dir(APP_NAME)) / ".packages-meta-ext-v1.json.lastmodified"
)


# --------------------------------------------------------------------------- #
# SQLite schema (normalised, single DB)                                       #
# --------------------------------------------------------------------------- #

DDL = f"""
BEGIN;

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

    def _get_installed_packages(self) -> Dict[str, str]:
        if not _HAVE_PYALPM:
            return {}
        try:
            handle = pyalpm.Handle("/", "/var/lib/pacman")
            localdb = handle.get_localdb()
            return {pkg.name: pkg.version for pkg in localdb.pkgcache}
        except pyalpm.error as e:
            LOGGER.error(f"Could not read local package database: {e}")
            return {}

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
        pkg_info = self.package_info(pkg_name, source="aur")
        if not pkg_info:
            pkg_info = self.package_info(pkg_name)
        if not pkg_info:
            return []
        deps = pkg_info.get("Depends", [])
        return [re.split(r"[<>=]", d)[0].strip() for d in deps]

    def search_by_provides(self, token: str) -> List[Tuple[str, str]]:
        """Return (name, source) where token ∈ Provides."""
        # Normalize the token by removing version constraints
        base_token = re.split(r"[<>=]", token)[0].strip()
        q = "SELECT name, source FROM links WHERE link_type = 'Provides' AND (target = ? OR target LIKE ?)"
        with self.connection() as c:
            return c.execute(q, (base_token, f"{base_token}=%")).fetchall()

    def search_by_depends(self, token: str) -> List[Tuple[str, str, str]]:
        """Return (name, source, type) where token ∈ any dependency type (Depends, MakeDepends, etc.)."""
        base_token = re.split(r"[<>=]", token)[0].strip()
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
            return c.execute(q, (base_token, f"{base_token}=%")).fetchall()

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
        limit: int = 1000,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        filters = filters or {}
        query = "SELECT DISTINCT p.source, p.name, p.version, COALESCE(p.popularity, 0.0) AS popularity, COALESCE(p.num_votes, 0) AS num_votes, p.pkg_id FROM packages p"
        params: List[Any] = []
        where_clauses, joins = [], []
        if search_term:
            operator = "REGEXP" if self._is_regex(search_term) else "LIKE"
            search_val = search_term if operator == "REGEXP" else f"%{search_term}%"
            where_clauses.append(f"(p.name {operator} ?)")
            params.append(search_val)
        for key, value in filters.items():
            if key in ["abandoned", "out_of_date"]:
                if value:
                    where_clauses.append(
                        f"p.{key} IS NOT NULL"
                        if key == "out_of_date"
                        else "(p.maintainer IS NULL OR p.maintainer = '')"
                    )
            elif key == "provides" and value:
                joins.append(
                    "JOIN links l_provides ON p.name = l_provides.name AND p.source = l_provides.source"
                )
                where_clauses.append(
                    "l_provides.link_type = 'Provides' AND l_provides.target LIKE ?"
                )
                params.append(f"{value}%")
            elif isinstance(value, str) and value:
                operator = "REGEXP" if self._is_regex(value) else "="
                where_clauses.append(f"p.{key} {operator} ?")
                params.append(value)
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
                        ) as status:
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
        if LAST_MODIFIED_FILE.exists():
            try:
                LAST_MODIFIED_FILE.unlink()
                LOGGER.info("Removed last modified file for full rebuild.")
            except OSError as e:
                LOGGER.error(f"Error removing last modified file: {e}")
        return self._rebuild(conn)

    def _update_database(self, conn: sqlite3.Connection) -> int:
        LOGGER.info("Performing incremental database update...")
        aur_updated_count = self._ingest_aur(conn)
        if aur_updated_count > 0:
            self._ingest_repo(conn)
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
        aur_count = self._ingest_aur(conn)
        self._ingest_repo(conn)
        conn.commit()
        return aur_count

    def _ingest_aur(self, conn: sqlite3.Connection) -> int:
        if not self.aur_json.is_file():
            LOGGER.warning(f"AUR JSON file not found: {self.aur_json}, downloading...")
            self._download_aur_json()

        last_modified_ts = 0
        if LAST_MODIFIED_FILE.exists():
            try:
                last_modified_ts = int(LAST_MODIFIED_FILE.read_text().strip())
            except (ValueError, OSError) as e:
                LOGGER.error(f"Error reading last modified file: {e}")

        self.db_age = os.path.getmtime(self.aur_json)
        with gzip.open(self.aur_json, "rt", encoding="utf-8") as fp:
            records = json.load(fp)

        new_max_last_modified = last_modified_ts
        updated_count = 0
        cur = conn.cursor()

        for rec in records:
            if rec.get("LastModified", 0) > last_modified_ts:
                self._insert_package_row(cur, rec, "aur")
                self._insert_links(cur, rec, "aur")
                self._insert_groups(cur, rec, "aur")
                new_max_last_modified = max(
                    new_max_last_modified, rec.get("LastModified", 0)
                )
                updated_count += 1

        if updated_count > 0:
            try:
                LAST_MODIFIED_FILE.write_text(str(new_max_last_modified))
            except OSError as e:
                LOGGER.error(f"Error writing to last modified file: {e}")

        LOGGER.info(
            f"AUR packages ingested: {updated_count} new/updated packages processed."
        )
        return updated_count

    def _ingest_repo(self, conn: sqlite3.Connection) -> None:
        if not _HAVE_PYALPM:
            LOGGER.info("pyalpm not available; skipping repo ingest.")
            return
        import pyalpm
        import re

        handle = pyalpm.Handle("/", "/var/lib/pacman")
        repo_regex = re.compile(r"^\[(.+)\]$")
        try:
            with open("/etc/pacman.conf", "r") as f:
                for line in f:
                    if match := repo_regex.match(line.strip()):
                        repo_name = match.group(1)
                        if repo_name.lower() != "options":
                            handle.register_syncdb(
                                repo_name, pyalpm.SIG_DATABASE_OPTIONAL
                            )
        except FileNotFoundError:
            LOGGER.error("/etc/pacman.conf not found. Cannot register sync repos.")
            return
        except pyalpm.error as e:
            LOGGER.error(f"Error registering sync repos: {e}")
            return
        cur = conn.cursor()
        local_pkgs = {p.name: p for p in handle.get_localdb().pkgcache}
        for db in handle.get_syncdbs():
            for pkg in db.pkgcache:
                pkg_to_insert = local_pkgs.pop(pkg.name, pkg)
                self._insert_repo_pkg(cur, pkg_to_insert, db.name)
        for name, pkg in local_pkgs.items():
            self._insert_repo_pkg(cur, pkg, "local")
        LOGGER.info("Repo packages ingested.")

    def _insert_package_row(
        self, cur: sqlite3.Cursor, rec: Dict[str, Any], source: str
    ) -> None:
        # For AUR packages, we need to clear old links and groups before upserting
        if source == "aur":
            cur.execute(
                "DELETE FROM links WHERE name = ? AND source = ?",
                (rec.get("Name"), "aur"),
            )
            cur.execute(
                "DELETE FROM package_groups WHERE name = ? AND source = ?",
                (rec.get("Name"), "aur"),
            )
        metadata = {
            "License": rec.get("License", []),
            "Keywords": rec.get("Keywords", []),
            "CoMaintainers": rec.get("CoMaintainers", []),
        }
        cur.execute(
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
            (
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
            ),
        )

    def _insert_repo_pkg(self, cur: sqlite3.Cursor, pkg: Any, source: str) -> None:
        cur.execute(
            "DELETE FROM links WHERE name = ? AND source = ?", (pkg.name, source)
        )
        cur.execute(
            "DELETE FROM package_groups WHERE name = ? AND source = ?",
            (pkg.name, source),
        )
        metadata = {
            "License": pkg.licenses,
            "files": [f[0] for f in getattr(pkg, "files", [])],
            "backup": [
                {"filename": b[0], "md5sum": b[1]} for b in getattr(pkg, "backup", [])
            ],
        }
        cur.execute(
            """INSERT INTO packages (
            name, version, description, url, filename, packager, arch, build_date,
            install_date, isize, size, md5sum, sha256sum, base64_sig,
            has_scriptlet, source, metadata)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                metadata=excluded.metadata""",
            (
                pkg.name,
                pkg.version,
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
            ),
        )
        rec_like = {
            "Name": pkg.name,
            "Version": pkg.version,
            "Depends": pkg.depends,
            "OptDepends": pkg.optdepends,
            "CheckDepends": getattr(pkg, "checkdepends", []),
            "MakeDepends": getattr(pkg, "makedepends", []),
            "Provides": pkg.provides,
            "Replaces": pkg.replaces,
            "Conflicts": pkg.conflicts,
            "Groups": pkg.groups,
        }
        self._insert_links(cur, rec_like, source)
        self._insert_groups(cur, rec_like, source)

    def _insert_links(
        self, cur: sqlite3.Cursor, rec: Dict[str, Any], source: str
    ) -> None:
        name = rec["Name"]
        version = rec.get("Version")
        for field in LINK_FIELDS:
            items = set(rec.get(field, []))
            if field == "Provides" and source != "aur" and version:
                # Add implicit self-provide with cleaned version for repo packages
                cleaned_version = version.split("-")[0].split(":")[-1]
                items.add(f"{name}={cleaned_version}")

            if items:
                cur.executemany(
                    "INSERT INTO links (name, source, link_type, target) VALUES (?,?,?,?)",
                    [(name, source, field, item) for item in items],
                )

    def _insert_groups(
        self, cur: sqlite3.Cursor, rec: Dict[str, Any], source: str
    ) -> None:
        name = rec["Name"]
        if grp_list := rec.get("Groups", []):
            cur.executemany(
                "INSERT INTO package_groups VALUES (?,?,?)",
                [(name, source, grp) for grp in grp_list],
            )


class DependencyResolver:
    """Resolves package dependency trees and detects cycles."""

    def __init__(self, db: PackageDB, console: Console):
        self.db = db
        self.console = console
        self.installed: Dict[str, str] = db.installed_packages

    def resolve_dependency_tree(self, package_names: List[str]) -> Dict[str, Any]:
        visiting: set[str] = set()
        visited: set[str] = set()
        satisfied: set[str] = set()
        order: List[Dict[str, Any]] = []
        cycles: List[List[str]] = []

        packages_to_resolve = []
        for name in package_names:
            info = self.db.package_info(name)
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
            if name not in visited:
                self._dfs(name, visiting, visited, order, cycles, [], satisfied)

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

    def _dfs(
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
        for dep_name in deps:
            if dep_name in self.installed:
                satisfied.add(dep_name)
                continue

            if dep_name in visiting:
                try:
                    cycle_start_index = path.index(dep_name)
                    cycles.append(path[cycle_start_index:])
                except ValueError:
                    cycles.append(path + [dep_name])
                continue

            if dep_name not in visited:
                self._dfs(
                    dep_name, visiting, visited, order, cycles, path[:], satisfied
                )

        path.pop()
        visiting.remove(pkg_name)
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
