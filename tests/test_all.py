"""End-to-end tests: path helpers, jar parsing, the Crafty backend against a
faithful fake server, the SSH backend against a fake SFTP client, and the full
scan → identify → update → install → roll back flow."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from craftymm.backends.base import ConflictError, join, norm, parent_of  # noqa: E402
from craftymm.backends.crafty import CraftyBackend, _parse_size  # noqa: E402
from craftymm.modmeta import murmur2_of, parse_jar, sha1_of  # noqa: E402
from fake_crafty import SERVER_ID, TOKEN, FakeCrafty  # noqa: E402
from fake_platforms import CF_KEY, FakePlatforms  # noqa: E402


# --------------------------------------------------------------------------- #
#  Path helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        ("/", ""),
        ("mods", "mods"),
        ("/mods/", "mods"),
        ("mods\\sodium.jar", "mods/sodium.jar"),
        ("a//b/./c", "a/b/c"),
        ("a/b/../c", "a/c"),
        ("../../etc/passwd", "etc/passwd"),
        ("/../..", ""),
    ],
)
def test_norm(raw, expected):
    assert norm(raw) == expected


def test_join_and_parent():
    assert join("mods", "sodium.jar") == "mods/sodium.jar"
    assert join("", "config") == "config"
    assert parent_of("config/fabric/x.json") == "config/fabric"
    assert parent_of("top.txt") == ""


def test_parse_size():
    assert _parse_size("4.2 MB") == int(4.2 * 1024**2)
    assert _parse_size("512 B") == 512
    assert _parse_size(1234) == 1234
    assert _parse_size("") == 0
    assert _parse_size("weird") == 0


# --------------------------------------------------------------------------- #
#  Jar metadata
# --------------------------------------------------------------------------- #
def make_jar(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_parse_fabric_jar():
    data = make_jar(
        {
            "fabric.mod.json": json.dumps(
                {
                    "id": "sodium",
                    "name": "Sodium",
                    "version": "0.5.8",
                    "description": "Rendering engine",
                    "authors": ["jellysquid3"],
                    "depends": {"minecraft": "1.20.1", "fabricloader": ">=0.15",
                                "fabric-api": "*"},
                }
            )
        }
    )
    m = parse_jar(data, "sodium-fabric-0.5.8.jar")
    assert (m.mod_id, m.name, m.version, m.loader) == (
        "sodium", "Sodium", "0.5.8", "fabric"
    )
    assert m.mc_versions == ["1.20.1"]
    assert "fabric-api" in m.depends and "fabricloader" not in m.depends


def test_parse_forge_mods_toml():
    toml = """
modLoader="javafml"
loaderVersion="[47,)"
license="MIT"
[[mods]]
modId="jei"
version="15.2.0.27"
displayName="Just Enough Items"
description='''Item viewer'''
authors="mezz"
[[dependencies.jei]]
    modId="minecraft"
    mandatory=true
    versionRange="[1.20.1,1.21)"
[[dependencies.jei]]
    modId="forge"
    mandatory=true
    versionRange="[47,)"
"""
    data = make_jar({"META-INF/mods.toml": toml})
    m = parse_jar(data, "jei-1.20.1-15.2.0.27.jar")
    assert m.mod_id == "jei"
    assert m.name == "Just Enough Items"
    assert m.version == "15.2.0.27"
    assert m.loader == "forge"
    assert "1.20.1" in m.mc_versions


def test_parse_neoforge_toml_takes_priority():
    toml = '[[mods]]\nmodId="testmod"\nversion="1.0.0"\ndisplayName="Test"\n'
    data = make_jar({"META-INF/neoforge.mods.toml": toml})
    m = parse_jar(data, "testmod-1.0.0.jar")
    assert m.loader == "neoforge" and m.mod_id == "testmod"


def test_parse_jarversion_placeholder_uses_manifest():
    toml = '[[mods]]\nmodId="x"\nversion="${file.jarVersion}"\ndisplayName="X"\n'
    data = make_jar(
        {
            "META-INF/mods.toml": toml,
            "META-INF/MANIFEST.MF": "Manifest-Version: 1.0\nImplementation-Version: 3.1.4\n",
        }
    )
    assert parse_jar(data, "x-3.1.4.jar").version == "3.1.4"


def test_parse_legacy_mcmod_info():
    data = make_jar(
        {
            "mcmod.info": json.dumps(
                [{"modid": "oldmod", "name": "Old Mod", "version": "1.2.3",
                  "mcversion": "1.12.2", "authorList": ["someone"]}]
            )
        }
    )
    m = parse_jar(data, "oldmod-1.12.2-1.2.3.jar")
    assert (m.mod_id, m.version, m.loader) == ("oldmod", "1.2.3", "forge")


def test_parse_multiloader_jar():
    data = make_jar(
        {
            "fabric.mod.json": json.dumps({"id": "both", "name": "Both", "version": "1"}),
            "META-INF/mods.toml": '[[mods]]\nmodId="both"\nversion="1"\n',
        }
    )
    assert parse_jar(data, "both-1.jar").loader == "multi"


def test_parse_garbage_jar_does_not_raise():
    m = parse_jar(b"this is not a zip file at all", "broken-1.0.0.jar")
    assert m.error
    assert m.name == "broken"  # filename fallback still works
    assert m.version == "1.0.0"


def test_filename_fallback_for_bare_jar():
    m = parse_jar(make_jar({"README.txt": "hi"}), "SomeMod-fabric-1.19.2-2.0.1.jar")
    assert m.version == "2.0.1"
    assert m.loader == "fabric"
    assert "1.19.2" in m.mc_versions


def test_murmur2_strips_whitespace_like_curseforge():
    a = murmur2_of(b"abc\t\n\r def")
    b = murmur2_of(b"abcdef")
    assert a == b


def test_murmur2_known_reference():
    # Cross-checked against the C murmurhash2 reference implementation.
    assert murmur2_of(b"") == 1540447798
    assert murmur2_of(b"minecraft") == 1532164859


# --------------------------------------------------------------------------- #
#  Crafty backend against the fake server
# --------------------------------------------------------------------------- #
@pytest.fixture
def crafty(tmp_path):
    root = tmp_path / "server"
    (root / "mods").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "server.properties").write_text(
        "level-name=world\nmax-players=20\n", encoding="utf-8"
    )
    with FakeCrafty(root) as fake:
        backend = CraftyBackend(fake.url, token=TOKEN, verify_ssl=False)
        backend.connect()
        backend.select_server(SERVER_ID)
        yield backend, root, fake


def test_crafty_login_with_password(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    with FakeCrafty(root) as fake:
        b = CraftyBackend(fake.url, username="admin", password="hunter2")
        b.connect()
        assert b.token == TOKEN
        assert b.list_servers()[0].name == "Test Server"


def test_crafty_login_rejects_bad_password(tmp_path):
    from craftymm.backends.base import AuthError

    root = tmp_path / "s"
    root.mkdir()
    with FakeCrafty(root) as fake:
        b = CraftyBackend(fake.url, username="admin", password="wrong")
        with pytest.raises(AuthError):
            b.connect()


def test_crafty_bad_token_is_auth_error(tmp_path):
    from craftymm.backends.base import AuthError

    root = tmp_path / "s"
    root.mkdir()
    with FakeCrafty(root) as fake:
        b = CraftyBackend(fake.url, token="nope")
        with pytest.raises(AuthError):
            b.connect()


def test_crafty_url_gets_https_prefix():
    assert CraftyBackend("example.test:8443", token="x").base_url == "https://example.test:8443"
    assert CraftyBackend("http://a/", token="x").base_url == "http://a"


def test_crafty_list_dir(crafty):
    backend, root, _ = crafty
    entries = backend.list_dir("")
    names = [e.name for e in entries]
    assert "mods" in names and "server.properties" in names
    # directories first
    assert entries[0].is_dir
    props = next(e for e in entries if e.name == "server.properties")
    assert props.path == "server.properties"
    assert props.size > 0


def test_crafty_read_write_roundtrip(crafty):
    backend, root, _ = crafty
    text, mtime = backend.read_text("server.properties")
    assert "max-players=20" in text
    backend.write_text("server.properties", "max-players=40\n", expect_mtime=mtime)
    assert "max-players=40" in (root / "server.properties").read_text()


def test_crafty_write_conflict_then_overwrite(crafty):
    backend, root, _ = crafty
    _, mtime = backend.read_text("server.properties")
    # Someone else edits the file after we read it.
    os.utime(root / "server.properties", (mtime + 100, mtime + 100))
    with pytest.raises(ConflictError):
        backend.write_text("server.properties", "mine\n", expect_mtime=mtime)
    backend.write_text("server.properties", "mine\n", expect_mtime=mtime, overwrite=True)
    assert (root / "server.properties").read_text() == "mine\n"


def test_crafty_write_without_mtime_forces_overwrite(crafty):
    """Without a modified_epoch Crafty would 409 every time; the backend must
    fall back to overwrite so plain saves still work."""
    backend, root, _ = crafty
    backend.write_text("server.properties", "forced\n")
    assert (root / "server.properties").read_text() == "forced\n"


def test_crafty_create_rename_delete(crafty):
    backend, root, _ = crafty
    backend.make_dir("", "newdir")
    assert (root / "newdir").is_dir()
    backend.make_file("newdir", "a.txt")
    assert (root / "newdir" / "a.txt").exists()
    backend.rename("newdir/a.txt", "b.txt")
    assert (root / "newdir" / "b.txt").exists()
    assert backend.exists("newdir/b.txt")
    backend.delete("newdir")
    assert not (root / "newdir").exists()


def test_crafty_upload_small_and_download(crafty):
    backend, root, _ = crafty
    payload = b"\x00\x01small jar bytes\xff"
    backend.upload_bytes("mods", "tiny.jar", payload)
    assert (root / "mods" / "tiny.jar").read_bytes() == payload
    assert backend.read_bytes("mods/tiny.jar") == payload


def test_crafty_upload_chunked(crafty, monkeypatch):
    import craftymm.backends.crafty as cm

    monkeypatch.setattr(cm, "CHUNK_THRESHOLD", 1024)
    monkeypatch.setattr(cm, "CHUNK_SIZE", 512)
    backend, root, _ = crafty
    payload = os.urandom(5000)
    seen: list[tuple[int, int]] = []
    backend.upload_bytes("mods", "big.jar", payload, progress=lambda c, t: seen.append((c, t)))
    assert (root / "mods" / "big.jar").read_bytes() == payload
    assert seen[-1] == (5000, 5000)
    assert len(seen) == 10  # ceil(5000/512)


def test_crafty_upload_creates_missing_directory(crafty):
    backend, root, _ = crafty
    backend.upload_bytes("deep/nested/dir", "x.jar", b"data")
    assert (root / "deep" / "nested" / "dir" / "x.jar").read_bytes() == b"data"


def test_crafty_move_and_copy(crafty):
    backend, root, _ = crafty
    backend.upload_bytes("mods", "m.jar", b"abc")
    backend.copy("mods/m.jar", "config")
    assert (root / "config" / "m.jar").read_bytes() == b"abc"
    backend.make_dir("", "moved")
    backend.move("mods/m.jar", "moved")
    assert (root / "moved" / "m.jar").exists()
    assert not (root / "mods" / "m.jar").exists()


def test_crafty_power_and_status(crafty):
    backend, _, fake = crafty
    backend.power("start")
    backend.power("stop")
    assert ("POST", f"/api/v2/servers/{SERVER_ID}/action/start_server") in fake.handler.calls
    assert ("POST", f"/api/v2/servers/{SERVER_ID}/action/stop_server") in fake.handler.calls
    st = backend.status()
    assert st["running"] is True and st["online"] == 3


def test_crafty_missing_file_raises(crafty):
    from craftymm.backends.base import BackendError

    backend, _, _ = crafty
    with pytest.raises(BackendError):
        backend.read_text("does-not-exist.txt")


# --------------------------------------------------------------------------- #
#  SSH backend (fake SFTP)
# --------------------------------------------------------------------------- #
class _Attr:
    def __init__(self, name, mode, size=0, mtime=1000):
        self.filename = name
        self.st_mode = mode
        self.st_size = size
        self.st_mtime = mtime


class _FakeSFTP:
    DIR = 0o040755
    REG = 0o100644

    def __init__(self):
        self.files = {"/srv/mods/a.jar": b"aaa", "/srv/server.properties": b"k=v\n"}
        self.dirs = {"/srv", "/srv/mods"}

    def stat(self, path):
        if path in self.dirs:
            return _Attr(path, self.DIR)
        if path in self.files:
            return _Attr(path, self.REG, len(self.files[path]))
        raise IOError(f"no such file: {path}")

    def listdir_attr(self, path):
        if path not in self.dirs:
            raise IOError("not a dir")
        out = []
        prefix = path.rstrip("/") + "/"
        for d in self.dirs:
            if d.startswith(prefix) and "/" not in d[len(prefix):]:
                out.append(_Attr(d[len(prefix):], self.DIR))
        for f in self.files:
            if f.startswith(prefix) and "/" not in f[len(prefix):]:
                out.append(_Attr(f[len(prefix):], self.REG, len(self.files[f])))
        return out

    def open(self, path, mode="rb"):
        sftp = self

        class _F(io.BytesIO):
            def __init__(self):
                super().__init__(sftp.files.get(path, b"") if "r" in mode else b"")
                self._path = path
                self._write = "w" in mode

            def prefetch(self, *a):
                pass

            def set_pipelined(self, *a):
                pass

            def __exit__(self, *exc):
                if self._write:
                    sftp.files[self._path] = self.getvalue()
                return super().__exit__(*exc)

        return _F()

    def remove(self, path):
        if path not in self.files:
            raise IOError("missing")
        del self.files[path]

    def rename(self, src, dst):
        if src in self.files:
            self.files[dst] = self.files.pop(src)
        elif src in self.dirs:
            self.dirs.discard(src)
            self.dirs.add(dst)
        else:
            raise IOError("missing")

    def mkdir(self, path):
        self.dirs.add(path)

    def rmdir(self, path):
        self.dirs.discard(path)

    def close(self):
        pass


def _ssh_backend():
    from craftymm.backends.ssh import SSHBackend

    b = SSHBackend(host="h", username="u", root="/srv")
    b._sftp = _FakeSFTP()
    b.connected = True
    return b


def test_ssh_list_and_read():
    b = _ssh_backend()
    names = [e.name for e in b.list_dir("")]
    assert set(names) == {"mods", "server.properties"}
    text, mtime = b.read_text("server.properties")
    assert text == "k=v\n" and mtime == 1000


def test_ssh_write_conflict():
    b = _ssh_backend()
    with pytest.raises(ConflictError):
        b.write_text("server.properties", "new", expect_mtime=1.0)
    b.write_text("server.properties", "new", expect_mtime=1.0, overwrite=True)
    assert b._sftp.files["/srv/server.properties"] == b"new"


def test_ssh_upload_creates_parents():
    b = _ssh_backend()
    b.upload_bytes("mods/nested/deep", "x.jar", b"zz")
    assert b._sftp.files["/srv/mods/nested/deep/x.jar"] == b"zz"
    assert "/srv/mods/nested" in b._sftp.dirs


def test_ssh_power_needs_a_command():
    from craftymm.backends.base import BackendError

    b = _ssh_backend()
    with pytest.raises(BackendError, match="No 'start' command"):
        b.power("start")


def test_ssh_root_never_escapes():
    b = _ssh_backend()
    assert b._abs("../../etc/passwd") == "/srv/etc/passwd"


# --------------------------------------------------------------------------- #
#  Full manager flow
# --------------------------------------------------------------------------- #
@pytest.fixture
def full_stack(tmp_path, monkeypatch):
    """Fake Crafty + fake Modrinth/CurseForge + a manager wired to both."""
    import craftymm.config as cfg
    import craftymm.providers.curseforge as cfmod
    import craftymm.providers.modrinth as mrmod

    appdir = tmp_path / "appdata"
    appdir.mkdir()
    monkeypatch.setattr(cfg, "app_dir", lambda: appdir)
    monkeypatch.setattr(cfg, "KEYRING_OK", False)
    monkeypatch.setattr(cfg, "_FALLBACK_PATH", appdir / "secrets.json")

    root = tmp_path / "server"
    (root / "mods").mkdir(parents=True)
    (root / "config").mkdir()

    jar_a = make_jar(
        {"fabric.mod.json": json.dumps(
            {"id": "sodium", "name": "Sodium", "version": "0.5.8",
             "depends": {"minecraft": "1.20.1"}})}
    )
    jar_b = make_jar(
        {"fabric.mod.json": json.dumps(
            {"id": "sodium", "name": "Sodium", "version": "0.5.9",
             "depends": {"minecraft": "1.20.1"}})}
    )
    (root / "mods" / "sodium-fabric-0.5.8.jar").write_bytes(jar_a)
    (root / "config" / "sodium.json").write_text('{"quality": "fast"}', encoding="utf-8")

    files = {"sodium-fabric-0.5.8.jar": jar_a, "sodium-fabric-0.5.9.jar": jar_b}

    with FakeCrafty(root) as fake, FakePlatforms(
        files, sha1_of(jar_a), sha1_of(jar_b)
    ) as plat:
        monkeypatch.setattr(mrmod, "API", f"{plat.url}/v2")
        monkeypatch.setattr(cfmod, "API", plat.url)

        from craftymm.config import Profile, Settings
        from craftymm.manager import ModManager
        from craftymm.providers import CurseForgeProvider, ModrinthProvider

        backend = CraftyBackend(fake.url, token=TOKEN)
        backend.connect()
        backend.select_server(SERVER_ID)

        profile = Profile(id="test", name="T", transport="crafty", mods_dir="mods")
        settings = Settings()
        manager = ModManager(
            backend,
            profile,
            settings,
            {
                "modrinth": ModrinthProvider(),
                "curseforge": CurseForgeProvider(CF_KEY),
            },
        )
        yield manager, root, jar_a, jar_b


def test_scan_reads_and_parses(full_stack):
    manager, root, jar_a, _ = full_stack
    mods = manager.scan()
    assert len(mods) == 1
    m = mods[0]
    assert m.filename == "sodium-fabric-0.5.8.jar"
    assert m.meta.mod_id == "sodium"
    assert m.meta.version == "0.5.8"
    assert m.sha1 == sha1_of(jar_a)
    assert manager.loader == "fabric"
    assert manager.mc_version == "1.20.1"


def test_scan_uses_cache_on_second_pass(full_stack):
    manager, root, _, _ = full_stack
    manager.scan()
    downloads = []
    original = manager.backend.read_bytes
    manager.backend.read_bytes = lambda *a, **k: (downloads.append(a), original(*a, **k))[1]
    manager.scan()
    assert downloads == [], "second scan should not re-download unchanged jars"


def test_identify_then_check_updates(full_stack):
    manager, _, jar_a, jar_b = full_stack
    manager.scan()
    assert manager.identify() == 1
    m = manager.mods[0]
    assert m.source == "modrinth"
    assert m.version_number == "0.5.8"

    assert manager.check_updates() == 1
    assert m.update_available
    assert m.latest_version_number == "0.5.9"


def test_pinned_mod_is_not_offered_an_update(full_stack):
    manager, _, _, _ = full_stack
    manager.scan()
    manager.identify()
    manager.set_pinned(manager.mods[0], True)
    manager.check_updates()
    assert manager.mods[0].update_available is False


def test_install_update_replaces_and_backs_up(full_stack):
    manager, root, jar_a, jar_b = full_stack
    manager.scan()
    manager.identify()
    manager.check_updates()
    mod = manager.mods[0]

    version = manager.providers["modrinth"].version_by_id(mod.latest_version_id)
    name = manager.install_version(version, replace=mod)

    assert name == "sodium-fabric-0.5.9.jar"
    assert (root / "mods" / "sodium-fabric-0.5.9.jar").read_bytes() == jar_b
    assert not (root / "mods" / "sodium-fabric-0.5.8.jar").exists()

    backups = manager.backups.all()
    assert len(backups) == 1
    assert manager.backups.read(backups[0]["id"]) == jar_a


def test_install_rejects_checksum_mismatch(full_stack, monkeypatch):
    from craftymm.backends.base import BackendError

    manager, root, _, _ = full_stack
    manager.scan()
    manager.identify()
    manager.check_updates()
    version = manager.providers["modrinth"].version_by_id("ver-2")
    version.sha1 = "0" * 40
    with pytest.raises(BackendError, match="Checksum mismatch"):
        manager.install_version(version)


def test_rollback_restores_the_old_jar(full_stack):
    manager, root, jar_a, _ = full_stack
    manager.scan()
    manager.identify()
    manager.check_updates()
    mod = manager.mods[0]
    version = manager.providers["modrinth"].version_by_id(mod.latest_version_id)
    manager.install_version(version, replace=mod)

    backup_id = manager.backups.all()[0]["id"]
    manager.restore_backup(backup_id)
    assert (root / "mods" / "sodium-fabric-0.5.8.jar").read_bytes() == jar_a


def test_enable_disable_roundtrip(full_stack):
    manager, root, _, _ = full_stack
    manager.scan()
    mod = manager.mods[0]
    new = manager.set_disabled(mod, True)
    assert new.endswith(".jar.disabled")
    assert (root / "mods" / new).exists()

    manager.scan()
    assert manager.mods[0].disabled is True
    manager.set_disabled(manager.mods[0], False)
    assert (root / "mods" / "sodium-fabric-0.5.8.jar").exists()


def test_delete_backs_up_first(full_stack):
    manager, root, jar_a, _ = full_stack
    manager.scan()
    manager.delete_mod(manager.mods[0])
    assert not (root / "mods" / "sodium-fabric-0.5.8.jar").exists()
    assert manager.backups.read(manager.backups.all()[0]["id"]) == jar_a


def test_install_local_jar(full_stack, tmp_path):
    manager, root, _, jar_b = full_stack
    local = tmp_path / "local-mod.jar"
    local.write_bytes(jar_b)
    manager.install_local_jar(str(local))
    assert (root / "mods" / "local-mod.jar").read_bytes() == jar_b


def test_config_edit_creates_backup(full_stack, root_path=None):
    manager, root, _, _ = full_stack
    text, mtime = manager.read_config("config/sodium.json")
    assert text == '{"quality": "fast"}'
    manager.write_config(
        "config/sodium.json", '{"quality": "fancy"}', expect_mtime=mtime, original=text
    )
    assert (root / "config" / "sodium.json").read_text() == '{"quality": "fancy"}'
    history = manager.backups.for_path("config/sodium.json")
    assert len(history) == 1
    assert manager.backups.read(history[0]["id"]) == b'{"quality": "fast"}'


def test_missing_mods_dir_gives_a_useful_error(full_stack):
    from craftymm.backends.base import BackendError

    manager, _, _, _ = full_stack
    manager.profile.mods_dir = "not-there"
    with pytest.raises(BackendError, match="mods folder path"):
        manager.scan()


def test_backup_pruning_keeps_the_newest(full_stack):
    manager, _, _, _ = full_stack
    manager.backups.keep = 3
    for i in range(6):
        manager.backups.add("mods/x.jar", f"v{i}".encode(), "mod")
    entries = manager.backups.for_path("mods/x.jar")
    assert len(entries) == 3
    assert manager.backups.read(entries[0]["id"]) == b"v5"


# --------------------------------------------------------------------------- #
#  Providers
# --------------------------------------------------------------------------- #
def test_modrinth_search_sends_the_right_facets(full_stack):
    manager, _, _, _ = full_stack
    mr = manager.providers["modrinth"]
    hits = mr.search("sodium", loader="fabric", mc_version="1.20.1")
    assert hits[0].title == "Sodium"
    assert hits[0].page_url.endswith("/mod/sodium")


def test_curseforge_requires_a_key(full_stack):
    from craftymm.providers.base import ProviderError

    manager, _, _, _ = full_stack
    cf = manager.providers["curseforge"]
    cf.set_key("")
    assert cf.available is False
    with pytest.raises(ProviderError, match="No CurseForge API key"):
        cf.search("jei")
    cf.set_key(CF_KEY)
    assert cf.search("jei")[0].title == "Just Enough Items"


def test_curseforge_fingerprint_identify(full_stack):
    manager, _, _, _ = full_stack
    manager.scan()
    mods = manager.mods
    found = manager.providers["curseforge"].identify(mods)
    assert found[mods[0].filename].filename == "jei-1.20.1-15.2.0.jar"


def test_curseforge_loader_ids_cover_modern_loaders():
    from craftymm.providers.curseforge import LOADER_IDS

    assert LOADER_IDS["forge"] == 1
    assert LOADER_IDS["fabric"] == 4
    assert LOADER_IDS["quilt"] == 5
    assert LOADER_IDS["neoforge"] == 6


# --------------------------------------------------------------------------- #
#  Config / profile storage
# --------------------------------------------------------------------------- #
def test_settings_roundtrip(tmp_path, monkeypatch):
    import craftymm.config as cfg

    appdir = tmp_path / "cfg"
    appdir.mkdir()
    monkeypatch.setattr(cfg, "CONFIG_PATH", appdir / "config.json")
    monkeypatch.setattr(cfg, "KEYRING_OK", False)
    monkeypatch.setattr(cfg, "_FALLBACK_PATH", appdir / "secrets.json")

    s = cfg.Settings()
    p = cfg.Profile(name="Home", transport="ssh", ssh_host="mc.example")
    s.add_profile(p)
    p.set_secret("ssh_password", "s3cret")
    s.save()

    s2 = cfg.Settings.load()
    assert len(s2.profiles) == 1
    assert s2.current().ssh_host == "mc.example"
    assert s2.current().get_secret("ssh_password") == "s3cret"


def test_removing_a_profile_clears_its_secret(tmp_path, monkeypatch):
    import craftymm.config as cfg

    appdir = tmp_path / "cfg2"
    appdir.mkdir()
    monkeypatch.setattr(cfg, "CONFIG_PATH", appdir / "config.json")
    monkeypatch.setattr(cfg, "KEYRING_OK", False)
    monkeypatch.setattr(cfg, "_FALLBACK_PATH", appdir / "secrets.json")

    s = cfg.Settings()
    p = cfg.Profile(name="X")
    s.add_profile(p)
    p.set_secret("crafty_token", "abc")
    s.remove_profile(p.id)
    assert json.loads((appdir / "secrets.json").read_text()) == {}


def test_unreadable_config_falls_back_to_defaults(tmp_path, monkeypatch):
    import craftymm.config as cfg

    appdir = tmp_path / "cfg3"
    appdir.mkdir()
    bad = appdir / "config.json"
    bad.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_PATH", bad)
    assert cfg.Settings.load().profiles == []


# --------------------------------------------------------------------------- #
#  Data-loss regressions
# --------------------------------------------------------------------------- #
def test_delete_aborts_when_the_backup_cannot_be_taken(full_stack):
    """The confirm dialog promises a backup; if we can't take one we must not
    delete anyway."""
    from craftymm.backends.base import BackendError

    manager, root, _, _ = full_stack
    manager.scan()
    mod = manager.mods[0]

    def boom(*a, **k):
        raise BackendError("connection dropped")

    manager.backend.read_bytes = boom
    with pytest.raises(BackendError, match="Nothing was modified"):
        manager.delete_mod(mod)
    assert (root / "mods" / "sodium-fabric-0.5.8.jar").exists()
    assert manager.backups.all() == []


def test_backup_can_be_skipped_when_the_user_turns_it_off(full_stack):
    manager, root, _, _ = full_stack
    manager.scan()
    manager.settings.backup_mods = False
    manager.backend.read_bytes = lambda *a, **k: (_ for _ in ()).throw(RuntimeError())
    manager.delete_mod(manager.mods[0])
    assert not (root / "mods" / "sodium-fabric-0.5.8.jar").exists()


def test_forced_overwrite_backs_up_the_live_remote_version(full_stack):
    """On a conflict the editor's copy is stale - the thing being destroyed is
    what's on the server right now, so that's what must be saved."""
    manager, root, _, _ = full_stack
    opened_text, mtime = manager.read_config("config/sodium.json")

    # Someone else edits it while our editor is open.
    (root / "config" / "sodium.json").write_text('{"quality": "THEIRS"}', encoding="utf-8")

    with pytest.raises(ConflictError):
        manager.write_config(
            "config/sodium.json", '{"quality": "MINE"}',
            expect_mtime=mtime, original=opened_text,
        )
    manager.write_config(
        "config/sodium.json", '{"quality": "MINE"}',
        expect_mtime=mtime, overwrite=True, original=opened_text,
    )
    assert (root / "config" / "sodium.json").read_text() == '{"quality": "MINE"}'
    saved = [
        manager.backups.read(e["id"])
        for e in manager.backups.for_path("config/sodium.json")
    ]
    assert b'{"quality": "THEIRS"}' in saved, "the overwritten version was not kept"


def test_updating_a_disabled_mod_leaves_it_disabled(full_stack):
    manager, root, _, jar_b = full_stack
    manager.scan()
    manager.set_disabled(manager.mods[0], True)
    manager.scan()
    manager.identify()
    manager.check_updates()
    mod = manager.mods[0]
    assert mod.disabled

    version = manager.providers["modrinth"].version_by_id(mod.latest_version_id)
    name = manager.install_version(version, replace=mod)
    assert name == "sodium-fabric-0.5.9.jar.disabled"
    assert (root / "mods" / name).read_bytes() == jar_b
    files = [p.name for p in (root / "mods").iterdir()]
    assert files == ["sodium-fabric-0.5.9.jar.disabled"]


def test_ssh_overwrite_prefers_atomic_posix_rename():
    """remove()-then-rename() leaves a window where the file does not exist;
    posix_rename replaces in one step."""
    b = _ssh_backend()
    calls = []
    b._sftp.posix_rename = lambda s, d: (calls.append(("posix_rename", s, d)),
                                         b._sftp.rename(s, d))[0]
    real_remove = b._sftp.remove
    b._sftp.remove = lambda p: (calls.append(("remove", p)), real_remove(p))[1]
    b.write_text("server.properties", "new", overwrite=True)
    assert calls[0][0] == "posix_rename"
    assert not any(c[0] == "remove" and c[1] == "/srv/server.properties" for c in calls)
    assert b._sftp.files["/srv/server.properties"] == b"new"


def test_ssh_falls_back_when_posix_rename_is_unsupported():
    b = _ssh_backend()

    def unsupported(*a):
        raise IOError("unsupported extension")

    b._sftp.posix_rename = unsupported
    b.write_text("server.properties", "fallback", overwrite=True)
    assert b._sftp.files["/srv/server.properties"] == b"fallback"


def test_failed_upload_does_not_destroy_the_existing_file():
    from craftymm.backends.base import BackendError

    b = _ssh_backend()
    original = b._sftp.files["/srv/mods/a.jar"]

    def refuse(path, mode="rb"):
        if path.endswith(".part"):
            raise IOError("disk full")
        return _FakeSFTP.open(b._sftp, path, mode)

    b._sftp.open = refuse
    with pytest.raises(BackendError, match="Upload of a.jar failed"):
        b.upload_bytes("mods", "a.jar", b"replacement")
    assert b._sftp.files["/srv/mods/a.jar"] == original
