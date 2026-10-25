"""
Samba usershares: the [global] settings and the directory they live in.

Every SMB share FreedomNAS makes is a usershare (`net usershare add`), which
fails outright until smb.conf names a usershare path and that directory
exists. The setup wizard prepares both on a new system; the reconciler does
the same before it recreates the shares of a restored configuration, which
reaches a fresh system before any wizard has run.
"""

try:
    from freedomnas.engine import CommandExecutionError, CommandTranslationError
except ImportError:
    from engine import CommandExecutionError, CommandTranslationError  # type: ignore


def prepare(engine) -> None:
    """
    Make usershares possible (spec §7.6).

    The [global] usershare settings are appended only when smb.conf lacks
    them: an installation that wrote a complete smb.conf already has them,
    and a second copy would be noise. The directory is created through the
    profile, since where it lives and how it is created is the edition's.
    """
    smb_conf = engine.get_profile_value("samba", "config_file")
    usershare_path = engine.get_profile_value("samba", "usershare_path")
    try:
        with open(smb_conf) as f:
            current = f.read()
    except FileNotFoundError:
        current = ""
    if "usershare path" not in current:
        with open(smb_conf, "a") as f:
            f.write(
                "\n[global]\n"
                f"   usershare path = {usershare_path}\n"
                "   usershare max shares = 100\n"
                "   usershare allow guests = no\n"
            )
    group_settings(engine)
    engine.execute("samba", "usershare_prepare", path=usershare_path)
    # Samba reads [global] when it starts, and FreeBSD's samba package ships
    # no smb4.conf at all, so it did not start at boot: start it now with
    # the usershare settings. Without Samba installed there is nothing to do.
    try:
        engine.execute("services", "restart",
                       service_name=engine.get_profile_value("samba", "smb_service"), check=False)
    except (CommandExecutionError, CommandTranslationError):
        pass


def group_settings(engine) -> bool:
    """
    The [global] settings a group share needs (`samba.group_share_settings`),
    appended once when smb.conf lacks them. On Linux, Samba's default masks
    leave the group read-only on every new file whatever the directory's ACL
    says; FreeBSD's ZFS ACLs need none. Returns whether smb.conf changed.
    """
    lines = engine.get_profile_value("samba", "group_share_settings")
    smb_conf = engine.get_profile_value("samba", "config_file")
    try:
        with open(smb_conf) as f:
            current = f.read()
    except FileNotFoundError:
        current = ""
    missing = [line for line in lines if line not in current]
    if not missing:
        return False
    with open(smb_conf, "a") as f:
        f.write("\n[global]\n" + "".join(f"   {line}\n" for line in missing))
    return True
